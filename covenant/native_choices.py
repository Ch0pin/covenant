"""Supported native choices, derived from immutable harness request details."""
import json


def codex_choices(kind, details):
    choices = []
    def add(identifier, label, description, result, decision="approve"):
        choices.append(dict(id=identifier, label=label, description=description, result=result, decision=decision))
    if kind == "tool":
        meta = details.get("_meta") or {}
        if not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call":
            return choices
        persist = meta.get("persist", [])
        persist = [persist] if isinstance(persist, str) else persist if isinstance(persist, list) else []
        for scope, label, description in (
            ("session", "Allow for this session", "Codex remembers this native tool approval scope for this session."),
            ("always", "Allow and remember", "Codex saves this native tool approval scope for future sessions."),
        ):
            if scope in persist:
                add(scope, label, description, {"action": "accept", "content": None, "_meta": {"persist": scope}})
    elif kind in {"command", "file"}:
        advertised = details.get("availableDecisions")
        allowed = advertised if isinstance(advertised, list) else ["accept", "acceptForSession", "decline", "cancel"] if advertised is None else []
        if "acceptForSession" in allowed and details.get("kind") != "humanCommand":
            add("session", "Allow for this session", "Codex remembers the native approval scope for this session.", {"decision": "acceptForSession"})
        if "cancel" in allowed:
            add("cancel", "Reject and interrupt turn", "Reject this request and interrupt the current Codex turn.", {"decision": "cancel"}, "reject")
        if kind == "command" and details.get("kind") != "humanCommand":
            for i, value in enumerate(allowed):
                if not isinstance(value, dict): continue
                amendment = value.get("acceptWithExecpolicyAmendment", {}).get("execpolicy_amendment") if isinstance(value.get("acceptWithExecpolicyAmendment"), dict) else None
                if set(value) == {"acceptWithExecpolicyAmendment"} and isinstance(amendment, list) and amendment and all(isinstance(v, str) for v in amendment):
                    add(f"command_rule_{i}", "Allow and save command rule", "Save the native command-prefix rule: " + json.dumps(amendment), {"decision": value})
                policy = value.get("applyNetworkPolicyAmendment")
                rule = policy.get("network_policy_amendment") if isinstance(policy, dict) else None
                if set(value) == {"applyNetworkPolicyAmendment"} and isinstance(rule, dict) and rule.get("action") in {"allow", "deny"} and isinstance(rule.get("host"), str):
                    action = rule["action"]
                    add(f"network_rule_{i}", ("Allow" if action == "allow" else "Deny") + " and save host rule",
                        "Save the native network rule: " + json.dumps(rule), {"decision": value}, "approve" if action == "allow" else "reject")
    return choices


def public_choices(harness, kind, details):
    return [{k: v for k, v in option.items() if k != "result"} for option in codex_choices(kind, details)] if harness == "codex" else []


def selected_choice(harness, kind, details, identifier):
    return next((c for c in codex_choices(kind, details) if c["id"] == identifier), None) if harness == "codex" else None
