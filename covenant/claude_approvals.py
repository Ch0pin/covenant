"""Persistent Claude hooks: link a successful Covenant join and forward native prompts."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .approval_bridge import ApprovalClient, claude_decision, claude_hook
from .approvals import close_bridge, create_bridge
from .automatic_approvals import bind_session
from .db import Database


def install(settings, destination=None):
    destination = destination or Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude')) / 'settings.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = shlex.join([sys.executable, '-m', 'covenant.claude_approvals', '--database',
                         str(settings.database.resolve()), '--port', str(settings.port)])
    with open(str(destination) + '.covenant.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = destination.read_text() if destination.exists() else None
        config = json.loads(before) if before is not None else {}
        if not isinstance(config, dict) or not isinstance(config.get('hooks', {}), dict):
            raise ValueError('Claude settings must contain a JSON object with an object-valued hooks field')
        hooks = config.setdefault('hooks', {})
        for event, matcher, timeout in [('PostToolUse', 'mcp__covenant__join_room', 10),
                                         ('PermissionRequest', '*', 600), ('SessionEnd', '*', 10)]:
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise ValueError('Claude hook entries must be arrays; existing settings were preserved')
            # Replace only our marked entries, preserving all user/plugin hooks.
            retained = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get('hooks', []), list):
                    raise ValueError('Invalid Claude hook configuration; existing settings were preserved')
                original = group.get('hooks', [])
                if any(not isinstance(h, dict) for h in original):
                    raise ValueError('Invalid Claude hook entry; existing settings were preserved')
                remaining = [h for h in original if h.get('statusMessage') != 'Covenant approval forwarding']
                if remaining == original:
                    retained.append(group)
                elif remaining:
                    retained.append({**group, 'hooks': remaining})
            groups[:] = retained
            groups.append({'matcher': matcher, 'hooks': [{'type': 'command', 'command': command,
                'timeout': timeout, 'statusMessage': 'Covenant approval forwarding'}]})
        output = json.dumps(config, indent=2) + '\n'
        if before is not None and json.loads(before) == config:
            return destination, None
        backup = None
        if before is not None:
            backup = destination.with_name(destination.name + '.covenant-backup-' + uuid4().hex[:8])
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(before)
        fd, tmp = tempfile.mkstemp(prefix='.covenant-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, destination)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return destination, backup


def end_session(db, session_id):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM approval_sessions WHERE harness=\'claude\' AND session_id=?', (session_id,))
        conn.execute('UPDATE approval_bridges SET expires_at=0 WHERE harness=\'claude\' AND session_id=?', (session_id,))
        conn.execute('''UPDATE approval_requests SET status='cancelled' WHERE bridge_id IN
            (SELECT id FROM approval_bridges WHERE harness='claude' AND session_id=?)
            AND status IN ('pending','decided','submitted')''', (session_id,))


def session_binding(db, session_id):
    with db.connect() as conn:
        # Retain inactive bindings to deny rather than bypass an operator pause/revocation.
        row = conn.execute('''SELECT s.*,a.room_id,a.handle,a.revoked,a.token_hash,r.status
            FROM approval_sessions s JOIN agents a ON a.id=s.agent_id JOIN rooms r ON r.id=a.room_id
            WHERE s.harness='claude' AND s.session_id=?''', (session_id,)).fetchone()
        return dict(row) if row else None


async def dispatch(payload, db, port, api_factory=ApprovalClient):
    event = payload.get('hook_event_name')
    session_id = str(UUID(payload['session_id']))
    if event == 'PostToolUse':
        if payload.get('tool_name') != 'mcp__covenant__join_room':
            return None
        response = payload.get('tool_response')
        if isinstance(response, dict) and (response.get('isError') or response.get('is_error')):
            return None
        args = payload.get('tool_input', {})
        if bind_session(db, args['room_url'], args['access_token'], 'claude', session_id):
            return {'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                'additionalContext': 'Covenant native approvals are linked to this session. The operator can approve or reject them in Covenant.'}}
        return None
    if event == 'SessionEnd':
        end_session(db, session_id)
        return None
    if event != 'PermissionRequest':
        return None
    binding = session_binding(db, session_id)
    if not binding:
        return None  # Unjoined Claude sessions keep their normal native prompts.
    if binding['revoked'] or binding['status'] != 'active' or binding['agent_token_hash'] != binding['token_hash']:
        return claude_decision(False, 'Covenant access is paused or ended. Ask the operator to restore access and rejoin.')
    with db.connect() as conn:
        runtime = conn.execute('SELECT base_url FROM approval_runtime WHERE id=1').fetchone()
    base = runtime['base_url'] if runtime else f'http://127.0.0.1:{port}'
    parsed = urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
        return claude_decision(False, 'Invalid local Covenant approval address.')
    bid, token = create_bridge(db, binding['room_id'], binding['handle'], 'claude', session_id,
                               expected_token_hash=binding['agent_token_hash'])
    api = api_factory(base, token)
    try:
        return await claude_hook(payload, api)
    finally:
        await api.close()
        close_bridge(db, bid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--port', type=int, default=6666)
    args = parser.parse_args()
    # The legacy launcher owns this session's PermissionRequest hook already.
    if os.environ.get('COVENANT_APPROVAL_TOKEN'):
        return
    payload = {}
    result = None
    try:
        payload = json.loads(sys.stdin.read(100001))
        if not args.database.is_file():
            return
        db = Database(args.database)
        with db.connect() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='approval_sessions'").fetchone():
                # Setup can precede the first upgraded server start. No session
                # could have registered yet, so preserve ordinary native prompts.
                return
        result = asyncio.run(dispatch(payload, db, args.port))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        if payload.get('hook_event_name') == 'PermissionRequest':
            result = claude_decision(False, 'Covenant approval hook unavailable. Check the local server and hook setup.')
        else:
            print('Covenant approval registration unavailable; check setup and rejoin.', file=sys.stderr)
    if result is not None:
        print(json.dumps(result))


if __name__ == '__main__':
    main()
