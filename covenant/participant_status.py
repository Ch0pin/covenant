"""Evidence-based operator status; no prompts, agent calls, or inferred refusal reasons."""
import time
from uuid import UUID

from fastapi import Request

NATIVE_MAX_AGE = 90
DELIVERY_MAX_AGE = 120
ERRORS = {
    "usageLimitExceeded": ("usage_limit", "Usage limit reached"),
    "rateLimitExceeded": ("rate_limit", "Rate limit reached"),
    "sessionBudgetExceeded": ("budget_limit", "Session budget exhausted"),
    "contextWindowExceeded": ("context_limit", "Context limit reached"),
    "cyberPolicy": ("cyber_policy", "Cybersecurity policy restriction"),
    "misalignmentPolicyViolation": ("request_refused", "Request refused · safety policy"),
    "tooManyDenials": ("request_denied", "Repeated requests denied"),
    "unauthorized": ("authentication", "Authentication required"),
    "sandboxError": ("sandbox", "Sandbox error"),
    "modelNotFound": ("model_unavailable", "Model unavailable"),
    "httpConnectionFailed": ("connection_error", "Service connection failed"),
    "responseStreamConnectionFailed": ("connection_error", "Service connection failed"),
    "responseStreamDisconnected": ("connection_error", "Response connection lost"),
    "responseTooManyFailedAttempts": ("service_error", "Service retries exhausted"),
    "serverOverloaded": ("service_error", "Service overloaded"),
    "flexUnavailable": ("service_error", "Service unavailable"),
    "internalServerError": ("service_error", "Service error"),
    "badRequest": ("request_error", "Request error"),
}


def error_reason(error):
    if not isinstance(error, dict):
        return None
    code = error.get("codexErrorInfo")
    if isinstance(code, dict):
        code = next(iter(code), None)
    if isinstance(code, str):
        code = code[:1].lower() + code[1:]
    if not isinstance(code, str):
        code = None
    # Never classify raw error messages or assistant prose, which may contain secrets.
    kind, label = ERRORS.get(code, ("service_error", "Harness reported an error"))
    return {"code": kind, "label": label, "level": "error"}


