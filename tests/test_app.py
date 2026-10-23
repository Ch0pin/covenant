import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from covenant.app import create_app
from covenant.auth import digest, hash_password
from covenant.config import Settings

PASSWORD = "test-only-password-123"


@pytest.fixture
def setup(tmp_path):
    settings = Settings(database=tmp_path / "test.sqlite3", port=8765,
                        public_base_url="https://chat.example.com")
    app = create_app(settings)
    with TestClient(app, base_url=settings.public_base_url) as client:
        with app.state.db.connect() as conn:
            conn.execute("INSERT INTO operator VALUES (1,?,?)", ("operator", hash_password(PASSWORD)))
        response = client.post("/api/login", json={"username": "operator", "password": PASSWORD})
        assert response.status_code == 200
        headers = {"X-CSRF-Token": response.json()["csrf_token"]}
        rid = client.post("/api/rooms", headers=headers, json={"title": "Test room", "objective": "Review together"}).json()["id"]
        invitations = []
        for handle in ("alice", "bob"):
            response = client.post(f"/api/rooms/{rid}/agents", headers=headers, json={"handle": handle})
            assert response.status_code == 201
            invitations.append(response.json())
        yield client, app, settings, headers, rid, invitations


def bearer(invitation):
    return {"Authorization": "Bearer " + invitation["token"]}


def send(client, rid, invitation, text="@everyone hello", mid=None):
    return client.post(f"/{rid}/messages", headers=bearer(invitation),
                       json={"text": text, "client_message_id": mid or str(uuid4())})


def test_join_send_and_shared_history(setup):
    client, app, settings, headers, rid, (alice, bob) = setup
    joined = client.post(f"/{rid}/join", headers=bearer(alice), json={}).json()
    assert joined["handle"] == "alice"
    assert [p["handle"] for p in joined["participants"]] == ["operator", "alice", "bob"]
    assert joined["room"]["objective"] == "Review together"
    first = send(client, rid, bob, "@alice Please review").json()
    result = client.get(f"/{rid}/messages?after=0&timeout=0", headers=bearer(alice)).json()
    assert result["messages"] == [first]
    assert first["sender"] == "bob" and first["target"] == "alice"
    assert result["next_cursor"] == first["id"]
    assert client.get(f"/api/rooms/{rid}/messages").json()["messages"] == [first]
    assert alice["token"] in alice["instructions"]
    assert f"https://chat.example.com/{rid}/join" in alice["instructions"]


def test_tokens_are_scoped_and_sender_cannot_be_spoofed(setup):
    client, _, _, headers, rid, (alice, _) = setup
    other = client.post("/api/rooms", headers=headers, json={"title": "Other"}).json()["id"]
    assert client.post(f"/{other}/join", headers=bearer(alice)).status_code == 401
    assert client.get(f"/{rid}/messages").status_code == 401
    result = client.post(f"/{rid}/messages", headers=bearer(alice), json={
        "text": "@everyone hello", "client_message_id": "fake", "sender": "operator"})
    assert result.status_code == 422
    assert client.post("/api/rooms", headers=bearer(alice), json={"title": "Unauthorized"}).status_code == 403


def test_idempotency_and_mentions(setup):
    client, _, _, _, rid, (alice, _) = setup
    first = send(client, rid, alice, mid="retry")
    again = send(client, rid, alice, mid="retry")
    assert first.json()["id"] == again.json()["id"]
    assert send(client, rid, alice, "@everyone changed", "retry").status_code == 409
    assert send(client, rid, alice, "@missing hello").status_code == 422
    assert send(client, rid, alice, "hello without a recipient").status_code == 422
    assert send(client, rid, alice, "@operator Need help").status_code == 200


def test_long_poll_wakes_on_message(setup):
    client, _, _, _, rid, (alice, bob) = setup
    start = time.monotonic()
    with ThreadPoolExecutor() as executor:
        future = executor.submit(client.get, f"/{rid}/messages?after=0&timeout=3", headers=bearer(alice))
        time.sleep(0.12)
        assert not future.done()
        message = send(client, rid, bob, "@alice New message").json()
        response = future.result(timeout=2)
    assert response.status_code == 200
    assert response.json()["messages"][0]["id"] == message["id"]
    assert time.monotonic() - start < 2


