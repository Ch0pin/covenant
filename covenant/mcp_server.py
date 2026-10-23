"""Session-local stdio MCP adapter for Covenant's authenticated HTTP API."""
import asyncio
import json
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_room_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ToolError("Use the HTTP(S) room URL from the invitation, with no credentials or query string.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ToolError("Remote rooms require HTTPS.")
    try:
        _ = parsed.port
        if parsed.path != "/" + str(UUID(parsed.path.lstrip("/"))):
            raise ValueError
    except ValueError:
        raise ToolError("The room URL must end in a single room UUID.") from None
    return value


def request_json(room_url: str, token: str, suffix: str, body=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    request = Request(room_url + suffix, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
    try:
        # Ignore unrelated shell proxy settings and never forward a token on redirects.
        with build_opener(NoRedirects(), ProxyHandler({})).open(request, timeout=35) as response:
            return json.load(response)
    except HTTPError as exc:
        details = {401: "Agent access is invalid or revoked. Ask the operator for a fresh invitation.",
                   403: "The server rejected this request.",
                   404: "Conversation not found. Check the room URL.",
                   409: "The room is paused/archived, or this message ID was used for different text.",
                   422: "Invalid message or recipient. Use a current @handle, @operator, or @everyone.",
                   429: "Request limit reached. Wait before retrying."}
        if 300 <= exc.code < 400:
            detail = "Redirect refused. Use the current room URL supplied by the operator."
        else:
            detail = details.get(exc.code, "The server is unavailable. Retry with backoff.")
        raise ToolError(f"HTTP {exc.code}: {detail}") from None
    except (URLError, TimeoutError, OSError):
        raise ToolError("Cannot reach Covenant. Check the server/tunnel and retry with backoff.") from None
    except (ValueError, UnicodeError):
        raise ToolError("Covenant returned an unexpected response. Check the room URL.") from None


def create_mcp_server():
    server = MCPServer("covenant", instructions=(
        "Join using the operator's invitation. Retain your existing task instructions. "
        "Send messages to @handle, @operator, or @everyone. Call wait_for_messages while "
        "available; save next_cursor and revision after processing. Ignore your own posts "
        "as response triggers. Respect room pause/archive status. Tokens stay private."),
        log_level="WARNING")
    connection = None
    join_lock = asyncio.Lock()

    @server.tool(annotations=ToolAnnotations(destructive_hint=False))
    async def join_room(
        room_url: Annotated[str, Field(max_length=2048)],
        access_token: Annotated[str, Field(min_length=32, max_length=256)],
    ) -> dict:
        """Join with the room URL and agent token from the invitation. Returns identity,
        participants, recent history, next_cursor and revision. One connection per MCP
        process; rejoin after a process restart. Preserve a saved cursor on reconnect.
        The token is held in this process's memory and is never returned by this tool.
        """
        nonlocal connection
        url = validate_room_url(room_url)
        if any(char.isspace() for char in access_token):
            raise ToolError("The access token must contain no whitespace.")
        async with join_lock:
            result = await asyncio.to_thread(request_json, url, access_token, "/join", {})
            connection = (url, access_token)
        return result

    def current_connection():
        if connection is None:
            raise ToolError("Call join_room with your invitation before sending or waiting.")
        return connection

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    async def send_message(
        text: Annotated[str, Field(min_length=1, max_length=16000)],
        client_message_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict:
        """Post text beginning with one or more recipients, e.g. @alice @operator FYI.
        Use @handle, @operator, or @everyone. Supply a new unique
        client_message_id for each new message, and reuse the SAME ID for retries.
        Sender identity comes from the token supplied to join_room.
        """
        url, token = current_connection()
        return await asyncio.to_thread(request_json, url, token, "/messages", {
            "text": text, "client_message_id": client_message_id})

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    async def wait_for_messages(
        after: Annotated[int, Field(ge=0)],
        revision: Annotated[int | None, Field(ge=0)] = None,
        timeout: Annotated[float, Field(ge=0, le=25)] = 25,
    ) -> dict:
        """Wait up to 25 seconds for shared room messages or status changes. Pass your
        saved next_cursor as after and your saved revision. Save returned cursors only
        after processing. Repeat even on an empty response; use timeout=0 to read now.
        While paused, stop starting room-driven work and keep polling. Stop on archived.
        This call receives messages; the calling agent decides when to act and reply.
        """
        url, token = current_connection()
        params = {"after": after, "timeout": timeout}
        if revision is not None:
            params["revision"] = revision
        return await asyncio.to_thread(request_json, url, token, "/messages?" + urlencode(params))

    return server


def main():
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
