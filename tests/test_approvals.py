import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from test_app import setup, bearer
from covenant.approvals import create_bridge, close_bridge
from covenant.approval_bridge import ApprovalClient, CodexApprovals, claude_hook, codex_prompt


@pytest.fixture
def approvals(setup):
    client, app, settings, csrf, rid, invitations = setup
    session = str(uuid4())
    bid, token = create_bridge(app.state.db, rid, "alice", "codex", session)
    auth = {"Authorization": "Bearer " + token}
    payload = {"request_key": "42", "session_id": session, "kind": "command", "details": {"command": "printf hello"}}
    return client, app, csrf, rid, invitations, bid, auth, payload


def submit(a, payload=None):
    client, _, _, _, _, _, auth, default = a
    response = client.post("/approval-bridge/requests", headers=auth, json=payload or default)
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_operator_decision_single_consumption_and_duplicate_delivery(approvals, decision):
    client, app, csrf, rid, invitations, bid, auth, payload = approvals
    aid = submit(approvals)
    assert submit(approvals) == aid
    assert client.post(f"/approval-bridge/requests/{aid}/take", headers=auth).json() == {"status": "pending"}
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": decision}).status_code == 200
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": decision}).status_code == 409
    assert client.post(f"/approval-bridge/requests/{aid}/take", headers=auth).json() == {"status": "decided", "decision": decision}
    assert client.post(f"/approval-bridge/requests/{aid}/take", headers=auth).json() == {"status": "submitted"}
    client.post(f"/approval-bridge/requests/{aid}/finish", headers=auth, json={"status": "resolved"})
    row = client.get("/api/approvals").json()["requests"][0]
    assert row["status"] == "resolved" and row["decision"] == decision
    assert "token" not in json.dumps(row)


def test_agent_cannot_submit_or_decide_and_csrf_required(approvals):
    client, _, csrf, rid, invitations, bid, auth, payload = approvals
    aid = submit(approvals)
    assert client.post("/approval-bridge/requests", headers=bearer(invitations[0]), json=payload).status_code == 401
    assert client.post(f"/api/approvals/{aid}/decision", headers=auth, json={"decision": "approve"}).status_code == 403
    assert client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"}).status_code == 403
    assert client.post(f"/api/approvals/{aid}/decision", headers={**csrf, "Origin": "https://evil.example"}, json={"decision": "approve"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/approvals", headers=auth).status_code == 401
    assert client.post(f"/api/approvals/{aid}/decision", headers=auth, json={"decision": "approve"}).status_code == 401


@pytest.mark.parametrize("change", ["paused", "archived", "revoke", "rotate", "disconnect", "timeout", "close"])
def test_stale_requests_cannot_be_approved(approvals, change):
    client, app, csrf, rid, inv, bid, auth, _ = approvals
    aid = submit(approvals)
    if change in {"paused", "archived"}:
        client.patch(f"/api/rooms/{rid}", headers=csrf, json={"status": change})
    elif change in {"revoke", "rotate"}:
        client.post(f"/api/rooms/{rid}/agents/{inv[0]['id']}/{change}", headers=csrf)
    elif change == "close":
        close_bridge(app.state.db, bid)
    else:
        column = "alive_until" if change == "disconnect" else "expires_at"
        with app.state.db.connect() as conn:
            conn.execute(f"UPDATE approval_requests SET {column}=0 WHERE id=?", (aid,))
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "approve"}).status_code == 409
    taken = client.post(f"/approval-bridge/requests/{aid}/take", headers=auth)
    assert "decision" not in taken.json()


def test_approval_queued_then_revoked_never_reaches_bridge(approvals):
    client, app, csrf, rid, inv, bid, auth, _ = approvals
    aid = submit(approvals)
    client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "approve"})
    client.post(f"/api/rooms/{rid}/agents/{inv[0]['id']}/revoke", headers=csrf)
    assert client.post(f"/approval-bridge/requests/{aid}/take", headers=auth).status_code == 401


def test_immutable_request_scope_and_size(approvals):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    aid = submit(approvals)
    for change in [{"details": {"command": "changed"}}, {"session_id": str(uuid4())}]:
        assert client.post("/approval-bridge/requests", headers=auth, json={**payload, **change}).status_code == 409
    assert client.post("/approval-bridge/requests", headers=auth, json={**payload, "details": {"command": "x" * 60001}}).status_code == 422
    other_bid, other_token = create_bridge(app.state.db, rid, "bob", "codex", payload["session_id"])
    assert client.post(f"/approval-bridge/requests/{aid}/take", headers={"Authorization": "Bearer " + other_token}).status_code == 404
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "approve", "command": "changed"}).status_code == 422


