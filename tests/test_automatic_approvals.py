import asyncio
import json
import secrets
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from websockets.asyncio.server import serve

from covenant.app import create_app
from covenant.auth import digest, hash_password
from covenant.automatic_approvals import ApprovalManager, bind_session
from covenant.claude_approvals import dispatch, install
from covenant.config import Settings
from covenant.db import Database


@pytest.fixture
def isolated(tmp_path):
    settings = Settings(database=tmp_path / 'test.sqlite3', port=8765, public_base_url='http://127.0.0.1:8765')
    db = Database(settings.database)
    db.initialize()
    sid, rid, aid = (str(uuid4()) for _ in range(3))
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute('INSERT INTO operator VALUES (1,?,?)', ('test', hash_password('isolated-password-123')))
        conn.execute('INSERT INTO rooms(id,title,created_at,updated_at) VALUES (?,?,?,?)', (rid, 'Isolated approvals', time.time(), time.time()))
        conn.execute('INSERT INTO agents(id,room_id,handle,token_hash,joined_at,created_at) VALUES (?,?,?,?,?,?)',
                     (aid, rid, 'tester', digest(token), time.time(), time.time()))
    return settings, db, sid, rid, aid, token


async def until(check, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            value = await check()
            if value:
                return value
            await asyncio.sleep(.02)


@pytest.mark.parametrize('kind', ['command', 'mcp'])
@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_server_start_and_restart_forward_native_requests(isolated, monkeypatch, kind, decision):
    settings, db, sid, rid, aid, token = isolated
    with db.connect() as conn:
        conn.execute('INSERT INTO delivery_bindings VALUES (?,?,?,?,?)', (aid, sid, str(uuid4()), 0, time.time()))
    answers, connections = [], []
    async def native(ws):
        connections.append(ws)
        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get('method')
            if method == 'initialized':
                continue
            if 'result' in msg:
                answers.append(msg['result'])
                await ws.send(json.dumps({'method': 'serverRequest/resolved', 'params': {'threadId': sid, 'requestId': 77}}))
                continue
            result = {'thread': {'status': {'type': 'active'}}} if method == 'thread/read' else {}
            await ws.send(json.dumps({'id': msg['id'], 'result': result}))
            if method == 'thread/resume':
                params = {'threadId': sid, 'turnId': 'turn', 'command': 'printf harmless', 'availableDecisions': ['accept', 'cancel']}
                approval_method = 'item/commandExecution/requestApproval'
                if kind == 'mcp':
                    approval_method = 'mcpServer/elicitation/request'
                    params = {'threadId': sid, 'turnId': 'turn', 'serverName': 'fixture', 'mode': 'form',
                        'message': 'Allow harmless fixture tool?', 'requestedSchema': {'type': 'object', 'properties': {}},
                        '_meta': {'codex_approval_kind': 'mcp_tool_call', 'tool_params': {'text': 'hello'}}}
                # An unrelated thread must never appear in this room.
                await ws.send(json.dumps({'id': 76, 'method': approval_method, 'params': {**params, 'threadId': str(uuid4())}}))
                await ws.send(json.dumps({'id': 77, 'method': approval_method, 'params': params}))
    async def run():
        async with serve(native, '127.0.0.1', 0) as harness:
            monkeypatch.setenv('COVENANT_CODEX_REMOTE', 'ws://127.0.0.1:' + str(harness.sockets[0].getsockname()[1]))
            app = create_app(settings)
            for cycle in range(2):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_base_url) as client:
                        login = await client.post('/api/login', json={'username': 'test', 'password': 'isolated-password-123'})
                        csrf = {'X-CSRF-Token': login.json()['csrf_token']}
                        async def pending():
                            response = (await client.get('/api/approvals')).json()
                            rows = [r for r in response['requests'] if r['status'] == 'pending']
                            return rows
                        requests = await until(pending)
                        assert len(requests) == 1
                        assert requests[0]['session_id'] == sid and requests[0]['handle'] == 'tester'
                        status = (await client.get('/api/approvals')).json()['connections']
                        assert status[0]['state'] == 'connected'
                        response = await client.post('/api/approvals/' + requests[0]['id'] + '/decision', headers=csrf, json={'decision': decision})
                        assert response.status_code == 200
                        async def received(): return len(answers) == cycle + 1
                        await until(received)
                        expected = 'accept' if decision == 'approve' else 'decline'
                        assert answers[-1] == ({'action': expected, 'content': None} if kind == 'mcp' else {'decision': expected})
                with db.connect() as conn:
                    assert not conn.execute("SELECT 1 FROM approval_bridges WHERE expires_at>?", (time.time(),)).fetchone()
            assert len(connections) == 2
    asyncio.run(run())


