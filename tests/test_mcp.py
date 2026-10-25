import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

from covenant.auth import digest
from covenant.db import Database
from covenant.mcp_server import validate_room_url


@pytest.fixture
def live_room(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    db = Database(tmp_path / "mcp.sqlite3")
    db.initialize()
    rid, now = str(uuid4()), time.time()
    tokens = {handle: secrets.token_urlsafe(32) for handle in ("alice", "bob")}
    with db.connect() as conn:
        conn.execute("INSERT INTO rooms(id,title,objective,created_at,updated_at) VALUES (?,?,?,?,?)",
                     (rid, "MCP integration", "Exchange through two independent MCP processes", now, now))
        for handle, token in tokens.items():
            conn.execute("INSERT INTO agents(id,room_id,handle,token_hash,created_at) VALUES (?,?,?,?,?)",
                         (str(uuid4()), rid, handle, digest(token), now))
    env = dict(os.environ, PORT=str(port), BIND_HOST="127.0.0.1", PUBLIC_BASE_URL=base,
               DATABASE_PATH=str(db.path))
    process = subprocess.Popen([sys.executable, "-m", "covenant", "serve"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with urlopen(base + "/api/status", timeout=0.2) as response:
                    if response.status == 200:
                        break
            except OSError:
                if process.poll() is not None:
                    pytest.fail("Isolated HTTP server exited during startup")
                time.sleep(0.05)
        else:
            pytest.fail("Isolated HTTP server did not start")
        yield base + "/" + rid, tokens, db, rid
    finally:
        process.terminate()
        process.wait(timeout=10)


def result_data(result):
    assert not result.is_error, str(result.content)
    return result.structured_content or json.loads(result.content[0].text)


def test_two_real_stdio_clients_exchange_and_revoke(live_room, tmp_path):
    url, tokens, db, rid = live_room

    async def scenario():
        async with AsyncExitStack() as stack:
            clients = []
            for _ in range(2):
                streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=sys.executable, args=["-m", "covenant.mcp_server"])))
                session = await stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=40))
                await session.initialize()
                clients.append(session)
            alice, bob = clients
            tools = await alice.list_tools()
            assert {tool.name for tool in tools.tools} == {"join_room", "send_message", "wait_for_messages", "delivery_status", "acknowledge_delivery",
                "read_history", "acquire_file_locks", "renew_file_locks", "release_file_locks", "list_file_locks"}
            error = await alice.call_tool("wait_for_messages", {"after": 0, "timeout": 0})
            assert error.is_error
            joined_a = result_data(await alice.call_tool("join_room", {"room_url": url, "access_token": tokens["alice"]}))
            joined_b = result_data(await bob.call_tool("join_room", {"room_url": url, "access_token": tokens["bob"]}))
            assert joined_a["handle"] == "alice" and joined_b["handle"] == "bob"
            assert tokens["alice"] not in json.dumps(joined_a)
            waiting = asyncio.create_task(bob.call_tool("wait_for_messages", {
                "after": joined_b["next_cursor"], "revision": joined_b["revision"], "timeout": 3}))
            await asyncio.sleep(0.15)
            assert not waiting.done()
            message = result_data(await alice.call_tool("send_message", {
                "text": "@bob Hello through MCP", "client_message_id": "mcp-1"}))
            received = result_data(await asyncio.wait_for(waiting, timeout=3))
            assert received["messages"][0] == message
            assert message["sender"] == "alice"
            reply = result_data(await bob.call_tool("send_message", {
                "text": "@alice Received", "client_message_id": "mcp-2"}))
            assert reply["sender"] == "bob"
            repeated = result_data(await bob.call_tool("send_message", {
                "text": "@alice Received", "client_message_id": "mcp-2"}))
            assert repeated["id"] == reply["id"]
            missed = result_data(await alice.call_tool("wait_for_messages", {"after": message["id"], "timeout": 0}))
            assert missed["messages"][0] == reply
            wrong = await alice.call_tool("join_room", {"room_url": url, "access_token": "x" * 43})
            assert wrong.is_error
            empty = result_data(await alice.call_tool("wait_for_messages", {"after": reply["id"], "timeout": 0.1}))
            assert empty["messages"] == []
            noise = result_data(await alice.call_tool("send_message", {
                "text": "@operator Only for the operator", "client_message_id": "mcp-noise"}))
            filtered = result_data(await bob.call_tool("wait_for_messages", {"after": reply["id"], "timeout": 0}))
            assert filtered["messages"] == [] and filtered["next_cursor"] == noise["id"]
            shared = result_data(await bob.call_tool("read_history", {"scope": "all", "limit": 1}))
            assert shared["messages"] == [noise] and "next_cursor" not in shared
            inbox = result_data(await bob.call_tool("join_room", {
                "room_url": url, "access_token": tokens["bob"], "history_limit": 0}))
            assert inbox["messages"] == [] and inbox["next_cursor"] == noise["id"]
            file = tmp_path / "shared.py"
            file.write_text("original")
            alias = tmp_path / "alias.py"
            alias.symlink_to(file)
            reserved = result_data(await alice.call_tool("acquire_file_locks", {"paths": [str(alias)]}))
            assert reserved["acquired"] and reserved["paths"] == [str(file.resolve())]
            denied = result_data(await bob.call_tool("acquire_file_locks", {"paths": [str(file)]}))
            assert not denied["acquired"]
            listing = result_data(await alice.call_tool("list_file_locks", {}))
            assert listing["locks"][0]["lock_id"] == reserved["lock_id"]
            renewed = result_data(await alice.call_tool("renew_file_locks", {"lock_id": reserved["lock_id"]}))
            assert renewed["renewed"]
            released = result_data(await alice.call_tool("release_file_locks", {"lock_id": reserved["lock_id"]}))
            assert released["released"] == 1
            assert result_data(await bob.call_tool("acquire_file_locks", {"paths": [str(file)]}))["acquired"]
            assert file.read_text() == "original"  # Coordination never edits the reserved file.
            invalid = await alice.call_tool("acquire_file_locks", {"paths": ["relative.py"]})
            assert invalid.is_error
            with db.connect() as conn:
                conn.execute("UPDATE agents SET revoked=1 WHERE room_id=? AND handle='alice'", (rid,))
            revoked = await alice.call_tool("wait_for_messages", {"after": reply["id"], "timeout": 0})
            assert revoked.is_error and "401" in revoked.content[0].text
            assert tokens["alice"] not in revoked.content[0].text
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com/550e8400-e29b-41d4-a716-446655440000", "https://user:pass@example.com/550e8400-e29b-41d4-a716-446655440000", "https://example.com/550e8400-e29b-41d4-a716-446655440000?token=secret"])
def test_mcp_rejects_invalid_connection_urls(url):
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError):
        validate_room_url(url)


