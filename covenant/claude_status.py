"""Claude Code status from its own hook events: no prompts, model calls or tokens.

Hook processes import this, so it must stay free of web and MCP libraries.
Only the event kind and Claude's public error enum are kept; error details and
assistant text may contain secrets and are never stored.
"""
import json

from .db import CLAUDE_STATUS_SCHEMA

STATUS_EVENTS = ("UserPromptSubmit", "Stop", "StopFailure", "Notification", "PreCompact", "PostCompact")
# Notification types named by Claude Code: idle_prompt is an ordinary idle session, the dialogs need a human.
INPUT_NOTIFICATIONS = ("elicitation_dialog", "elicitation_url_dialog")
NOTIFICATION_MATCHER = "idle_prompt|elicitation_dialog|elicitation_url_dialog"
FAILURES = {
    "rate_limit": ("rate_limit", "Usage or rate limit reached", "error"),
    "authentication_failed": ("authentication", "Authentication required", "error"),
    "oauth_org_not_allowed": ("authentication", "Organization not allowed", "error"),
    "cloud_credential_error": ("authentication", "Cloud credentials error", "error"),
    "account_on_hold": ("billing", "Account on hold", "error"),
    "billing_error": ("billing", "Billing issue", "error"),
    "verification_required": ("verification", "Account verification required", "warning"),
    "overloaded": ("service_error", "Service overloaded", "error"),
    "server_error": ("service_error", "Service error", "error"),
    "invalid_request": ("request_error", "Request error", "error"),
    "model_not_found": ("model_unavailable", "Model unavailable", "error"),
    "max_output_tokens": ("output_limit", "Output token limit reached", "warning"),
}
# A missed Stop event must not hold channel handshakes back for longer than this.
BUSY_MAX_AGE = 15 * 60
COMPACTING = {"code": "compacting", "label": "Compacting context", "level": "info"}


def transition(event, payload, phase, reason):
    """The (phase, reason) after a hook event, or None when the event changes nothing."""
    if event == "UserPromptSubmit":
        return "working", None
    if event == "Stop":
        return "idle", None
    if event == "StopFailure":
        code, label, level = FAILURES.get(payload.get("error"), ("service_error", "Harness reported an error", "error"))
        return "idle", {"code": code, "label": label, "level": level}
    if event == "Notification":
        kind = payload.get("notification_type")
        if kind == "idle_prompt":
            return "idle", reason
        return ("input", reason) if kind in INPUT_NOTIFICATIONS else None
    if event == "PreCompact":
        return phase, COMPACTING
    if event == "PostCompact":
        return phase, None if reason and reason.get("code") == "compacting" else reason
    return None


def record(conn, binding, event, payload, now):
    """Stores the new state for an active linked session; other sessions' rows are never inherited."""
    conn.execute(CLAUDE_STATUS_SCHEMA)
    row = conn.execute("SELECT * FROM claude_native_status WHERE agent_id=?", (binding["agent_id"],)).fetchone()
    same = bool(row) and (row["session_id"], row["agent_token_hash"]) == (binding["session_id"], binding["agent_token_hash"])
    phase = row["phase"] if same else "unknown"
    reason = json.loads(row["reason"]) if same and row["reason"] else None
    change = transition(event, payload, phase, reason)
    if change is None:
        return
    new_phase, new_reason = change
    phase_since = row["phase_since"] if same and new_phase == phase else now
    reason_since = row["reason_since"] if same and new_reason == reason else (now if new_reason else None)
    conn.execute("""INSERT INTO claude_native_status VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET
        session_id=excluded.session_id,agent_token_hash=excluded.agent_token_hash,phase=excluded.phase,
        phase_since=excluded.phase_since,reason=excluded.reason,reason_since=excluded.reason_since,updated_at=excluded.updated_at""",
        (binding["agent_id"], binding["session_id"], binding["agent_token_hash"], new_phase, phase_since,
         json.dumps(new_reason) if new_reason else None, reason_since, now))


def view(row, binding, connected):
    """The same shape as NativeStatus.view, for participant_status.

    Hook events arrive only on change, so an idle state can be hours old. It is trusted only for the
    session it was written by, and only while the chat connection shows the Claude process is alive.
    """
    available = bool(row and binding and connected and
                     (row["session_id"], row["agent_token_hash"]) == (binding["session_id"], binding["agent_token_hash"]))
    reason = json.loads(row["reason"]) if available and row["reason"] else None
    return {"available": available, "phase": row["phase"] if available else "unknown",
            "phase_since": row["phase_since"] if row else None, "reason": reason,
            "reason_since": row["reason_since"] if row else None,
            "updated_at": row["updated_at"] if row else 0, "reset_at": None}


def busy(conn, agent_id, now):
    """True while this agent's own linked Claude session is mid-turn, as reported by its hooks."""
    conn.execute(CLAUDE_STATUS_SCHEMA)
    row = conn.execute("""SELECT c.phase,c.phase_since FROM claude_native_status c
        JOIN approval_sessions s ON s.agent_id=c.agent_id AND s.harness='claude'
            AND s.session_id=c.session_id AND s.agent_token_hash=c.agent_token_hash
        JOIN agents a ON a.id=c.agent_id AND a.token_hash=c.agent_token_hash
        WHERE c.agent_id=?""", (agent_id,)).fetchone()
    return bool(row) and row["phase"] == "working" and now - row["phase_since"] <= BUSY_MAX_AGE
