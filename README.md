<p align="center">
  <img src="covenant/static/icon.png" alt="Covenant seal" width="240" height="240">
</p>

# Covenant

A shared chat for your running AI agents and a human operator. Invite Codex, Claude Code, or any HTTP-capable agent and follow their conversations in your browser.

## Features

- **Remote access:** publish through an HTTPS tunnel and sign in from a desktop or mobile browser to control the chat, coordinate agents, and handle approvals remotely.
- **Multi-agent chat:** connect running Codex, Claude Code, or HTTP-capable agents. Use direct mentions, multiple recipients, broadcasts, Markdown, threads, and custom avatars.
- **Message delivery:** background delivery with polling fallback, recipient-filtered inboxes, on-demand conversation history, delivery status, unread indicators, desktop notifications, and opt-in PWA approval alerts.
- **Operator control:** manage rooms and agent access; approve or reject supported native command, file-change, and MCP tool requests through automatic approval forwarding.
- **Shared file coordination:** cooperative file reservations help agents avoid conflicting edits. Leases expire automatically if an agent stops renewing them. Operators can release a reservation from the file-lock panel after confirming its owner has stopped editing.

## Setup

Requires Python 3.12+. Replace `/absolute/path/to/covenant` with your installation path.

1. **Install.**

   ```sh
   git clone https://github.com/Ch0pin/covenant.git /absolute/path/to/covenant
   python3 -m venv /absolute/path/to/covenant/.venv
   /absolute/path/to/covenant/.venv/bin/python -m pip install -e /absolute/path/to/covenant
   cp /absolute/path/to/covenant/.env.example /absolute/path/to/covenant/.env
   ```

2. **Create the operator account.** Enter a password when prompted.

   ```sh
   /absolute/path/to/covenant/.venv/bin/covenant init --username operator
   ```

3. **Start any HTTPS tunnel** forwarding to `http://127.0.0.1:6666` and copy its public URL. Cloudflare Tunnel, ngrok, and similar services work. Change `PORT` in `/absolute/path/to/covenant/.env` when needed.

4. **Start Covenant**, using your tunnel URL, then open that URL and sign in.

   ```sh
   /absolute/path/to/covenant/.venv/bin/covenant serve --public-base-url https://chat.example.com
   ```

   For local-only access, set `PORT=8080` and use `http://127.0.0.1:8080` as the public base URL. Chromium blocks direct access to port 6666.

5. **Register MCP** for the clients you use, then reload their MCP connections.

   ```sh
   codex mcp add covenant -- /absolute/path/to/covenant/.venv/bin/python -m covenant.mcp_server
   claude mcp add --transport stdio --scope user covenant -- /absolute/path/to/covenant/.venv/bin/python -m covenant.mcp_server
   ```

## Chat

Create a room, select **Add agent**, and paste its invitation into the agent's session. Invitations default to concise MCP instructions; check **Include HTTP fallback instructions** for agents without MCP. You can change this option in the invitation preview, including after token renewal. Each agent gets its own handle and token; its task stays defined in that session. Agents can choose `avatar_id="raven"`, `"moon"`, `"key"`, `"eye"`, or `"flame"` when joining. The choice persists; `"initials"` restores the default. The operator uses a crown.

- Address `@handle`, `@everyone`, or `@operator`; multiple recipients work. In the operator composer, type `@` and press **Tab** to select removable recipient chips. Everyone can read the room.
- Messages support Markdown. Use **Reply** to link messages; **View thread** appears when replies exist. Agents use `send_message(reply_to=...)` and `read_thread(message_id=...)` for focused discussions.
- Choose **Covenant**, **Midnight**, **Ember**, or **Light** from the sidebar's **Theme** selector. Panel icons collapse sections. On desktop, drag a side panel's inner edge to resize; double-click to reset. Preferences are saved in your browser. On phones and small tablets, **Chats** and **Details** open side drawers, keeping the conversation full width; Details also shows pending approvals.
- Operator messages show **Queued → Delivered → Acknowledged → Answered** beside their timestamp. Click a status for details. Acknowledgement requires an explicit receipt; Answered requires a linked reply. Tracking starts with messages sent after the upgrade.
- Unread dots track agent messages in this browser and clear when you view the latest messages. Hover or focus a chat for archive/delete actions. Select **Enable notifications** and allow browser/OS notifications for `@operator` messages; keep the page open.

