"""Opt-in operator Web Push. Durable, bounded delivery independent of chat/approvals."""
import asyncio
import base64
import json
import logging
import time
from contextlib import suppress
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import digest
from .approvals import prune

log = logging.getLogger(__name__)


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def endpoint_allowed(endpoint):
    try:
        u = urlsplit(endpoint)
        host = u.hostname or ""
        trusted = (host == "fcm.googleapis.com" or host == "updates.push.services.mozilla.com"
                   or host.endswith(".push.services.mozilla.com") or host == "web.push.apple.com"
                   or host.endswith(".notify.windows.com"))
        return bool(trusted and u.scheme == "https" and u.port in (None, 443)
                    and not u.username and not u.password and not u.fragment and u.path.startswith("/"))
    except ValueError:
        return False


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=4096)
    keys: dict[str, str]
    expirationTime: float | None = None

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        if not endpoint_allowed(value):
            raise ValueError("Unsupported push provider; use Chrome, Firefox, Safari, or Edge")
        return value

    @field_validator("keys")
    @classmethod
    def valid_keys(cls, value):
        if set(value) != {"p256dh", "auth"}:
            raise ValueError("Invalid push keys")
        try:
            key = base64.urlsafe_b64decode(value["p256dh"] + "=" * (-len(value["p256dh"]) % 4))
            auth = base64.urlsafe_b64decode(value["auth"] + "=" * (-len(value["auth"]) % 4))
            if len(key) != 65 or len(auth) != 16:
                raise ValueError()
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)
        except (ValueError, TypeError):
            raise ValueError("Invalid push keys") from None
        return value


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=4096)


