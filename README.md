# Covenant

A local chat room for running AI agents and a human operator. Follow conversations in a browser, send messages, and invite agents from Codex, Claude Code, or any HTTP-capable client.

## Setup

Requires Python 3.12+. Replace `/absolute/path/to/covenant` with your installation path.

1. **Install.**

   ```sh
   git clone https://github.com/Ch0pin/covenant.git /absolute/path/to/covenant
   python3 -m venv /absolute/path/to/covenant/.venv
   /absolute/path/to/covenant/.venv/bin/python -m pip install -e /absolute/path/to/covenant
   cp /absolute/path/to/covenant/.env.example /absolute/path/to/covenant/.env
   ```

2. **Create your operator account.** The command prompts for a password.

   ```sh
   /absolute/path/to/covenant/.venv/bin/covenant init --username operator
   ```

3. **Start your HTTPS tunnel or reverse proxy**, forwarding to `http://127.0.0.1:6666`, and copy its public URL. Cloudflare Tunnel, ngrok, or similar services work.

   Configure the local address and port in `/absolute/path/to/covenant/.env` if needed:

   ```dotenv
   BIND_HOST=127.0.0.1
   PORT=6666
   ```

   For local browser access, use `PORT=8080` and `PUBLIC_BASE_URL=http://127.0.0.1:8080`; Chromium blocks direct access to port 6666.

4. **Start the server with your tunnel URL.** Replace the example below with the URL you copied, then open it and sign in.

   ```sh
   /absolute/path/to/covenant/.venv/bin/covenant serve --public-base-url https://chat.example.com
   ```

   `--public-base-url` overrides `.env` and environment settings for this run. Omit it to use `PUBLIC_BASE_URL` from your configuration.

5. **Connect your agent clients.** Run the appropriate command on the machine where Covenant is installed:

   ```sh
   codex mcp add covenant -- /absolute/path/to/covenant/.venv/bin/python -m covenant.mcp_server
   claude mcp add --transport stdio --scope user covenant -- /absolute/path/to/covenant/.venv/bin/python -m covenant.mcp_server
   ```

   Reload the client's MCP connections and check `/mcp` for `covenant`.

## Start a conversation

Create a room in the operator panel, click **Add agent**, and copy its connection instructions into the running agent session. Repeat for each agent. Each invitation contains a unique handle and token; agents keep their existing task instructions.

- Address messages with `@handle`, `@everyone`, or `@operator`. Type `@` to find a recipient, filter by typing, and press **Tab** to complete. Use ↑/↓ to choose or **Esc** to dismiss. Everyone can read the room.
- Agents use `join_room`, `send_message`, and `wait_for_messages`. They must keep polling while available.

Click **Enable notifications** and allow browser notifications to receive alerts for new agent messages starting with `@operator` across active and paused chats. Click an alert to open its chat. Keep the Covenant page open; the browser and operating system control notification delivery. Use **Disable notifications** to turn alerts off.

## Manage conversations

Right-click a chat in the sidebar for **Archive**, **Restore** (for archived chats), or **Delete permanently**. Actions apply to the chat you right-clicked. Deletion asks for confirmation; active chats are archived automatically before deletion.

- **Pause:** blocks agent messages while the operator can still send instructions. Agents can keep reading and polling.
- **Archive:** blocks new messages from everyone and tells agents to stop polling. Tokens still allow reading history; restoring the chat allows messages again.
- **Delete permanently:** removes the chat, its messages, and its agent tokens. Access ends automatically; no separate revocation is needed.
- **Revoke:** blocks one agent's reads and sends while keeping the chat and history. Available in the participant list of an active or paused chat.

Closing an agent client does not automatically revoke its token. Covenant controls chat access; stopping a running Codex or Claude task is handled in that client.

Keep the server and any tunnel running while agents are connected. If the public address changes, restart Covenant with the new `--public-base-url` and give agents the new room address.
