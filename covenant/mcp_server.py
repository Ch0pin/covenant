"""Session-local stdio MCP adapter for Covenant's authenticated HTTP API."""
import asyncio
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

import httpx
from .receipt_client import report

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .delivery import CodexDelivery, codex_endpoint
from .claude_delivery import ChannelMCPServer, ClaudeDelivery


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_room_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ToolError("Use the HTTP(S) room URL from the invitation, with no credentials or query string.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ToolError("Remote rooms require HTTPS.")
    try:
        _ = parsed.port
        if parsed.path != "/" + str(UUID(parsed.path.lstrip("/"))):
            raise ValueError
    except ValueError:
        raise ToolError("The room URL must end in a single room UUID.") from None
    return value


def request_json(room_url: str, token: str, suffix: str, body=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    request = Request(room_url + suffix, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
    try:
        # Ignore unrelated shell proxy settings and never forward a token on redirects.
        with build_opener(NoRedirects(), ProxyHandler({})).open(request, timeout=35) as response:
            return json.load(response)
    except HTTPError as exc:
        details = {401: "Agent access is invalid or revoked. Ask the operator for a fresh invitation.",
                   403: "The server rejected this request.",
                   404: "Conversation or endpoint not found. Check the room URL and whether the server needs upgrading.",
                   409: "The room is paused/archived, a registration changed, or this message ID was used for different text.",
                   422: "Invalid request. Check tool arguments, recipient handles, and allowed limits.",
                   429: "Request limit reached. Wait before retrying."}
        if 300 <= exc.code < 400:
            detail = "Redirect refused. Use the current room URL supplied by the operator."
        else:
            detail = details.get(exc.code, "The server is unavailable. Retry with backoff.")
        raise ToolError(f"HTTP {exc.code}: {detail}") from None
    except (URLError, TimeoutError, OSError):
        raise ToolError("Cannot reach Covenant. Check the server/tunnel and retry with backoff.") from None
    except (ValueError, UnicodeError):
        raise ToolError("Covenant returned an unexpected response. Check the room URL.") from None


def create_mcp_server():
    delivery = None
    fallback_reason = None
    receipts_enabled = False

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            if delivery:
                await delivery.stop()

    server = ChannelMCPServer("covenant", instructions=(
        "Join using the operator's invitation. Retain your existing task instructions. "
        "Send messages to @handle, @operator, or @everyone. In polling mode, call "
        "wait_for_messages while available; save next_cursor and revision after processing. Ignore your own posts "
        "as response triggers. Codex agents can run printenv CODEX_THREAD_ID in their own "
        "shell and pass that value as session_id to join_room to enable background delivery. "
        "Claude Code can join with delivery_mode=claude when this MCP server is enabled as a channel. "
        "For Covenant channel events, call acknowledge_delivery with the event receipt after reading it. "
        "A handshake receipt confirms channel activation. Until confirmed, keep polling without rejoining. "
        "Check delivery_status at work checkpoints; fallback includes a safe polling cursor. "
        "While delivery is listening, manual polling is unnecessary. "
        "If Claude reports fallback, call wait_for_messages from its saved cursor. "
        "If Codex delivery retries or stops, rejoin without session_id and poll from your last processed cursor. "
        "Inbox delivery includes only messages addressed to you or everyone, excluding your own posts. "
        "Use read_thread(message_id) for one discussion or read_history(scope=all) for wider context; history cursors never replace the live inbox cursor. "
        "Broadcast shared decisions and blockers to @everyone. Before editing shared files, acquire_file_locks "
        "for every affected path, edit only after acquired=true, renew before expiry and release after finishing. "
        "On conflict work elsewhere; on expiry or renewal failure stop editing and reacquire, then reread files. "
        "These are cooperative leases: direct filesystem writes bypass them. "
        "For tracked operator requests (receipt_requested=true), call acknowledge_message(message_id) after reading, then send the answer "
        "with reply_to=message_id. Linked replies mark Answered: keep progress acknowledgements unlinked. "
        "Respect room pause/archive status. Tokens stay private."),
        log_level="WARNING", lifespan=lifespan)
    connection = None
    join_lock = asyncio.Lock()

    @server.tool(annotations=ToolAnnotations(destructive_hint=False))
    async def join_room(
        room_url: Annotated[str, Field(max_length=2048)],
        access_token: Annotated[str, Field(min_length=32, max_length=256)],
        session_id: UUID | None = None,
        delivery_mode: Literal["polling", "codex", "claude"] | None = None,
        history_limit: Annotated[int, Field(ge=0, le=50)] = 20,
        history_scope: Literal["inbox", "all"] = "inbox",
        ctx: Context | None = None,
    ) -> dict:
        """Join with the room URL and agent token from the invitation. Returns identity,
        participants, up to history_limit recent inbox messages, next_cursor and revision.
        Use history_limit=0 to skip catch-up or history_scope="all" for shared history. One connection per MCP
        process; rejoin after a process restart. Preserve a saved cursor on reconnect.
        For Codex background delivery, obtain your own ID with printenv CODEX_THREAD_ID
        and pass session_id. Claude Code: pass delivery_mode="claude" after enabling
        server:covenant as a startup channel; no session UUID is needed. Wait for the
        channel handshake and acknowledge its receipt. Omit both delivery_mode and
        session_id, or select delivery_mode="polling", for ordinary polling. This bridge and the
        addressed Codex session must use the same local/shared Codex server.
        The token is held in this process's memory and is never returned by this tool.
        """
        nonlocal connection, delivery, fallback_reason, receipts_enabled
        url = validate_room_url(room_url)
        if any(char.isspace() for char in access_token):
            raise ToolError("The access token must contain no whitespace.")
        mode = delivery_mode or ("codex" if session_id else "polling")
        if mode == "codex" and session_id is None:
            raise ToolError("Codex delivery requires session_id from CODEX_THREAD_ID.")
        if mode != "codex" and session_id is not None:
            raise ToolError("session_id is only used with Codex delivery. Omit it for Claude or polling.")
        endpoint = codex_endpoint() if mode == "codex" else None
        registration = {"session_id": str(session_id), "connection_id": str(uuid4())} if mode == "codex" else {}
        # Default joins remain compatible with older servers; optional history needs the upgrade.
        if history_limit != 20 or history_scope != "inbox":
            registration.update(history_limit=history_limit, history_scope=history_scope)
        async with join_lock:
            result = await asyncio.to_thread(request_json, url, access_token, "/join", registration)
            if delivery:
                await delivery.stop()
                delivery = None
            connection = (url, access_token)
            receipts_enabled = result.get("capabilities", {}).get("message_receipts", False)
            fallback_reason = None
            if mode == "codex":
                received = result.get("delivery", {})
                if (received.get("mode") != "codex" or received.get("session_id") != str(session_id)
                        or received.get("connection_id") != registration["connection_id"]):
                    raise ToolError("This server has not enabled background delivery. The room is joined; use polling until its planned upgrade.")
                delivery = CodexDelivery(url, access_token, result["handle"], received, endpoint)
                delivery.start()
                result["delivery"] = delivery.status()
            elif mode == "claude":
                if ctx is None or ctx.session.protocol_version >= "2026-07-28":
                    fallback_reason = "Claude channels need the legacy MCP handshake. Restart Claude with MCP_PROTOCOL_NEGOTIATION=legacy and its Covenant channel enabled; polling remains available."
                else:
                    try:
                        delivery = ClaudeDelivery(url, access_token, result["handle"], result["next_cursor"],
                                                  ctx.session.send_notification, receipts_enabled=receipts_enabled)
                        delivery.start()
                    except (OSError, ValueError, KeyError):
                        fallback_reason = "Cannot open Claude delivery checkpoint, or another listener holds this invitation. Use polling and check COVENANT_STATE_DIR."
                result["delivery"] = delivery.status() if delivery else {
                    "mode": "polling", "state": "fallback", "last_error": fallback_reason,
                    "polling_recommended": True, "cursor": 0}
        await report_received(result, "catch_up")
        if receipts_enabled:
            status = delivery.status() if delivery else {"mode": "polling", "state": "fallback" if fallback_reason else "joined", "last_error": fallback_reason}
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + access_token}, trust_env=False) as client:
                await report(client, url, "/delivery-status", {"mode": status["mode"], "state": status["state"], "last_error": status.get("last_error")})
        return result

    async def report_received(result, mode):
        if not receipts_enabled:
            return
        ids = [m["id"] for m in result.get("messages", []) if m.get("receipt_requested")]
        if ids:
            url, token = current_connection()
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
                await report(client, url, "/message-receipts", {"message_ids": ids, "stage": "delivered", "mode": mode})

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    async def delivery_status() -> dict:
        """Check this bridge's delivery mode, retry errors and last delivered message.
        Claude mode stays unverified until its channel handshake is acknowledged.
        In fallback mode use cursor as after for wait_for_messages.
        Delivered means Codex accepted input into a turn; an agent reply confirms processing.
        While Claude is awaiting_confirmation, keep polling without rejoining. On
        Claude fallback, poll from cursor. For Codex failures, rejoin without session_id
        and poll from your last processed cursor.
        """
        if delivery:
            return delivery.status()
        result = {"mode": "polling", "state": "joined" if connection else "not_joined"}
        if fallback_reason:
            result.update(state="fallback", last_error=fallback_reason, polling_recommended=True, cursor=0)
        return result

    def current_connection():
        if connection is None:
            raise ToolError("Call join_room with your invitation before sending or waiting.")
        return connection

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    async def acknowledge_delivery(receipt: UUID) -> dict:
        """Confirm receipt of a Claude channel event after reading it. Copy receipt from
        that event's metadata. This confirms reception, not task completion. Send a brief
        @operator reply separately for human requests. Duplicate receipts are safe.
        """
        async with join_lock:
            if not isinstance(delivery, ClaudeDelivery):
                raise ToolError("No Claude channel is joined. Use join_room with delivery_mode=claude.")
            url, token = current_connection()
            # An event already in Claude's queue must not bypass revocation or archive.
            page = await asyncio.to_thread(request_json, url, token,
                "/messages?" + urlencode({"after": delivery.scan_cursor, "timeout": 0, "transport": "claude"}))
            if page["room_status"] == "archived":
                await delivery.stop()
                raise ToolError("Conversation archived; channel delivery ended.")
            # Persist the explicit model receipt before advancing the durable channel cursor.
            mid = next((mid for mid, item in delivery.pending.items()
                        if item["receipt"] == str(receipt) and item.get("receipt_requested", False)), None)
            if receipts_enabled and mid is not None:
                await asyncio.to_thread(request_json, url, token, "/message-receipts", {
                    "message_ids": [mid], "stage": "acknowledged", "mode": "claude"})
            try:
                return delivery.acknowledge(str(receipt))
            except OSError:
                await delivery.stop()
                raise ToolError("Cannot save delivery acknowledgement. Poll from the last saved cursor.") from None

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    async def send_message(
        text: Annotated[str, Field(min_length=1, max_length=16000)],
        client_message_id: Annotated[str, Field(min_length=1, max_length=128)],
        reply_to: Annotated[int | None, Field(ge=1, le=2**63 - 1)] = None,
    ) -> dict:
        """Post text beginning with one or more recipients, e.g. @alice @operator FYI.
        Use @handle, @operator, or @everyone. Supply a new unique
        client_message_id for each new message, and reuse the SAME ID for retries.
        Sender identity comes from the token supplied to join_room. Use reply_to to
        join any existing message's thread in this room. Replying to a tracked operator
        request addressed to you, with @operator or @everyone, marks it Answered;
        keep brief progress acknowledgements unlinked. Use read_thread for context.
        """
        url, token = current_connection()
        body = {"text": text, "client_message_id": client_message_id}
        if reply_to is not None:
            body["reply_to"] = reply_to
        return await asyncio.to_thread(request_json, url, token, "/messages", body)

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    async def acknowledge_message(message_id: Annotated[int, Field(ge=1, le=2**63 - 1)]) -> dict:
        """Explicitly acknowledge an operator message after reading it, before lengthy
        work. This records reception, not task completion. Answer separately with
        send_message(reply_to=message_id). Claude channel receipts also acknowledge.
        """
        url, token = current_connection()
        return await asyncio.to_thread(request_json, url, token, "/message-receipts", {
            "message_ids": [message_id], "stage": "acknowledged"})

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    async def wait_for_messages(
        after: Annotated[int, Field(ge=0)],
        revision: Annotated[int | None, Field(ge=0)] = None,
        timeout: Annotated[float, Field(ge=0, le=25)] = 25,
    ) -> dict:
        """Wait up to 25 seconds for messages addressed to you/@everyone or status changes.
        Own posts are excluded. Empty filtered pages can advance next_cursor; follow
        has_more until false. Use read_history for explicit broader context. Pass your
        saved next_cursor as after and your saved revision. Save returned cursors only
        after processing. Repeat even on an empty response; use timeout=0 to read now.
        While paused, stop starting room-driven work and keep polling. Stop on archived.
        This call receives messages; the calling agent decides when to act and reply.
        """
        url, token = current_connection()
        params = {"after": after, "timeout": timeout}
        if revision is not None:
            params["revision"] = revision
        result = await asyncio.to_thread(request_json, url, token, "/messages?" + urlencode(params))
        await report_received(result, "polling")
        return result

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    async def read_thread(
        message_id: Annotated[int, Field(ge=1, le=2**63 - 1)],
        after: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict:
        """Fetch one discussion's root and replies, including replies addressed to other
        room participants. Any message in that thread can identify it. Replies are in
        chronological order. Use this result's next_cursor as after for more thread
        replies; NEVER replace your live inbox cursor with this separate thread cursor.
        """
        url, token = current_connection()
        return await asyncio.to_thread(request_json, url, token,
            f"/threads/{message_id}?" + urlencode({"after": after, "limit": limit}))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    async def read_history(
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        scope: Literal["inbox", "all"] = "inbox",
        before: Annotated[int | None, Field(ge=1, le=2**63 - 1)] = None,
    ) -> dict:
        """Read bounded history, oldest first within the selected recent page. Choose
        scope="all" explicitly to read conversations between other participants.
        Use next_before to fetch older pages when has_more is true. This is catch-up
        context only: NEVER replace your live inbox cursor with a history position.
        """
        url, token = current_connection()
        params = {"limit": limit, "scope": scope}
        if before is not None:
            params["before"] = before
        return await asyncio.to_thread(request_json, url, token, "/history?" + urlencode(params))

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    async def acquire_file_locks(
        paths: Annotated[list[str], Field(min_length=1, max_length=50)],
        lease_seconds: Annotated[int, Field(ge=30, le=3600)] = 300,
    ) -> dict:
        """Reserve all shared files you will edit as one atomic lease. Supply absolute
        paths on this MCP host; symlinks resolve locally. All agents must use the same
        canonical paths and Covenant server. Edit only if acquired=true. On conflict,
        wait or work elsewhere; there is no partial acquisition. Save lock_id, renew
        before expires_at, release when done. After reacquiring, reread files first.
        Leases are cooperative: ordinary shell/editor writes can bypass them.
        If a response is lost, list_file_locks shows your lease IDs for recovery.
        """
        url, token = current_connection()
        canonical = []
        for value in paths:
            path = Path(value)
            if not path.is_absolute():
                raise ToolError("Supply absolute file paths from the shared checkout.")
            try:
                path = path.resolve()
                if path.is_dir():
                    raise ToolError("Reserve individual files, including each rename source and destination.")
                canonical.append(os.path.normcase(str(path)))
            except (OSError, RuntimeError, ValueError):
                raise ToolError("Cannot resolve a file path on the MCP host.") from None
        return await asyncio.to_thread(request_json, url, token, "/file-locks/acquire",
                                       {"paths": canonical, "lease_seconds": lease_seconds})

    @server.tool(annotations=ToolAnnotations(destructive_hint=False))
    async def renew_file_locks(
        lock_id: UUID,
        lease_seconds: Annotated[int, Field(ge=30, le=3600)] = 300,
    ) -> dict:
        """Extend an owned, unexpired lease for all its files. Renew before expires_at.
        On renewed=false or an uncertain renewal, stop edits until ownership is
        established. Expired leases cannot be revived; acquire again and reread files.
        """
        url, token = current_connection()
        return await asyncio.to_thread(request_json, url, token, "/file-locks/renew",
                                       {"lock_id": str(lock_id), "lease_seconds": lease_seconds})

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    async def release_file_locks(lock_id: UUID) -> dict:
        """Release your lease after all edits and write-producing commands finish.
        Repeating release is safe; an old ID cannot release a newer lease.
        """
        url, token = current_connection()
        return await asyncio.to_thread(request_json, url, token, "/file-locks/release", {"lock_id": str(lock_id)})

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    async def list_file_locks() -> dict:
        """List active file reservations in this room, with your lease IDs for recovery."""
        url, token = current_connection()
        return await asyncio.to_thread(request_json, url, token, "/file-locks")

    return server


def main():
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
