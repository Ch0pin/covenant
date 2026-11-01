import asyncio
from contextlib import suppress
import json
import sys
from uuid import uuid4

import httpx
import pytest

from covenant.delivery import (CodexDelivery, OpenCodeDelivery, DeliveryError,
                               deliver_codex, deliver_opencode)


def listener(enqueue):
    return CodexDelivery("https://chat.example.com/" + str(uuid4()), "test-private-token", "alice",
                         {"session_id": str(uuid4()), "connection_id": str(uuid4()), "cursor": 0},
                         "ws://127.0.0.1:12345", enqueue=enqueue)


def test_operator_priority_ack_retry_and_stable_message_identity():
    async def scenario():
        calls, acks = [], []
        async def enqueue(command, session_id, text):
            calls.append(json.loads(text.split("\n", 1)[1]))
        worker = listener(enqueue)
        page = {"room_status": "active", "revision": 1, "next_cursor": 3,
                "messages": [{"id": 1, "sender": "bob", "text": "@alice first"},
                             {"id": 2, "sender": "operator", "text": "@alice urgent"}]}
        def handler(request):
            assert request.headers["authorization"] == "Bearer test-private-token"
            if request.url.path.endswith("/ack"):
                acks.append(json.loads(request.content))
                if len(acks) == 1:
                    return httpx.Response(503)
                return httpx.Response(200, json={"cursor": 3})
            return httpx.Response(200, json=page)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     headers={"Authorization": "Bearer test-private-token"}) as client:
            assert await worker.step(client)
            assert [m["id"] for m in page["messages"]] == [1, 2]
            assert [m["message_id"] for m in calls] == [2, 1]
            assert all(m["room_id"] == worker.room_url.rsplit("/", 1)[1] for m in calls)
            assert worker.cursor == 0 and worker.ack_cursor == 3
            with pytest.raises(DeliveryError):
                await worker.step(client)
            assert await worker.step(client)
            assert worker.cursor == 3 and worker.ack_cursor is None
            assert len(calls) == 2 and acks[0] == acks[1]
    asyncio.run(scenario())


def test_partial_queue_failure_retries_only_unqueued_messages():
    async def scenario():
        attempts = []
        async def enqueue(command, session_id, text):
            mid = json.loads(text.split("\n", 1)[1])["message_id"]
            attempts.append(mid)
            if mid == 2 and attempts.count(2) == 1:
                raise DeliveryError("temporary failure")
        worker = listener(enqueue)
        page = {"room_status": "active", "revision": 1, "next_cursor": 2,
                "messages": [{"id": i, "sender": "bob", "text": "@alice hi"} for i in (1, 2)]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page))) as client:
            with pytest.raises(DeliveryError):
                await worker.step(client)
            assert worker.ack_cursor is None
            assert await worker.step(client)
            assert attempts == [1, 2, 2]
            assert worker.ack_cursor == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("code,terminal", [(401, True), (403, True), (404, True), (409, True),
                                          (302, True), (422, True), (429, False), (503, False)])
def test_delivery_http_errors_are_redacted_and_classified(code, terminal):
    async def scenario():
        worker = listener(None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(code, text="test-private-token"))) as client:
            with pytest.raises(DeliveryError) as caught:
                await worker.step(client)
            assert caught.value.terminal is terminal
            assert "test-private-token" not in str(caught.value)
    asyncio.run(scenario())


