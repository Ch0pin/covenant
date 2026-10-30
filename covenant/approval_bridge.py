"""Opt-in adapters for real harness approval prompts. Never executes requested tools."""
import asyncio
from contextlib import suppress
import json
import os
import shlex
import shutil
import subprocess
import sys
from uuid import uuid4

import httpx

from .approvals import close_bridge, create_bridge
from .delivery import codex_connection, codex_endpoint


class ApprovalClient:
    def __init__(self, base_url, token):
        self.client = httpx.AsyncClient(base_url=base_url, headers={"Authorization": "Bearer " + token},
                                        timeout=5, follow_redirects=False, trust_env=False)

    async def request(self, path, body=None):
        response = await self.client.post("/approval-bridge/requests" + path, json=body)
        response.raise_for_status()
        return response.json()

    async def submit(self, payload):
        return (await self.request("", payload))["id"]

    async def wait(self, aid):
        while True:
            result = await self.request(f"/{aid}/take")
            if result["status"] == "decided":
                return result["decision"]
            if result["status"] != "pending":
                return None
            await asyncio.sleep(1)

    async def finish(self, aid, status):
        with suppress(httpx.HTTPError):
            await self.request(f"/{aid}/finish", {"status": status})

    async def close(self):
        await self.client.aclose()


def codex_prompt(event, items):
    """Translate supported native permissions; leave questions/authentication in Codex."""
    method, params = event.get("method"), event.get("params", {})
    kinds = {"item/commandExecution/requestApproval": "command", "item/fileChange/requestApproval": "file"}
    if "id" not in event:
        return None
    if method == "mcpServer/elicitation/request":
        meta = params.get("_meta") or {}
        schema = params.get("requestedSchema") or {}
        # Codex marks its own tool permission requests. Generic MCP forms, OAuth
        # links and verification challenges need their native UI and input.
        if (not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call"
                or params.get("mode") != "form"
                or schema != {"type": "object", "properties": {}}):
            return None
        details = dict(params)
        details.update(tool_name=f"{params.get('serverName', 'MCP')} · MCP tool",
                       tool_input=meta.get("tool_params"), reason=params.get("message"))
        return {"request_key": json.dumps(event["id"]), "session_id": params["threadId"],
                "kind": "tool", "details": details,
                "can_approve": bool(params.get("serverName") and params.get("message")
                                    and isinstance(meta.get("tool_params"), dict))}
    if method not in kinds:
        return None
    item = items.get(params.get("itemId"), {})
    details = dict(params)
    if item:
        details["item"] = item
    can_approve = bool(params.get("command") or params.get("networkApprovalContext") or item.get("command")) if kinds[method] == "command" else bool(item.get("changes"))
    choices = params.get("availableDecisions")
    if choices is not None and "accept" not in choices:
        can_approve = False
    # Unknown decision variants remain in the native harness.
    if choices is not None and not {"decline", "cancel"}.intersection(v for v in choices if isinstance(v, str)):
        return None
    return {"request_key": json.dumps(event["id"]), "session_id": params["threadId"],
            "kind": kinds[method], "details": details, "can_approve": can_approve}


