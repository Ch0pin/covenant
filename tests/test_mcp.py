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


def test_two_real_stdio_clients_exchange_and_revoke(live_room):
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
            assert {tool.name for tool in tools.tools} == {"join_room", "send_message", "wait_for_messages"}
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