def test_supervisor_reconnects_and_cleans_up_on_rotation(isolated):
    settings, db, sid, rid, aid, token = isolated
    assert bind_session(db, settings.public_base_url + '/' + rid, token, 'codex', sid)
    attempts, stopped = [], []
    class Worker:
        def __init__(self, api, session, on_connected):
            self.connected = on_connected
        async def run(self, endpoint):
            attempts.append(endpoint)
            if len(attempts) == 1:
                raise OSError('simulated disconnect')
            self.connected()
            try:
                await asyncio.Future()
            finally:
                stopped.append(True)
    async def run():
        app = create_app(settings)
        manager = ApprovalManager(app, db, settings, worker_factory=Worker)
        app.state.approval_manager = manager
        await manager.start()
        try:
            async def connected(): return manager.status() and manager.status()[0]['state'] == 'connected'
            await until(connected)
            with db.connect() as conn:
                conn.execute('UPDATE agents SET token_hash=? WHERE id=?', ('rotated', aid))
            await manager.reconcile()
            assert stopped and not manager.jobs and not manager.status()
            with db.connect() as conn:
                assert not conn.execute('SELECT 1 FROM approval_bridges WHERE expires_at>?', (time.time(),)).fetchone()
        finally:
            await manager.stop()
    asyncio.run(run())


def test_single_supervisor_and_no_duplicate_session_routing(isolated):
    settings, db, sid, rid, aid, token = isolated
    other = str(uuid4())
    with db.connect() as conn:
        conn.execute('INSERT INTO agents(id,room_id,handle,token_hash,joined_at,created_at) VALUES (?,?,?,?,?,?)',
                     (other, rid, 'other', digest('other'), time.time(), time.time()))
        for identifier in [aid, other]:
            conn.execute('INSERT INTO delivery_bindings VALUES (?,?,?,?,?)', (identifier, sid, str(uuid4()), 0, time.time()))
    async def run():
        app = create_app(settings)
        first, second = [ApprovalManager(app, db, settings) for _ in range(2)]
        await first.start()
        await second.start()
        try:
            assert second.task is None
            await first.reconcile()
            assert not first.jobs
            assert len(first.status()) == 2
            assert all(s['state'] == 'disconnected' for s in first.status())
        finally:
            await first.stop()
            await second.stop()
    asyncio.run(run())


def test_claude_setup_preserves_settings_is_idempotent_and_has_no_credentials(isolated, tmp_path):
    settings, db, sid, rid, aid, token = isolated
    dest = tmp_path / 'claude settings' / 'settings.json'
    dest.parent.mkdir()
    original = {'permissions': {'ask': ['Bash']}, 'hooks': {'PermissionRequest': [{'hooks': [{'type': 'command', 'command': 'existing-hook'}]}]}}
    dest.write_text(json.dumps(original))
    path, backup = install(settings, dest)
    assert json.loads(backup.read_text()) == original
    result = json.loads(path.read_text())
    assert result['permissions'] == original['permissions']
    assert result['hooks']['PermissionRequest'][0] == original['hooks']['PermissionRequest'][0]
    assert token not in path.read_text()
    assert set(result['hooks']) == {'PostToolUse', 'PermissionRequest', 'SessionEnd'}
    assert install(settings, dest) == (dest, None)
    assert path.stat().st_mode & 0o777 == 0o600
    dest.write_text('{broken')
    with pytest.raises(ValueError): install(settings, dest)
    assert dest.read_text() == '{broken'