def test_archive_between_poll_and_enqueue_stops_delivery():
    async def scenario():
        calls = []
        async def enqueue(*args):
            calls.append(args)
        worker = listener(enqueue)
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"room_status": "active" if len(requests) == 1 else "archived",
                "revision": 1, "next_cursor": 1, "messages": [{"id": 1, "sender": "bob", "text": "@alice hi"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert not await worker.step(client)
            assert not calls and worker.ack_cursor is None and worker.state == "archived"
    asyncio.run(scenario())



@pytest.mark.parametrize("state,method", [("active", "turn/steer"), ("idle", "turn/start")])
@pytest.mark.parametrize("transport", ["websocket", "unix"])
def test_delivery_enters_active_or_idle_turn(state, method, transport):
    import tempfile
    from contextlib import AsyncExitStack
    from websockets.asyncio.server import serve, unix_serve
    async def scenario():
        requests = []
        sid = str(uuid4())
        message = '$(touch should-never-exist) `echo test` "quotes"\nsecond line'
        async def handler(ws):
            async for raw in ws:
                request = json.loads(raw)
                requests.append(request)
                name = request["method"]
                if name == "initialized":
                    continue
                if name == "initialize":
                    result = {}
                elif name == "thread/read":
                    result = {"thread": {"status": {"type": state}, "canAcceptDirectInput": True}}
                elif name == "thread/turns/list":
                    result = {"data": [{"id": "active-turn", "status": "inProgress"}]}
                else:
                    assert name == method
                    result = {"turnId": "active-turn"} if state == "active" else {"turn": {"id": "new-turn"}}
                await ws.send(json.dumps({"method": "irrelevant/notification", "params": {}}))
                await ws.send(json.dumps({"id": request["id"], "result": result}))
        async with AsyncExitStack() as stack:
            if transport == "unix":
                directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="covenant-", dir="/tmp"))
                path = directory + "/codex.sock"
                await stack.enter_async_context(unix_serve(handler, path))
                endpoint = "unix://" + path
            else:
                server = await stack.enter_async_context(serve(handler, "127.0.0.1", 0))
                endpoint = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            assert await deliver_codex(endpoint, sid, message) == ("steer" if state == "active" else "start")
            assert await deliver_codex(endpoint, sid, message) in {"steer", "start"}
        delivered = [r for r in requests if r["method"] == method]
        assert len(delivered) == 2
        params = delivered[0]["params"]
        assert params["threadId"] == sid
        assert params["input"] == [{"type": "text", "text": message}]
        assert params["clientUserMessageId"] == delivered[1]["params"]["clientUserMessageId"]
        if state == "active":
            assert params["expectedTurnId"] == "active-turn"
            assert "turn/start" not in [r["method"] for r in requests]
        assert set(params) <= {"threadId", "input", "clientUserMessageId", "expectedTurnId"}
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["closed", "race", "rejected", "unconfirmed"])
def test_delivery_failure_never_queues_or_advances_cursor(failure):
    from websockets.asyncio.server import serve
    async def scenario():
        requests = []
        async def handler(ws):
            async for raw in ws:
                request = json.loads(raw)
                name = request["method"]
                requests.append(name)
                if name == "initialized": continue
                result = {}
                if name == "thread/read":
                    result = {"thread": {"status": {"type": "notLoaded" if failure == "closed" else "active"}}}
                if name == "thread/turns/list":
                    result = {"data": [{"id": "turn", "status": "completed" if failure == "race" else "inProgress"}]}
                response = {"id": request["id"], "result": result}
                if name == "turn/steer" and failure == "rejected":
                    response = {"id": request["id"], "error": {"code": -32600, "message": "secret-token"}}
                await ws.send(json.dumps(response))
        async with serve(handler, "127.0.0.1", 0) as server:
            worker = listener(deliver_codex)
            worker.endpoint = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            page = {"room_status": "active", "revision": 1, "next_cursor": 1,
                    "messages": [{"id": 1, "sender": "operator", "text": "@alice status?"}]}
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page))) as client:
                with pytest.raises(DeliveryError) as caught: await worker.step(client)
                assert "secret-token" not in str(caught.value)
                assert worker.cursor == 0 and worker.ack_cursor is None
                assert not worker.sent
        assert "turn/start" not in requests and "thread/queue/add" not in requests
    asyncio.run(scenario())


def test_delivery_timeout_and_cancel_close_only_client_connection():
    from websockets.asyncio.server import serve
    async def scenario(cancel):
        closed = asyncio.Event()
        async def handler(ws):
            await ws.wait_closed()
            closed.set()
        async with serve(handler, "127.0.0.1", 0) as server:
            endpoint = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            task = asyncio.create_task(deliver_codex(endpoint, str(uuid4()), "test", timeout=0.1))
            if cancel:
                await asyncio.sleep(0.05)
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else DeliveryError): await task
            await asyncio.wait_for(closed.wait(), 2)
            assert server.is_serving()
    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_delivery_failure_recommends_polling_and_operator_ack():
    worker = listener(None)
    worker.state = "retrying"
    assert worker.status()["polling_recommended"]
    assert "acknowledge_message" in worker.prompt({"id": 1, "sender": "operator", "text": "status?"})
    worker.state = "listening"
    assert not worker.status()["polling_recommended"]


