import pytest
from fastapi.testclient import TestClient
from covenant.app import create_app
from test_app import setup, bearer


@pytest.mark.parametrize('choice', ['raven', 'moon', 'key', 'eye', 'flame'])
def test_avatar_selection_persists_and_can_reset(setup, choice):
    client, _, settings, _, rid, (alice, bob) = setup
    initial = client.post(f'/{rid}/join', headers=bearer(alice), json={}).json()
    assert initial['avatar_id'] == 'initials'
    selected = client.post(f'/{rid}/join', headers=bearer(alice), json={'avatar_id': choice}).json()
    assert selected['avatar_id'] == choice and selected['revision'] > initial['revision']
    assert next(a for a in selected['participants'] if a['handle']=='alice')['avatar_id'] == choice
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={}).json()['avatar_id'] == choice
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={'avatar_id': choice}).json()['revision'] == selected['revision']
    room = client.get(f'/api/rooms/{rid}').json()
    assert next(a for a in room['agents'] if a['handle']=='alice')['avatar_id'] == choice
    assert next(a for a in room['agents'] if a['handle']=='bob')['avatar_id'] == 'initials'
    with TestClient(create_app(settings), base_url=settings.public_base_url) as restarted:
        assert restarted.post(f'/{rid}/join', headers=bearer(alice), json={}).json()['avatar_id'] == choice
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={'avatar_id':'initials'}).json()['avatar_id'] == 'initials'


def test_avatar_selection_is_validated_and_scoped(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    for value in ['https://example.com/avatar.png', '<svg onload=alert(1)>', 'unknown', '__proto__']:
        assert client.post(f'/{rid}/join', headers=bearer(alice), json={'avatar_id':value}).status_code == 422
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={'avatar_id':'eye','handle':'bob'}).status_code == 422
    client.post(f'/{rid}/join', headers=bearer(alice), json={'avatar_id':'eye'})
    # Shared cosmetic selections are allowed; handles still distinguish agents.
    assert client.post(f'/{rid}/join', headers=bearer(bob), json={'avatar_id':'eye'}).status_code == 200
    other = client.post('/api/rooms', headers=headers, json={'title':'Other'}).json()['id']
    assert client.post(f'/{other}/join', headers=bearer(alice), json={'avatar_id':'moon'}).status_code == 401
    client.post(f'/api/rooms/{rid}/agents/{alice["id"]}/revoke', headers=headers)
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={'avatar_id':'moon'}).status_code == 401
    client.patch(f'/api/rooms/{rid}', headers=headers, json={'status':'archived'})
    assert client.post(f'/{rid}/join', headers=bearer(bob), json={'avatar_id':'moon'}).status_code == 409
    assert client.post(f'/{rid}/join', headers=bearer(bob), json={}).json()['avatar_id'] == 'eye'
    assert client.delete(f'/api/rooms/{rid}', headers=headers).status_code == 200


def test_avatar_schema_upgrade_preserves_existing_agents(setup):
    client, app, _, _, rid, (alice, _) = setup
    with app.state.db.connect() as conn:
        before = [tuple(r) for r in conn.execute('SELECT * FROM agents')]
        conn.execute('DROP TABLE agent_avatars')
    app.state.db.initialize()
    app.state.db.initialize()
    with app.state.db.connect() as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM agents')] == before
    assert client.post(f'/{rid}/join', headers=bearer(alice), json={}).json()['avatar_id'] == 'initials'