class CodexApprovals:
    def __init__(self, api, session_id, on_connected=None):
        self.on_connected = on_connected
        self.on_status = None
        self.status_task = None
        self.status_revision = 0
        self.api, self.session_id = api, session_id
        self.pending, self.items = {}, {}
        self.rpc_waiters = {}
        self.websocket = None
        self.reader = None

    def emit_status(self, method, params):
        if self.on_status:
            try:
                self.on_status(method, params)
            except (KeyError, TypeError, ValueError):
                # Telemetry must not interrupt approval forwarding.
                print("Native status unavailable for one event.", file=sys.stderr)

    async def watch_status(self):
        # Read-only native metadata: no model invocation and no agent polling.
        while True:
            revision = self.status_revision
            try:
                thread = (await self.rpc("thread/read", {"threadId": self.session_id, "includeTurns": False})).get("thread", {})
                latest = await self.rpc("thread/turns/list", {"threadId": self.session_id, "limit": 1,
                                                            "sortDirection": "desc", "itemsView": "notLoaded"})
                turns = latest.get("data", [])
                if revision == self.status_revision:
                    self.emit_status("covenant/status-snapshot", {"status": thread.get("status"), "turn": turns[0] if turns else None})
            except Exception:
                self.emit_status("covenant/status-unavailable", {})
            await asyncio.sleep(30)

    async def rpc(self, method, params):
        rid = "covenant-approval-" + str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.rpc_waiters[rid] = future
        try:
            await self.websocket.send(json.dumps({"id": rid, "method": method, "params": params}))
            result = await asyncio.wait_for(future, 10)
            if "error" in result:
                raise RuntimeError("Codex rejected " + method + "; use the harness prompt")
            return result["result"]
        finally:
            self.rpc_waiters.pop(rid, None)

    async def handle(self, event, job):
        aid = None
        expired = False
        job["resolution"] = asyncio.Event()
        try:
            payload = codex_prompt(event, self.items)
            if payload is None:
                return
            aid = await self.api.submit(payload)
            job["aid"] = aid
            decision = await self.api.wait(aid)
            # The reader marks native resolutions synchronously, before doing any HTTP work.
            if job["resolved"]:
                return
            # Reject denies this action. The protocol also accepts decline when the
            # native picker only advertises cancel; cancel interrupts the entire turn.
            expired = decision is None
            answer = "accept" if decision == "approve" else "decline"
            result = ({"action": answer, "content": None}
                      if event["method"] == "mcpServer/elicitation/request"
                      else {"decision": answer})
            await self.websocket.send(json.dumps({"id": event["id"], "result": result}))
            if expired:
                # Expiry must resolve the native action, never interrupt the whole turn.
                # A native event confirms closure; a concurrent native decision may win.
                if not job["resolved"]:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(job["resolution"].wait(), 10)
                return
            # Keep the request alive until the harness confirms resolution (or disconnects).
            while not job["resolved"]:
                result = await self.api.request(f"/{aid}/take")
                if result["status"] not in {"submitted", "pending"}:
                    break
                await asyncio.sleep(1)
        except (httpx.HTTPError, ValueError, KeyError):
            print("Covenant approval forwarding unavailable; answer this prompt in Codex.", file=sys.stderr)
        finally:
            if aid:
                await self.api.finish(aid, "expired_resolved" if expired and job.get("native_resolved") else "resolved" if job["resolved"] else "cancelled")

    async def read(self):
        try:
            async for raw in self.websocket:
                event = json.loads(raw)
                if "method" not in event:
                    waiter = self.rpc_waiters.get(event.get("id"))
                    if waiter and not waiter.done():
                        waiter.set_result(event)
                    continue
                params = event.get("params", {})
                if event["method"] == "account/rateLimits/updated":
                    self.emit_status(event["method"], params)
                if params.get("threadId") != self.session_id:
                    continue
                method = event["method"]
                if method in {"thread/status/changed", "turn/started", "turn/completed", "error",
                              "model/safetyBuffering/updated", "model/verification", "thread/closed"}:
                    self.status_revision += 1
                    self.emit_status(method, params)
                if method == "item/started":
                    item = params.get("item", {})
                    if item.get("id"):
                        self.items[item["id"]] = item
                        if len(self.items) > 200:
                            self.items.pop(next(iter(self.items)))
                elif method == "serverRequest/resolved":
                    job = self.pending.get(json.dumps(params.get("requestId")))
                    if job:
                        job["resolved"] = True
                        job["native_resolved"] = True
                        if job.get("resolution"):
                            job["resolution"].set()
                        job["task"].cancel()
                elif "id" in event and method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "mcpServer/elicitation/request"}:
                    key = json.dumps(event["id"])
                    if key not in self.pending:
                        job = {"resolved": False, "aid": None}
                        self.pending[key] = job
                        job["task"] = asyncio.create_task(self.handle(event, job))
                        def finished(task, key=key):
                            self.pending.pop(key, None)
                            if not task.cancelled() and task.exception():
                                print("Approval forwarding stopped for one request; use the native prompt.", file=sys.stderr)
                        job["task"].add_done_callback(finished)
                elif method in {"turn/completed", "thread/closed"}:
                    for job in self.pending.values():
                        if not job["task"].done():
                            job["resolved"] = True
                            job["task"].cancel()
                    self.items.clear()
                    if method == "thread/closed":
                        return
        finally:
            for future in self.rpc_waiters.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex approval connection closed"))

    async def run(self, endpoint):
        try:
            async with codex_connection(endpoint) as websocket:
                self.websocket = websocket
                self.reader = asyncio.create_task(self.read())
                await self.rpc("initialize", {"clientInfo": {"name": "covenant_approvals", "version": "0.1.0"},
                                              "capabilities": {"experimentalApi": True}})
                await websocket.send(json.dumps({"method": "initialized", "params": {}}))
                thread = (await self.rpc("thread/read", {"threadId": self.session_id, "includeTurns": False}))["thread"]
                self.emit_status("covenant/status-snapshot", {"status": thread.get("status")})
                if thread.get("status", {}).get("type") not in {"active", "idle"}:
                    raise RuntimeError("Open this Codex session first, then attach approvals")
                # No configuration overrides and no new turn: subscribe to the already loaded session.
                await self.rpc("thread/resume", {"threadId": self.session_id, "excludeTurns": True})
                if self.on_connected:
                    self.on_connected()
                else:
                    print("Covenant approvals attached. Keep this terminal open; Ctrl+C detaches.", flush=True)
                if self.on_status:
                    self.status_task = asyncio.create_task(self.watch_status())
                await self.reader
        finally:
            if self.status_task:
                self.status_task.cancel()
                await asyncio.gather(self.status_task, return_exceptions=True)
            if self.reader:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            tasks = [job["task"] for job in self.pending.values()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


def claude_decision(allowed, message="Operator rejected the request in Covenant"):
    decision = {"behavior": "allow" if allowed else "deny"}
    if not allowed:
        decision["message"] = message
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


async def claude_hook(payload, api):
    if payload.get("hook_event_name") != "PermissionRequest":
        return None
    aid = None
    try:
        aid = await api.submit({"request_key": str(uuid4()), "session_id": payload["session_id"],
                                "kind": "tool", "details": {k: payload[k] for k in ("tool_name", "tool_input", "cwd") if k in payload}})
        async with asyncio.timeout(590):
            decision = await api.wait(aid)
        if decision is None:
            return claude_decision(False, "Covenant request expired or access ended; ask the operator again")
        return claude_decision(decision == "approve")
    except (httpx.HTTPError, TimeoutError, KeyError, ValueError):
        return claude_decision(False, "Covenant approvals unavailable; ask the operator again")
    finally:
        if aid:
            # This means the hook has a result, not that the tool executed successfully.
            await api.finish(aid, "resolved")


def hook_main():
    async def run():
        api = None
        try:
            payload = json.loads(sys.stdin.read(100001))
            api = ApprovalClient(os.environ["COVENANT_APPROVAL_URL"], os.environ["COVENANT_APPROVAL_TOKEN"])
            result = await claude_hook(payload, api)
        except (ValueError, KeyError):
            result = claude_decision(False, "Covenant approval hook is not configured")
        finally:
            if api:
                await api.close()
        if result is not None:
            print(json.dumps(result))
    asyncio.run(run())


def launch(args, settings, db):
    base_url = f"http://127.0.0.1:{settings.port}"
    if args.harness == "codex" and not args.session:
        raise ValueError("Codex approvals require --session SESSION_UUID")
    if not db.path.is_file():
        raise ValueError("Start Covenant before attaching approvals")
    # Refuse to migrate an active old server's database. Upgrade only on server startup.
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='approval_bridges'").fetchone():
            raise ValueError("Restart Covenant at a convenient break to activate approval support")
    if args.harness == "codex":
        print("Codex approvals are managed by covenant serve. Join with session_id and check the participant approval status.")
        return 0
    bridge_id, token = create_bridge(db, str(args.room), args.agent, args.harness, str(args.session) if args.session else None)
    try:
        executable = shutil.which("claude")
        if not executable:
            raise ValueError("Claude executable is missing from PATH")
        command = shlex.join([sys.executable, "-m", "covenant.approval_bridge"])
        hook_settings = {"hooks": {"PermissionRequest": [{"hooks": [{"type": "command", "command": command, "timeout": 600}]}]}}
        env = dict(os.environ, COVENANT_APPROVAL_URL=base_url, COVENANT_APPROVAL_TOKEN=token,
                   MCP_PROTOCOL_NEGOTIATION="legacy")
        extra = args.harness_args
        if extra and extra[0] == "--":
            extra = extra[1:]
        result = subprocess.run([executable, "--settings", json.dumps(hook_settings),
                                 "--dangerously-load-development-channels", "server:covenant", *extra], env=env)
        return result.returncode
    finally:
        close_bridge(db, bridge_id)
    return 0


if __name__ == "__main__":
    hook_main()
