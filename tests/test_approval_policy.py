import time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from covenant.app import create_app
from covenant.approvals import create_bridge
from test_app import setup, bearer, PASSWORD
from test_approvals import approvals, submit


def policy(a, enabled=True):
    client, _, csrf, rid, *_ = a
    return client.patch(f"/api/rooms/{rid}/approval-policy", headers=csrf, json={"auto_approve": enabled})


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize("kind", ["command", "file", "tool"])
def test_auto_approves_pending_and_future_requests_through_existing_bridge(approvals, harness, kind):
    client, app, csrf, rid, invitations, bid, auth, payload = approvals
    with app.state.db.connect() as conn:
        conn.execute("UPDATE approval_bridges SET harness=? WHERE id=?", (harness, bid))
    aid = submit(approvals, {**payload, "kind": kind})
    assert client.get('/api/approvals').json()['auto_approve_rooms'] == {}
    assert client.post(f'/approval-bridge/requests/{aid}/take', headers=auth).json()['status'] == 'pending'
    assert policy(approvals).json() == {'auto_approve': True, 'approved_count': 1}
    future = submit(approvals, {**payload, 'request_key': 'next', 'kind': kind})
    for request_id in (aid, future):
        # No operator refresh or model call is required to decide or deliver the request.
        assert client.post(f'/approval-bridge/requests/{request_id}/take', headers=auth).json() == {'status': 'decided', 'decision': 'approve'}
        assert client.post(f'/approval-bridge/requests/{request_id}/take', headers=auth).json() == {'status': 'submitted'}
    rows = client.get('/api/approvals').json()['requests']
    assert all(row['auto_approved'] and row['decision'] == 'approve' for row in rows)
    with app.state.db.connect() as conn:
        assert conn.execute('SELECT DISTINCT decided_by FROM approval_requests').fetchone()[0] == 'operator'


def test_policy_is_room_scoped_persists_and_can_be_disabled(approvals):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    assert policy(approvals).status_code == 200
    with TestClient(create_app(app.state.settings), base_url=app.state.settings.public_base_url) as restarted:
        restarted.post('/api/login', json={'username': 'operator', 'password': PASSWORD})
        assert restarted.get('/api/approvals').json()['auto_approve_rooms'][rid] is True
    other = client.post('/api/rooms', headers=csrf, json={'title': 'Manual room'}).json()['id']
    client.post(f'/api/rooms/{other}/agents', headers=csrf, json={'handle': 'alice'})
    _, token = create_bridge(app.state.db, other, 'alice', 'codex', payload['session_id'])
    headers = {'Authorization': 'Bearer ' + token}
    aid = client.post('/approval-bridge/requests', headers=headers, json=payload).json()['id']
    assert client.post(f'/approval-bridge/requests/{aid}/take', headers=headers).json()['status'] == 'pending'
    assert policy(approvals, False).json()['auto_approve'] is False
    manual = submit(approvals)
    assert client.post(f'/approval-bridge/requests/{manual}/take', headers=auth).json()['status'] == 'pending'


def test_policy_requires_operator_csrf_and_strict_boolean(approvals):
    client, app, csrf, rid, inv, *_ = approvals
    url = f'/api/rooms/{rid}/approval-policy'
    for headers in ({}, bearer(inv[0]), {**csrf, 'Origin': 'https://elsewhere.example'}):
        assert client.patch(url, headers=headers, json={'auto_approve': True}).status_code == 403
    for value in ('true', 1, None):
        assert client.patch(url, headers=csrf, json={'auto_approve': value}).status_code == 422
    assert client.patch(url, headers=csrf, json={'auto_approve': True, 'updated_by': 'attacker'}).status_code == 422
    assert client.patch(f'/api/rooms/{uuid4()}/approval-policy', headers=csrf, json={'auto_approve': True}).status_code == 404
    client.cookies.clear()
    assert client.patch(url, headers=bearer(inv[0]), json={'auto_approve': True}).status_code == 401


@pytest.mark.parametrize('invalid', ['expired', 'disconnected', 'revoked', 'rotated', 'changed_session', 'incomplete'])
def test_auto_approve_preserves_native_and_liveness_constraints(approvals, invalid):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    aid = submit(approvals, {**payload, 'can_approve': invalid != 'incomplete'})
    with app.state.db.connect() as conn:
        if invalid == 'expired': conn.execute('UPDATE approval_requests SET expires_at=0 WHERE id=?', (aid,))
        elif invalid == 'disconnected': conn.execute('UPDATE approval_requests SET alive_until=0 WHERE id=?', (aid,))
        elif invalid == 'revoked': conn.execute('UPDATE agents SET revoked=1 WHERE id=?', (inv[0]['id'],))
        elif invalid == 'rotated': conn.execute("UPDATE agents SET token_hash='rotated' WHERE id=?", (inv[0]['id'],))
        elif invalid == 'changed_session':
            conn.execute('INSERT INTO approval_sessions VALUES (?,?,?,?,?)', (inv[0]['id'], 'codex', str(uuid4()), conn.execute('SELECT token_hash FROM agents WHERE id=?', (inv[0]['id'],)).fetchone()[0], time.time()))
    assert policy(approvals).json()['approved_count'] == 0
    with app.state.db.connect() as conn:
        assert conn.execute('SELECT decision FROM approval_requests WHERE id=?', (aid,)).fetchone()[0] is None
        assert conn.execute('SELECT COUNT(*) FROM approval_auto_decisions').fetchone()[0] == 0


@pytest.mark.parametrize('room_state', ['paused', 'archived'])
def test_inactive_room_policy_can_be_turned_off_and_does_not_approve(approvals, room_state):
    client, app, csrf, rid, *_ = approvals
    policy(approvals)
    client.patch(f'/api/rooms/{rid}', headers=csrf, json={'status': room_state})
    assert policy(approvals).status_code == 409
    assert policy(approvals, False).status_code == 200
    if room_state == 'archived':
        assert client.delete(f'/api/rooms/{rid}', headers=csrf).status_code == 200
        with app.state.db.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM room_approval_policy').fetchone()[0] == 0


def test_manual_rejection_and_auto_approval_cannot_overwrite_each_other(approvals):
    client, app, csrf, rid, inv, bid, auth, payload = approvals
    aid = submit(approvals)
    with ThreadPoolExecutor() as pool:
        manual = pool.submit(client.post, f'/api/approvals/{aid}/decision', headers=csrf, json={'decision': 'reject'})
        automatic = pool.submit(policy, approvals)
        manual_status = manual.result().status_code
        assert automatic.result().status_code == 200
    row = next(r for r in client.get('/api/approvals').json()['requests'] if r['id'] == aid)
    assert row['decision'] == ('reject' if manual_status == 200 else 'approve')
    assert bool(row['auto_approved']) == (manual_status == 409)
    assert client.post(f'/approval-bridge/requests/{aid}/take', headers=auth).json()['decision'] == row['decision']