def test_real_claude_join_hook_registers_without_an_extra_command(isolated):
    settings, db, sid, rid, aid, token = isolated
    payload = {'hook_event_name': 'PostToolUse', 'session_id': sid, 'tool_name': 'mcp__covenant__join_room',
        'tool_input': {'room_url': settings.public_base_url + '/' + rid, 'access_token': token},
        'tool_response': {'isError': False}}
    result = subprocess.run([sys.executable, '-m', 'covenant.claude_approvals', '--database', str(db.path)],
        input=json.dumps(payload), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'linked' in json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
    assert token not in result.stdout + result.stderr
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM approval_sessions').fetchone()
        assert row['session_id'] == sid and row['harness'] == 'claude'


@pytest.mark.parametrize('decision,expected', [('approve', 'allow'), ('reject', 'deny')])
def test_claude_persistent_hook_roundtrip_after_join_and_server_restart(isolated, decision, expected):
    settings, db, sid, rid, aid, token = isolated
    join = {'hook_event_name': 'PostToolUse', 'session_id': sid, 'tool_name': 'mcp__covenant__join_room',
        'tool_input': {'room_url': settings.public_base_url + '/' + rid, 'access_token': token}, 'tool_response': {}}
    request = {'hook_event_name': 'PermissionRequest', 'session_id': sid, 'tool_name': 'Bash', 'tool_input': {'command': 'printf harmless'}}
    async def run():
        assert await dispatch(request, db, settings.port) is None
        assert await dispatch(join, db, settings.port)
        app = create_app(settings)
        from covenant.approval_bridge import ApprovalClient
        class API(ApprovalClient):
            def __init__(self, base, credential):
                self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_base_url,
                    headers={'Authorization': 'Bearer ' + credential})
        for _ in range(2):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_base_url) as client:
                    login = (await client.post('/api/login', json={'username': 'test', 'password': 'isolated-password-123'})).json()
                    task = asyncio.create_task(dispatch(request, db, settings.port, api_factory=API))
                    async def pending():
                        rows = (await client.get('/api/approvals')).json()['requests']
                        return next((r for r in rows if r['status'] == 'pending'), None)
                    row = await until(pending)
                    await client.post('/api/approvals/' + row['id'] + '/decision',
                        headers={'X-CSRF-Token': login['csrf_token']}, json={'decision': decision})
                    result = await asyncio.wait_for(task, 3)
                    assert result['hookSpecificOutput']['decision']['behavior'] == expected
        await dispatch({'hook_event_name': 'SessionEnd', 'session_id': sid}, db, settings.port)
        assert await dispatch(request, db, settings.port) is None
    asyncio.run(run())


@pytest.mark.parametrize('change', ['revoked', 'paused', 'rotated'])
def test_claude_inactive_link_denies_and_failed_join_does_not_register(isolated, change):
    settings, db, sid, rid, aid, token = isolated
    join = {'hook_event_name': 'PostToolUse', 'session_id': sid, 'tool_name': 'mcp__covenant__join_room',
        'tool_input': {'room_url': settings.public_base_url + '/' + rid, 'access_token': token}, 'tool_response': {'isError': True}}
    assert asyncio.run(dispatch(join, db, settings.port)) is None
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM approval_sessions').fetchone()[0] == 0
    join['tool_response'] = {}
    asyncio.run(dispatch(join, db, settings.port))
    with db.connect() as conn:
        if change == 'paused': conn.execute("UPDATE rooms SET status='paused'")
        elif change == 'revoked': conn.execute('UPDATE agents SET revoked=1')
        else: conn.execute("UPDATE agents SET token_hash='rotated'")
    result = asyncio.run(dispatch({'hook_event_name': 'PermissionRequest', 'session_id': sid}, db, settings.port))
    assert result['hookSpecificOutput']['decision']['behavior'] == 'deny'


