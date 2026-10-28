"""Operator decisions for native harness requests; isolated from agent chat credentials."""
import json
import secrets
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import digest

LEASE_SECONDS = 30
REQUEST_SECONDS = 600


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NativeRequest(ApprovalInput):
    request_key: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    kind: Literal["command", "file", "tool"]
    details: dict
    can_approve: bool = True

    @model_validator(mode="after")
    def bounded_details(self):
        if len(json.dumps(self.details).encode()) > 60000:
            raise ValueError("Approval details exceed the display limit; answer in the harness")
        return self


class Decision(ApprovalInput):
    decision: Literal["approve", "reject"]


class Finish(ApprovalInput):
    status: Literal["resolved", "cancelled"]


def create_bridge(db, room_id, handle, harness, session_id=None, expected_token_hash=None):
    """Called only by the local operator CLI, never by a room agent HTTP endpoint."""
    token, bridge_id, now = secrets.token_urlsafe(32), str(uuid4()), time.time()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        identity = conn.execute("""SELECT a.* FROM agents a JOIN rooms r ON r.id=a.room_id
            WHERE a.room_id=? AND a.handle=? AND a.revoked=0 AND r.status='active'""",
            (room_id, handle)).fetchone()
        if not identity or (expected_token_hash is not None and identity["token_hash"] != expected_token_hash):
            raise ValueError("Choose an active room and an existing, non-revoked agent handle")
        conn.execute("INSERT INTO approval_bridges VALUES (?,?,?,?,?,?,?)", (
            bridge_id, identity["id"], digest(token), identity["token_hash"], harness,
            session_id, now + 86400))
    return bridge_id, token


def close_bridge(db, bridge_id):
    with db.connect() as conn:
        conn.execute("UPDATE approval_bridges SET expires_at=0 WHERE id=?", (bridge_id,))
        conn.execute("""UPDATE approval_requests SET status='cancelled'
            WHERE bridge_id=? AND status IN ('pending','decided','submitted')""", (bridge_id,))


def prune(conn):
    now = time.time()
    conn.execute("""UPDATE approval_requests SET status='expired'
        WHERE status IN ('pending','decided','submitted') AND (expires_at<=? OR alive_until<=?)""", (now, now))
    conn.execute("""UPDATE approval_requests SET status='cancelled'
        WHERE status IN ('pending','decided','submitted') AND bridge_id IN (
        SELECT b.id FROM approval_bridges b JOIN agents a ON a.id=b.agent_id JOIN rooms r ON r.id=a.room_id
        WHERE b.expires_at<=? OR a.revoked=1 OR a.token_hash!=b.agent_token_hash OR r.status!='active')""", (now,))


