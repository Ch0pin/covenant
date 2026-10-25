import asyncio
import hmac
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import csrf_token, digest, hash_password, verify_password
from .config import Settings
from .db import Database
from .instructions import connection_instructions

STATIC = Path(__file__).parent / "static"
COOKIE = "covenant_session"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class NewRoom(Input):
    title: str = Field(min_length=1, max_length=100)
    objective: str = Field(default="", max_length=4000)


class RoomStatus(Input):
    status: Literal["active", "paused", "archived"]


class NewAgent(Input):
    handle: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")

    @field_validator("handle", mode="before")
    @classmethod
    def normalize_handle(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class JoinRequest(Input):
    session_id: UUID | None = None
    connection_id: UUID | None = None

    @model_validator(mode="after")
    def paired_registration(self):
        if (self.session_id is None) != (self.connection_id is None):
            raise ValueError("Supply both session_id and connection_id for Codex delivery")
        return self


class DeliveryAck(Input):
    connection_id: UUID
    cursor: int = Field(ge=0, le=2**63 - 1)


class NewMessage(Input):
    client_message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=16000)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    db = Database(settings.database)
    conditions = defaultdict(asyncio.Condition)
    login_attempts = defaultdict(deque)
    dummy_hash = hash_password(secrets.token_urlsafe(24))

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        yield

    app = FastAPI(title="Covenant", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.db = db
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
        "127.0.0.1", "localhost", urlsplit(settings.public_base_url).hostname])

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic errors can contain submitted inputs, including passwords.
        return JSONResponse({"detail": "Invalid request", "errors": [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
            for e in exc.errors()]}, status_code=422)

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in settings.allowed_origins:
                return JSONResponse({"detail": "Untrusted request origin"}, status_code=403)
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 100_000:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'")
        return response

    def operator(request):
        token = request.cookies.get(COOKIE, "")
        with db.connect() as conn:
            session = conn.execute("SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?",
                                   (digest(token), time.time())).fetchone()
            account = conn.execute("SELECT username FROM operator WHERE id=1").fetchone()
        if not session or not account:
            raise HTTPException(401, "Log in to continue")
        if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), csrf_token(token)):
            raise HTTPException(403, "Invalid CSRF token; refresh and try again")
        return account["username"]

    def room(conn, room_id):
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Conversation not found")
        return dict(row)

    def agent(conn, request, room_id):
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 256:
            raise HTTPException(401, "A valid agent bearer token is required")
        row = conn.execute("SELECT * FROM agents WHERE token_hash=? AND room_id=? AND revoked=0",
                           (digest(token), room_id)).fetchone()
        if not row:
            raise HTTPException(401, "Agent token is invalid or revoked")
        return dict(row)

    def participants(conn, room_id, include_revoked=False):
        query = "SELECT id, handle, joined_at, last_seen, revoked FROM agents WHERE room_id=?"
        if not include_revoked:
            query += " AND revoked=0"
        return [dict(r) for r in conn.execute(query + " ORDER BY created_at", (room_id,))]

    def bump(conn, room_id):
        conn.execute("UPDATE rooms SET revision=revision+1, updated_at=? WHERE id=?",
                     (time.time(), room_id))

    async def notify(room_id):
        async with conditions[room_id]:
            conditions[room_id].notify_all()

    def message_targets(text):
        targets, remaining = [], text
        while match := re.match(r"^@([a-z][a-z0-9_]{0,31})(?=\s|$)", remaining):
            if match.group(1) not in targets:
                targets.append(match.group(1))
            remaining = remaining[match.end():].lstrip()
        return targets, remaining

    def message_dict(row):
        result = {key: row[key] for key in ("id", "sender", "target", "text", "created_at")}
        result["targets"] = message_targets(row["text"])[0]
        return result

    def envelope(conn, room_id, after=0, before=None):
        current = room(conn, room_id)
        if before is not None:
            rows = list(conn.execute("SELECT * FROM messages WHERE room_id=? AND id<? ORDER BY id DESC LIMIT 101",
                                     (room_id, before)))
            more = len(rows) > 100
            rows = list(reversed(rows[:100]))
        else:
            rows = list(conn.execute("SELECT * FROM messages WHERE room_id=? AND id>? ORDER BY id LIMIT 101",
                                     (room_id, after)))
            more = len(rows) > 100
            rows = rows[:100]
        return {"room": current, "room_status": current["status"],
                "messages": [message_dict(r) for r in rows],
                "next_cursor": rows[-1]["id"] if rows else after,
                "has_more": more, "revision": current["revision"],
                "participants": [{"handle": "operator", "kind": "operator"}] + participants(conn, room_id)}

    def insert_message(conn, room_id, sender, body, is_operator=False):
        current = room(conn, room_id)
        old = conn.execute("SELECT * FROM messages WHERE room_id=? AND sender=? AND client_message_id=?",
                           (room_id, sender, body.client_message_id)).fetchone()
        if old:
            if old["text"] != body.text:
                raise HTTPException(409, "That client_message_id was already used for different text")
            return message_dict(old)
        if current["status"] == "archived" or (current["status"] == "paused" and not is_operator):
            raise HTTPException(409, f"Conversation is {current['status']}")
        targets, message_body = message_targets(body.text)
        if not targets or not message_body:
            raise HTTPException(422, "Start with one or more @handles, @operator, or @everyone followed by your message")
        for recipient in targets:
            if recipient not in {"operator", "everyone"} and not conn.execute(
                    "SELECT 1 FROM agents WHERE room_id=? AND handle=? AND revoked=0",
                    (room_id, recipient)).fetchone():
                raise HTTPException(422, f"Unknown or revoked participant: @{recipient}")
        target = targets[0]  # Preserve the original field for existing clients.
        cursor = conn.execute("INSERT INTO messages(room_id,sender,target,text,client_message_id,created_at) VALUES (?,?,?,?,?,?)",
                              (room_id, sender, target, body.text, body.client_message_id, time.time()))
        conn.execute("UPDATE rooms SET updated_at=? WHERE id=?", (time.time(), room_id))
        return message_dict(conn.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone())

    @app.get("/api/status")
    async def status():
        with db.connect() as conn:
            configured = bool(conn.execute("SELECT 1 FROM operator").fetchone())
        return {"configured": configured}

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        now = time.time()
        ip = request.client.host if request.client else "unknown"
        # Bound memory and throttle expensive password verification before running it.
        if len(login_attempts) > 2048:
            for key in list(login_attempts):
                if not login_attempts[key] or login_attempts[key][-1] < now - 60:
                    del login_attempts[key]
            if len(login_attempts) > 2048:
                raise HTTPException(429, "Please try again in 60 seconds", headers={"Retry-After": "60"})
        attempts = login_attempts[ip]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "Please try again in 60 seconds", headers={"Retry-After": "60"})
        attempts.append(now)
        with db.connect() as conn:
            account = conn.execute("SELECT * FROM operator WHERE id=1").fetchone()
        verified = await asyncio.to_thread(verify_password, body.password,
                                           account["password_hash"] if account else dummy_hash)
        if not account:
            raise HTTPException(503, "Create the operator account with covenant init first")
        if not verified or not hmac.compare_digest(body.username.encode(), account["username"].encode()):
            raise HTTPException(401, "Incorrect username or password")
        token = secrets.token_urlsafe(32)
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            conn.execute("INSERT INTO sessions VALUES (?,?)", (digest(token), now + 43200))
        response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https",
                            samesite="strict", max_age=43200)
        return {"username": account["username"], "csrf_token": csrf_token(token)}

    @app.get("/api/me")
    async def me(request: Request):
        username = operator(request)
        return {"username": username, "csrf_token": csrf_token(request.cookies[COOKIE]),
                "public_base_url": settings.public_base_url}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        operator(request)
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies[COOKIE]),))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/operator-notifications")
    async def operator_notifications(request: Request, after: int | None = Query(None, ge=0)):
        operator(request)
        with db.connect() as conn:
            # Establish a baseline on enable/login so old history never generates alerts.
            if after is None:
                cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
                return {"messages": [], "next_cursor": cursor, "has_more": False}
            rows = list(conn.execute("""SELECT m.id, m.room_id, m.sender,
                m.text, r.title AS room_title
                FROM messages m JOIN rooms r ON r.id=m.room_id
                WHERE m.id>? AND m.text LIKE '%@operator%' AND m.sender!='operator'
                AND r.status!='archived' ORDER BY m.id LIMIT 101""", (after,)))
            messages = []
            for row in rows[:100]:
                if "operator" in message_targets(row["text"])[0]:
                    message = dict(row)
                    message["text"] = message["text"][:240]
                    messages.append(message)
            return {"messages": messages,
                    "next_cursor": rows[min(len(rows), 100) - 1]["id"] if rows else after,
                    "has_more": len(rows) > 100}

    @app.get("/api/rooms")
    async def list_rooms(request: Request, archived: bool = False):
        operator(request)
        with db.connect() as conn:
            rows = conn.execute("""SELECT r.*, (SELECT text FROM messages WHERE room_id=r.id ORDER BY id DESC LIMIT 1) AS preview,
                (SELECT COUNT(*) FROM agents WHERE room_id=r.id AND revoked=0) AS agent_count
                FROM rooms r WHERE (status='archived')=? ORDER BY updated_at DESC""", (archived,))
            return {"rooms": [dict(r) for r in rows]}

    @app.post("/api/rooms", status_code=201)
    async def new_room(body: NewRoom, request: Request):
        operator(request)
        room_id = str(uuid4())
        now = time.time()
        with db.connect() as conn:
            conn.execute("INSERT INTO rooms(id,title,objective,created_at,updated_at) VALUES (?,?,?,?,?)",
                         (room_id, body.title, body.objective, now, now))
            return room(conn, room_id)

    @app.get("/api/rooms/{room_id}")
    async def get_room(room_id: UUID, request: Request):
        operator(request)
        with db.connect() as conn:
            result = room(conn, str(room_id))
            result["agents"] = participants(conn, str(room_id), True)
            return result

    @app.patch("/api/rooms/{room_id}")
    async def set_status(room_id: UUID, body: RoomStatus, request: Request):
        operator(request)
        rid = str(room_id)
        with db.connect() as conn:
            room(conn, rid)
            conn.execute("UPDATE rooms SET status=? WHERE id=?", (body.status, rid))
            bump(conn, rid)
            result = room(conn, rid)
        await notify(rid)
        return result

    @app.delete("/api/rooms/{room_id}")
    async def delete_room(room_id: UUID, request: Request):
        operator(request)
        rid = str(room_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if room(conn, rid)["status"] != "archived":
                raise HTTPException(409, "Archive this conversation before deleting it")
            conn.execute("DELETE FROM messages WHERE room_id=?", (rid,))
            conn.execute("DELETE FROM agents WHERE room_id=?", (rid,))
            conn.execute("DELETE FROM rooms WHERE id=?", (rid,))
        await notify(rid)
        return {"ok": True}

    @app.post("/api/rooms/{room_id}/agents", status_code=201)
    async def add_agent(room_id: UUID, body: NewAgent, request: Request):
        operator(request)
        if body.handle in {"operator", "everyone"}:
            raise HTTPException(422, "That handle is reserved")
        rid, token, aid = str(room_id), secrets.token_urlsafe(32), str(uuid4())
        with db.connect() as conn:
            if room(conn, rid)["status"] == "archived":
                raise HTTPException(409, "Restore this conversation before adding agents")
            try:
                conn.execute("INSERT INTO agents(id,room_id,handle,token_hash,created_at) VALUES (?,?,?,?,?)",
                             (aid, rid, body.handle, digest(token), time.time()))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "This handle already exists in the conversation")
            bump(conn, rid)
        await notify(rid)
        return {"id": aid, "handle": body.handle, "token": token,
                "room_url": f"{settings.public_base_url}/{rid}",
                "instructions": connection_instructions(settings.public_base_url, rid, body.handle, token)}

    @app.post("/api/rooms/{room_id}/agents/{agent_id}/revoke")
    async def revoke_agent(room_id: UUID, agent_id: UUID, request: Request):
        operator(request)
        rid = str(room_id)
        with db.connect() as conn:
            room(conn, rid)
            if not conn.execute("UPDATE agents SET revoked=1 WHERE id=? AND room_id=?",
                                (str(agent_id), rid)).rowcount:
                raise HTTPException(404, "Agent not found")
            bump(conn, rid)
        await notify(rid)
        return {"ok": True}

    @app.post("/api/rooms/{room_id}/agents/{agent_id}/rotate")
    async def rotate_token(room_id: UUID, agent_id: UUID, request: Request):
        operator(request)
        rid, token = str(room_id), secrets.token_urlsafe(32)
        with db.connect() as conn:
            if room(conn, rid)["status"] == "archived":
                raise HTTPException(409, "Restore this conversation before renewing access")
            row = conn.execute("SELECT handle FROM agents WHERE id=? AND room_id=?", (str(agent_id), rid)).fetchone()
            if not row:
                raise HTTPException(404, "Agent not found")
            conn.execute("UPDATE agents SET token_hash=?, revoked=0, joined_at=NULL, last_seen=NULL WHERE id=?",
                         (digest(token), str(agent_id)))
            bump(conn, rid)
        await notify(rid)
        return {"token": token, "handle": row["handle"],
                "instructions": connection_instructions(settings.public_base_url, rid, row["handle"], token)}

    @app.get("/api/rooms/{room_id}/messages")
    async def operator_messages(room_id: UUID, request: Request, after: int = Query(0, ge=0),
                                before: int | None = Query(None, ge=1)):
        operator(request)
        with db.connect() as conn:
            return envelope(conn, str(room_id), after, before)

    @app.post("/api/rooms/{room_id}/messages")
    async def operator_send(room_id: UUID, body: NewMessage, request: Request):
        operator(request)
        with db.connect() as conn:
            result = insert_message(conn, str(room_id), "operator", body, True)
        await notify(str(room_id))
        return result

    @app.post("/{room_id}/join")
    async def join(room_id: UUID, request: Request, body: JoinRequest | None = None):
        rid, now = str(room_id), time.time()
        body = body or JoinRequest()
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = agent(conn, request, rid)
            if body.session_id and room(conn, rid)["status"] == "archived":
                raise HTTPException(409, "Restore this conversation before enabling delivery")
            conn.execute("UPDATE agents SET joined_at=COALESCE(joined_at,?),last_seen=? WHERE id=?",
                         (now, now, identity["id"]))
            if identity["joined_at"] is None:
                bump(conn, rid)
            result = envelope(conn, rid, before=2**63 - 1)
            result["history_truncated"] = result.pop("has_more")
            result["handle"] = identity["handle"]
            if body.session_id:
                # Keep unqueued messages across reconnects. First registration starts
                # after the history returned above, which the joining agent already reads.
                conn.execute("""INSERT INTO delivery_bindings
                    (agent_id,session_id,connection_id,cursor,updated_at) VALUES (?,?,?,?,?)
                    ON CONFLICT(agent_id) DO UPDATE SET session_id=excluded.session_id,
                    connection_id=excluded.connection_id,updated_at=excluded.updated_at""",
                    (identity["id"], str(body.session_id), str(body.connection_id), result["next_cursor"], now))
                binding = conn.execute("SELECT cursor FROM delivery_bindings WHERE agent_id=?",
                                       (identity["id"],)).fetchone()
                result["delivery"] = {"mode": "codex", "session_id": str(body.session_id),
                                      "connection_id": str(body.connection_id), "cursor": binding["cursor"]}
            else:
                # An explicit ordinary join switches this participant back to polling.
                conn.execute("DELETE FROM delivery_bindings WHERE agent_id=?", (identity["id"],))
                result["delivery"] = {"mode": "polling"}
        await notify(rid)
        return result

    @app.post("/{room_id}/messages")
    async def agent_send(room_id: UUID, body: NewMessage, request: Request):
        rid = str(room_id)
        with db.connect() as conn:
            identity = agent(conn, request, rid)
            result = insert_message(conn, rid, identity["handle"], body)
            conn.execute("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), identity["id"]))
        await notify(rid)
        return result

    def delivery_binding(conn, identity, connection_id):
        binding = conn.execute("SELECT * FROM delivery_bindings WHERE agent_id=?",
                               (identity["id"],)).fetchone()
        if not binding or binding["connection_id"] != str(connection_id):
            raise HTTPException(409, "Delivery registration was replaced; rejoin this room")
        return binding

    @app.get("/{room_id}/delivery")
    async def delivery_poll(room_id: UUID, request: Request, connection_id: UUID,
                            revision: int | None = Query(None, ge=0),
                            timeout: float = Query(25, ge=0, le=25)):
        return await wait_for_changes(str(room_id), request, 0, revision, timeout, connection_id)

    @app.post("/{room_id}/delivery/ack")
    async def delivery_ack(room_id: UUID, request: Request, body: DeliveryAck):
        rid = str(room_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = agent(conn, request, rid)
            binding = delivery_binding(conn, identity, body.connection_id)
            latest = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE room_id=?", (rid,)).fetchone()[0]
            if body.cursor > latest:
                raise HTTPException(422, "Cursor exceeds the room history")
            cursor = max(binding["cursor"], body.cursor)
            conn.execute("UPDATE delivery_bindings SET cursor=?,updated_at=? WHERE agent_id=?",
                         (cursor, time.time(), identity["id"]))
        return {"cursor": cursor}

    @app.get("/{room_id}/messages")
    async def poll(room_id: UUID, request: Request, after: int = Query(0, ge=0),
                   revision: int | None = Query(None, ge=0), timeout: float = Query(25, ge=0, le=25)):
        return await wait_for_changes(str(room_id), request, after, revision, timeout)

    async def wait_for_changes(rid, request, after, revision, timeout, connection_id=None):
        with db.connect() as conn:
            identity = agent(conn, request, rid)
            conn.execute("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), identity["id"]))
            baseline = room(conn, rid)["revision"] if revision is None else revision
        deadline = asyncio.get_running_loop().time() + timeout
        async with conditions[rid]:
            while True:
                with db.connect() as conn:
                    identity = agent(conn, request, rid)  # Recheck access on every wake.
                    if connection_id is not None:
                        binding = delivery_binding(conn, identity, connection_id)
                        after = binding["cursor"]
                    result = envelope(conn, rid, after)
                    if connection_id is not None:
                        scanned = bool(result["messages"])
                        if result["room_status"] != "active":
                            result.update(messages=[], next_cursor=after, has_more=False)
                        else:
                            result["messages"] = [m for m in result["messages"]
                                if m["sender"] != identity["handle"]
                                and ({identity["handle"], "everyone"} & set(m["targets"]))]
                            if scanned:
                                return result  # Advance past messages addressed elsewhere.
                remaining = deadline - asyncio.get_running_loop().time()
                if (result["messages"] or result["revision"] != baseline
                        or result["room_status"] == "archived" or remaining <= 0
                        or (connection_id is not None and result["room_status"] == "paused")):
                    return result
                if await request.is_disconnected():
                    return Response(status_code=204)
                try:
                    await asyncio.wait_for(conditions[rid].wait(), min(remaining, 1.0))
                except TimeoutError:
                    pass

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/{room_id}")
    async def room_page(room_id: UUID):
        return FileResponse(STATIC / "index.html")

    return app