class NativeStatus:
    """Only bounded, public protocol enums and timestamps are retained in memory."""
    def __init__(self):
        self.phase = "unknown"
        self.phase_since = time.time()
        self.reason = None
        self.reason_since = None
        self.reason_updated_at = 0
        self.turn_id = None
        self.updated_at = 0
        self.reset_at = None
        self.snapshot_ok = False

    def set_phase(self, phase, now):
        if phase != self.phase:
            self.phase, self.phase_since = phase, now

    def set_reason(self, reason, now):
        self.reason_updated_at = now
        if reason != self.reason:
            self.reason, self.reason_since = reason, now if reason else None

    def thread(self, status, now):
        if not isinstance(status, dict):
            return
        kind, flags = status.get("type"), status.get("activeFlags", [])
        if not isinstance(flags, list):
            flags = []
        phase = {"idle": "idle", "active": "working", "notLoaded": "disconnected", "systemError": "system_error"}.get(kind, "unknown")
        if kind == "active":
            if "waitingOnApproval" in flags:
                phase = "approval"
            elif "waitingOnUserInput" in flags:
                phase = "input"
        self.set_phase(phase, now)

    def turn(self, turn, now, *, started=False):
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            return
        new_turn = turn["id"] != self.turn_id
        if new_turn or started:
            self.set_reason(None, now)
        self.turn_id = turn["id"]
        status = turn.get("status")
        if status == "failed":
            self.set_reason(error_reason(turn.get("error") or {}), now)
        elif status == "completed":
            self.set_reason(None, now)
        elif status == "interrupted":
            self.set_reason({"code": "interrupted", "label": "Turn interrupted", "level": "warning"}, now)
        if started:
            self.set_phase("working", now)
        elif status in {"failed", "completed", "interrupted"}:
            self.set_phase("idle", now)

    def consume(self, method, params, now=None):
        now = time.time() if now is None else now
        if not isinstance(params, dict):
            return
        if method == "account/rateLimits/updated":
            limits = params.get("rateLimits", {})
            if not isinstance(limits, dict):
                return
            resets = []
            for key in ("primary", "secondary"):
                window = limits.get(key)
                if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float)) and window["usedPercent"] >= 100:
                    reset = window.get("resetsAt")
                    if isinstance(reset, (int, float)) and now < reset < now + 366 * 86400:
                        resets.append(reset)
            self.reset_at = max(resets) if resets else None
            return
        if method == "covenant/status-unavailable":
            self.snapshot_ok = False
            return
        if method in {"covenant/status-snapshot", "thread/status/changed"}:
            status = params.get("status")
            if not isinstance(status, dict) or status.get("type") not in {"idle", "active", "notLoaded", "systemError"}:
                self.snapshot_ok = False
                return
        if method == "covenant/status-snapshot":
            self.thread(params.get("status"), now)
            self.turn(params.get("turn"), now)
        elif method == "thread/status/changed":
            self.thread(params.get("status"), now)
        elif method in {"turn/started", "turn/completed"}:
            self.turn(params.get("turn"), now, started=method == "turn/started")
        elif method == "error":
            if self.turn_id and params.get("turnId") and params["turnId"] != self.turn_id:
                return
            reason = error_reason(params.get("error"))
            if reason:
                if params.get("willRetry") is True:
                    reason = {**reason, "label": reason["label"] + " · retrying", "level": "warning"}
                self.set_reason(reason, now)
        elif method == "model/safetyBuffering/updated":
            if self.turn_id and params.get("turnId") != self.turn_id:
                return
            if params.get("showBufferingUi") is True:
                self.set_reason({"code": "safety_processing", "label": "Safety processing", "level": "warning"}, now)
            elif self.reason and self.reason["code"] == "safety_processing":
                self.set_reason(None, now)
        elif method == "model/verification":
            if self.turn_id and params.get("turnId") != self.turn_id:
                return
            verifications = params.get("verifications", [])
            if isinstance(verifications, list) and verifications:
                label = "Cybersecurity verification required" if "trustedAccessForCyber" in verifications else "Account verification required"
                self.set_reason({"code": "verification", "label": label, "level": "warning"}, now)
            elif self.reason and self.reason["code"] == "verification":
                self.set_reason(None, now)
        elif method == "thread/closed":
            self.set_phase("disconnected", now)
            self.set_reason(None, now)
        else:
            return
        self.updated_at, self.snapshot_ok = now, True

    def view(self, connected, now):
        fresh = bool((connected or self.phase in {"disconnected", "system_error"}) and self.snapshot_ok and now - self.updated_at <= NATIVE_MAX_AGE)
        if self.reason and now - self.reason_updated_at > NATIVE_MAX_AGE:
            fresh = False
        return {"available": fresh, "phase": self.phase if fresh else "unknown", "phase_since": self.phase_since,
                "reason": self.reason if fresh else None, "reason_since": self.reason_since,
                "updated_at": self.updated_at, "reset_at": self.reset_at if self.reset_at and self.reset_at > now else None}