def register_approval_routes(app, db, operator):
    def bridge(conn, request):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256:
            raise HTTPException(401, "Approval bridge credential required")
        row = conn.execute("""SELECT b.*,r.status AS room_status FROM approval_bridges b
            JOIN agents a ON a.id=b.agent_id JOIN rooms r ON r.id=a.room_id
            WHERE b.token_hash=? AND b.expires_at>? AND a.revoked=0 AND a.token_hash=b.agent_token_hash""",
            (digest(token), time.time())).fetchone()
        if not row:
            raise HTTPException(401, "Approval bridge access ended")
        linked = conn.execute("SELECT * FROM approval_sessions WHERE agent_id=?", (row["agent_id"],)).fetchone()
        if linked and (linked["harness"] != row["harness"] or (row["session_id"] and linked["session_id"] != row["session_id"])
                       or linked["agent_token_hash"] != row["agent_token_hash"]):
            raise HTTPException(401, "Native session registration changed")
        if row["room_status"] != "active":
            raise HTTPException(409, "Approvals require an active room")
        return row

    @app.get("/api/approvals")
    async def list_approvals(request: Request):
        operator(request)
        with db.connect() as conn:
            prune(conn)
            rows = conn.execute("""SELECT q.*,a.handle,a.room_id,r.title AS room_title,b.harness,b.session_id
                FROM approval_requests q JOIN approval_bridges b ON b.id=q.bridge_id
                JOIN agents a ON a.id=b.agent_id JOIN rooms r ON r.id=a.room_id
                WHERE q.status IN ('pending','decided','submitted') OR q.created_at>?
                ORDER BY CASE WHEN q.status='pending' THEN 0 ELSE 1 END,q.created_at DESC LIMIT 100""",
                (time.time() - 3600,)).fetchall()
            manager = getattr(app.state, "approval_manager", None)
            return {"connections": manager.status() if manager else [], "requests": [{**{k: row[k] for k in (
                "id", "status", "decision", "handle", "room_id", "room_title", "harness", "session_id", "created_at")},
                **json.loads(row["payload"])} for row in rows]}

    @app.post("/api/approvals/{approval_id}/decision")
    async def decide(approval_id: UUID, request: Request, body: Decision):
        username = operator(request)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prune(conn)
            row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (str(approval_id),)).fetchone()
            if not row:
                raise HTTPException(404, "Approval request not found")
            if row["status"] != "pending" or row["alive_until"] <= time.time():
                raise HTTPException(409, "This request has ended or already received a decision")
            if body.decision == "approve" and not json.loads(row["payload"])["can_approve"]:
                raise HTTPException(409, "Review and approve this request in the harness")
            conn.execute("UPDATE approval_requests SET status='decided',decision=?,decided_by=?,decided_at=? WHERE id=?",
                         (body.decision, username, time.time(), str(approval_id)))
        return {"status": "decided", "decision": body.decision}

    @app.post("/approval-bridge/requests")
    async def submit(request: Request, body: NativeRequest):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = bridge(conn, request)
            linked = conn.execute("SELECT session_id FROM approval_sessions WHERE agent_id=?", (owner["agent_id"],)).fetchone()
            if linked and linked["session_id"] != body.session_id:
                raise HTTPException(409, "Native session registration changed")
            prune(conn)
            if owner["session_id"] and owner["session_id"] != body.session_id:
                raise HTTPException(409, "Harness session differs from the registered session")
            if not owner["session_id"]:
                conn.execute("UPDATE approval_bridges SET session_id=? WHERE id=?", (body.session_id, owner["id"]))
            payload = json.dumps(body.model_dump(exclude={"request_key", "session_id"}), sort_keys=True)
            old = conn.execute("SELECT * FROM approval_requests WHERE bridge_id=? AND request_key=?",
                               (owner["id"], body.request_key)).fetchone()
            if old:
                if old["payload"] != payload:
                    raise HTTPException(409, "Request content changed; create a new request")
                return {"id": old["id"]}
            pending = conn.execute("SELECT COUNT(*) FROM approval_requests WHERE bridge_id=? AND status IN ('pending','decided','submitted')",
                                   (owner["id"],)).fetchone()[0]
            if pending >= 50:
                raise HTTPException(429, "Too many outstanding approval requests")
            aid, now = str(uuid4()), time.time()
            conn.execute("""INSERT INTO approval_requests
                (id,bridge_id,request_key,payload,created_at,expires_at,alive_until) VALUES (?,?,?,?,?,?,?)""",
                (aid, owner["id"], body.request_key, payload, now, now + REQUEST_SECONDS, now + LEASE_SECONDS))
            return {"id": aid}

    @app.post("/approval-bridge/requests/{approval_id}/take")
    async def take(approval_id: UUID, request: Request):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = bridge(conn, request)
            prune(conn)
            row = conn.execute("SELECT * FROM approval_requests WHERE id=? AND bridge_id=?",
                               (str(approval_id), owner["id"])).fetchone()
            if not row:
                raise HTTPException(404, "Approval request not found")
            if row["status"] in {"pending", "decided", "submitted"}:
                conn.execute("UPDATE approval_requests SET alive_until=? WHERE id=?", (time.time() + LEASE_SECONDS, str(approval_id)))
            if row["status"] == "decided":
                # A decision is consumed once. Lost response never becomes an implicit approval.
                conn.execute("UPDATE approval_requests SET status='submitted' WHERE id=?", (str(approval_id),))
                return {"status": "decided", "decision": row["decision"]}
            return {"status": row["status"]}

    @app.post("/approval-bridge/requests/{approval_id}/finish")
    async def finish(approval_id: UUID, request: Request, body: Finish):
        with db.connect() as conn:
            owner = bridge(conn, request)
            conn.execute("UPDATE approval_requests SET status=? WHERE id=? AND bridge_id=? AND status IN ('pending','decided','submitted')",
                         (body.status, str(approval_id), owner["id"]))
        return {"ok": True}
