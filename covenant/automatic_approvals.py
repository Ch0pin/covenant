"""Server-owned native approval connections. No model calls or tool execution."""
import asyncio
from contextlib import suppress
import fcntl
import logging
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .approval_bridge import ApprovalClient, CodexApprovals
from .approvals import close_bridge, create_bridge
from .auth import digest
from .delivery import codex_endpoint

log = logging.getLogger(__name__)


def bind_session(db, room_url, token, harness, session_id):
    """Local MCP/hook registration only; no HTTP endpoint grants bridge credentials."""
    rid = str(UUID(urlsplit(room_url).path.strip('/')))
    session_id = str(UUID(str(session_id)))
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        agent = conn.execute('''SELECT a.* FROM agents a JOIN rooms r ON r.id=a.room_id
            WHERE a.room_id=? AND a.token_hash=? AND a.revoked=0 AND r.status='active'
            AND a.joined_at IS NOT NULL''', (rid, digest(token))).fetchone()
        if not agent:
            return False
        store_binding(conn, agent, harness, session_id, allow_move=True)
    return True


def store_binding(conn, agent, harness, session_id, *, allow_move=False):
    """Record an authenticated join atomically with its room/session binding."""
    if not allow_move:
        owner = conn.execute("""SELECT s.agent_id FROM approval_sessions s
            JOIN agents a ON a.id=s.agent_id JOIN rooms r ON r.id=a.room_id
            WHERE s.harness=? AND s.session_id=? AND s.agent_id!=? AND a.revoked=0
            AND s.agent_token_hash=a.token_hash AND r.status!='archived'""",
            (harness, session_id, agent['id'])).fetchone()
        if owner:
            raise ValueError("This native session is already linked to another participant. Revoke its previous invitation before moving it.")
    # One native session belongs to one participant at a time. A local rejoin
    # explicitly moves that session; close outstanding requests on the old link.
    old = conn.execute('''SELECT agent_id FROM approval_sessions
        WHERE agent_id=? OR (harness=? AND session_id=?)''',
        (agent['id'], harness, session_id)).fetchall()
    same = conn.execute('''SELECT 1 FROM approval_sessions WHERE agent_id=? AND harness=?
        AND session_id=? AND agent_token_hash=?''',
        (agent['id'], harness, session_id, agent['token_hash'])).fetchone()
    if not same:
        for row in old:
            if row['agent_id'] != agent['id']:
                conn.execute('DELETE FROM delivery_bindings WHERE agent_id=? AND session_id=?', (row['agent_id'], session_id))
            conn.execute('UPDATE approval_bridges SET expires_at=0 WHERE agent_id=?', (row['agent_id'],))
            conn.execute('''UPDATE approval_requests SET status='cancelled' WHERE bridge_id IN
                (SELECT id FROM approval_bridges WHERE agent_id=?) AND status IN ('pending','decided','submitted')''',
                (row['agent_id'],))
        conn.execute('DELETE FROM approval_sessions WHERE agent_id=? OR (harness=? AND session_id=?)',
                     (agent['id'], harness, session_id))
    conn.execute('''INSERT INTO approval_sessions VALUES (?,?,?,?,?) ON CONFLICT(agent_id)
        DO UPDATE SET updated_at=excluded.updated_at''',
        (agent['id'], harness, session_id, agent['token_hash'], time.time()))


