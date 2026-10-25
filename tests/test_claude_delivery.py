import asyncio
import json
import stat
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from covenant.claude_delivery import Checkpoint, ClaudeDelivery
from covenant.delivery import DeliveryError


def setup_worker(tmp_path, *, cursor=0):
    events, now = [], [0.0]
    async def notify(event):
        events.append(event.params)
    worker = ClaudeDelivery('https://example.com/' + str(uuid4()), 'private-token', 'alice',
                            cursor, notify, directory=tmp_path, clock=lambda: now[0])
    return worker, events, now


def page(messages=(), cursor=0, state='active'):
    return {'messages': list(messages), 'next_cursor': cursor, 'revision': 1, 'room_status': state}


def message(mid, sender='bob', targets=None):
    return {'id': mid, 'sender': sender, 'targets': targets or ['alice'], 'text': '@alice request ' + str(mid)}


async def handshake(worker, events, client):
    await worker.step(client)
    assert events[-1].meta['event'] == 'handshake'
    worker.acknowledge(events[-1].meta['receipt'])
    assert worker.status()['channel_verified']


def test_handshake_requires_receipt_and_silent_drop_falls_back(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page([message(1)], 1)))) as client:
            assert await worker.step(client)
            assert worker.status()['state'] == 'awaiting_confirmation'
            assert not worker.status()['channel_verified']
            assert worker.challenge not in json.dumps(worker.status())
            with pytest.raises(ToolError): worker.acknowledge(str(uuid4()))
            now[0] = 61
            assert not await worker.step(client)
            assert worker.status()['mode'] == 'polling'
            assert worker.status()['cursor'] == 0
            assert len(events) == 1
            with pytest.raises(ToolError): worker.acknowledge(worker.challenge)
        await worker.stop()
    asyncio.run(scenario())


def test_operator_priority_receipts_target_filter_and_durable_gap_cursor(tmp_path):
    async def scenario():
        worker, events, _ = setup_worker(tmp_path)
        current = page()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=current))) as client:
            await handshake(worker, events, client)
            current = page([message(1), message(2, 'operator', ['operator', 'alice']),
                            message(3, 'bob', ['everyone']), message(4, 'alice'),
                            message(5, 'bob', ['other'])], 5)
            await worker.step(client)
            assert [e.meta['message_id'] for e in events[1:]] == ['2', '1', '3']
            assert worker.checkpoint.cursor == 0  # writing notifications never acknowledges them
            receipts = {int(e.meta['message_id']): e.meta['receipt'] for e in events[1:]}
            worker.acknowledge(receipts[2])
            assert worker.checkpoint.cursor == 0
            worker.acknowledge(receipts[1])
            assert worker.checkpoint.cursor == 2
            worker.acknowledge(receipts[1])  # retry is safe
            # An earlier pending receipt must not prevent a newer operator request.
            current = page([message(6, 'operator')], 6)
            await worker.step(client)
            assert events[-1].meta['message_id'] == '6'
            worker.acknowledge(events[-1].meta['receipt'])
            assert worker.checkpoint.cursor == 2
            worker.acknowledge(receipts[3])
            assert worker.checkpoint.cursor == 6
            saved = worker.checkpoint.path
            assert json.loads(saved.read_text()) == {'cursor': 6}
            assert stat.S_IMODE(saved.stat().st_mode) == 0o600
            assert 'private-token' not in saved.read_text()
        await worker.stop()
        # Tunnel hostname changes and MCP restarts preserve the same room/token cursor.
        changed = worker.room_url.replace('example.com', 'new.example.com')
        resumed = ClaudeDelivery(changed, worker.token, worker.handle, 10, worker.notify, directory=tmp_path)
        assert resumed.checkpoint.cursor == 6 and not resumed.confirmed
        with pytest.raises(ToolError): resumed.acknowledge(receipts[3])
        await resumed.stop()
    asyncio.run(scenario())


def test_unacknowledged_message_replays_after_restart_and_timeout(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        current = page()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=current))) as client:
            await handshake(worker, events, client)
            current = page([message(1)], 1)
            await worker.step(client)
            current = page([], 1)
            now[0] = 301
            assert not await worker.step(client)
            assert worker.status()['mode'] == 'polling' and worker.checkpoint.cursor == 0
        await worker.stop()
        resumed = ClaudeDelivery(worker.room_url, worker.token, worker.handle, 1, worker.notify, directory=tmp_path)
        assert resumed.scan_cursor == 0
        await resumed.stop()
    asyncio.run(scenario())


def test_pause_then_resume_and_archive_before_emit(tmp_path):
    async def scenario():
        worker, events, _ = setup_worker(tmp_path)
        current = page()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=current))) as client:
            await handshake(worker, events, client)
            current = page([message(1)], 1, 'paused')
            await worker.step(client)
            assert len(events) == 1 and worker.checkpoint.cursor == 0
            current = page([message(1)], 1)
            await worker.step(client)
            assert len(events) == 2
        calls = []
        def handler(_):
            calls.append(1)
            return httpx.Response(200, json=page([message(2)], 2, 'active' if len(calls) == 1 else 'archived'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert not await worker.step(client)
            assert len(events) == 2 and worker.state == 'archived'
        await worker.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize('status,terminal', [(401, True), (404, True), (302, True), (429, False), (503, False)])
def test_authentication_and_transient_errors_preserve_cursor(tmp_path, status, terminal):
    async def scenario():
        worker, _, _ = setup_worker(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text='private-token'))) as client:
            with pytest.raises(DeliveryError) as caught: await worker.step(client)
            assert caught.value.terminal == terminal
            assert 'private-token' not in str(caught.value)
            assert worker.checkpoint.cursor == 0
        await worker.stop()
    asyncio.run(scenario())


def test_checkpoint_blocks_duplicate_listener_and_rejects_corrupt_state(tmp_path):
    url = 'https://example.com/' + str(uuid4())
    checkpoint = Checkpoint(url, 'test-token', 3, tmp_path)
    with pytest.raises(BlockingIOError): Checkpoint(url, 'test-token', 3, tmp_path)
    checkpoint.close()
    checkpoint.path.write_text('{"cursor": 99}')
    with pytest.raises(ValueError): Checkpoint(url, 'test-token', 3, tmp_path)
    checkpoint.path.write_text('{"cursor": 2}')
    resumed = Checkpoint(url, 'test-token', 3, tmp_path)
    assert resumed.cursor == 2
    resumed.close()


def test_broken_channel_transport_falls_back_without_acknowledging(tmp_path):
    async def scenario():
        worker, events, _ = setup_worker(tmp_path)
        current = page()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=current))) as client:
            await handshake(worker, events, client)
            async def broken(_):
                raise RuntimeError('private-token')
            worker.notify = broken
            current = page([message(1)], 1)
            with pytest.raises(DeliveryError) as caught: await worker.step(client)
            assert 'private-token' not in str(caught.value)
            assert worker.status()['mode'] == 'polling'
            assert worker.status()['cursor'] == 0
            assert worker.status()['polling_recommended']
        await worker.stop()
    asyncio.run(scenario())
