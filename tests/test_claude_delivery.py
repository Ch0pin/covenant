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


def test_handshake_requires_receipt_and_silent_drop_retries(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page([message(1)], 1)))) as client:
            assert await worker.step(client)
            assert worker.status()['state'] == 'awaiting_confirmation'
            assert not worker.status()['channel_verified']
            assert worker.challenge not in json.dumps(worker.status())
            with pytest.raises(ToolError): worker.acknowledge(str(uuid4()))
            now[0] = 61
            assert await worker.step(client)
            assert worker.status()['state'] == 'retrying'
            assert worker.status()['cursor'] == 0
            assert len(events) == 2
            worker.acknowledge(worker.challenge)
            assert worker.status()['channel_verified']
            assert await worker.step(client)
            assert events[-1].meta['message_id'] == '1'
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
            assert await worker.step(client)
            assert worker.status()['automatic_recovery'] and worker.checkpoint.cursor == 0
            assert events[-1].meta['event'] == 'handshake'
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


def test_broken_channel_transport_retries_without_acknowledging(tmp_path):
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
            assert worker.status()['state'] == 'retrying'
            assert not caught.value.terminal
            assert worker.status()['cursor'] == 0
            assert worker.status()['polling_recommended']
        await worker.stop()
    asyncio.run(scenario())


def test_stopped_listener_reports_polling_recovery_after_http_404(tmp_path, monkeypatch):
    async def scenario():
        worker, events, _ = setup_worker(tmp_path, cursor=17)
        worker.confirmed = True
        worker.state = 'listening'
        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda _: httpx.Response(404))
        monkeypatch.setattr('covenant.claude_delivery.httpx.AsyncClient',
                            lambda **kwargs: original_client(transport=transport, **kwargs))
        await worker.run()
        status = worker.status()
        assert status['state'] == 'stopped'
        assert status['mode'] == 'polling'
        assert status['polling_recommended'] is True
        assert status['cursor'] == 17
        assert 'HTTP 404' in status['last_error']
        assert events == []
    asyncio.run(scenario())


def test_recovery_replays_original_receipts_and_accepts_late_acknowledgements(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        current = page()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=current))) as client:
            await handshake(worker, events, client)
            current = page([message(1), message(2)], 2)
            await worker.step(client)
            receipts = {int(e.meta['message_id']): e.meta['receipt'] for e in events if e.meta['event'] == 'message'}
            current = page([], 2);now[0] = 301
            assert await worker.step(client)
            assert worker.status()['state'] == 'retrying'
            worker.acknowledge(receipts[2])  # A late receipt still closes its original message.
            assert worker.checkpoint.cursor == 0
            worker.acknowledge(events[-1].meta['receipt'])
            assert worker.status()['state'] == 'listening' and worker.last_error is None
            await worker.step(client)
            assert events[-1].meta['message_id'] == '1'
            assert events[-1].meta['receipt'] == receipts[1]
            worker.acknowledge(receipts[1])
            assert worker.checkpoint.cursor == 2 and not worker.pending
        await worker.stop()
    asyncio.run(scenario())


def test_recovery_probe_backoff_is_bounded_and_does_not_discard_messages(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page([message(1)], 1)))) as client:
            for instant, expected in [(0,1),(61,2),(62,2),(76,3),(77,3),(106,4),(107,4),(166,5),(226,6)]:
                now[0] = instant
                assert await worker.step(client)
                assert len(events) == expected
                assert worker.checkpoint.cursor == 0
            worker.acknowledge(events[-1].meta['receipt'])
            await worker.step(client)
            assert events[-1].meta['message_id'] == '1'
        await worker.stop()
    asyncio.run(scenario())


def test_transport_recovers_and_replays_after_a_notification_failure(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        current = page()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=current))) as client:
            await handshake(worker, events, client)
            original_notify = worker.notify
            async def broken(_): raise RuntimeError('private transport error')
            worker.notify = broken; current = page([message(1)], 1)
            with pytest.raises(DeliveryError): await worker.step(client)
            receipt = worker.pending[1]['receipt']
            worker.notify = original_notify; now[0] = 15
            await worker.step(client)
            worker.acknowledge(events[-1].meta['receipt'])
            await worker.step(client)
            assert events[-1].meta['receipt'] == receipt
            worker.acknowledge(receipt)
            assert worker.checkpoint.cursor == 1
        await worker.stop()
    asyncio.run(scenario())


def test_running_listener_recovers_without_manual_poll_or_rejoin(tmp_path, monkeypatch):
    async def scenario():
        events, now, message_count = [], [0.0], [0]
        complete = asyncio.Event()
        async def notify(event):
            events.append(event.params)
            if event.params.meta['event'] == 'handshake':
                worker.acknowledge(event.params.meta['receipt'])
            else:
                message_count[0] += 1
                if message_count[0] == 2:
                    worker.acknowledge(event.params.meta['receipt']);complete.set()
        worker = ClaudeDelivery('https://example.com/'+str(uuid4()), 'token', 'alice', 0, notify,
                                directory=tmp_path, clock=lambda: now[0])
        async def handler(request):
            await asyncio.sleep(0.01)
            if message_count[0] == 1: now[0] = 301
            return httpx.Response(200, json=page([message(1)] if int(request.url.params['after']) < 1 else [], 1))
        original_client = httpx.AsyncClient
        monkeypatch.setattr('covenant.claude_delivery.httpx.AsyncClient',
                            lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
        worker.start()
        try:
            await asyncio.wait_for(complete.wait(), 3)
            assert not worker.task.done()
            assert worker.checkpoint.cursor == 1 and worker.state == 'listening'
            messages = [e for e in events if e.meta['event'] == 'message']
            assert messages[0].meta['receipt'] == messages[1].meta['receipt']
        finally: await worker.stop()
    asyncio.run(scenario())


def test_full_backlog_replays_in_batches_without_skipping_new_operator_message(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        current = page()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=current))) as client:
            await handshake(worker, events, client)
            current = page([message(i) for i in range(1,501)], 500)
            await worker.step(client)
            current = page([], 500); now[0] = 301
            await worker.step(client);worker.acknowledge(events[-1].meta['receipt'])
            current = page([message(501, 'operator')], 501)
            count = len(events)
            await worker.step(client)
            assert len(events)-count == 10
            assert worker.scan_cursor == 500 and worker.checkpoint.cursor == 0
            worker.acknowledge(events[-1].meta['receipt'])
            count = len(events)
            await worker.step(client)
            assert events[count].meta['message_id'] == '501'
        await worker.stop()
    asyncio.run(scenario())


def test_confirmation_wait_cursor_never_skips_backlog_when_ack_arrives_mid_poll(tmp_path):
    async def scenario():
        worker, events, now = setup_worker(tmp_path)
        calls = []
        async def handler(request):
            after = int(request.url.params['after']);calls.append(after)
            if len(calls) == 2:
                worker.acknowledge(worker.challenge)
            return httpx.Response(200, json=page([message(1)] if after < 1 else [], 1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.step(client)
            assert worker.confirmation_cursor == 1 and worker.scan_cursor == 0
            await worker.step(client)
            assert worker.confirmed and worker.scan_cursor == 0 and worker.checkpoint.cursor == 0
            await worker.step(client)
            assert calls[:3] == [0, 1, 0]
            assert events[-1].meta['message_id'] == '1'
            worker.acknowledge(events[-1].meta['receipt'])
            assert worker.checkpoint.cursor == 1
        await worker.stop()
    asyncio.run(scenario())