def test_codex_bridge_receives_without_agent_polling(live_room, tmp_path):
    url, tokens, db, rid = live_room
    from websockets.asyncio.server import serve
    sid = str(uuid4())
    delivered = []

    async def handler(ws):
        async for raw in ws:
            request = json.loads(raw)
            method = request["method"]
            if method == "initialized": continue
            result = {}
            if method == "thread/read":
                result = {"thread": {"status": {"type": "active"}}}
            elif method == "thread/turns/list":
                result = {"data": [{"id": "busy-turn", "status": "inProgress"}]}
            elif method == "turn/steer":
                assert request["params"]["expectedTurnId"] == "busy-turn"
                delivered.append(request["params"])
                result = {"turnId": "busy-turn"}
            else:
                assert method == "initialize"
            await ws.send(json.dumps({"id": request["id"], "result": result}))

    async def scenario():
        async with AsyncExitStack() as stack:
            codex = await stack.enter_async_context(serve(handler, "127.0.0.1", 0))
            endpoint = "ws://127.0.0.1:" + str(codex.sockets[0].getsockname()[1])
            clients = []
            for _ in range(2):
                streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=sys.executable, args=["-m", "covenant.mcp_server"],
                    env={"COVENANT_CODEX_REMOTE": endpoint})))
                session = await stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=10))
                await session.initialize()
                clients.append(session)
            alice, bob = clients
            initial = result_data(await alice.call_tool("join_room", {
                "room_url": url, "access_token": tokens["alice"], "session_id": sid}))
            assert initial["delivery"]["mode"] == "codex"
            assert initial["delivery"]["state"] == "listening"
            await bob.call_tool("join_room", {"room_url": url, "access_token": tokens["bob"]})
            sent = result_data(await bob.call_tool("send_message", {
                "text": "@operator @alice Automatic delivery", "client_message_id": "push-1"}))
            for _ in range(100):
                status = result_data(await alice.call_tool("delivery_status", {}))
                if status["cursor"] >= sent["id"]:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail(f"Listener did not deliver: {status}")
            assert delivered[0]["threadId"] == sid
            assert "Automatic delivery" in delivered[0]["input"][0]["text"]
            assert tokens["alice"] not in delivered[0]["input"][0]["text"]
            assert status["delivery_method"] == "steer"
            # Rejoining preserves the acknowledgement and does not queue old history.
            await alice.call_tool("join_room", {"room_url": url, "access_token": tokens["alice"], "session_id": sid})
            await asyncio.sleep(0.15)
            assert len(delivered) == 1
            with db.connect() as conn:
                conn.execute("UPDATE agents SET revoked=1 WHERE room_id=? AND handle='alice'", (rid,))
            for _ in range(60):
                status = result_data(await alice.call_tool("delivery_status", {}))
                if status["state"] == "stopped":
                    break
                await asyncio.sleep(0.05)
            assert status["state"] == "stopped" and "401" in status["last_error"]
            assert len(delivered) == 1
    asyncio.run(scenario())