def test_authenticated_joins_replace_binding_preserve_polling_and_require_fresh_token(isolated):
    settings, db, sid, rid, aid, token = isolated
    from covenant.approvals import create_bridge
    app = create_app(settings)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_base_url) as client:
            headers = {'Authorization': 'Bearer ' + token}
            join = {'session_id': sid, 'connection_id': str(uuid4())}
            response = await client.post('/' + rid + '/join', headers=headers, json=join)
            assert response.status_code == 200
            bid, bridge_token = create_bridge(db, rid, 'tester', 'codex', sid)
            new_sid = str(uuid4())
            join['session_id'] = new_sid
            assert (await client.post('/' + rid + '/join', headers=headers, json=join)).status_code == 200
            with db.connect() as conn:
                assert conn.execute('SELECT session_id FROM approval_sessions').fetchone()[0] == new_sid
                assert conn.execute('SELECT expires_at FROM approval_bridges WHERE id=?', (bid,)).fetchone()[0] == 0
            # Polling is a message transport fallback, not a request to disable approvals.
            assert (await client.post('/' + rid + '/join', headers=headers, json={})).status_code == 200
            with db.connect() as conn:
                assert conn.execute('SELECT session_id FROM approval_sessions').fetchone()[0] == new_sid
                assert conn.execute('SELECT count(*) FROM delivery_bindings').fetchone()[0] == 0
                conn.execute('UPDATE agents SET token_hash=? WHERE id=?', (digest('replacement'), aid))
            manager = ApprovalManager(app, db, settings)
            assert not manager.candidates()
            assert (await client.post('/' + rid + '/join', headers=headers, json=join)).status_code == 401
            response = await client.post('/' + rid + '/join', headers={'Authorization': 'Bearer replacement'}, json=join)
            assert response.status_code == 200
            assert manager.candidates()[0]['session_id'] == new_sid
    asyncio.run(run())


def test_session_reassignment_does_not_revive_previous_codex_link(isolated):
    settings, db, sid, rid, aid, token = isolated
    other = str(uuid4())
    other_token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute('INSERT INTO agents(id,room_id,handle,token_hash,joined_at,created_at) VALUES (?,?,?,?,?,?)',
            (other, rid, 'other', digest(other_token), time.time(), time.time()))
        conn.execute('INSERT INTO delivery_bindings VALUES (?,?,?,?,?)', (aid, sid, str(uuid4()), 0, time.time()))
    assert bind_session(db, settings.public_base_url + '/' + rid, token, 'codex', sid)
    assert bind_session(db, settings.public_base_url + '/' + rid, other_token, 'codex', sid)
    manager = ApprovalManager(create_app(settings), db, settings)
    assert [r['agent_id'] for r in manager.candidates()] == [other]


def test_token_rotated_during_bridge_creation_is_rejected(isolated):
    settings, db, sid, rid, aid, token = isolated
    from covenant.approvals import create_bridge
    with db.connect() as conn:
        conn.execute('UPDATE agents SET token_hash=? WHERE id=?', (digest('new'), aid))
    with pytest.raises(ValueError):
        create_bridge(db, rid, 'tester', 'codex', sid, expected_token_hash=digest(token))
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM approval_bridges').fetchone()[0] == 0


def test_http_join_cannot_take_another_active_participants_native_session(isolated):
    settings, db, sid, rid, aid, token = isolated
    other = str(uuid4())
    with db.connect() as conn:
        conn.execute('INSERT INTO agents(id,room_id,handle,token_hash,created_at) VALUES (?,?,?,?,?)',
            (other, rid, 'other', digest('other-token'), time.time()))
    app = create_app(settings)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_base_url) as client:
            join = {'session_id': sid, 'connection_id': str(uuid4())}
            assert (await client.post('/' + rid + '/join', headers={'Authorization': 'Bearer ' + token}, json=join)).status_code == 200
            response = await client.post('/' + rid + '/join', headers={'Authorization': 'Bearer other-token'}, json=join)
            assert response.status_code == 409
            with db.connect() as conn:
                assert conn.execute('SELECT agent_id FROM approval_sessions').fetchone()[0] == aid
                assert conn.execute('SELECT agent_id FROM delivery_bindings').fetchone()[0] == aid
    asyncio.run(run())