def test_opencode_delivery_wakes_session_with_stable_prompt_id(monkeypatch):
    calls = []
    prompt = "Covenant chat delivery.\n{\"message_id\":7}"

    class Process:
        returncode = 0

        def __init__(self, payload):
            self.payload = payload

        async def communicate(self):
            message_id = json.loads(self.payload)["id"]
            return json.dumps({"data": {"id": message_id, "sessionID": "ses_test123"}}).encode(), b""

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return Process(args[6])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    async def scenario():
        assert await deliver_opencode("/usr/bin/opencode", "ses_test123", prompt) == "steer"
        assert await deliver_opencode("/usr/bin/opencode", "ses_test123", prompt) == "steer"

    asyncio.run(scenario())
    assert len(calls) == 2
    first = calls[0][0]
    assert first[:3] == ("/usr/bin/opencode", "api", "session.prompt")
    assert first[3:5] == ("--param", "sessionID=ses_test123")
    payload = json.loads(first[6])
    repeated = json.loads(calls[1][0][6])
    assert payload["id"].startswith("msg_covenant_")
    assert repeated["id"] == payload["id"]
    assert payload["text"] == prompt
    assert payload["delivery"] == "steer" and payload["resume"] is True


@pytest.mark.parametrize(("stdout", "stderr"), [
    (b'{"message":"Session not found: ses_409example"}', b"HTTP 404 Not Found"),
    (b'{"_tag":"ConflictError","message":"Conflict"}', b"HTTP 409 Conflict"),
])
def test_opencode_delivery_never_infers_admission_from_failed_cli_output(monkeypatch, stdout, stderr):
    class Process:
        returncode = 1

        async def communicate(self):
            return stdout, stderr

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(DeliveryError, match="rejected session delivery"):
        asyncio.run(deliver_opencode("/usr/bin/opencode", "ses_409example", "message"))


def test_opencode_delivery_requires_confirmation_of_exact_message(monkeypatch):
    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"data":{"id":"msg_some_other_message"}}', b""

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(DeliveryError, match="did not confirm admission"):
        asyncio.run(deliver_opencode("/usr/bin/opencode", "ses_test123", "message"))


def test_opencode_delivery_cancellation_stops_subprocess_before_returning(tmp_path):
    started = tmp_path / "started"
    completed = tmp_path / "completed"
    script = tmp_path / "slow-opencode"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, time\n"
        f"pathlib.Path({str(started)!r}).write_text('started')\n"
        "time.sleep(0.4)\n"
        f"pathlib.Path({str(completed)!r}).write_text('completed')\n"
    )
    script.chmod(0o700)

    async def scenario():
        task = asyncio.create_task(deliver_opencode(str(script), "ses_test123", "message", timeout=5))

        async def wait_for_start():
            while not started.exists():
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(wait_for_start(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, DeliveryError):
                    await task
        await asyncio.sleep(0.45)
        assert not completed.exists()

    asyncio.run(scenario())


def test_opencode_delivery_timeout_kills_and_awaits_controlled_process(monkeypatch):
    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(DeliveryError, match="timed out"):
        asyncio.run(deliver_opencode("/usr/bin/opencode", "ses_test123", "message", timeout=0.01))
    assert process.killed and process.waited


def test_opencode_worker_reports_mode_and_acks_with_it():
    async def scenario():
        calls, acknowledgements = [], []

        async def enqueue(command, session_id, text):
            calls.append((command, session_id, text))
            return "steer"

        worker = OpenCodeDelivery(
            "https://chat.example.com/" + str(uuid4()), "test-private-token", "alice",
            {"session_id": "ses_test123", "connection_id": str(uuid4()), "cursor": 0},
            "/usr/bin/opencode", enqueue=enqueue,
        )
        page = {"room_status": "active", "revision": 1, "next_cursor": 1,
                "messages": [{"id": 1, "sender": "operator", "text": "@alice status?"}]}

        def handler(request):
            if request.url.path.endswith("/ack"):
                acknowledgements.append(json.loads(request.content))
                return httpx.Response(200, json={"cursor": 1})
            return httpx.Response(200, json=page)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await worker.step(client)
            assert await worker.step(client)
        assert worker.status()["mode"] == "opencode"
        assert calls[0][1] == "ses_test123"
        assert acknowledgements == [{"connection_id": worker.connection_id,
                                     "cursor": 1, "mode": "opencode"}]

    asyncio.run(scenario())
