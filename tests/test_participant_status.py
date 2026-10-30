import asyncio
import json
import time
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from websockets.asyncio.server import serve

from test_app import setup, bearer, send
from test_automatic_approvals import isolated, until
from covenant.app import create_app
from covenant.automatic_approvals import bind_session
from covenant.config import Settings
from covenant.participant_status import NativeStatus, ERRORS, error_reason, participant_status


@pytest.mark.parametrize('code', list(ERRORS))
def test_native_explicit_error_codes_are_sanitized_and_clear_on_new_turn(code):
    state = NativeStatus()
    state.consume('turn/started', {'turn': {'id': 't1', 'status': 'inProgress'}}, now=100)
    state.consume('error', {'threadId': 'session', 'turnId': 't1', 'error': {
        'codexErrorInfo': code, 'message': 'private-token-private-prompt', 'additionalDetails': 'private data'}}, now=110)
    view = state.view(True, 120)
    assert view['reason']['code'] == ERRORS[code][0]
    assert view['reason_since'] == 110 and 'private' not in json.dumps(view)
    assert not state.view(True, 201)['available']
    state.consume('turn/started', {'turn': {'id': 't2', 'status': 'inProgress'}}, now=202)
    assert not state.view(True, 202)['reason']
    state.consume('error', {'turnId': 't1', 'error': {'codexErrorInfo': 'cyberPolicy'}}, now=203)
    assert not state.view(True, 203)['reason']