def participant_status(agent, room_state, delivery, approval, receipt, native, *, now, warning_seconds, activity_at=None):
    available = bool(native and native.get("available"))
    telemetry = "Native state available" if available else "Native state unavailable; using chat and approval signals"
    def result(code, label, level="neutral", since=None, detail="", reset_at=None):
        return {"code": code, "label": label, "level": level, "since": since,
                "detail": detail, "telemetry": telemetry, "native_available": available, "reset_at": reset_at}
    if agent["revoked"]:
        return result("revoked", "Access revoked")
    if room_state == "archived":
        return result("archived", "Conversation archived")
    if room_state == "paused":
        return result("paused", "Conversation paused", "warning")
    if not agent["joined_at"]:
        return result("not_joined", "Not joined yet")
    if approval:
        label = "Waiting for approval" if approval["status"] == "pending" else "Approval response pending"
        return result("approval", label, "warning", approval["created_at"], "A native action is waiting for the operator or its decision to reach the harness.")
    if available and native["phase"] == "approval":
        return result("approval", "Waiting for approval", "warning", native["phase_since"], "The native harness is waiting. Check pending approvals or the agent’s terminal.")
    if available and native.get("reason"):
        r = native["reason"]
        reset = native.get("reset_at") if r["code"] in {"usage_limit", "rate_limit"} else None
        return result(r["code"], r["label"], r["level"], native["reason_since"], "Explicitly reported by the native harness for this session. A failed request does not imply the connection is closed.", reset)
    if available and native["phase"] == "input":
        return result("input", "Waiting for input", "warning", native["phase_since"], "The native harness is waiting for human input.")
    if available and native["phase"] in {"disconnected", "system_error"}:
        return result(native["phase"], "Session disconnected" if native["phase"] == "disconnected" else "Harness error", "error", native["phase_since"])
    if delivery and delivery["state"] in {"retrying", "stopped", "fallback"}:
        return result("delivery_error", "Delivery retrying" if delivery["state"] == "retrying" else "Delivery needs attention", "warning", delivery["updated_at"], "Chat delivery reported a failure or fallback. This does not establish a model or policy restriction.")
    if receipt and receipt["delivered_at"] is not None:
        since = receipt["delivered_at"]
        if now - since >= warning_seconds:
            if available and native["phase"] == "working":
                return result("ack_overdue", "Working · acknowledgement overdue", "warning", since,
                              f"The native turn is active, but operator message #{receipt['message_id']} has no acknowledgement.")
            return result("ack_overdue", "Acknowledgement overdue", "warning", since,
                          f"Operator message #{receipt['message_id']} was delivered but has no acknowledgement. This is a message-receipt warning, not evidence that the agent stopped working.")
        return result("awaiting_ack", "Awaiting acknowledgement", "warning", since, f"Waiting for acknowledgement of operator message #{receipt['message_id']}.")
    if receipt:
        return result("awaiting_delivery", "Awaiting delivery", "warning", receipt["queued_at"], f"Operator message #{receipt['message_id']} has not been delivered.")
    if activity_at is not None and now - activity_at <= DELIVERY_MAX_AGE:
        return result("active_recently", "Active recently", "ok", activity_at,
                      "The agent recently sent a room message or explicitly acknowledged a request. This does not mark other requests as answered.")
    seen = max(agent["last_seen"] or 0, delivery["updated_at"] if delivery else 0)
    if now - seen > DELIVERY_MAX_AGE:
        return result("connection_stale", "Chat connection stale", "warning", seen or None, "No recent chat contact. The agent’s native process may still be running.")
    if delivery and delivery["state"] in {"starting", "awaiting_confirmation", "joined"}:
        return result("connecting", "Connection awaiting confirmation", "warning", delivery["updated_at"])
    if available and native["phase"] == "working":
        return result("working", "Working", "info", native["phase_since"], "Native turn active. This does not guarantee that every delivered message has been processed.")
    if available and native["phase"] == "idle":
        return result("idle", "Idle", "ok", native["phase_since"])
    return result("connected", "Connected · native state unavailable", "ok", detail="Recent chat contact confirms the connection, not that the agent is actively processing work.")


