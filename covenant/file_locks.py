"""Cooperative leases for canonical file paths, shared across rooms on this server."""
import posixpath
import time
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LockInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcquireLocks(LockInput):
    paths: list[str] = Field(min_length=1, max_length=50)
    lease_seconds: int = Field(default=300, ge=30, le=3600)

    @field_validator("paths")
    @classmethod
    def canonical_paths(cls, paths):
        normalized = []
        for path in paths:
            if (not path.startswith("/") or path.startswith("//") or len(path) > 4096
                    or any(ord(c) < 32 for c in path)):
                raise ValueError("Use canonical absolute POSIX file paths")
            path = posixpath.normpath(path)
            if path == "/":
                raise ValueError("Lock individual files, not a filesystem root")
            normalized.append(path)
        return sorted(set(normalized))


class Lease(LockInput):
    lock_id: UUID


class RenewLease(Lease):
    lease_seconds: int = Field(default=300, ge=30, le=3600)


def visible_file_locks(conn, room_id):
    """Read active reservations and their release IDs for the authenticated operator."""
    now = time.time()
    rows = conn.execute("""SELECT l.path, l.lock_id, a.handle AS owner, l.expires_at
        FROM file_locks l JOIN agents a ON a.id=l.agent_id JOIN rooms r ON r.id=a.room_id
        WHERE a.room_id=? AND a.revoked=0 AND r.status!='archived' AND l.expires_at>?
        ORDER BY a.handle,l.path LIMIT 501""", (room_id, now)).fetchall()
    return {"locks": [dict(row) for row in rows[:500]], "truncated": len(rows) > 500,
            "server_time": now}


def register_file_lock_routes(app, db, agent, room, operator):
    def prune(conn, now):
        # This also handles token rotation/revocation and room archive/delete cleanup.
        conn.execute("""DELETE FROM file_locks WHERE expires_at<=? OR agent_id IN (
            SELECT a.id FROM agents a JOIN rooms r ON r.id=a.room_id
            WHERE a.revoked=1 OR r.status='archived')""", (now,))

    def require_active(conn, rid):
        if room(conn, rid)["status"] != "active":
            raise HTTPException(409, "File locks can only be acquired in active rooms")

    def operator_lease(conn, rid, lock_id):
        room(conn, rid)
        now = time.time()
        prune(conn, now)
        rows = conn.execute("""SELECT l.path,l.agent_id,l.expires_at,a.handle FROM file_locks l
            JOIN agents a ON a.id=l.agent_id WHERE l.lock_id=? AND a.room_id=? ORDER BY l.path""",
            (str(lock_id), rid)).fetchall()
        if not rows:
            raise HTTPException(409, "This reservation has ended or changed. Refresh the file locks.")
        return rows, now

    @app.get("/api/rooms/{room_id}/file-locks/{lock_id}")
    async def inspect_lease(room_id: UUID, lock_id: UUID, request: Request):
        operator(request)
        with db.connect() as conn:
            rows, now = operator_lease(conn, str(room_id), lock_id)
            return {"lock_id": str(lock_id), "owner": rows[0]["handle"],
                    "paths": [r["path"] for r in rows], "expires_at": rows[0]["expires_at"],
                    "server_time": now}

    @app.post("/api/rooms/{room_id}/file-locks/{lock_id}/release")
    async def operator_release(room_id: UUID, lock_id: UUID, request: Request):
        operator(request)
        rid = str(room_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows, _ = operator_lease(conn, rid, lock_id)
            # Match this exact lease and owner. A stale confirmation must never
            # release a replacement reservation on the same paths.
            count = conn.execute("DELETE FROM file_locks WHERE lock_id=? AND agent_id=?",
                                 (str(lock_id), rows[0]["agent_id"])).rowcount
            return {"released": count, "file_locks": visible_file_locks(conn, rid)}

    @app.post("/{room_id}/file-locks/acquire")
    async def acquire(room_id: UUID, request: Request, body: AcquireLocks):
        rid = str(room_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            identity = agent(conn, request, rid)
            require_active(conn, rid)
            prune(conn, now)
            placeholders = ",".join("?" for _ in body.paths)
            rows = list(conn.execute(f"""SELECT l.*, a.handle, a.room_id FROM file_locks l
                JOIN agents a ON a.id=l.agent_id WHERE l.path IN ({placeholders})""", body.paths))
            if rows:
                conflicts = []
                for row in rows:
                    item = {"path": row["path"], "expires_at": row["expires_at"],
                            "owner": row["handle"] if row["room_id"] == rid else "another room"}
                    if row["agent_id"] == identity["id"]:
                        item["lock_id"] = row["lock_id"]
                    conflicts.append(item)
                return {"acquired": False, "conflicts": conflicts}
            lock_id, expires_at = str(uuid4()), now + body.lease_seconds
            conn.executemany("INSERT INTO file_locks(path,lock_id,agent_id,expires_at) VALUES (?,?,?,?)",
                             [(path, lock_id, identity["id"], expires_at) for path in body.paths])
            return {"acquired": True, "lock_id": lock_id, "paths": body.paths, "expires_at": expires_at}

    @app.post("/{room_id}/file-locks/renew")
    async def renew(room_id: UUID, request: Request, body: RenewLease):
        rid = str(room_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            identity = agent(conn, request, rid)
            prune(conn, now)
            # Paused agents may finish current edits safely; archive ends all leases.
            expires_at = now + body.lease_seconds
            rows = conn.execute("SELECT path FROM file_locks WHERE lock_id=? AND agent_id=?",
                                (str(body.lock_id), identity["id"])).fetchall()
            if not rows:
                return {"renewed": False, "reason": "Lease expired, ended, or belongs to another agent. Stop editing and reacquire."}
            conn.execute("UPDATE file_locks SET expires_at=? WHERE lock_id=? AND agent_id=?",
                         (expires_at, str(body.lock_id), identity["id"]))
            return {"renewed": True, "lock_id": str(body.lock_id), "expires_at": expires_at,
                    "paths": [row["path"] for row in rows]}

    @app.post("/{room_id}/file-locks/release")
    async def release(room_id: UUID, request: Request, body: Lease):
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = agent(conn, request, str(room_id))
            count = conn.execute("DELETE FROM file_locks WHERE lock_id=? AND agent_id=?",
                                 (str(body.lock_id), identity["id"])).rowcount
            return {"released": count}

    @app.get("/{room_id}/file-locks")
    async def list_locks(room_id: UUID, request: Request):
        rid = str(room_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = agent(conn, request, rid)
            prune(conn, time.time())
            rows = conn.execute("""SELECT l.*, a.handle FROM file_locks l JOIN agents a ON a.id=l.agent_id
                WHERE a.room_id=? ORDER BY l.path LIMIT 501""", (rid,)).fetchall()
            locks = []
            for row in rows[:500]:
                item = {"path": row["path"], "owner": row["handle"], "expires_at": row["expires_at"]}
                if row["agent_id"] == identity["id"]:
                    item["lock_id"] = row["lock_id"]
                locks.append(item)
            return {"locks": locks, "truncated": len(rows) > 500}