def test_unknown_errors_do_not_guess_cyber_or_quota_reason():
    assert error_reason({'message': 'cyber quota refusal'})['code'] == 'service_error'
    assert error_reason({'codexErrorInfo': ['bad shape']})['code'] == 'service_error'
    assert error_reason({'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 503}}})['code'] == 'connection_error'


def test_safety_and_verification_require_explicit_events_and_clear():
    s = NativeStatus()
    s.consume('model/safetyBuffering/updated', {'showBufferingUi': True, 'reasons': ['untrusted text']}, now=100)
    assert s.reason['label'] == 'Safety processing'
    s.consume('model/safetyBuffering/updated', {'showBufferingUi': False}, now=101)
    assert s.reason is None
    s.consume('model/verification', {'verifications': ['trustedAccessForCyber']}, now=102)
    assert s.reason['label'] == 'Cybersecurity verification required'
    s.consume('model/verification', {'verifications': []}, now=103)
    assert s.reason is None


def test_snapshots_keep_error_age_and_restore_prior_failed_turn_without_payload():
    s = NativeStatus()
    snapshot = {'status': {'type': 'idle'}, 'turn': {'id': 'failed', 'status': 'failed', 'error': {'codexErrorInfo': 'usageLimitExceeded', 'message': 'secret'}}}
    s.consume('covenant/status-snapshot', snapshot, now=100)
    s.consume('covenant/status-snapshot', snapshot, now=130)
    assert s.reason_since == 100 and s.view(True, 200)['available']
    s.consume('account/rateLimits/updated', {'rateLimits': {'primary': {'usedPercent': 100, 'resetsAt': 300}}}, now=130)
    assert s.view(True, 200)['reset_at'] == 300
    assert s.view(True, 301)['reset_at'] is None
    s.consume('covenant/status-unavailable', {}, now=202)
    assert not s.view(True, 203)['available']
    s.consume('turn/completed', {'turn': {'id': 'ok', 'status': 'completed'}}, now=204)
    assert s.view(True, 204)['phase'] == 'idle' and s.reason is None


def derived(*, native=None, approval=None, receipt=None, state='active', revoked=0, seen=990, joined=800, delivery=None, threshold=120):
    return participant_status({'revoked': revoked, 'last_seen': seen, 'joined_at': joined}, state,
        delivery or {'state': 'listening', 'updated_at': seen}, approval, receipt, native, now=1000, warning_seconds=threshold)


def test_approval_pause_and_known_blockers_take_precedence_over_missing_ack():
    r = {'message_id': 1, 'delivered_at': 800, 'queued_at': 790}
    assert derived(receipt=r)['code'] == 'ack_overdue'
    assert derived(receipt={**r, 'delivered_at': 950})['code'] == 'awaiting_ack'
    assert derived(receipt={**r, 'delivered_at': None})['code'] == 'awaiting_delivery'
    assert derived(receipt=r, state='paused')['code'] == 'paused'
    assert derived(receipt=r, revoked=1)['code'] == 'revoked'
    assert derived(receipt=r, approval={'status':'pending','created_at':900})['code'] == 'approval'
    s = NativeStatus();s.consume('error', {'error':{'codexErrorInfo':'contextWindowExceeded'}}, now=990)
    assert derived(receipt=r, native=s.view(True, 1000))['code'] == 'context_limit'
    assert derived(receipt=r, threshold=300)['code'] == 'awaiting_ack'
    assert derived(seen=800)['code'] == 'connection_stale'
    assert derived()['label'] == 'Connected · native state unavailable'
    assert derived(joined=None)['code'] == 'not_joined'


def test_native_active_flags_and_closed_sessions():
    s = NativeStatus()
    for flag, phase in [('waitingOnApproval','approval'),('waitingOnUserInput','input')]:
        s.consume('thread/status/changed', {'status':{'type':'active','activeFlags':[flag]}}, now=990)
        assert derived(native=s.view(True, 1000))['code'] == phase
    s.consume('thread/closed', {}, now=995)
    assert derived(native=s.view(False, 1000))['code'] == 'disconnected'


def test_status_endpoint_is_operator_only_and_filters_receipts(setup):
    client, app, settings, csrf, rid, (alice, bob) = setup
    url = f'/api/rooms/{rid}/participant-status'
    assert client.get(url).json()['statuses'][alice['id']]['code'] == 'not_joined'
    client.post(f'/{rid}/join', headers=bearer(alice), json={})
    # Agent messages are not operator requests and never trigger the ack warning.
    send(client, rid, bob, '@alice agent question')
    assert client.get(url).json()['statuses'][alice['id']]['code'] == 'connecting'
    message = client.post(f'/api/rooms/{rid}/messages', headers=csrf, json={'text':'@alice check in','client_message_id':'request'}).json()
    with app.state.db.connect() as c:
        c.execute('UPDATE message_receipts SET delivered_at=? WHERE message_id=?', (time.time()-121, message['id']))
    statuses = client.get(url).json()['statuses']
    assert statuses[alice['id']]['code'] == 'ack_overdue'
    assert statuses[bob['id']]['code'] == 'not_joined'
    client.post(f'/{rid}/message-receipts', headers=bearer(alice), json={'message_ids':[message['id']], 'stage':'acknowledged'})
    assert client.get(url).json()['statuses'][alice['id']]['code'] != 'ack_overdue'
    client.cookies.clear()
    assert client.get(url, headers=bearer(alice)).status_code == 401


def test_rotating_token_discards_native_state_and_old_unacked_requests(setup):
    client, app, settings, csrf, rid, (alice,bob) = setup
    client.post(f'/{rid}/join', headers=bearer(alice), json={})
    message = client.post(f'/api/rooms/{rid}/messages', headers=csrf, json={'text':'@alice old','client_message_id':'old'}).json()
    with app.state.db.connect() as c:
        c.execute('UPDATE message_receipts SET delivered_at=? WHERE message_id=?', (time.time()-200,message['id']))
    fresh = client.post(f'/api/rooms/{rid}/agents/{alice["id"]}/rotate', headers=csrf).json()
    client.post(f'/{rid}/join', headers=bearer(fresh), json={})
    assert client.get(f'/api/rooms/{rid}/participant-status').json()['statuses'][alice['id']]['code'] != 'ack_overdue'
    manager = app.state.approval_manager
    native = NativeStatus();native.consume('error', {'error':{'codexErrorInfo':'cyberPolicy'}})
    manager.native_states[alice['id']]={'fingerprint':('old','oldtoken'), 'status':native}
    manager.states[alice['id']]={'state':'connected'}
    assert manager.participant_status(alice['id'], {'session_id':'new','agent_token_hash':'newtoken'},time.time()) is None


def test_native_events_reach_ui_api_and_wrong_threads_are_ignored(isolated, monkeypatch):
    settings, db, sid, rid, aid, token = isolated
    assert bind_session(db, settings.public_base_url+'/'+rid, token, 'codex', sid)
    sockets=[]
    async def native(ws):
        sockets.append(ws)
        async for raw in ws:
            msg=json.loads(raw);method=msg.get('method')
            if method=='initialized':continue
            result = {'thread':{'status':{'type':'active','activeFlags':[]}}} if method=='thread/read' else {'data':[{'id':'turn','status':'inProgress'}]} if method=='thread/turns/list' else {}
            await ws.send(json.dumps({'id':msg['id'],'result':result}))
    async def run():
        async with serve(native,'127.0.0.1',0) as server:
            monkeypatch.setenv('COVENANT_CODEX_REMOTE','ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1]))
            app=create_app(settings)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=settings.public_base_url) as client:
                    await client.post('/api/login',json={'username':'test','password':'isolated-password-123'})
                    async def available():
                        r=(await client.get(f'/api/rooms/{rid}/participant-status')).json()['statuses'][aid]
                        return r if r['native_available'] else None
                    await until(available)
                    async def emit(method, params):await sockets[0].send(json.dumps({'method':method,'params':params}))
                    await emit('error',{'threadId':'another-session','turnId':'turn','error':{'codexErrorInfo':'cyberPolicy'}})
                    await emit('error',{'threadId':sid,'turnId':'turn','error':{'codexErrorInfo':'usageLimitExceeded','message':'secret'}})
                    async def blocked():
                        r=await available();return r if r and r['code']=='usage_limit' else None
                    status=await until(blocked);assert 'secret' not in json.dumps(status)
                    await emit('turn/started',{'threadId':sid,'turn':{'id':'new','status':'inProgress'}})
                    async def cleared():
                        r=await available();return r if r and r['code']!='usage_limit' else None
                    await until(cleared)
    asyncio.run(run())


def test_threshold_configuration(monkeypatch):
    monkeypatch.setenv('STATUS_ACK_WARNING_SECONDS','180')
    assert Settings.load().status_ack_warning_seconds==180
    for seconds in (0,29,86401):
        with pytest.raises(ValueError):replace(Settings(),status_ack_warning_seconds=seconds)


def test_old_transient_safety_event_is_not_kept_current_by_unrelated_snapshots():
    s = NativeStatus()
    s.consume('model/safetyBuffering/updated', {'showBufferingUi': True}, now=100)
    s.consume('covenant/status-snapshot', {'status': {'type': 'active'}, 'turn': {'id': 'same', 'status': 'inProgress'}}, now=120)
    # Establish the turn before a subsequent explicit buffering event.
    s.consume('model/safetyBuffering/updated', {'turnId': 'same', 'showBufferingUi': True}, now=125)
    s.consume('covenant/status-snapshot', {'status': {'type': 'active'}, 'turn': {'id': 'same', 'status': 'inProgress'}}, now=220)
    assert not s.view(True, 220)['available']


def test_unknown_protocol_status_marks_telemetry_unavailable():
    s = NativeStatus()
    s.consume('covenant/status-snapshot', {'status': {'type': 'idle'}}, now=100)
    assert s.view(True, 100)['available']
    s.consume('covenant/status-snapshot', {'status': {'type': 'future-status'}}, now=101)
    assert not s.view(True, 101)['available']


@pytest.mark.parametrize("activity_kind", ["message", "acknowledgement"])
def test_recent_agent_activity_supersedes_old_receipts_without_resolving_them(setup, activity_kind):
    client, app, settings, csrf, rid, (alice, bob) = setup
    client.post(f'/{rid}/join', headers=bearer(alice), json={})
    old = client.post(f'/api/rooms/{rid}/messages', headers=csrf,
        json={'text': '@alice old request', 'client_message_id': 'old-request'}).json()
    now = time.time()
    with app.state.db.connect() as c:
        c.execute('UPDATE agents SET joined_at=? WHERE id=?', (now-2000, alice['id']))
        c.execute('UPDATE message_receipts SET queued_at=?, delivered_at=? WHERE message_id=?',
                  (now-1000, now-900, old['id']))
    url = f'/api/rooms/{rid}/participant-status'
    # Polling refreshes last_seen and delivery, but does not demonstrate model activity.
    client.get(f'/{rid}/messages?after=0&timeout=0', headers=bearer(alice))
    assert client.get(url).json()['statuses'][alice['id']]['code'] == 'ack_overdue'
    if activity_kind == 'message':
        assert send(client, rid, alice, '@bob Continuing work').status_code == 200
    else:
        other = client.post(f'/api/rooms/{rid}/messages', headers=csrf,
            json={'text': '@alice separate request', 'client_message_id': 'separate'}).json()
        response = client.post(f'/{rid}/message-receipts', headers=bearer(alice),
            json={'message_ids': [other['id']], 'stage': 'acknowledged'})
        assert response.status_code == 200
    status = client.get(url).json()['statuses'][alice['id']]
    assert status['code'] == 'active_recently' and status['level'] == 'ok'
    assert status['since'] >= now
    assert status['unacknowledged_count'] == 1
    assert f"#{old['id']}" in status['detail']
    with app.state.db.connect() as c:
        receipt = c.execute('SELECT * FROM message_receipts WHERE message_id=? AND agent_id=?',
                            (old['id'], alice['id'])).fetchone()
        assert receipt['acknowledged_at'] is None and receipt['answered_at'] is None
    # Another participant's messages cannot make Alice appear active.
    assert client.get(url).json()['statuses'][bob['id']]['code'] == 'not_joined'


def test_new_request_after_activity_is_tracked_even_with_older_missing_receipts(setup):
    client, app, settings, csrf, rid, (alice, bob) = setup
    client.post(f'/{rid}/join', headers=bearer(alice), json={})
    old = client.post(f'/api/rooms/{rid}/messages', headers=csrf,
        json={'text': '@alice old request', 'client_message_id': 'old'}).json()
    activity = send(client, rid, alice, '@bob Progress update').json()
    new = client.post(f'/api/rooms/{rid}/messages', headers=csrf,
        json={'text': '@alice new request', 'client_message_id': 'new'}).json()
    now = time.time()
    with app.state.db.connect() as c:
        c.execute('UPDATE agents SET joined_at=? WHERE id=?', (now-2000, alice['id']))
        c.execute('UPDATE messages SET created_at=? WHERE id=?', (now-300, activity['id']))
        c.execute('UPDATE message_receipts SET queued_at=?, delivered_at=? WHERE message_id=?',
                  (now-1000, now-900, old['id']))
        c.execute('UPDATE message_receipts SET queued_at=?, delivered_at=? WHERE message_id=?',
                  (now-250, now-200, new['id']))
    status = client.get(f'/api/rooms/{rid}/participant-status').json()['statuses'][alice['id']]
    assert status['code'] == 'ack_overdue'
    assert status['since'] == now-200
    assert status['detail'].startswith(f"Operator message #{new['id']} was delivered")
    assert status['unacknowledged_count'] == 2


def test_native_working_is_preserved_in_overdue_acknowledgement_label():
    native = NativeStatus()
    native.consume('turn/started', {'turn': {'id': 'active', 'status': 'inProgress'}}, now=990)
    status = derived(native=native.view(True, 1000),
                     receipt={'message_id': 1, 'delivered_at': 800, 'queued_at': 790})
    assert status['code'] == 'ack_overdue'
    assert status['label'] == 'Working · acknowledgement overdue'


def test_recent_activity_expires_without_becoming_an_unresponsive_verdict():
    agent = {'revoked': 0, 'joined_at': 100, 'last_seen': 1000}
    status = participant_status(agent, 'active', None, None, None, None,
                                now=1000, warning_seconds=120, activity_at=900)
    assert status['code'] == 'active_recently' and status['since'] == 900
    status = participant_status(agent, 'active', None, None, None, None,
                                now=1021, warning_seconds=120, activity_at=900)
    assert status['code'] == 'connected'
