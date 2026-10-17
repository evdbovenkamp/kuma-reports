"""Read-only access to the Uptime Kuma database (SQLite or MariaDB/MySQL, Kuma 1.x and 2.x)."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import cached_property
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from . import config

log = logging.getLogger(__name__)


@dataclass
class KumaSource:
    kind: str          # sqlite | mariadb
    description: str   # human readable, no secrets
    engine: Engine


def _read_db_config() -> dict | None:
    path = config.KUMA_DATA_DIR / "db-config.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _sqlite_engine(path: str) -> Engine:
    # Open strictly read-only. The data volume itself must stay writable for SQLite in WAL mode
    # (the -shm file), but this process never writes to it.
    def creator():
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=30)
    return create_engine("sqlite://", creator=creator, pool_pre_ping=True)


def _mysql_engine(user: str, password: str, database: str, *, host: str | None = None,
                  port: int = 3306, socket: str | None = None) -> Engine:
    auth = f"{quote_plus(user)}:{quote_plus(password or '')}"
    if socket:
        url = f"mysql+pymysql://{auth}@localhost/{database}?unix_socket={socket}&charset=utf8mb4"
    else:
        url = f"mysql+pymysql://{auth}@{host}:{port}/{database}?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)


def build_source() -> KumaSource:
    """Work out how to reach the Kuma database, preferring explicit settings over discovery."""
    if config.KUMA_DB_URL:
        url = config.KUMA_DB_URL
        kind = "sqlite" if url.startswith("sqlite") else "mariadb"
        safe = url.split("@")[-1] if "@" in url else url
        return KumaSource(kind, f"URL ({safe})", create_engine(url, pool_pre_ping=True))

    db_type = config.KUMA_DB_TYPE
    db_cfg = _read_db_config() if db_type == "auto" else None
    if db_type == "auto":
        if db_cfg and db_cfg.get("type") == "embedded-mariadb":
            db_type = "embedded-mariadb"
        elif db_cfg and db_cfg.get("type") == "mariadb":
            db_type = "mariadb"
        elif config.KUMA_DB_HOST:
            db_type = "mariadb"
        elif Path(config.KUMA_DB_SOCKET).exists() and not Path(config.KUMA_DB_PATH).exists():
            db_type = "embedded-mariadb"
        else:
            db_type = "sqlite"

    if db_type == "sqlite":
        return KumaSource("sqlite", f"SQLite {config.KUMA_DB_PATH}", _sqlite_engine(config.KUMA_DB_PATH))

    if db_type in ("embedded-mariadb", "socket"):
        name = config.KUMA_DB_NAME or "kuma"
        return KumaSource(
            "mariadb", f"embedded MariaDB via {config.KUMA_DB_SOCKET} (user {config.KUMA_DB_USER})",
            _mysql_engine(config.KUMA_DB_USER, config.KUMA_DB_PASSWORD, name, socket=config.KUMA_DB_SOCKET))

    # External MariaDB/MySQL. Host/port/db may come from Kuma's db-config.json; credentials
    # should be a dedicated read-only user, but fall back to Kuma's own when none are given.
    db_cfg = db_cfg or _read_db_config() or {}
    host = config.KUMA_DB_HOST or db_cfg.get("hostname")
    port = int(config.KUMA_DB_PORT or db_cfg.get("port") or 3306)
    name = config.KUMA_DB_NAME or db_cfg.get("dbName") or "kuma"
    user, password = config.KUMA_DB_USER, config.KUMA_DB_PASSWORD
    if not password and db_cfg.get("username"):
        log.warning("No KUMA_DB_PASSWORD set; using Kuma's own (read-write) database credentials")
        user, password = db_cfg["username"], db_cfg.get("password", "")
    return KumaSource("mariadb", f"MariaDB {host}:{port}/{name} (user {user})",
                      _mysql_engine(user, password, name, host=host, port=port))


def _to_dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    s = str(value).replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(s)


class KumaReader:
    def __init__(self, source: KumaSource):
        self.source = source
        self.engine = source.engine

    @cached_property
    def _tables(self) -> dict[str, set[str]]:
        insp = inspect(self.engine)
        return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names()}

    def has_table(self, name: str) -> bool:
        return name in self._tables

    def version_hint(self) -> str:
        if self.has_table("stat_daily"):
            return "2.x"
        return "1.x"

    def ping(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def monitors(self) -> list[dict]:
        cols = self._tables.get("monitor", set())
        wanted = ["id", "name", "type", "parent", "active", "description", "url", "hostname", "interval", "created_date"]
        present = [c for c in wanted if c in cols]
        sql = "SELECT " + ", ".join(f"`{c}`" for c in present) + " FROM monitor"
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text(sql))]
            tags = conn.execute(text(
                "SELECT mt.monitor_id, mt.tag_id, mt.value, t.name, t.color "
                "FROM monitor_tag mt JOIN tag t ON t.id = mt.tag_id")).all()
            notifs = []
            if self.has_table("monitor_notification"):
                notifs = conn.execute(text("SELECT monitor_id, notification_id FROM monitor_notification")).all()
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            r.setdefault("parent", None)
            r["tags"], r["notification_ids"] = [], []
            if not r.get("url") and r.get("hostname"):
                r["url"] = r["hostname"]
        for mid, tid, value, name, color in tags:
            if mid in by_id:
                by_id[mid]["tags"].append({"id": tid, "name": name, "color": color, "value": value or ""})
        for mid, nid in notifs:
            if mid in by_id:
                by_id[mid]["notification_ids"].append(nid)
        return rows

    def tags(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text("SELECT id, name, color FROM tag ORDER BY name"))]

    def notifications(self) -> list[dict]:
        """All notification providers, with their JSON config parsed. Contains secrets!"""
        with self.engine.connect() as conn:
            rows = conn.execute(text("SELECT id, name, active, config FROM notification ORDER BY name")).all()
        out = []
        for nid, name, active, cfg in rows:
            try:
                parsed = json.loads(cfg or "{}")
            except ValueError:
                parsed = {}
            out.append({"id": nid, "name": name or parsed.get("name") or f"#{nid}",
                        "active": bool(active), "type": parsed.get("type"), "config": parsed})
        return out

    def important_heartbeats(self, after_id: int, limit: int = 5000) -> list[dict]:
        sql = text("SELECT id, monitor_id, status, time, msg FROM heartbeat "
                   "WHERE important = 1 AND id > :after ORDER BY id LIMIT :limit")
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"after": after_id, "limit": limit}).all()
        return [{"id": r[0], "monitor_id": r[1], "status": int(r[2]), "time": _to_dt(r[3]), "msg": r[4]}
                for r in rows]

    def max_heartbeat_id(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM heartbeat")).scalar() or 0)

    def daily_stats(self, since: date, monitor_ids: list[int]) -> list[dict]:
        """Per monitor, per UTC day: up/down check counts and response times."""
        out = []
        with self.engine.connect() as conn:
            if self.has_table("stat_daily"):
                ts = int(datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp())
                rows = conn.execute(text(
                    "SELECT monitor_id, timestamp, up, down, ping, ping_max FROM stat_daily "
                    "WHERE timestamp >= :ts"), {"ts": ts}).all()
                for mid, t, up, down, p, pmax in rows:
                    out.append({"monitor_id": mid, "day": datetime.fromtimestamp(t, timezone.utc).date(),
                                "up": up, "down": down, "ping_avg": p or None, "ping_max": pmax or None})
                return out
            # Kuma 1.x: aggregate raw heartbeats, per monitor so the (monitor_id, time) index is used.
            sql = text(
                "SELECT DATE(time) AS d, "
                "SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN status = 0 THEN 1 ELSE 0 END), "
                "AVG(CASE WHEN status = 1 THEN ping END), MAX(CASE WHEN status = 1 THEN ping END) "
                "FROM heartbeat WHERE monitor_id = :m AND time >= :since GROUP BY DATE(time)")
            for mid in monitor_ids:
                for d, up, down, p, pmax in conn.execute(sql, {"m": mid, "since": since.isoformat()}):
                    day = d if isinstance(d, date) else date.fromisoformat(str(d))
                    out.append({"monitor_id": mid, "day": day, "up": int(up or 0), "down": int(down or 0),
                                "ping_avg": float(p) if p is not None else None,
                                "ping_max": float(pmax) if pmax is not None else None})
        return out


_reader: KumaReader | None = None


def reader() -> KumaReader:
    global _reader
    if _reader is None:
        _reader = KumaReader(build_source())
    return _reader


def smtp_notifications() -> list[dict]:
    return [n for n in reader().notifications() if n["type"] == "smtp"]


def split_addresses(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in value.replace(";", ",").split(",") if a.strip()]
