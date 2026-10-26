"""Bounded, explicitly requested thread context, separate from inbox cursors."""
from uuid import UUID
from fastapi import HTTPException, Query, Request, Path as PathParam
from .receipts import receipt_rows


def thread_root(conn, message_id):
    return conn.execute("""WITH RECURSIVE ancestors(id) AS (
        SELECT ? UNION ALL SELECT r.reply_to FROM message_replies r JOIN ancestors a ON r.message_id=a.id
    ) SELECT MIN(id) FROM ancestors""", (message_id,)).fetchone()[0]


def register_thread_routes(app, db, agent, operator, room, message_dict):
    def read(conn, rid, message_id, after, limit, include_receipts=False):
        if not conn.execute("SELECT 1 FROM messages WHERE id=? AND room_id=?", (message_id, rid)).fetchone():
            raise HTTPException(404, "Message not found in this conversation")
        root_id = thread_root(conn, message_id)
        root = conn.execute("SELECT * FROM messages WHERE id=? AND room_id=?", (root_id, rid)).fetchone()
        rows = conn.execute("""WITH RECURSIVE discussion(id) AS (
            SELECT ? UNION ALL SELECT r.message_id FROM message_replies r JOIN discussion d ON r.reply_to=d.id
        ) SELECT m.* FROM discussion d JOIN messages m ON m.id=d.id
        WHERE m.room_id=? AND m.id!=? AND m.id>? ORDER BY m.id LIMIT ?""",
            (root_id, rid, root_id, after, limit+1)).fetchall()
        messages = [message_dict(row, conn) for row in rows[:limit]]
        result = {"root": message_dict(root, conn), "messages": messages,
                  "next_cursor": messages[-1]["id"] if messages else after,
                  "has_more": len(rows) > limit}
        if include_receipts:
            all_messages = [result["root"], *messages]
            receipts = receipt_rows(conn, rid, [m["id"] for m in all_messages if m["sender"] == "operator"])
            for message in all_messages:
                if message["sender"] == "operator":
                    message["receipts"] = receipts.get(str(message["id"]), [])
        return result

    @app.get("/api/rooms/{room_id}/threads/{message_id}")
    async def operator_thread(room_id: UUID, request: Request, message_id: int = PathParam(ge=1, le=2**63-1),
                              after: int = Query(0, ge=0, le=2**63-1), limit: int = Query(20, ge=1, le=50)):
        operator(request)
        with db.connect() as conn:
            room(conn, str(room_id))
            return read(conn, str(room_id), message_id, after, limit, True)

    @app.get("/{room_id}/threads/{message_id}")
    async def agent_thread(room_id: UUID, request: Request, message_id: int = PathParam(ge=1, le=2**63-1),
                           after: int = Query(0, ge=0, le=2**63-1), limit: int = Query(20, ge=1, le=50)):
        with db.connect() as conn:
            agent(conn, request, str(room_id))
            return read(conn, str(room_id), message_id, after, limit)