def test_new_bridge_preserves_polling_with_a_legacy_http_server(monkeypatch):
    from covenant import mcp_server
    from mcp.server.mcpserver.exceptions import ToolError
    calls = []
    def legacy_request(url, token, suffix, body=None):
        calls.append(suffix)
        return {"handle": "alice", "messages": [], "next_cursor": 0, "revision": 1}
    monkeypatch.setattr(mcp_server, "request_json", legacy_request)
    monkeypatch.setattr(mcp_server, "codex_endpoint", lambda: "ws://127.0.0.1:12345")
    async def scenario():
        server = mcp_server.create_mcp_server()
        with pytest.raises(ToolError, match="use polling"):
            await server.call_tool("join_room", {"room_url": "https://chat.example.com/" + str(uuid4()),
                "access_token": "x" * 43, "session_id": str(uuid4())})
        status = result_data(await server.call_tool("delivery_status", {}))
        assert status == {"mode": "polling", "state": "joined"}
        result = result_data(await server.call_tool("wait_for_messages", {"after": 0, "timeout": 0}))
        assert result["messages"] == []
        assert len(calls) == 2
    asyncio.run(scenario())


def test_claude_channel_stdio_receipts_restart_and_polling(live_room, tmp_path):
    from mcp.client import NotificationBinding
    from covenant.claude_delivery import ChannelParams
    url, tokens, db, rid = live_room

    async def scenario():
        events = asyncio.Queue()
        async def receive(params):
            await events.put(params)
        async def open_client(stack):
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=sys.executable, args=['-m', 'covenant.mcp_server'],
                env={'COVENANT_STATE_DIR': str(tmp_path / 'state')})))
            client = await stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=10,
                notification_bindings=[NotificationBinding(method='notifications/claude/channel',
                    params_type=ChannelParams, handler=receive)]))
            initialized = await client.initialize()
            assert initialized.capabilities.experimental['claude/channel'] == {}
            return client
        async def post(mid, text):
            from covenant.mcp_server import request_json
            return await asyncio.to_thread(request_json, url, tokens['bob'], '/messages',
                {'client_message_id': mid, 'text': text})
        async def join_and_confirm(client):
            joined = result_data(await client.call_tool('join_room', {
                'room_url': url, 'access_token': tokens['alice'], 'delivery_mode': 'claude'}))
            assert joined['delivery']['state'] == 'awaiting_confirmation'
            event = await asyncio.wait_for(events.get(), 3)
            assert event.meta['event'] == 'handshake'
            ack = result_data(await client.call_tool('acknowledge_delivery', {'receipt': event.meta['receipt']}))
            assert ack['channel_verified'] and ack['state'] == 'listening'
            return ack
        async with AsyncExitStack() as stack:
            client = await open_client(stack)
            await join_and_confirm(client)
            sent = await post('channel-1', '@alice @operator From another agent')
            event = await asyncio.wait_for(events.get(), 3)
            assert event.meta['message_id'] == str(sent['id'])
            assert 'From another agent' in event.content and tokens['alice'] not in event.content
            status = result_data(await client.call_tool('delivery_status', {}))
            assert status['cursor'] < sent['id']
            result_data(await client.call_tool('acknowledge_delivery', {'receipt': event.meta['receipt']}))
            # The existing send tool provides the response path back to the same room.
            reply = result_data(await client.call_tool('send_message', {
                'text': '@operator Received the channel message.', 'client_message_id': 'channel-reply'}))
            assert reply['sender'] == 'alice'
            pending = await post('channel-2', '@alice Preserve this across MCP restart')
            pending_event = await asyncio.wait_for(events.get(), 3)
            assert pending_event.meta['message_id'] == str(pending['id'])
        # Full MCP process shutdown/restart; no model polling was required to receive.
        async with AsyncExitStack() as stack:
            client = await open_client(stack)
            confirmed = await join_and_confirm(client)
            assert confirmed['cursor'] < pending['id']
            replay = await asyncio.wait_for(events.get(), 3)
            assert replay.meta['message_id'] == str(pending['id'])
            assert replay.meta['receipt'] != pending_event.meta['receipt']
            wrong = await client.call_tool('acknowledge_delivery', {'receipt': pending_event.meta['receipt']})
            assert wrong.is_error
            # Explicit polling fallback remains usable with all unacknowledged messages.
            saved = result_data(await client.call_tool('delivery_status', {}))['cursor']
            joined = result_data(await client.call_tool('join_room', {
                'room_url': url, 'access_token': tokens['alice'], 'delivery_mode': 'polling'}))
            assert joined['delivery']['mode'] == 'polling'
            received = result_data(await client.call_tool('wait_for_messages', {'after': saved, 'timeout': 0}))
            assert any(m['id'] == pending['id'] for m in received['messages'])
            await join_and_confirm(client)
            event = await asyncio.wait_for(events.get(), 3)
            with db.connect() as conn:
                conn.execute("UPDATE agents SET revoked=1 WHERE room_id=? AND handle='alice'", (rid,))
            revoked = await client.call_tool('acknowledge_delivery', {'receipt': event.meta['receipt']})
            assert revoked.is_error and '401' in revoked.content[0].text
    asyncio.run(scenario())