def test_incomplete_native_preview_requires_native_approval(approvals):
    client, _, csrf, _, _, _, _, payload = approvals
    aid = submit(approvals, {**payload, "can_approve": False})
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "approve"}).status_code == 409
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "reject"}).status_code == 200


def test_two_operator_tabs_first_decision_wins(approvals):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    aid = submit(approvals)
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(lambda decision: client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": decision}).status_code, ["approve", "reject"]))
    assert sorted(results) == [200, 409]


def test_archive_delete_cascades_without_breaking_chat(approvals):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    submit(approvals)
    client.patch(f"/api/rooms/{rid}", headers=csrf, json={"status": "archived"})
    assert client.delete(f"/api/rooms/{rid}", headers=csrf).status_code == 200
    with app.state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


class FakeAPI:
    def __init__(self, decision):
        self.decision = decision
        self.payload = None
        self.finished = []
    async def submit(self, payload):
        self.payload = payload
        return "request"
    async def wait(self, aid):
        return self.decision
    async def finish(self, aid, status):
        self.finished.append((aid, status))


@pytest.mark.parametrize("decision, behavior", [("approve", "allow"), ("reject", "deny"), (None, "deny")])
def test_claude_native_permission_result(decision, behavior):
    api = FakeAPI(decision)
    result = asyncio.run(claude_hook({"hook_event_name": "PermissionRequest", "session_id": "session", "tool_name": "Bash", "tool_input": {"command": "printf hello"}}, api))
    assert result["hookSpecificOutput"]["decision"]["behavior"] == behavior
    assert api.payload["details"]["tool_input"]["command"] == "printf hello"
    assert "updatedInput" not in result["hookSpecificOutput"]["decision"]


def test_claude_outage_denies_request():
    class Unavailable(FakeAPI):
        async def submit(self, payload):
            raise httpx.ConnectError("offline")
    result = asyncio.run(claude_hook({"hook_event_name": "PermissionRequest", "session_id": "session"}, Unavailable(None)))
    assert result["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_codex_prompt_includes_full_permissions_and_file_changes():
    event = {"id": 1, "method": "item/commandExecution/requestApproval", "params": {"threadId": "t", "command": "printf hello", "availableDecisions": ["accept", "cancel"], "additionalPermissions": {"fileSystem": {"read": ["/tmp"]}}}}
    prompt = codex_prompt(event, {})
    assert prompt["can_approve"] and prompt["details"]["additionalPermissions"] == event["params"]["additionalPermissions"]
    assert "rejection_effect" not in prompt["details"]
    file_event = {"id": "2", "method": "item/fileChange/requestApproval", "params": {"threadId": "t", "itemId": "file"}}
    assert not codex_prompt(file_event, {})["can_approve"]
    assert codex_prompt(file_event, {"file": {"changes": [{"path": "/tmp/a", "diff": "+one"}]}})["can_approve"]
    assert codex_prompt({"id": 3, "method": "unknown"}, {}) is None


def test_pause_then_restore_does_not_revive_approval(approvals):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    aid = submit(approvals)
    client.patch(f"/api/rooms/{rid}", headers=csrf, json={"status": "paused"})
    client.patch(f"/api/rooms/{rid}", headers=csrf, json={"status": "active"})
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "approve"}).status_code == 409


