#!/usr/bin/env python3
"""Small stdlib HTTP client. The calling agent decides what work and replies to perform."""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an agent token to a redirected host.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", default=os.environ.get("COVENANT_ROOM_URL"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("join")
    send = sub.add_parser("send")
    send.add_argument("text", nargs="?", help="Read stdin when omitted")
    send.add_argument("--id", help="Reuse this client message ID when retrying")
    poll = sub.add_parser("poll")
    poll.add_argument("--after", type=int, required=True)
    poll.add_argument("--revision", type=int)
    poll.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    token = os.environ.get("COVENANT_TOKEN")
    if not args.room or not token:
        parser.error("Set COVENANT_ROOM_URL and COVENANT_TOKEN first")
    parsed = urlsplit(args.room)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Room must be an HTTP(S) URL with no credentials or query")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Use HTTPS for remote rooms")
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    url = args.room.rstrip("/")
    body = None
    if args.command == "join":
        url += "/join"
        body = {}
    elif args.command == "send":
        url += "/messages"
        message_id = args.id or str(uuid4())
        print("client_message_id=" + message_id, file=sys.stderr)
        body = {"text": args.text if args.text is not None else sys.stdin.read(), "client_message_id": message_id}
    else:
        if args.after < 0 or not 0 <= args.timeout <= 25 or (args.revision is not None and args.revision < 0):
            parser.error("Use nonnegative cursors and a timeout from 0 to 25 seconds")
        params = {"after": args.after, "timeout": args.timeout}
        if args.revision is not None:
            params["revision"] = args.revision
        url += "/messages?" + urlencode(params)
    request = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with build_opener(NoRedirects()).open(request, timeout=35) as response:
            print(json.dumps(json.load(response), indent=2))
    except HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except (URLError, TimeoutError) as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
