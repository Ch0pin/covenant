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
    assert result["handle"] == "alice" and result["messages"] == []
    history = client.get(f"/{rid}/history?scope=all", headers=bearer(renewed)).json()
    assert history["messages"][0]["sender"] == "alice"


def test_history_pagination_reconnect_and_persistence(setup):
    client, app, settings, headers, rid, (alice, _) = setup
    with app.state.db.connect() as conn:
        conn.executemany("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
                         [(rid, "bob", "everyone", f"@everyone {i}", str(i), time.time()) for i in range(205)])
    page1 = client.get(f"/{rid}/messages?after=0&timeout=0", headers=bearer(alice)).json()
    page2 = client.get(f"/{rid}/messages?after={page1['next_cursor']}&timeout=0", headers=bearer(alice)).json()
    page3 = client.get(f"/{rid}/messages?after={page2['next_cursor']}&timeout=0", headers=bearer(alice)).json()
    assert [len(p["messages"]) for p in (page1, page2, page3)] == [100, 100, 5]
    assert page1["has_more"] and page2["has_more"] and not page3["has_more"]
    joined = client.post(f"/{rid}/join", headers=bearer(alice)).json()
    assert len(joined["messages"]) == 20 and joined["history_truncated"]
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


def test_delete_archived_room_removes_history_and_access_only_for_that_room(setup):
    client, app, settings, headers, rid, (alice, bob) = setup
    send(client, rid, alice)
    other = client.post("/api/rooms", headers=headers, json={"title": "Keep me"}).json()["id"]
    other_agent = client.post(f"/api/rooms/{other}/agents", headers=headers, json={"handle": "carol"}).json()
    send(client, other, other_agent)
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "archived"})
    assert client.delete(f"/api/rooms/{rid}", headers=headers).status_code == 200
    assert client.get(f"/api/rooms/{rid}").status_code == 404
    assert client.get(f"/api/rooms/{rid}/messages").status_code == 404
    assert client.get("/api/rooms?archived=true").json()["rooms"] == []
    assert client.delete(f"/api/rooms/{rid}", headers=headers).status_code == 404
    for invitation in (alice, bob):
        assert client.post(f"/{rid}/join", headers=bearer(invitation)).status_code == 401
        assert client.get(f"/{rid}/messages?timeout=0", headers=bearer(invitation)).status_code == 401
        assert send(client, rid, invitation).status_code == 401
    with app.state.db.connect() as conn:
        for table in ("messages", "agents"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE room_id=?", (rid,)).fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with TestClient(create_app(settings), base_url=settings.public_base_url) as restarted:
        assert restarted.post(f"/{rid}/join", headers=bearer(alice)).status_code == 401
        remaining = restarted.post(f"/{other}/join", headers=bearer(other_agent), json={"history_scope": "all"}).json()
        assert remaining["room"]["title"] == "Keep me"
        assert len(remaining["messages"]) == 1


def test_delete_requires_operator_csrf_and_archived_room(setup):
    client, app, settings, headers, rid, (alice, _) = setup
    for status in ("active", "paused"):
        client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": status})
        assert client.delete(f"/api/rooms/{rid}", headers=headers).status_code == 409
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "archived"})
    assert client.delete(f"/api/rooms/{rid}").status_code == 403
    assert client.delete(f"/api/rooms/{rid}", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    with TestClient(app, base_url=settings.public_base_url) as anonymous:
        assert anonymous.delete(f"/api/rooms/{rid}").status_code == 401
        assert anonymous.delete(f"/api/rooms/{rid}", headers=bearer(alice)).status_code == 401
    assert client.get(f"/api/rooms/{rid}").status_code == 200


def test_operator_notifications_baseline_filtering_all_rooms_and_cursor(setup):
    client, _, _, headers, rid, (alice, _) = setup
    send(client, rid, alice, "@operator Old history")
    baseline = client.get("/api/operator-notifications").json()
    assert baseline["messages"] == []
    cursor = baseline["next_cursor"]
    send(client, rid, alice, "@everyone Broadcast")
    send(client, rid, alice, "@bob Agent message")
    client.post(f"/api/rooms/{rid}/messages", headers=headers, json={
        "text": "@operator Operator's own message", "client_message_id": "self"})
    direct = send(client, rid, alice, "@operator Please help").json()
    other = client.post("/api/rooms", headers=headers, json={"title": "Other chat"}).json()["id"]
    carol = client.post(f"/api/rooms/{other}/agents", headers=headers, json={"handle": "carol"}).json()
    second = send(client, other, carol, "@operator " + "x" * 300).json()
    client.patch(f"/api/rooms/{other}", headers=headers, json={"status": "paused"})
    result = client.get(f"/api/operator-notifications?after={cursor}").json()
    assert [m["id"] for m in result["messages"]] == [direct["id"], second["id"]]
    assert result["messages"][1]["room_id"] == other
    assert result["messages"][1]["room_title"] == "Other chat"
    assert len(result["messages"][1]["text"]) == 240
    assert client.get(f"/api/operator-notifications?after={result['next_cursor']}").json()["messages"] == []
    client.patch(f"/api/rooms/{other}", headers=headers, json={"status": "archived"})
    assert len(client.get(f"/api/operator-notifications?after={cursor}").json()["messages"]) == 1
    assert client.delete(f"/api/rooms/{other}", headers=headers).status_code == 200
    assert len(client.get(f"/api/operator-notifications?after={cursor}").json()["messages"]) == 1


def test_operator_notifications_auth_and_pagination(setup):
    client, app, settings, _, rid, (alice, _) = setup
    with TestClient(app, base_url=settings.public_base_url) as anonymous:
        assert anonymous.get("/api/operator-notifications").status_code == 401
        assert anonymous.get("/api/operator-notifications", headers=bearer(alice)).status_code == 401
    assert client.get("/api/operator-notifications?after=-1").status_code == 422
    with app.state.db.connect() as conn:
        conn.executemany("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
            [(rid, "alice", "operator", "@operator help", str(i), time.time()) for i in range(105)])
    first = client.get("/api/operator-notifications?after=0").json()
    assert len(first["messages"]) == 100 and first["has_more"]
    second = client.get(f"/api/operator-notifications?after={first['next_cursor']}").json()
    assert len(second["messages"]) == 5 and not second["has_more"]
    assert set(m["id"] for m in first["messages"]).isdisjoint(m["id"] for m in second["messages"])


def test_multiple_recipients_are_validated_returned_and_notify_operator(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    sent = send(client, rid, alice, "@bob @operator FYI, **ready**").json()
    assert sent["target"] == "bob"
    assert sent["targets"] == ["bob", "operator"]
    assert client.get(f"/{rid}/messages?after=0&timeout=0", headers=bearer(bob)).json()["messages"][0]["targets"] == ["bob", "operator"]
    alerts = client.get("/api/operator-notifications?after=0").json()["messages"]
    assert [m["id"] for m in alerts] == [sent["id"]]
    assert send(client, rid, alice, "@bob @missing FYI").status_code == 422
    assert send(client, rid, alice, "@bob @operator").status_code == 422
    duplicate = send(client, rid, alice, "@operator @operator FYI").json()
    assert duplicate["targets"] == ["operator"]
    client.post(f"/api/rooms/{rid}/agents/{bob['id']}/revoke", headers=headers)
    assert send(client, rid, alice, "@operator @bob FYI").status_code == 422


def test_notification_matching_ignores_body_mentions_and_advances_past_them(setup):
    client, app, _, _, rid, (alice, _) = setup
    with app.state.db.connect() as conn:
        conn.executemany("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
            [(rid, "alice", "bob", "@bob Quoting @operator in the body", str(i), time.time()) for i in range(100)])
    wanted = send(client, rid, alice, "@bob @operator @everyone Human input needed").json()
    first = client.get("/api/operator-notifications?after=0").json()
    assert first["messages"] == [] and first["has_more"]
    assert first["next_cursor"] > 0
    second = client.get(f"/api/operator-notifications?after={first['next_cursor']}").json()
    assert [m["id"] for m in second["messages"]] == [wanted["id"]]
    assert not second["has_more"]


def register_delivery(client, rid, invitation, session_id=None):
    body = {"session_id": session_id or str(uuid4()), "connection_id": str(uuid4())}
    response = client.post(f"/{rid}/join", headers=bearer(invitation), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def delivery_get(client, rid, invitation, registration, **kwargs):
    return client.get(f"/{rid}/delivery", headers=bearer(invitation), params={
        "connection_id": registration["connection_id"], "timeout": 0, **kwargs})


def test_delivery_registration_filters_targets_and_scopes_cursor(setup):
    client, app, _, headers, rid, (alice, bob) = setup
    old = send(client, rid, bob, "@alice Existing history").json()
    joined = register_delivery(client, rid, alice)
    binding = joined["delivery"]
    assert binding["cursor"] == old["id"]
    assert delivery_get(client, rid, alice, binding).json()["messages"] == []
    direct = send(client, rid, bob, "@operator @alice Please review").json()
    broadcast = send(client, rid, bob, "@everyone Shared update").json()
    send(client, rid, alice, "@everyone Own post")
    irrelevant = send(client, rid, bob, "@operator Only for human").json()
    page = delivery_get(client, rid, alice, binding).json()
    assert page["messages"] == [direct, broadcast]
    assert page["next_cursor"] == irrelevant["id"]
    # Polling alone never acknowledges delivery; the listener queues first.
    repeated = delivery_get(client, rid, alice, binding).json()
    assert repeated["messages"] == page["messages"]
    assert repeated["next_cursor"] == page["next_cursor"]
    ack = {"connection_id": binding["connection_id"], "cursor": page["next_cursor"]}
    assert client.post(f"/{rid}/delivery/ack", headers=bearer(bob), json=ack).status_code == 409
    assert client.post(f"/{rid}/delivery/ack", headers=bearer(alice), json=ack).json()["cursor"] == irrelevant["id"]
    assert delivery_get(client, rid, alice, binding).json()["messages"] == []
    assert client.post(f"/{rid}/delivery/ack", headers=bearer(alice), json={**ack, "cursor": 0}).json()["cursor"] == irrelevant["id"]
    assert client.post(f"/{rid}/delivery/ack", headers=bearer(alice), json={**ack, "cursor": irrelevant["id"] + 1}).status_code == 422
    assert "session_id" not in json.dumps(client.get(f"/api/rooms/{rid}").json())
    assert "session_id" not in json.dumps(client.post(f"/{rid}/join", headers=bearer(bob)).json())


def test_delivery_reconnect_preserves_pending_and_replaces_old_connection(setup):
    client, app, settings, _, rid, (alice, bob) = setup
    old = register_delivery(client, rid, alice)["delivery"]
    pending = send(client, rid, bob, "@alice Catch up after reconnect").json()
    new = register_delivery(client, rid, alice, old["session_id"])["delivery"]
    assert old["connection_id"] != new["connection_id"]
    assert new["cursor"] == old["cursor"]
    assert delivery_get(client, rid, alice, old).status_code == 409
    assert delivery_get(client, rid, alice, new).json()["messages"] == [pending]
    with TestClient(create_app(settings), base_url=settings.public_base_url) as restarted:
        assert delivery_get(restarted, rid, alice, new).json()["messages"] == [pending]
    # An ordinary client may explicitly switch back to polling with its same token.
    plain = client.post(f"/{rid}/join", headers=bearer(alice), json={}).json()
    assert plain["delivery"] == {"mode": "polling"}
    assert pending in plain["messages"]
    assert delivery_get(client, rid, alice, new).status_code == 409
    assert send(client, rid, alice).status_code == 200


@pytest.mark.parametrize("ending", ["revoke", "rotate", "archived", "delete", "replace"])
def test_waiting_delivery_observes_access_lifecycle(setup, ending):
    client, _, _, headers, rid, (alice, _) = setup
    binding = register_delivery(client, rid, alice)["delivery"]
    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(delivery_get, client, rid, alice, binding, timeout=3)
        time.sleep(0.1)
        assert not waiting.done()
        if ending in {"revoke", "rotate"}:
            client.post(f"/api/rooms/{rid}/agents/{alice['id']}/{ending}", headers=headers)
        elif ending == "replace":
            register_delivery(client, rid, alice)
        else:
            client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "archived"})
        response = waiting.result(timeout=2)
    if ending in {"revoke", "rotate"}:
        assert response.status_code == 401
    elif ending == "replace":
        assert response.status_code == 409
    else:
        assert response.json()["room_status"] == "archived"
        assert response.json()["messages"] == []
        assert client.post(f"/{rid}/join", headers=bearer(alice), json={
            "session_id": str(uuid4()), "connection_id": str(uuid4())}).status_code == 409
        if ending == "delete":
            assert client.delete(f"/api/rooms/{rid}", headers=headers).status_code == 200
            assert delivery_get(client, rid, alice, binding).status_code == 401


def test_paused_delivery_keeps_pending_messages_until_resume(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    binding = register_delivery(client, rid, alice)["delivery"]
    pending = send(client, rid, bob, "@alice Pending task").json()
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "paused"})
    paused = delivery_get(client, rid, alice, binding).json()
    assert paused["messages"] == [] and paused["next_cursor"] == binding["cursor"]
    assert client.post(f"/api/rooms/{rid}/messages", headers=headers, json={
        "text": "@alice Human instruction", "client_message_id": "pause-human"}).status_code == 200
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "active"})
    assert delivery_get(client, rid, alice, binding).json()["messages"][0] == pending


@pytest.mark.parametrize("body", [{"session_id": "not-a-uuid"}, {"session_id": str(uuid4())},
                                   {"connection_id": str(uuid4())}, {"provider": "unknown"}])
def test_registration_validation_keeps_existing_access(setup, body):
    client, _, _, _, rid, (alice, _) = setup
    assert client.post(f"/{rid}/join", headers=bearer(alice), json=body).status_code == 422
    assert client.post(f"/{rid}/join", headers=bearer(alice)).status_code == 200


def test_delivery_schema_upgrade_preserves_existing_data(setup):
    client, app, _, _, rid, (alice, bob) = setup
    message = send(client, rid, bob).json()
    with app.state.db.connect() as conn:
        conn.execute("DROP TABLE delivery_bindings")  # Isolated pre-upgrade database.
        before = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
                  for table in ("rooms", "agents", "messages", "operator", "sessions")}
    app.state.db.initialize()
    app.state.db.initialize()  # Migration is repeatable.
    with app.state.db.connect() as conn:
        after = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in before}
    assert before == after
    assert client.get(f"/{rid}/messages?timeout=0", headers=bearer(alice)).json()["messages"] == [message]
