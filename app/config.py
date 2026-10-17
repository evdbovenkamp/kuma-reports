"""Runtime configuration, read from environment variables."""
import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        file_path = os.environ.get(f"{name}_FILE")  # docker secrets support
        if file_path and Path(file_path).is_file():
            return Path(file_path).read_text().strip()
        return default
    return value


DATA_DIR = Path(_env("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Kuma database. Either a full SQLAlchemy URL in KUMA_DB_URL, or the parts below.
#   SQLite (Kuma 1.x / 2.x default):  KUMA_DB_TYPE=sqlite  KUMA_DB_PATH=/kuma/kuma.db
#   Embedded MariaDB (Kuma 2.x):      KUMA_DB_TYPE=mariadb KUMA_DB_SOCKET=/kuma/run/mariadb.sock
#   External MariaDB/MySQL:           KUMA_DB_TYPE=mariadb KUMA_DB_HOST=db KUMA_DB_PORT=3306
KUMA_DB_URL = _env("KUMA_DB_URL")
KUMA_DB_TYPE = (_env("KUMA_DB_TYPE", "auto") or "auto").lower()
KUMA_DATA_DIR = Path(_env("KUMA_DATA_DIR", "/kuma"))
KUMA_DB_PATH = _env("KUMA_DB_PATH", str(KUMA_DATA_DIR / "kuma.db"))
KUMA_DB_SOCKET = _env("KUMA_DB_SOCKET", str(KUMA_DATA_DIR / "run" / "mariadb.sock"))
KUMA_DB_HOST = _env("KUMA_DB_HOST")
KUMA_DB_PORT = int(_env("KUMA_DB_PORT", "3306"))
KUMA_DB_NAME = _env("KUMA_DB_NAME", "kuma")
KUMA_DB_USER = _env("KUMA_DB_USER", "reporter")
KUMA_DB_PASSWORD = _env("KUMA_DB_PASSWORD", "")

# Public URL of Uptime Kuma, used for links in reports (optional).
KUMA_BASE_URL = (_env("KUMA_BASE_URL", "") or "").rstrip("/")

TIMEZONE = ZoneInfo(_env("TZ", "UTC") or "UTC")
IMPORT_INTERVAL = int(_env("IMPORT_INTERVAL", "300"))
HISTORY_KEEP_DAYS = int(_env("HISTORY_KEEP_DAYS", "400"))  # archived report HTML

# Authentication: "form" (built-in login) or "none" (e.g. behind an auth proxy).
AUTH_MODE = (_env("AUTH_MODE", "form") or "form").lower()
ADMIN_USERNAME = _env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")

BASE_URL = (_env("BASE_URL", "") or "").rstrip("/")
# Serve the GUI under a sub-path, e.g. "/reports" behind https://kuma.example.com/reports
BASE_PATH = "/" + (_env("BASE_PATH", "") or "").strip("/") if (_env("BASE_PATH", "") or "").strip("/") else ""
LOG_LEVEL = (_env("LOG_LEVEL", "INFO") or "INFO").upper()


def _secret_key() -> str:
    key = _env("SECRET_KEY")
    if key:
        return key
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_urlsafe(48)
    key_file.write_text(key)
    key_file.chmod(0o600)
    return key


SECRET_KEY = _secret_key()
VERSION = "0.1.2"