def test_codex_native_resolution_cancels_wait_and_other_threads_are_ignored():
    async def run():
        started = asyncio.Event()
        class Waiting(FakeAPI):
            async def wait(self, aid):
                started.set()
                await asyncio.Future()
        api = Waiting(None)
        listener = CodexApprovals(api, "mine")
        class Socket:
            def __aiter__(self):
                return self.events()
            async def events(self):
                for tid in ["other", "mine"]:
                    yield json.dumps({"id": tid, "method": "item/commandExecution/requestApproval", "params": {"threadId": tid, "command": "printf harmless"}})
                await started.wait()
                yield json.dumps({"method": "serverRequest/resolved", "params": {"threadId": "mine", "requestId": "mine"}})
            async def send(self, data):
                raise AssertionError("A native resolution must not send an approval")
        listener.websocket = Socket()
        await listener.read()
        tasks = [x["task"] for x in listener.pending.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        assert api.payload["session_id"] == "mine"
        assert api.finished == [("request", "resolved")]
    asyncio.run(run())


def test_claude_hook_http_roundtrip(approvals):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    async def run():
        api = ApprovalClient("https://chat.example.com", "unused")
        await api.close()
        api.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://chat.example.com", headers=auth)
        task = asyncio.create_task(claude_hook({"hook_event_name": "PermissionRequest", "session_id": payload["session_id"], "tool_name": "Bash", "tool_input": {"command": "printf harmless"}}, api))
        for _ in range(50):
            rows = client.get("/api/approvals").json()["requests"]
            if rows:
                break
            await asyncio.sleep(.01)
        assert rows
        client.post(f"/api/approvals/{rows[0]['id']}/decision", headers=csrf, json={"decision": "approve"})
        result = await asyncio.wait_for(task, 3)
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        await api.close()
    asyncio.run(run())


def test_claude_launcher_keeps_secrets_out_of_arguments_and_closes_grant(approvals, monkeypatch):
    from types import SimpleNamespace
    from covenant.approval_bridge import launch
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    seen = []
    monkeypatch.setattr("covenant.approval_bridge.shutil.which", lambda name: "/usr/local/bin/claude")
    def run(argv, env):
        assert env["COVENANT_APPROVAL_TOKEN"] not in " ".join(argv)
        assert argv[-2:] == ["--resume", "existing-session"]
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 600
        assert "permissions" not in settings
        seen.append(env["COVENANT_APPROVAL_TOKEN"])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("covenant.approval_bridge.subprocess.run", run)
    assert launch(SimpleNamespace(harness="claude",room=rid,agent="alice",session=None,harness_args=["--resume", "existing-session"]),app.state.settings,app.state.db) == 0
    assert seen
    assert client.post("/approval-bridge/requests",headers={"Authorization":"Bearer " + seen[0]},json=payload).status_code == 401


def test_approval_launcher_never_initializes_an_old_database(tmp_path):
    from types import SimpleNamespace
    from covenant.db import Database
    from covenant.config import Settings
    from covenant.approval_bridge import launch
    db = Database(tmp_path / "absent.sqlite3")
    args = SimpleNamespace(harness="codex",session=uuid4())
    with pytest.raises(ValueError, match="Start Covenant"):
        launch(args,Settings(database=db.path),db)
    assert not db.path.exists()


@pytest.mark.parametrize("choices", [None, ["accept", "decline", "cancel"], ["accept", "cancel"]])
def test_codex_reject_declines_tool_without_interrupting_turn(choices):
    async def run():
        api = FakeAPI("reject")
        listener = CodexApprovals(api, "session")
        job = {"resolved": False, "aid": None}
        sent = []
        class Socket:
            async def send(self, wire):
                sent.append(json.loads(wire))
                job["resolved"] = True
        listener.websocket = Socket()
        params = {"threadId": "session", "command": "printf harmless"}
        if choices is not None:
            params["availableDecisions"] = choices
        await listener.handle({"id": 42, "method": "item/commandExecution/requestApproval", "params": params}, job)
        assert sent == [{"id": 42, "result": {"decision": "decline"}}]
        assert api.finished == [("request", "resolved")]
    asyncio.run(run())


def mcp_permission_event():
    # Captured from the installed Codex harness with an isolated MCP fixture.
    return {"id": 0, "method": "mcpServer/elicitation/request", "params": {
        "threadId": "session", "turnId": "turn", "serverName": "fixture", "mode": "form",
        "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_description": "Test tool",
                  "tool_params": {"text": "hello"}, "tool_params_display": []},
        "message": 'Allow the fixture MCP server to run tool "approval_probe"?',
        "requestedSchema": {"type": "object", "properties": {}}}}


def test_codex_mcp_permission_preserves_prompt_and_arguments_without_item_cache():
    event = mcp_permission_event()
    prompt = codex_prompt(event, {})
    assert prompt["kind"] == "tool" and prompt["can_approve"]
    assert prompt["request_key"] == "0"
    assert prompt["details"]["reason"] == event["params"]["message"]
    assert prompt["details"]["tool_input"] == {"text": "hello"}
    assert prompt["details"]["_meta"] == event["params"]["_meta"]
    del event["params"]["_meta"]["tool_params"]
    assert not codex_prompt(event, {})["can_approve"]


