import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from covenant.approvals import create_bridge
from covenant.automatic_approvals import ApprovalManager, bind_session
from covenant.app import create_app
from covenant.claude_approvals import dispatch, end_session
from test_app import setup, bearer, register_delivery, delivery_get


def snapshot(db, aid):
    with db.connect() as conn:
        return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} WHERE agent_id=?", (aid,))]
                for table in ('participant_session_owners', 'approval_sessions', 'delivery_bindings', 'agent_delivery_status')}


def test_child_join_and_anonymous_fallback_cannot_replace_owner_or_approval(setup):
    client, app, settings, csrf, rid, (alice, bob) = setup
    parent = register_delivery(client, rid, alice)['delivery']
    message = client.post(f'/api/rooms/{rid}/messages', headers=csrf,
                         json={'text': '@alice Status?', 'client_message_id': 'status'}).json()
    bid, credential = create_bridge(app.state.db, rid, 'alice', 'codex', parent['session_id'])
    native_auth = {'Authorization': 'Bearer ' + credential}
    pending = client.post('/approval-bridge/requests', headers=native_auth,
        json={'session_id': parent['session_id'], 'request_key': 'main', 'kind': 'command', 'details': {'command': 'printf harmless'}}).json()['id']
    before = snapshot(app.state.db, alice['id'])
    for body in ({'session_id': str(uuid4()), 'connection_id': str(uuid4()), 'avatar_id': 'moon'},
                 {}, {'owner_session_id': str(uuid4())}):
        response = client.post(f'/{rid}/join', headers=bearer(alice), json=body)
        assert response.status_code == 409
        assert 'invitation' in response.json()['detail']
        assert snapshot(app.state.db, alice['id']) == before
    assert delivery_get(client, rid, alice, parent).json()['messages'][0]['id'] == message['id']
    assert client.post(f'/approval-bridge/requests/{pending}/take', headers=native_auth).json()['status'] == 'pending'
    assert client.post(f'/api/approvals/{pending}/decision', headers=csrf, json={'decision': 'approve'}).status_code == 200
    assert client.post(f'/approval-bridge/requests/{pending}/take', headers=native_auth).json()['decision'] == 'approve'


def test_polling_retains_owner_across_restart_and_rejects_subagent(setup):
    client, app, settings, csrf, rid, (alice, _) = setup
    parent = register_delivery(client, rid, alice)['delivery']
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={'owner_session_id': parent['session_id']}).status_code == 200
    app.state.db.initialize()
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={'session_id': str(uuid4()), 'connection_id': str(uuid4())}).status_code == 409
    with app.state.db.connect() as conn:
        assert conn.execute('SELECT session_id FROM approval_sessions WHERE agent_id=?', (alice['id'],)).fetchone()[0] == parent['session_id']
    assert register_delivery(client, rid, alice, parent['session_id'])['delivery']['session_id'] == parent['session_id']


def test_operator_token_renewal_reassigns_owner_and_preserves_history(setup):
    client, app, settings, csrf, rid, (alice, _) = setup
    parent = register_delivery(client, rid, alice)['delivery']
    client.post(f'/api/rooms/{rid}/messages', headers=csrf, json={'text': '@alice Keep this history', 'client_message_id': 'history'})
    old_token = alice['token']
    fresh = client.post(f'/api/rooms/{rid}/agents/{alice["id"]}/rotate', headers=csrf).json()
    assert client.post(f'/{rid}/join', headers={'Authorization': 'Bearer ' + old_token}, json={}).status_code == 401
    with app.state.db.connect() as conn:
        for table in ('participant_session_owners', 'approval_sessions', 'delivery_bindings'):
            assert conn.execute(f'SELECT count(*) FROM {table} WHERE agent_id=?', (alice['id'],)).fetchone()[0] == 0
    new = register_delivery(client, rid, fresh)['delivery']
    assert new['session_id'] != parent['session_id']
    assert len(client.get(f'/api/rooms/{rid}/messages').json()['messages']) == 1


def test_claude_owner_survives_session_end_and_hook_rejects_reassignment(setup):
    client, app, settings, csrf, rid, (alice, _) = setup
    client.post(f'/{rid}/join', headers=bearer(alice), json={})
    parent, child = str(uuid4()), str(uuid4())
    url = settings.public_base_url + '/' + rid
    assert bind_session(app.state.db, url, alice['token'], 'claude', parent)
    before = snapshot(app.state.db, alice['id'])
    payload = {'hook_event_name': 'PostToolUse', 'session_id': child, 'tool_name': 'mcp__covenant__join_room',
               'tool_input': {'room_url': url, 'access_token': alice['token']}, 'tool_response': {}}
    result = asyncio.run(dispatch(payload, app.state.db, settings.port))
    assert 'another native session' in result['hookSpecificOutput']['additionalContext']
    assert snapshot(app.state.db, alice['id']) == before
    end_session(app.state.db, parent)
    with pytest.raises(ValueError, match='another native session'):
        bind_session(app.state.db, url, alice['token'], 'claude', child)
    assert bind_session(app.state.db, url, alice['token'], 'claude', parent)


