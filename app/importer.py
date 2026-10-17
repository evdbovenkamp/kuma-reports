"""Incrementally copies monitors, tags, state changes and daily stats from Kuma into the local DB.

Keeping our own copy means reports keep working beyond Kuma's data retention
(keepDataPeriodDays) and for monitors that were deleted in Kuma.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import delete, select

from . import db
from .kuma import _to_dt, reader

log = logging.getLogger(__name__)
_lock = threading.Lock()


def sync_monitors(session) -> int:
    k = reader()
    rows = k.monitors()
    seen = set()
    for r in rows:
        seen.add(r["id"])
        m = session.get(db.Monitor, r["id"]) or db.Monitor(id=r["id"])
        m.name = r.get("name") or f"#{r['id']}"
        m.type = r.get("type") or "?"
        m.parent_id = r.get("parent")
        m.active = bool(r.get("active", True))
        m.description = r.get("description")
        m.url = r.get("url")
        m.interval = r.get("interval")
        m.created_at = _to_dt(r.get("created_date"))
        m.tags = r["tags"]
        m.notification_ids = r["notification_ids"]
        m.deleted = False
        m.updated_at = db.utcnow()
        session.add(m)
    for m in session.scalars(select(db.Monitor).where(db.Monitor.deleted.is_(False))):
        if m.id not in seen:
            m.deleted = True
            m.active = False
    session.execute(delete(db.Tag))
    for t in k.tags():
        session.add(db.Tag(id=t["id"], name=t["name"], color=t.get("color")))
    return len(rows)


def sync_state_changes(session) -> int:
    k = reader()
    last_id = int(db.kv_get(session, "last_heartbeat_id", "0") or 0)
    max_id = k.max_heartbeat_id()
    if max_id < last_id:
        # Kuma database was reset or restored from an older backup.
        log.warning("Kuma heartbeat ids went backwards (%s < %s); rescanning", max_id, last_id)
        last_id = 0
    total = 0
    while True:
        batch = k.important_heartbeats(last_id)
        if not batch:
            break
        for hb in batch:
            session.merge(db.StateChange(id=hb["id"], monitor_id=hb["monitor_id"], status=hb["status"],
                                         time=hb["time"], msg=(hb["msg"] or "")[:2000]))
        last_id = batch[-1]["id"]
        total += len(batch)
        db.kv_set(session, "last_heartbeat_id", str(last_id))
        session.commit()
    return total


def sync_daily_stats(session) -> int:
    k = reader()
    first = db.kv_get(session, "daily_stats_done") is None
    since = (db.utcnow() - timedelta(days=400 if first else 2)).date()
    ids = list(session.scalars(select(db.Monitor.id).where(db.Monitor.deleted.is_(False))))
    rows = k.daily_stats(since, ids)
    for r in rows:
        session.merge(db.DailyStat(**r))
    db.kv_set(session, "daily_stats_done", "1")
    return len(rows)


def run_import() -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": True}
    try:
        with db.SessionLocal() as session:
            try:
                n_mon = sync_monitors(session)
                session.commit()
                n_sc = sync_state_changes(session)
                n_daily = sync_daily_stats(session)
                db.kv_set(session, "last_import_at", db.utcnow().isoformat())
                db.kv_set(session, "last_import_error", None)
                db.kv_set(session, "kuma_version", reader().version_hint())
                session.commit()
                result = {"monitors": n_mon, "state_changes": n_sc, "daily_rows": n_daily}
                log.info("Import done: %s", result)
                return result
            except Exception as exc:  # keep the scheduler alive, surface the error in the GUI
                session.rollback()
                log.exception("Import failed")
                db.kv_set(session, "last_import_error", f"{db.utcnow().isoformat()} {exc}")
                session.commit()
                return {"error": str(exc)}
    finally:
        _lock.release()