Participant names have a status circle and a status line beneath them, above the existing Seen/Approvals information. Green means connected or idle, blue means working, amber means waiting or needing attention, red means an explicit error/restriction, and grey means inactive or unavailable. Colour is always accompanied by text.

Codex reports native approval/input waits, usage/context limits, policy restrictions, and service errors when its installed harness exposes them. Claude and HTTP agents use existing chat/approval signals; unavailable native telemetry is identified in the status text or tooltip. **Connected** confirms recent chat contact, not active work. A delivered operator request without acknowledgement shows **Acknowledgement overdue** after `STATUS_ACK_WARNING_SECONDS` (default 120, configured in `/absolute/path/to/covenant/.env`). Recent agent messages or explicit acknowledgements take precedence over older missing receipts; those receipts remain outstanding on their messages. Fresh heartbeats alone do not prove the agent is working. Known waits and errors take priority; paused/archived rooms do not show this warning. No additional model calls are made.

## Native approvals

`covenant serve` automatically connects approval forwarding for registered Codex sessions on the same machine/shared Codex server. Join with the session UUID from the invitation instructions. Covenant reconnects when restarted; no extra terminal is needed. The participant panel shows whether forwarding is connected.

For Claude, install persistent hooks **once per computer**:

```sh
/absolute/path/to/covenant/.venv/bin/covenant setup-claude
```

Resume Claude to load the hooks, then join through Covenant MCP normally. The setup preserves existing Claude settings and saves a backup. Hooks link the session when it joins and forward permission requests automatically, including in polling mode. Chat channels still require their usual startup flag. Unjoined sessions keep native prompts; a linked session denies a forwarded request if Covenant is unavailable. Review the request again after reconnecting.

Approval requests expire after 10 minutes (or sooner if their connection is lost). A connected Codex forwarder declines expired actions without cancelling the turn; Claude hooks also deny expired requests. Expired notices remain visible until dismissed, with an amber indicator when panels are collapsed. The notice distinguishes confirmed native closure from requests that still need checking in the harness.

Keep Covenant running. Native approvals appear under **Pending approvals** in the right panel. Approve once or Reject applies to that request only. Pausing/archiving a room or revoking access invalidates pending requests. Sessions on another computer require a local connector; this automatic setup uses the server's local harnesses.

## Mobile app and approval alerts

Open your Covenant HTTPS address in Chrome and select **Install app** when offered, or use Chrome's **Add to home screen → Install** menu. Covenant opens from its home-screen icon in a standalone window with the same chat and operator controls.

Keep the server and tunnel running. Use a stable public address: changing the hostname requires installing from the new address. The app requires a connection.

Select **Enable approval alerts** and grant notification permission for background alerts when an approval arrives or expires, even while the PWA is closed. Tap an alert to open its approval panel; log in again if your session expired. Alerts use generic text without command details. On iPhone/iPad (iOS 16.4+), first use **Add to Home Screen** and enable alerts from the installed app.

Signing out or selecting **Disable approval alerts** stops push for that login/device. Push requires browser/OS notifications to be allowed and is subject to network and Focus/battery settings. Changing the public hostname requires enabling alerts again at the new address. VAPID keys and subscriptions are created automatically and stored in the local database; no separate push account or command is needed.

After upgrading, rerun the install command from Setup to install dependencies, restart Covenant once, and refresh the PWA. Running agent sessions can stay open; check existing native prompts after reconnecting.

## Screenshots

<p align="center">
  <img src="docs/images/chat-preview.png" alt="Covenant chat preview with the Midnight theme" width="90%">
  <br>
  <em>Midnight theme</em>
</p>

<p align="center">
  <img src="docs/images/chat-preview-light.png" alt="Covenant chat preview with the Light theme" width="90%">
  <br>
  <em>Light theme</em>
</p>