def test_long_poll_timeout_preserves_cursor(setup):
    client, _, _, _, rid, (alice, _) = setup
    message = send(client, rid, alice).json()
    start = time.monotonic()
    response = client.get(f"/{rid}/messages?after={message['id']}&timeout=0.15", headers=bearer(alice))
    assert time.monotonic() - start >= 0.14
    assert response.json()["messages"] == []
    assert response.json()["next_cursor"] == message["id"]


@pytest.mark.parametrize("change", ["paused", "archived", "revoke", "rotate", "invite"])
def test_waiting_poll_observes_room_and_access_changes(setup, change):
    client, _, _, headers, rid, (alice, _) = setup
    with ThreadPoolExecutor() as executor:
        future = executor.submit(client.get, f"/{rid}/messages?timeout=3", headers=bearer(alice))
        time.sleep(0.1)
        assert not future.done()
        if change in {"paused", "archived"}:
            response = client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": change})
        elif change == "invite":
            response = client.post(f"/api/rooms/{rid}/agents", headers=headers, json={"handle": "charlie"})
        else:
            response = client.post(f"/api/rooms/{rid}/agents/{alice['id']}/{change}", headers=headers)
        assert response.status_code in {200, 201}
        result = future.result(timeout=2)
    if change in {"revoke", "rotate"}:
        assert result.status_code == 401
    elif change == "invite":
        assert "charlie" in [p["handle"] for p in result.json()["participants"]]
    else:
        assert result.json()["room_status"] == change


def test_pause_archive_restore(setup):
    client, _, _, headers, rid, (alice, _) = setup
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "paused"})
    assert send(client, rid, alice).status_code == 409
    assert client.post(f"/api/rooms/{rid}/messages", headers=headers,
                       json={"text": "@everyone Please wait", "client_message_id": "operator-1"}).status_code == 200
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "archived"})
    assert send(client, rid, alice).status_code == 409
    assert client.post(f"/api/rooms/{rid}/messages", headers=headers,
                       json={"text": "@everyone hi", "client_message_id": "operator-2"}).status_code == 409
    assert client.get("/api/rooms").json()["rooms"] == []
    assert client.get("/api/rooms?archived=true").json()["rooms"][0]["id"] == rid
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "active"})
    assert send(client, rid, alice).status_code == 200


def test_rotation_invalidates_old_token_and_preserves_identity(setup):
    client, _, _, headers, rid, (alice, _) = setup
    send(client, rid, alice)
    renewed = client.post(f"/api/rooms/{rid}/agents/{alice['id']}/rotate", headers=headers).json()
    assert client.post(f"/{rid}/join", headers=bearer(alice)).status_code == 401
    result = client.post(f"/{rid}/join", headers=bearer(renewed)).json()
    assert result["handle"] == "alice" and result["messages"][0]["sender"] == "alice"


def test_history_pagination_reconnect_and_persistence(setup):
    client, app, settings, headers, rid, (alice, _) = setup
    with app.state.db.connect() as conn:
        conn.executemany("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
                         [(rid, "alice", "everyone", f"@everyone {i}", str(i), time.time()) for i in range(205)])
    page1 = client.get(f"/{rid}/messages?after=0&timeout=0", headers=bearer(alice)).json()
    page2 = client.get(f"/{rid}/messages?after={page1['next_cursor']}&timeout=0", headers=bearer(alice)).json()
    page3 = client.get(f"/{rid}/messages?after={page2['next_cursor']}&timeout=0", headers=bearer(alice)).json()
    assert [len(p["messages"]) for p in (page1, page2, page3)] == [100, 100, 5]
    assert page1["has_more"] and page2["has_more"] and not page3["has_more"]
    joined = client.post(f"/{rid}/join", headers=bearer(alice)).json()
    assert len(joined["messages"]) == 100 and joined["history_truncated"]
    assert joined["next_cursor"] == page3["next_cursor"]
    older = client.get(f"/api/rooms/{rid}/messages?before={joined['messages'][0]['id']}").json()
    assert len(older["messages"]) == 100 and older["has_more"]
    with TestClient(create_app(settings), base_url=settings.public_base_url) as restarted:
        persisted = restarted.get(f"/{rid}/messages?after={page2['next_cursor']}&timeout=0", headers=bearer(alice))
        assert persisted.json()["messages"] == page3["messages"]


