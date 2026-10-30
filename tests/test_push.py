import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from test_app import setup
from test_approvals import approvals, submit
from covenant.push import PushManager, Subscription, b64, ensure_keys, send_push


def subscription(endpoint="https://fcm.googleapis.com/fcm/send/test"):
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": b64(key), "auth": b64(b"x" * 16)}, "expirationTime": None}


@pytest.fixture
def push(approvals):
    client, app, csrf, *_ = approvals
    client.portal.call(app.state.push_manager.stop)
    sent = []
    def sender(*args):
        sent.append(args)
        return 201
    manager = PushManager(app.state.db, app.state.settings, sender)
    manager.keys = ensure_keys(app.state.db)
    sub = subscription()
    assert client.post("/api/push/subscriptions", headers=csrf, json=sub).status_code == 200
    return approvals, manager, sent, sub


def test_opt_in_authenticated_csrf_protected_and_no_secrets(setup):
    client, app, settings, csrf, *_ = setup
    config = client.get("/api/push/config").json()
    assert set(config) == {"public_key"}
    assert config["public_key"] == ensure_keys(app.state.db)["public_key"]
    sub = subscription()
    assert client.post("/api/push/subscriptions", json=sub).status_code == 403
    assert client.post("/api/push/subscriptions", headers={**csrf, "Origin": "https://evil.example"}, json=sub).status_code == 403
    assert client.post("/api/push/subscriptions", headers=csrf, json=sub).status_code == 200
    assert client.post("/api/push/subscriptions", headers=csrf, json=sub).status_code == 200
    assert client.get("/service-worker.js").headers["Service-Worker-Allowed"] == "/"
    with app.state.db.connect() as c:
        assert c.execute("select count(*) from push_subscriptions").fetchone()[0] == 1
    client.post("/api/logout", headers=csrf)
    with app.state.db.connect() as c:
        assert c.execute("select count(*) from push_subscriptions").fetchone()[0] == 0
    assert client.get("/api/push/config").status_code == 401


@pytest.mark.parametrize("endpoint", ["http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://fcm.googleapis.com.evil.test/x", "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x", "https://evil.test/x"])
def test_push_endpoints_cannot_target_arbitrary_servers(endpoint):
    with pytest.raises(ValueError): Subscription(**subscription(endpoint))


def test_push_pending_expired_deduplicated_and_restart_safe(push):
    approvals, manager, sent, sub = push
    aid = submit(approvals)
    asyncio.run(manager.tick()); asyncio.run(manager.tick())
    assert len(sent) == 1 and sent[0][1]["kind"] == "pending"
    assert "printf" not in json.dumps(sent) and "session_hash" not in json.dumps(sent)
    app = approvals[1]
    # A fresh manager retains the same keys and durable event receipts.
    restarted = PushManager(app.state.db, app.state.settings, manager.sender)
    restarted.keys = ensure_keys(app.state.db)
    assert restarted.keys == manager.keys
    asyncio.run(restarted.tick()); assert len(sent) == 1
    with app.state.db.connect() as c:
        c.execute("UPDATE approval_requests SET expires_at=0 WHERE id=?", (aid,))
    asyncio.run(restarted.tick()); asyncio.run(restarted.tick())
    assert [args[1]["kind"] for args in sent] == ["pending", "expired"]


def test_push_skips_decided_requests_and_dead_sessions(push):
    approvals, manager, sent, sub = push
    client, app, csrf, *_ = approvals
    aid = submit(approvals)
    client.post(f"/api/approvals/{aid}/decision", headers=csrf, json={"decision": "reject"})
    asyncio.run(manager.tick()); assert not sent
    submit(approvals, {**approvals[-1], "request_key": "second"})
    with app.state.db.connect() as c: c.execute("UPDATE sessions SET expires_at=0")
    asyncio.run(manager.tick()); assert not sent


def test_push_transient_retry_then_gone_removes_subscription(push):
    approvals, manager, sent, sub = push
    submit(approvals)
    manager.sender = lambda *args: 503
    asyncio.run(manager.tick())
    with approvals[1].state.db.connect() as c:
        row = c.execute("SELECT * FROM push_deliveries").fetchone()
        assert row["attempts"] == 1 and row["sent_at"] is None
        assert row["next_attempt"] > time.time()
        c.execute("UPDATE push_deliveries SET next_attempt=0")
    manager.sender = lambda *args: 410
    asyncio.run(manager.tick())
    with approvals[1].state.db.connect() as c:
        assert c.execute("select count(*) from push_subscriptions").fetchone()[0] == 0


def test_unsubscribe_and_changed_origin_stop_sends(push):
    approvals, manager, sent, sub = push
    client, app, csrf, *_ = approvals
    submit(approvals)
    manager.settings = SimpleNamespace(public_base_url="https://new.example.com")
    asyncio.run(manager.tick()); assert not sent
    client.post("/api/push/unsubscribe", headers=csrf, json={"endpoint": sub["endpoint"]})
    manager.settings = app.state.settings
    asyncio.run(manager.tick()); assert not sent


def test_real_push_encryption_vapid_and_redirect_policy(setup, monkeypatch):
    from requests import Response
    client, app, *_ = setup
    observed = []
    def request(self, method, url, **kwargs):
        observed.append((url, kwargs))
        response = Response(); response.status_code = 201
        return response
    monkeypatch.setattr("requests.Session.request", request)
    keys = ensure_keys(app.state.db)
    assert send_push(subscription(), {"secret": "never plaintext"}, keys["private_key"], "https://chat.example.com", 120) == 201
    url, kwargs = observed[0]
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 5
    assert b"never plaintext" not in kwargs["data"]
    assert kwargs["headers"]["authorization"].startswith("vapid ")