def test_opencode_owner_survives_restart_without_codex_approval_adoption(setup):
    client, app, settings, csrf, rid, (alice, _) = setup
    session_id = 'ses_review123'
    response = client.post(f'/{rid}/join', headers=bearer(alice), json={
        'session_id': session_id, 'connection_id': str(uuid4()), 'delivery_mode': 'opencode'})
    assert response.status_code == 200

    def assert_opencode_only(db):
        with db.connect() as conn:
            owner = conn.execute('SELECT harness,session_id FROM participant_session_owners WHERE agent_id=?',
                                 (alice['id'],)).fetchone()
            delivery = conn.execute('SELECT harness,session_id FROM delivery_bindings WHERE agent_id=?',
                                    (alice['id'],)).fetchone()
            assert tuple(owner) == ('opencode', session_id)
            assert tuple(delivery) == ('opencode', session_id)
            assert conn.execute('SELECT count(*) FROM approval_sessions WHERE agent_id=?',
                                (alice['id'],)).fetchone()[0] == 0

    assert_opencode_only(app.state.db)
    assert ApprovalManager(app, app.state.db, settings).candidates() == []

    restarted = create_app(settings)
    restarted.state.db.initialize()
    assert_opencode_only(restarted.state.db)
    assert ApprovalManager(restarted, restarted.state.db, settings).candidates() == []


def test_existing_binding_is_pinned_during_idempotent_upgrade(setup):
    client, app, settings, csrf, rid, (alice, _) = setup
    parent = register_delivery(client, rid, alice)['delivery']
    with app.state.db.connect() as conn:
        conn.execute('DROP TABLE participant_session_owners')
    app.state.db.initialize(); app.state.db.initialize()
    with app.state.db.connect() as conn:
        owner = conn.execute('SELECT * FROM participant_session_owners WHERE agent_id=?', (alice['id'],)).fetchone()
        assert owner['session_id'] == parent['session_id']
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={'session_id': str(uuid4()), 'connection_id': str(uuid4())}).status_code == 409


def test_legacy_delivery_binding_migrates_to_codex_harness(setup):
    client, app, settings, csrf, rid, (alice, _) = setup
    session_id = str(uuid4())
    with app.state.db.connect() as conn:
        conn.execute('DROP TABLE delivery_bindings')
        conn.execute('''CREATE TABLE delivery_bindings (
            agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL, connection_id TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL)''')
        conn.execute('UPDATE agents SET joined_at=? WHERE id=?', (1, alice['id']))
        conn.execute('INSERT INTO delivery_bindings VALUES (?,?,?,?,?)',
                     (alice['id'], session_id, str(uuid4()), 0, 1))
    app.state.db.initialize()
    with app.state.db.connect() as conn:
        binding = conn.execute('SELECT harness,session_id FROM delivery_bindings WHERE agent_id=?',
                               (alice['id'],)).fetchone()
        owner = conn.execute('SELECT harness,session_id FROM participant_session_owners WHERE agent_id=?',
                             (alice['id'],)).fetchone()
    assert tuple(binding) == ('codex', session_id)
    assert tuple(owner) == ('codex', session_id)


def test_simultaneous_first_joins_choose_one_owner_atomically(setup):
    client, app, settings, csrf, rid, (alice, _) = setup
    def join(sid):
        response = client.post(f'/{rid}/join', headers=bearer(alice), json={'session_id': sid, 'connection_id': str(uuid4())})
        return sid, response.status_code
    with ThreadPoolExecutor() as pool:
        results = list(pool.map(join, [str(uuid4()), str(uuid4())]))
    assert sorted(status for _, status in results) == [200, 409]
    winner = next(sid for sid, status in results if status == 200)
    with app.state.db.connect() as conn:
        for table in ('participant_session_owners', 'approval_sessions', 'delivery_bindings'):
            assert conn.execute(f'SELECT session_id FROM {table} WHERE agent_id=?', (alice['id'],)).fetchone()[0] == winner


@pytest.mark.parametrize('explicit_owner', [False, True])
def test_mcp_rejected_rejoin_keeps_listener_and_owner_fallback_works(setup, monkeypatch, explicit_owner):
    from covenant import mcp_server
    from mcp.server.mcpserver.exceptions import ToolError
    client, app, settings, csrf, rid, (alice, _) = setup
    workers = []
    class Delivery:
        def __init__(self, url, token, handle, registration, endpoint):
            self.session_id = registration['session_id']; self.stopped = False; workers.append(self)
        def start(self): pass
        async def stop(self): self.stopped = True
        def status(self): return {'mode': 'codex', 'state': 'listening', 'session_id': self.session_id}
    def request(url, token, suffix, body=None):
        response = client.post('/' + rid + suffix, headers={'Authorization': 'Bearer ' + token}, json=body)
        if response.status_code != 200: raise ToolError(response.json()['detail'])
        result = response.json()
        result['capabilities']['message_receipts'] = False  # Keep test receipt reporting local.
        return result
    monkeypatch.setattr(mcp_server, 'CodexDelivery', Delivery)
    monkeypatch.setattr(mcp_server, 'request_json', request)
    async def run():
        server = mcp_server.create_mcp_server()
        join = server._tool_manager.get_tool('join_room').fn
        status = server._tool_manager.get_tool('delivery_status').fn
        url = settings.public_base_url + '/' + rid
        sid = str(uuid4())
        await join(room_url=url, access_token=alice['token'], delivery_mode='codex', session_id=sid)
        with pytest.raises(ToolError, match='another native session'):
            await join(room_url=url, access_token=alice['token'], delivery_mode='codex', session_id=str(uuid4()))
        assert not workers[0].stopped
        assert (await status())['session_id'] == sid
        options = {'session_id': sid} if explicit_owner else {}
        result = await join(room_url=url, access_token=alice['token'], delivery_mode='polling', **options)
        assert result['delivery']['mode'] == 'polling' and workers[0].stopped
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT session_id FROM approval_sessions WHERE agent_id=?', (alice['id'],)).fetchone()[0] == sid
    asyncio.run(run())
