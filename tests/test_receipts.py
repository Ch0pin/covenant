import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from covenant.app import create_app
from test_app import setup, bearer, send, register_delivery, delivery_get


def operator_send(client, rid, headers, text="@alice Please report", mid=None):
    response = client.post(f"/api/rooms/{rid}/messages", headers=headers,
        json={"text": text, "client_message_id": mid or str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def receipts(client, rid, mid):
    response = client.get(f"/api/rooms/{rid}/message-receipts?ids={mid}")
    assert response.status_code == 200, response.text
    return response.json()["receipts"].get(str(mid), [])


def event(client, rid, invitation, mid, stage="acknowledged", **extra):
    return client.post(f"/{rid}/message-receipts", headers=bearer(invitation),
        json={"message_ids": [mid], "stage": stage, **extra})


def test_poll_delivery_ack_and_explicit_reply_are_distinct_idempotent_stages(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    request = operator_send(client, rid, headers)
    mid = request["id"]
    assert receipts(client, rid, mid)[0]["status"] == "queued"
    client.get(f"/{rid}/messages?timeout=0", headers=bearer(bob))
    assert receipts(client, rid, mid)[0]["status"] == "queued"
    client.get(f"/{rid}/messages?timeout=0", headers=bearer(alice))
    assert receipts(client, rid, mid)[0]["status"] == "queued"  # A prepared response is not a receipt.
    assert event(client, rid, alice, mid, "delivered", mode="polling").status_code == 200
    first = receipts(client, rid, mid)[0]
    assert first["status"] == "delivered" and first["delivery_mode"] == "polling"
    assert first["acknowledged_at"] is None
    send(client, rid, alice, "@operator An unrelated reply")
    assert receipts(client, rid, mid)[0]["status"] == "delivered"
    assert event(client, rid, alice, mid).status_code == 200
    acknowledged = receipts(client, rid, mid)[0]
    assert acknowledged["status"] == "acknowledged"
    assert event(client, rid, alice, mid).status_code == 200
    assert receipts(client, rid, mid)[0]["acknowledged_at"] == acknowledged["acknowledged_at"]
    body = {"text": "@operator Complete", "client_message_id": "linked", "reply_to": mid}
    answer = client.post(f"/{rid}/messages", headers=bearer(alice), json=body).json()
    assert client.post(f"/{rid}/messages", headers=bearer(alice), json=body).json() == answer
    done = receipts(client, rid, mid)[0]
    assert done["status"] == "answered" and done["reply_id"] == answer["id"]
    event(client, rid, alice, mid, "delivered", mode="polling")
    assert receipts(client, rid, mid)[0]["status"] == "answered"
    assert client.post(f"/{rid}/messages", headers=bearer(alice), json={**body, "reply_to": None}).status_code == 409


def test_broadcast_snapshots_recipients_and_keeps_independent_status(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    message = operator_send(client, rid, headers, "@everyone @alice Status?")
    client.post(f"/api/rooms/{rid}/agents", headers=headers, json={"handle": "carol"})
    assert event(client, rid, alice, message["id"]).status_code == 200
    rows = receipts(client, rid, message["id"])
    assert [(r["handle"], r["status"]) for r in rows] == [("alice", "acknowledged"), ("bob", "queued")]
    history = client.get(f"/api/rooms/{rid}/messages").json()
    assert history["messages"][0]["receipts"] == rows


def test_background_scans_do_not_claim_delivery_and_codex_ack_does(setup):
    client, _, _, headers, rid, (alice, _) = setup
    binding = register_delivery(client, rid, alice)["delivery"]
    message = operator_send(client, rid, headers)
    assert delivery_get(client, rid, alice, binding).json()["messages"] == [message]
    client.get(f"/{rid}/messages?timeout=0&transport=claude", headers=bearer(alice))
    assert receipts(client, rid, message["id"])[0]["status"] == "queued"
    client.post(f"/{rid}/delivery/ack", headers=bearer(alice), json={
        "connection_id": binding["connection_id"], "cursor": message["id"]})
    row = receipts(client, rid, message["id"])[0]
    assert row["status"] == "delivered" and row["delivery_mode"] == "codex"


def test_errors_are_visible_and_success_clears_them_without_downgrade(setup):
    client, _, _, headers, rid, (alice, _) = setup
    mid = operator_send(client, rid, headers)["id"]
    event(client, rid, alice, mid, "error", mode="codex", error="Session unavailable")
    assert receipts(client, rid, mid)[0]["last_error"] == "Session unavailable"
    assert receipts(client, rid, mid)[0]["status"] == "queued"
    client.post(f"/{rid}/delivery-status", headers=bearer(alice), json={
        "mode": "claude", "state": "fallback", "last_error": "Receipt timeout"})
    row = receipts(client, rid, mid)[0]
    assert row["delivery_state"] == "fallback" and row["delivery_error"] == "Receipt timeout"
    client.get(f"/{rid}/messages?after={mid}&timeout=0", headers=bearer(alice))
    row = receipts(client, rid, mid)[0]
    assert row["last_error"] is None and row["delivery_error"] is None and row["current_mode"] == "polling"
    event(client, rid, alice, mid, "error", error="Late retry")
    assert receipts(client, rid, mid)[0]["status"] == "delivered"
    assert receipts(client, rid, mid)[0]["last_error"] is None


def test_receipts_cannot_cross_room_recipient_or_revocation_boundaries(setup):
    client, _, _, headers, rid, (alice, bob) = setup
    mid = operator_send(client, rid, headers)["id"]
    assert event(client, rid, bob, mid).status_code == 404
    bad_reply = {"text": "@operator fake", "client_message_id": "fake", "reply_to": mid}
    # Other participants may join the discussion, but cannot answer Alice's receipt.
    assert client.post(f"/{rid}/messages", headers=bearer(bob), json=bad_reply).status_code == 200
    assert client.post(f"/{rid}/messages", headers=bearer(alice), json={**bad_reply, "text": "@bob hidden answer"}).status_code == 200
    assert receipts(client, rid, mid)[0]["status"] == "queued"
    other = client.post("/api/rooms", headers=headers, json={"title": "Other"}).json()["id"]
    assert event(client, other, alice, mid).status_code == 401
    client.post(f"/api/rooms/{rid}/agents/{alice['id']}/revoke", headers=headers)
    assert event(client, rid, alice, mid).status_code == 401
    assert receipts(client, rid, mid)[0]["revoked"] == 1
    client.cookies.clear()
    assert client.get(f"/api/rooms/{rid}/message-receipts?ids={mid}").status_code == 401


def test_history_reads_are_not_acknowledgements_and_receipts_survive_restart(setup):
    client, _, settings, headers, rid, (alice, _) = setup
    mid = operator_send(client, rid, headers)["id"]
    client.get(f"/{rid}/history?scope=all", headers=bearer(alice))
    assert receipts(client, rid, mid)[0]["status"] == "queued"
    client.post(f"/{rid}/join", headers=bearer(alice), json={"history_limit": 0})
    assert receipts(client, rid, mid)[0]["status"] == "queued"
    client.post(f"/{rid}/join", headers=bearer(alice), json={})
    assert receipts(client, rid, mid)[0]["status"] == "queued"
    event(client, rid, alice, mid, "delivered", mode="catch_up")
    assert receipts(client, rid, mid)[0]["status"] == "delivered"
    event(client, rid, alice, mid)
    with TestClient(create_app(settings), base_url=settings.public_base_url) as restarted:
        restarted.cookies.update(client.cookies)
        assert receipts(restarted, rid, mid)[0]["status"] == "acknowledged"
    client.patch(f"/api/rooms/{rid}", headers=headers, json={"status": "archived"})
    assert event(client, rid, alice, mid).status_code == 409
    assert client.delete(f"/api/rooms/{rid}", headers=headers).status_code == 200


def test_untracked_historical_messages_stay_unknown_and_migration_preserves_data(setup):
    client, app, _, _, rid, (alice, _) = setup
    with app.state.db.connect() as conn:
        mid = conn.execute("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
            (rid, "operator", "alice", "@alice pre-upgrade", "old", time.time())).lastrowid
        conn.execute("DROP TABLE message_replies")
        conn.execute("DROP TABLE message_receipts")
        conn.execute("DROP TABLE agent_delivery_status")
    app.state.db.initialize()
    app.state.db.initialize()
    assert receipts(client, rid, mid) == []
    assert client.get(f"/{rid}/messages?timeout=0", headers=bearer(alice)).json()["messages"][0]["id"] == mid
    assert receipts(client, rid, mid) == []
