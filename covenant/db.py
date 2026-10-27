import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator (
    id INTEGER PRIMARY KEY CHECK(id = 1), username TEXT NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','archived')),
    revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id),
    handle TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, revoked INTEGER NOT NULL DEFAULT 0,
    joined_at REAL, last_seen REAL, created_at REAL NOT NULL,
    UNIQUE(room_id, handle)
);
CREATE TABLE IF NOT EXISTS agent_avatars (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    avatar_id TEXT NOT NULL CHECK(avatar_id IN ('raven','moon','key','eye','flame'))
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES rooms(id),
    sender TEXT NOT NULL, target TEXT NOT NULL, text TEXT NOT NULL,
    client_message_id TEXT NOT NULL, created_at REAL NOT NULL,
    UNIQUE(room_id, sender, client_message_id)
);
CREATE TABLE IF NOT EXISTS delivery_bindings (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL, connection_id TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_locks (
    path TEXT PRIMARY KEY, lock_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS message_receipts (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    queued_at REAL NOT NULL, delivered_at REAL, acknowledged_at REAL, answered_at REAL,
    reply_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    delivery_mode TEXT, last_error TEXT,
    PRIMARY KEY(message_id, agent_id)
);
CREATE TABLE IF NOT EXISTS message_replies (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    reply_to INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_delivery_status (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    mode TEXT NOT NULL, state TEXT NOT NULL, last_error TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_bridges (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE, agent_token_hash TEXT NOT NULL,
    harness TEXT NOT NULL, session_id TEXT, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_requests (
    id TEXT PRIMARY KEY, bridge_id TEXT NOT NULL REFERENCES approval_bridges(id) ON DELETE CASCADE,
    request_key TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT, decided_by TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
    alive_until REAL NOT NULL, decided_at REAL, UNIQUE(bridge_id,request_key)
);
CREATE INDEX IF NOT EXISTS approvals_bridge ON approval_requests(bridge_id);
CREATE INDEX IF NOT EXISTS message_replies_parent ON message_replies(reply_to,message_id);
CREATE INDEX IF NOT EXISTS receipts_agent ON message_receipts(agent_id,message_id);
CREATE INDEX IF NOT EXISTS file_locks_lease ON file_locks(lock_id);
CREATE INDEX IF NOT EXISTS messages_room_id ON messages(room_id, id);
CREATE INDEX IF NOT EXISTS agents_room_id ON agents(room_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()