@pytest.mark.parametrize("patch", [
    {"_meta": None}, {"_meta": "unknown"}, {"_meta": {"codex_approval_kind": "unknown"}},
    {"mode": "url", "url": "https://example.com/login"},
    {"mode": "openai/userVerification"},
    {"requestedSchema": {"type": "object", "properties": {"password": {"type": "string"}}}},
])
def test_codex_mcp_forms_and_authentication_stay_in_harness(patch):
    event = mcp_permission_event()
    event["params"].update(patch)
    assert codex_prompt(event, {}) is None


@pytest.mark.parametrize("decision,action", [("approve", "accept"), ("reject", "decline"), (None, "decline")])
def test_codex_mcp_permission_reader_and_native_response(decision, action):
    async def run():
        api = FakeAPI(decision)
        listener = CodexApprovals(api, "session")
        sent, submitted = [], asyncio.Event()
        class Socket:
            def __aiter__(self):
                return self.events()
            async def events(self):
                wrong_thread = mcp_permission_event()
                wrong_thread["params"]["threadId"] = "other"
                yield json.dumps(wrong_thread)
                yield json.dumps(mcp_permission_event())
                await submitted.wait()
                yield json.dumps({"method": "serverRequest/resolved", "params": {
                    "threadId": "session", "requestId": 0}})
            async def send(self, wire):
                sent.append(json.loads(wire))
                submitted.set()
        async def take(path, body=None):
            return {"status": "submitted"}
        api.request = take
        listener.websocket = Socket()
        await asyncio.wait_for(listener.read(), 2)
        await asyncio.gather(*(job["task"] for job in listener.pending.values()), return_exceptions=True)
        assert sent == [{"id": 0, "result": {"action": action, "content": None}}]
        assert api.payload["session_id"] == "session"
        assert api.payload["kind"] == "tool"
        assert api.finished == [("request", "expired_resolved" if decision is None else "resolved")]
    asyncio.run(run())


@pytest.mark.parametrize("kind,method", [("command", "item/commandExecution/requestApproval"), ("file", "item/fileChange/requestApproval")])
def test_expiry_declines_native_action_and_persists_notice(approvals, kind, method):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    async def run():
        api = ApprovalClient("http://127.0.0.1", auth["Authorization"][7:])
        await api.client.aclose()
        api.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=auth)
        original_wait = api.wait
        async def expired(aid):
            with app.state.db.connect() as conn:
                conn.execute("UPDATE approval_requests SET expires_at=0 WHERE id=?", (aid,))
            return await original_wait(aid)
        api.wait = expired
        listener = CodexApprovals(api, payload["session_id"])
        sent = []
        delivered = asyncio.Event()
        class Socket:
            def __aiter__(self): return self.events()
            async def events(self):
                yield json.dumps({"id": 42, "method": method, "params": {"threadId": payload["session_id"], "command": "printf harmless"}})
                await delivered.wait()
                yield json.dumps({"method": "serverRequest/resolved", "params": {"threadId": payload["session_id"], "requestId": 42}})
            async def send(self, wire):
                sent.append(json.loads(wire)); delivered.set()
        listener.websocket = Socket()
        await asyncio.wait_for(listener.read(), 2)
        await asyncio.gather(*(j["task"] for j in listener.pending.values()), return_exceptions=True)
        assert sent == [{"id": 42, "result": {"decision": "decline"}}]
        await api.close()
    asyncio.run(run())
    row = client.get("/api/approvals").json()["requests"][0]
    assert row["status"] == "expired" and row["native_resolved"] == 1
    assert row["decision"] is None and row["dismissed_at"] is None
    aid = row["id"]
    assert client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "approve"}).status_code == 409
    assert client.post(f"/api/approvals/{aid}/dismiss").status_code == 403
    assert client.post(f"/api/approvals/{aid}/dismiss", headers=csrf).status_code == 200
    assert client.get("/api/approvals").json()["requests"][0]["dismissed_at"]


def test_old_expired_notice_survives_until_dismissed(approvals):
    client, app, csrf, *_ = approvals
    aid = submit(approvals)
    with app.state.db.connect() as conn:
        conn.execute("UPDATE approval_requests SET created_at=?,expires_at=0 WHERE id=?", (time.time()-172800, aid))
    row = client.get("/api/approvals").json()["requests"][0]
    assert row["id"] == aid and row["status"] == "expired" and not row["native_resolved"]
    client.post(f"/api/approvals/{aid}/dismiss", headers=csrf)
    assert not client.get("/api/approvals").json()["requests"]
