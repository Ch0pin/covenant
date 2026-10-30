import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database: Path = ROOT / "data" / "covenant.sqlite3"
    bind_host: str = "127.0.0.1"
    port: int = 6666
    public_base_url: str = "https://example.trycloudflare.com"
    status_ack_warning_seconds: int = 120

    def __post_init__(self):
        if not 1 <= self.port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if not 30 <= self.status_ack_warning_seconds <= 86400:
            raise ValueError("STATUS_ACK_WARNING_SECONDS must be between 30 and 86400")
        u = urlsplit(self.public_base_url)
        if (u.scheme not in {"http", "https"} or not u.hostname or u.username
                or u.password or u.path not in {"", "/"} or u.query or u.fragment):
            raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin without a path")
        if u.scheme == "http" and u.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Public addresses must use HTTPS")
        _ = u.port  # Validate malformed port values.
        object.__setattr__(self, "public_base_url", self.public_base_url.rstrip("/"))

    @property
    def allowed_origins(self):
        return {self.public_base_url, f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}"}

    @classmethod
    def load(cls, *, public_base_url: str | None = None):
        values = {}
        env_file = ROOT / ".env"
        if env_file.exists():
            for raw in env_file.read_text().splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
        values.update(os.environ)
        if public_base_url is not None:
            values["PUBLIC_BASE_URL"] = public_base_url
        db = Path(values.get("DATABASE_PATH", str(ROOT / "data" / "covenant.sqlite3")))
        if not db.is_absolute():
            db = ROOT / db
        return cls(database=db, bind_host=values.get("BIND_HOST", "127.0.0.1"),
                   port=int(values.get("PORT", "6666")),
                   status_ack_warning_seconds=int(values.get("STATUS_ACK_WARNING_SECONDS", "120")),
                   public_base_url=values.get("PUBLIC_BASE_URL", "https://example.trycloudflare.com"))
