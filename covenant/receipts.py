"""Persist explicit transport receipts and reply links; never infer a model response."""
import time
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["polling", "codex", "claude", "catch_up"]


class ReceiptEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_ids: list[int] = Field(min_length=1, max_length=100)
    stage: Literal["delivered", "acknowledged", "error"]
    mode: Mode | None = None
    error: str | None = Field(default=None, max_length=500)


class DeliveryStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Mode
    state: Literal["listening", "starting", "awaiting_confirmation", "paused", "archived",
                   "stopped", "retrying", "fallback", "joined"]
    last_error: str | None = Field(default=None, max_length=500)


def record_receipt(conn, agent_id, message_ids, stage, mode=None, error=None):
    now = time.time()
    for mid in set(message_ids):
        if stage == "error":
            conn.execute("UPDATE message_receipts SET last_error=?, delivery_mode=COALESCE(?,delivery_mode) WHERE message_id=? AND agent_id=? AND delivered_at IS NULL",
                         (error, mode, mid, agent_id))
        else:
            conn.execute("""UPDATE message_receipts SET delivered_at=COALESCE(delivered_at,?),
                acknowledged_at=CASE WHEN ?='acknowledged' THEN COALESCE(acknowledged_at,?) ELSE acknowledged_at END,
                delivery_mode=COALESCE(?,delivery_mode), last_error=NULL WHERE message_id=? AND agent_id=?""",
                (now, stage, now, mode, mid, agent_id))


def receipt_rows(conn, rid, ids):
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"""SELECT r.*, a.handle, a.revoked, s.mode AS current_mode,
        s.state AS delivery_state, s.last_error AS delivery_error, s.updated_at AS delivery_updated_at
        FROM message_receipts r JOIN agents a ON a.id=r.agent_id
        LEFT JOIN agent_delivery_status s ON s.agent_id=a.id
        WHERE a.room_id=? AND r.message_id IN ({placeholders}) ORDER BY r.message_id,a.handle""", [rid, *ids])
    result = {}
    for row in rows:
        item = dict(row)
        item.pop("agent_id")
        mid = item.pop("message_id")
        item["status"] = ("answered" if item["answered_at"] is not None else
                          "acknowledged" if item["acknowledged_at"] is not None else
                          "delivered" if item["delivered_at"] is not None else "queued")
        result.setdefault(str(mid), []).append(item)
    return result


def register_receipt_routes(app, db, agent, operator, room):
    @app.post("/{room_id}/message-receipts")
    async def receive(room_id: UUID, request: Request, body: ReceiptEvent):
        rid = str(room_id)
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = agent(conn, request, rid)
            if room(conn, rid)["status"] == "archived":
                raise HTTPException(409, "Conversation archived")
            for mid in set(body.message_ids):
                if not conn.execute("SELECT 1 FROM message_receipts WHERE message_id=? AND agent_id=?", (mid, identity["id"])).fetchone():
                    raise HTTPException(404, "Message has no receipt for this agent")
            record_receipt(conn, identity["id"], body.message_ids, body.stage, body.mode, body.error)
        return {"ok": True}

    @app.post("/{room_id}/delivery-status")
    async def status(room_id: UUID, request: Request, body: DeliveryStatus):
        with db.connect() as conn:
            identity = agent(conn, request, str(room_id))
            conn.execute("""INSERT INTO agent_delivery_status VALUES (?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET mode=excluded.mode,state=excluded.state,
                last_error=excluded.last_error,updated_at=excluded.updated_at""",
                (identity["id"], body.mode, body.state, body.last_error, time.time()))
        return {"ok": True}

    @app.get("/api/rooms/{room_id}/message-receipts")
    async def read(room_id: UUID, request: Request, ids: str = Query(max_length=2400)):
        operator(request)
        try:
            values = [int(value) for value in ids.split(",")]
            if not 1 <= len(values) <= 100 or any(not 0 < value < 2**63 for value in values):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Supply up to 100 positive message IDs") from None
        with db.connect() as conn:
            room(conn, str(room_id))
            return {"receipts": receipt_rows(conn, str(room_id), values)}