def register_participant_status_routes(app, db, operator, room):
    @app.get("/api/rooms/{room_id}/participant-status")
    async def statuses(room_id: UUID, request: Request):
        operator(request)
        now, rid = time.time(), str(room_id)
        manager = getattr(app.state, "approval_manager", None)
        with db.connect() as conn:
            state = room(conn, rid)["status"]
            agents = conn.execute("SELECT id,revoked,joined_at,last_seen FROM agents WHERE room_id=?", (rid,)).fetchall()
            deliveries = {r["agent_id"]: dict(r) for r in conn.execute("SELECT s.* FROM agent_delivery_status s JOIN agents a ON a.id=s.agent_id WHERE a.room_id=?", (rid,))}
            approvals = {}
            for row in conn.execute("""SELECT b.agent_id,q.status,q.created_at FROM approval_requests q
                JOIN approval_bridges b ON b.id=q.bridge_id JOIN agents a ON a.id=b.agent_id
                WHERE a.room_id=? AND q.status IN ('pending','decided','submitted')
                AND q.expires_at>? AND q.alive_until>? AND b.expires_at>? AND a.token_hash=b.agent_token_hash
                ORDER BY q.created_at""", (rid, now, now, now)):
                approvals.setdefault(row["agent_id"], dict(row))
            activity = {row["agent_id"]: row["activity_at"] for row in conn.execute("""
                SELECT agent_id,MAX(activity_at) AS activity_at FROM (
                    SELECT a.id AS agent_id,m.created_at AS activity_at FROM agents a
                    JOIN messages m ON m.room_id=a.room_id AND m.sender=a.handle
                    WHERE a.room_id=? AND m.created_at>=COALESCE(a.joined_at,0)
                    UNION ALL
                    SELECT a.id,r.acknowledged_at FROM agents a
                    JOIN message_receipts r ON r.agent_id=a.id
                    WHERE a.room_id=? AND r.acknowledged_at>=COALESCE(a.joined_at,0)
                ) GROUP BY agent_id""", (rid, rid))}
            receipts = {}
            for row in conn.execute("""SELECT r.* FROM message_receipts r JOIN agents a ON a.id=r.agent_id
                JOIN messages m ON m.id=r.message_id WHERE a.room_id=? AND m.sender='operator'
                AND r.acknowledged_at IS NULL AND r.answered_at IS NULL AND r.queued_at>=COALESCE(a.joined_at,0)
                ORDER BY CASE WHEN r.delivered_at IS NULL THEN 1 ELSE 0 END, r.delivered_at,r.queued_at""", (rid,)):
                receipts.setdefault(row["agent_id"], []).append(dict(row))
            # A rotated or reassigned invitation must never inherit another session's telemetry.
            bindings = {r["agent_id"]: dict(r) for r in conn.execute("""SELECT s.agent_id,s.session_id,s.agent_token_hash
                FROM approval_sessions s JOIN agents a ON a.id=s.agent_id WHERE a.room_id=? AND s.agent_token_hash=a.token_hash""", (rid,))}
        output = {}
        for a in agents:
            native = manager.participant_status(a["id"], bindings.get(a["id"]), now) if manager and hasattr(manager, "participant_status") else None
            recent_activity = activity.get(a["id"])
            outstanding = receipts.get(a["id"], [])
            # Old missing receipts describe old requests, not today's agent activity.
            # A newer request must still be tracked even when an older receipt is missing.
            current = next((r for r in outstanding if recent_activity is None or
                            (r["delivered_at"] if r["delivered_at"] is not None else r["queued_at"]) > recent_activity), None)
            status = participant_status(a, state, deliveries.get(a["id"]), approvals.get(a["id"]), current, native,
                                        now=now, warning_seconds=app.state.settings.status_ack_warning_seconds,
                                        activity_at=recent_activity)
            status["unacknowledged_count"] = len(outstanding)
            if outstanding:
                summary = f"{len(outstanding)} operator request(s) still lack acknowledgement; oldest is #{outstanding[0]['message_id']}. Receipts are unchanged."
                status["detail"] = " ".join(filter(None, [status["detail"], summary]))
            output[a["id"]] = status
        return {"statuses": output, "server_time": now, "ack_warning_seconds": app.state.settings.status_ack_warning_seconds}