class ApprovalManager:
    def __init__(self, app, db, settings, worker_factory=CodexApprovals):
        self.app, self.db, self.settings = app, db, settings
        self.worker_factory = worker_factory
        self.jobs, self.states = {}, {}
        self.task = None
        self.lock = None

    async def start(self):
        # A second web process must not answer the same native prompt twice.
        self.lock = open(str(self.db.path) + '.approvals.lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            log.warning('Another Covenant process owns automatic approval forwarding.')
            return
        with self.db.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO approval_runtime VALUES (1,?)',
                         (f'http://127.0.0.1:{self.settings.port}',))
        self.task = asyncio.create_task(self.run())

    def candidates(self):
        with self.db.connect() as conn:
            # Snapshot the credential generation on first adoption. Rotating a
            # token must require a fresh authenticated join, never reactivate an old session.
            conn.execute("""INSERT OR IGNORE INTO approval_sessions
                SELECT a.id,'codex',b.session_id,a.token_hash,? FROM delivery_bindings b
                JOIN agents a ON a.id=b.agent_id JOIN rooms r ON r.id=a.room_id
                WHERE a.revoked=0 AND a.joined_at IS NOT NULL AND r.status='active'
                AND NOT EXISTS (SELECT 1 FROM approval_sessions s WHERE s.agent_id=a.id)""", (time.time(),))
            # Existing Codex delivery registrations continue working after upgrade.
            rows = conn.execute('''SELECT a.id AS agent_id,a.handle,a.room_id,a.token_hash,
                'codex' AS harness,b.session_id FROM delivery_bindings b
                JOIN agents a ON a.id=b.agent_id JOIN rooms r ON r.id=a.room_id
                WHERE a.revoked=0 AND a.joined_at IS NOT NULL AND r.status='active' AND NOT EXISTS
                (SELECT 1 FROM approval_sessions s WHERE s.agent_id=a.id)
                UNION ALL SELECT a.id,a.handle,a.room_id,a.token_hash,s.harness,s.session_id
                FROM approval_sessions s JOIN agents a ON a.id=s.agent_id JOIN rooms r ON r.id=a.room_id
                WHERE a.revoked=0 AND r.status='active' AND s.agent_token_hash=a.token_hash''').fetchall()
        return [dict(row) for row in rows]

    def status(self):
        return [dict(value, agent_id=key) for key, value in self.states.items()]

    def set_state(self, row, state, detail=''):
        self.states[row['agent_id']] = {'harness': row['harness'], 'state': state, 'detail': detail}

    async def reconcile(self):
        candidates = self.candidates()
        counts = {}
        for row in candidates:
            key = (row['harness'], row['session_id'])
            counts[key] = counts.get(key, 0) + 1
        desired = {r['agent_id']: r for r in candidates if counts[(r['harness'], r['session_id'])] == 1}
        for aid, job in list(self.jobs.items()):
            row = desired.get(aid)
            fingerprint = (row['session_id'], row['token_hash']) if row else None
            if fingerprint != job['fingerprint'] or row['harness'] != 'codex':
                job['task'].cancel()
                with suppress(asyncio.CancelledError):
                    await job['task']
                del self.jobs[aid]
        self.states = {aid: status for aid, status in self.states.items() if aid in desired}
        for row in candidates:
            aid = row['agent_id']
            if aid not in desired:
                self.set_state(row, 'disconnected', 'This session is linked to multiple agents. Rejoin it with one invitation.')
            elif row['harness'] == 'claude':
                self.set_state(row, 'ready', 'Claude permission hook is linked to this participant.')
            elif aid not in self.jobs:
                self.set_state(row, 'connecting')
                self.jobs[aid] = {'fingerprint': (row['session_id'], row['token_hash']),
                                  'task': asyncio.create_task(self.connect_codex(row))}
        # Refresh credentials only for our live connections, without detaching a
        # session every 24 hours. Token rotation/revocation is checked by each API call.
        with self.db.connect() as conn:
            for job in self.jobs.values():
                if job.get('bridge_id'):
                    conn.execute('UPDATE approval_bridges SET expires_at=? WHERE id=? AND expires_at>?',
                                 (time.time() + 86400, job['bridge_id'], time.time()))

    async def connect_codex(self, row):
        delay = 2
        while True:
            bid = None
            api = None
            try:
                bid, token = create_bridge(self.db, row['room_id'], row['handle'], 'codex', row['session_id'], expected_token_hash=row['token_hash'])
                self.jobs[row['agent_id']]['bridge_id'] = bid
                api = ApprovalClient('http://127.0.0.1', token)
                await api.close()
                api.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                    base_url='http://127.0.0.1', headers={'Authorization': 'Bearer ' + token}, timeout=5)
                self.set_state(row, 'connecting')
                worker = self.worker_factory(api, row['session_id'],
                    on_connected=lambda: self.set_state(row, 'connected'))
                await worker.run(codex_endpoint())
            except asyncio.CancelledError:
                raise
            except Exception:
                # Native payloads/credentials may be present in exception messages.
                log.warning('Codex approval connection unavailable; retrying automatically.')
            finally:
                if api:
                    await api.close()
                if bid:
                    close_bridge(self.db, bid)
                self.set_state(row, 'disconnected', 'Open the registered Codex session on the configured shared server. Retrying automatically.')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def run(self):
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.warning('Automatic approval refresh failed; retrying.')
            await asyncio.sleep(2)

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        tasks = [job['task'] for job in self.jobs.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        if self.lock:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None