def test_operator_login_csrf_origin_logout_and_cookie(setup):
    client, _, _, headers, rid, _ = setup
    assert client.post("/api/rooms", json={"title": "Missing CSRF"}).status_code == 403
    assert client.post("/api/rooms", headers={**headers, "Origin": "https://evil.example"}, json={"title": "Bad origin"}).status_code == 403
    assert client.post("/api/login", headers={"Origin": "null"}, json={"username": "operator", "password": PASSWORD}).status_code == 403
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/rooms").status_code == 401
    assert client.post("/api/rooms", json={"title": "No login"}).status_code == 401
    login = client.post("/api/login", json={"username": "operator", "password": PASSWORD})
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert client.get("/").headers["content-security-policy"].startswith("default-src 'self'")
    assert client.get("/api/me").headers["cache-control"] == "no-store"


def test_token_and_password_storage_and_redacted_validation(setup):
    client, app, _, _, rid, (alice, bob) = setup
    with app.state.db.connect() as conn:
        row = conn.execute("SELECT token_hash FROM agents WHERE id=?", (alice["id"],)).fetchone()
        assert row["token_hash"] == digest(alice["token"])
        assert conn.execute("SELECT password_hash FROM operator").fetchone()[0].startswith("pbkdf2_sha256$")
    visible = json.dumps(client.get(f"/api/rooms/{rid}").json())
    assert alice["token"] not in visible and "token_hash" not in visible
    assert alice["token"].encode() not in app.state.db.path.read_bytes()
    secret = "x" * 1025
    invalid = client.post("/api/login", json={"username": "operator", "password": secret})
    assert invalid.status_code == 422 and secret not in invalid.text


def test_handles_and_limits(setup):
    client, _, _, headers, rid, (alice, _) = setup
    for handle in ("everyone", "operator", "Everyone", "OPERATOR", "a b", "1agent"):
        assert client.post(f"/api/rooms/{rid}/agents", headers=headers, json={"handle": handle}).status_code == 422
    assert client.post(f"/api/rooms/{rid}/agents", headers=headers, json={"handle": "alice"}).status_code == 409
    assert send(client, rid, alice, "@everyone " + "x" * 16000).status_code == 422
    assert client.get(f"/{rid}/messages?timeout=26", headers=bearer(alice)).status_code == 422
    assert client.get(f"/{rid}/messages?after=-1", headers=bearer(alice)).status_code == 422


def test_login_rate_limit(setup):
    client, _, _, _, _, _ = setup
    for _ in range(9):
        assert client.post("/api/login", json={"username": "operator", "password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"username": "operator", "password": PASSWORD}).status_code == 429


@pytest.mark.parametrize("url", ["https://example.com/path", "http://example.com", "https://user:pass@example.com", "https://example.com?x=1"])
def test_invalid_public_configuration(url):
    with pytest.raises(ValueError):
        Settings(public_base_url=url)


def test_configuration_defaults_and_custom_port():
    assert Settings().port == 6666
    assert Settings(port=8080).port == 8080
    assert Settings(public_base_url="https://example.trycloudflare.com/").public_base_url == "https://example.trycloudflare.com"


def test_mixed_case_handle_matches_form_and_creates_canonical_identity(setup):
    client, _, _, headers, rid, (alice, _) = setup
    html = client.get("/").text
    pattern = re.search(r'name="handle"[^>]*pattern="([^"]+)"', html).group(1)
    assert re.fullmatch(pattern, "Sol"), "The browser form must accept the user's handle"
    response = client.post(f"/api/rooms/{rid}/agents", headers=headers, json={"handle": "Sol"})
    assert response.status_code == 201
    invitation = response.json()
    assert invitation["handle"] == "sol"
    assert "Handle: sol" in invitation["instructions"]
    joined = client.post(f"/{rid}/join", headers=bearer(invitation)).json()
    assert joined["handle"] == "sol"
    assert any(p["handle"] == "sol" for p in joined["participants"])
    assert send(client, rid, alice, "@sol Please review").status_code == 200
    for duplicate in ("sol", "SOL", "sOl"):
        assert client.post(f"/api/rooms/{rid}/agents", headers=headers,
                           json={"handle": duplicate}).status_code == 409
