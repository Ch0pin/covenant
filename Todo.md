# Future tasks

- [x] Add participant status indicators to the existing right-panel participant list.
  - Place a small coloured status circle beside each participant’s name; show the status label, reason, and elapsed time below the name. Include an accessible text label so status is understandable without colour.
  - Show connection health separately from responsiveness: connected, awaiting acknowledgement, possibly stale, blocked, and disconnected.
  - Use existing connection, delivery, acknowledgement, and pending approval data; show a reason and elapsed time for each status.
  - Make the acknowledgement warning threshold configurable (initial suggestion: 2 minutes). Label overdue receipts as “Acknowledgement overdue”; recent agent messages/explicit acknowledgements take precedence over older receipts without marking them answered. Heartbeats alone do not establish agent activity.
  - Show explicit harness-reported reasons: usage limit reached (with reset time when available), context limit reached, waiting for approval, request refused, connection failure, and service/API error. Keep request outcomes separate from session health: a refused request can leave the agent connected.
  - Display safety-processing or account-verification states when exposed by the harness; label a cybersecurity restriction only when explicitly identified. Use an unavailable/unknown state when there is no supporting signal; missing acknowledgement alone is a message warning, not an agent-health verdict.
  - Collect reasons through supported harness telemetry without extra agent polling or token usage. Verify support per harness/version; Claude may need additional telemetry integration beyond the current MCP channel. Show unavailable telemetry explicitly.
  - Keep the initial version compatible with running Codex and Claude sessions; activation should require only a Covenant server restart and browser refresh, preserving saved chats.

- [ ] Extend Claude native telemetry beyond current chat/approval signals to expose structured usage, context, and refusal reasons where supported; keep unavailable reasons explicit until integrated.