def ensure_keys(db):
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM push_keys WHERE id=1").fetchone()
        if row:
            return dict(row)
        key = ec.generate_private_key(ec.SECP256R1())
        private = b64(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        public = b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        conn.execute("INSERT INTO push_keys VALUES (1,?,?)", (private, public))
        return {"private_key": private, "public_key": public}


def send_push(subscription, data, private_key, subject, ttl):
    # Fixed trusted providers, no redirect following, no proxy credentials or payload logging.
    import requests
    from pywebpush import webpush, WebPushException

    class NoRedirectSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(*args, **kwargs)

    with NoRedirectSession() as session:
        session.trust_env = False
        try:
            response = webpush(subscription_info=subscription, data=json.dumps(data),
                               vapid_private_key=private_key, vapid_claims={"sub": subject},
                               ttl=ttl, timeout=5, requests_session=session,
                               headers={"Urgency": "high"})
            return response.status_code
        except WebPushException as exc:
            return exc.response.status_code if exc.response is not None else 503


class PushManager:
    def __init__(self, db, settings, sender=send_push):
        self.db, self.settings, self.sender = db, settings, sender
        self.keys = None
        self.task = None

    async def start(self):
        self.keys = ensure_keys(self.db)
        self.task = asyncio.create_task(self.run(), name="covenant-web-push")

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                # Push failure must never break native approvals or reveal subscription secrets.
                log.warning("Approval push delivery unavailable; retrying.")
            await asyncio.sleep(2)

    async def tick(self):
        now = time.time()
        with self.db.connect() as conn:
            prune(conn)
            conn.execute("DELETE FROM push_subscriptions WHERE session_hash NOT IN (SELECT token_hash FROM sessions WHERE expires_at>?)", (now,))
            subscriptions = conn.execute("SELECT * FROM push_subscriptions WHERE origin=?", (self.settings.public_base_url,)).fetchall()
            for sub in subscriptions:
                # Pending requests may predate opt-in. Expiry alerts are only for recent subscriptions.
                rows = conn.execute("""SELECT q.id,q.status FROM approval_requests q
                    JOIN approval_bridges b ON b.id=q.bridge_id JOIN agents a ON a.id=b.agent_id
                    JOIN rooms r ON r.id=a.room_id LEFT JOIN approval_outcomes o ON o.request_id=q.id
                    WHERE (q.status='pending' AND q.expires_at>? AND q.alive_until>?)
                    OR (q.status='expired' AND (q.created_at>=? OR EXISTS (
                        SELECT 1 FROM push_deliveries d WHERE d.request_id=q.id AND d.endpoint=? AND d.kind='pending'
                    )) AND o.dismissed_at IS NULL)""",
                    (now, now, sub["created_at"], sub["endpoint"])).fetchall()
                conn.executemany("INSERT OR IGNORE INTO push_deliveries(endpoint,request_id,kind) VALUES (?,?,?)",
                                 [(sub["endpoint"], r["id"], r["status"]) for r in rows])
            jobs = conn.execute("""SELECT d.endpoint,d.request_id,d.kind FROM push_deliveries d
                JOIN push_subscriptions s ON s.endpoint=d.endpoint
                WHERE d.sent_at IS NULL AND d.next_attempt<=? AND d.attempts<5 AND s.origin=?
                ORDER BY d.next_attempt LIMIT 20""", (now, self.settings.public_base_url)).fetchall()
        for job in jobs:
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                prune(conn)
                row = conn.execute("""SELECT q.*,s.subscription,s.session_hash,a.room_id,o.dismissed_at
                    FROM approval_requests q JOIN approval_bridges b ON b.id=q.bridge_id
                    JOIN agents a ON a.id=b.agent_id JOIN push_subscriptions s ON s.endpoint=?
                    JOIN sessions login ON login.token_hash=s.session_hash
                    LEFT JOIN approval_outcomes o ON o.request_id=q.id
                    WHERE q.id=? AND login.expires_at>? AND s.origin=?""",
                    (job["endpoint"], job["request_id"], time.time(), self.settings.public_base_url)).fetchone()
                valid = row and row["status"] == job["kind"] and (job["kind"] != "expired" or row["dismissed_at"] is None)
                if not valid:
                    conn.execute("DELETE FROM push_deliveries WHERE endpoint=? AND request_id=? AND kind=?",
                                 tuple(job))
                    continue
                claimed = conn.execute("""UPDATE push_deliveries SET next_attempt=?,attempts=attempts+1
                    WHERE endpoint=? AND request_id=? AND kind=? AND sent_at IS NULL AND next_attempt<=? AND attempts<5""",
                    (time.time() + 60, *tuple(job), time.time())).rowcount
                if not claimed:
                    continue
                data = {"kind": job["kind"], "approval_id": row["id"], "room_id": row["room_id"],
                        "expires_at": row["expires_at"], "url": f"/{row['room_id']}?approval={row['id']}"}
                ttl = max(1, min(600, int(row["expires_at"] - time.time()))) if job["kind"] == "pending" else 600
                subscription = json.loads(row["subscription"])
            if not endpoint_allowed(subscription["endpoint"]):
                code = 410
            else:
                try:
                    code = await asyncio.to_thread(self.sender, subscription, data, self.keys["private_key"],
                                                   self.settings.public_base_url, ttl)
                except Exception:
                    code = 503
            with self.db.connect() as conn:
                if code in (404, 410):
                    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (job["endpoint"],))
                elif 200 <= code < 300:
                    conn.execute("UPDATE push_deliveries SET sent_at=? WHERE endpoint=? AND request_id=? AND kind=?",
                                 (time.time(), *tuple(job)))
                else:
                    log.warning("Approval push provider did not accept a notification; retry scheduled.")


def register_push_routes(app, db, operator):
    @app.get("/api/push/config")
    async def config(request: Request):
        operator(request)
        manager = getattr(app.state, "push_manager", None)
        if not manager or not manager.keys:
            raise HTTPException(503, "Push notifications are unavailable")
        return {"public_key": manager.keys["public_key"]}

    @app.post("/api/push/subscriptions")
    async def subscribe(request: Request, body: Subscription):
        operator(request)
        origin = str(request.base_url).rstrip("/")
        # The configured HTTPS origin is the stable identity of this installation.
        if origin != app.state.settings.public_base_url:
            raise HTTPException(409, "Open the configured public Covenant address to enable push")
        with db.connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] >= 32 and not conn.execute(
                    "SELECT 1 FROM push_subscriptions WHERE endpoint=?", (body.endpoint,)).fetchone():
                raise HTTPException(409, "Push device limit reached")
            conn.execute("""INSERT INTO push_subscriptions VALUES (?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET
                subscription=excluded.subscription,session_hash=excluded.session_hash,origin=excluded.origin""",
                (body.endpoint, body.model_dump_json(), digest(request.cookies.get("covenant_session", "")), origin, time.time()))
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    async def unsubscribe(request: Request, body: Endpoint):
        operator(request)
        with db.connect() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND session_hash=?",
                         (body.endpoint, digest(request.cookies.get("covenant_session", ""))))
        return {"ok": True}
