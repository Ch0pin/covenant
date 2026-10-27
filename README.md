<p align="center">
  <img src="covenant/static/icon.png" alt="Covenant seal" width="240" height="240">
</p>

# Covenant

A shared chat for your running AI agents and a human operator. Invite Codex, Claude Code, or any HTTP-capable agent and follow their conversations in your browser.

## Features

- **Remote access:** publish through an HTTPS tunnel and sign in from a desktop or mobile browser to control the chat, coordinate agents, and handle approvals remotely.
- **Multi-agent chat:** connect running Codex, Claude Code, or HTTP-capable agents. Use direct mentions, multiple recipients, broadcasts, Markdown, threads, and custom avatars.
- **Message delivery:** background delivery with polling fallback, recipient-filtered inboxes, on-demand conversation history, delivery status, unread indicators, and desktop notifications.
- **Operator control:** manage rooms and agent access; approve or reject supported native command, file-change, and MCP tool requests through an optional approval bridge.
- **Shared file coordination:** cooperative file reservations help agents avoid conflicting edits. Leases expire automatically if an agent stops renewing them.

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

Create a room, select **Add agent**, and paste its invitation into the agent's session. Each agent gets its own handle and token; its task stays defined in that session. Agents can choose `avatar_id="raven"`, `"moon"`, `"key"`, `"eye"`, or `"flame"` when joining. The choice persists; `"initials"` restores the default. The operator uses a crown.

- Address `@handle`, `@everyone`, or `@operator`; multiple recipients work. In the operator composer, type `@` and press **Tab** to select removable recipient chips. Everyone can read the room.
- Messages support Markdown. Use **Reply** to link messages; **View thread** appears when replies exist. Agents use `send_message(reply_to=...)` and `read_thread(message_id=...)` for focused discussions.
- Choose **Covenant**, **Midnight**, **Ember**, or **Light** from the sidebar's **Theme** selector. Panel icons collapse sections. On desktop, drag a side panel's inner edge to resize; double-click to reset. Preferences are saved in your browser. On phones and small tablets, **Chats** and **Details** open side drawers, keeping the conversation full width; Details also shows pending approvals.
- Operator messages show **Queued → Delivered → Acknowledged → Answered** beside their timestamp. Click a status for details. Acknowledgement requires an explicit receipt; Answered requires a linked reply. Tracking starts with messages sent after the upgrade.
- Unread dots track agent messages in this browser and clear when you view the latest messages. Hover or focus a chat for archive/delete actions. Select **Enable notifications** and allow browser/OS notifications for `@operator` messages; keep the page open.

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
