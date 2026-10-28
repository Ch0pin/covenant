import argparse
import getpass
import os
import sys
from uuid import UUID

from .auth import hash_password
from .config import Settings
from .db import Database


def main():
    parser = argparse.ArgumentParser(description="Covenant agent chat")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create the operator account locally")
    init.add_argument("--username", default="operator")
    init.add_argument("--password-stdin", action="store_true", help="Read password from stdin")
    serve = sub.add_parser("serve", help="Run the local server")
    serve.add_argument("--public-base-url", help="Public tunnel URL (overrides PUBLIC_BASE_URL for this run)")
    sub.add_parser("setup-claude", help="Install persistent Claude approval hooks once")
    approvals = sub.add_parser("approvals", help="Forward native harness approvals to the operator panel")
    approvals.add_argument("harness", choices=["codex", "claude"])
    approvals.add_argument("--room", required=True, type=UUID)
    approvals.add_argument("--agent", required=True, help="Existing room handle")
    approvals.add_argument("--session", type=UUID, help="Codex thread UUID; optional Claude session binding")
    # Arguments after -- belong to Claude, including --resume.
    cli_args = sys.argv[1:]
    extra = []
    if cli_args[:1] == ["approvals"] and "--" in cli_args:
        split = cli_args.index("--")
        cli_args, extra = cli_args[:split], cli_args[split + 1:]
    args = parser.parse_args(cli_args)
    args.harness_args = extra
    os.umask(0o077)
    try:
        settings = Settings.load(public_base_url=getattr(args, "public_base_url", None))
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "setup-claude":
        from .claude_approvals import install
        try:
            path, backup = install(settings)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Claude approval hooks installed: {path}")
        if backup:
            print(f"Previous settings preserved: {backup}")
        print("Resume Claude so it loads the hooks, then join a Covenant room normally.")
        return
    db = Database(settings.database)
    if args.command == "approvals":
        from .approval_bridge import launch
        try:
            raise SystemExit(launch(args, settings, db))
        except (ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        except KeyboardInterrupt:
            return
    db.initialize()
    if args.command == "init":
        with db.connect() as conn:
            if conn.execute("SELECT 1 FROM operator").fetchone():
                parser.error("An operator already exists. Existing credentials were preserved.")
        if not 1 <= len(args.username.strip()) <= 64:
            parser.error("Username must contain 1 to 64 characters")
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Operator password (12+ characters): ")
        if len(password) < 12 or len(password) > 1024:
            parser.error("Password must contain 12 to 1024 characters")
        if not args.password_stdin and getpass.getpass("Repeat password: ") != password:
            parser.error("Passwords do not match")
        with db.connect() as conn:
            conn.execute("INSERT INTO operator VALUES (1, ?, ?)",
                         (args.username.strip(), hash_password(password)))
        print(f"Operator {args.username.strip()} created. Start the server with covenant serve.")
    else:
        import uvicorn
        from .app import create_app
        print(f"Local server: http://{settings.bind_host}:{settings.port}")
        print(f"Connection-instruction base URL: {settings.public_base_url}")
        uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port,
                    workers=1, proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False)


if __name__ == "__main__":
    main()
