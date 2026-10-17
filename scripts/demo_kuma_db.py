#!/usr/bin/env python3
"""Create a fake Uptime Kuma (1.x-style SQLite) database with realistic demo data.

Used for screenshots and for trying Kuma Reports without a real Kuma:

    python scripts/demo_kuma_db.py /tmp/demo-kuma/kuma.db
    DATA_DIR=/tmp/demo-data KUMA_DB_TYPE=sqlite KUMA_DB_PATH=/tmp/demo-kuma/kuma.db AUTH_MODE=none \\
      uvicorn app.main:app --port 8080
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAYS = 75
STEP = timedelta(minutes=5)
rng = random.Random(42)

SCHEMA = """
CREATE TABLE monitor (id INTEGER PRIMARY KEY, name TEXT, active INTEGER DEFAULT 1, user_id INTEGER,
  interval INTEGER DEFAULT 60, url TEXT, type TEXT, hostname TEXT, parent INTEGER, description TEXT,
  created_date DATETIME);
CREATE TABLE tag (id INTEGER PRIMARY KEY, name TEXT, color TEXT, created_date DATETIME);
CREATE TABLE monitor_tag (id INTEGER PRIMARY KEY, monitor_id INTEGER, tag_id INTEGER, value TEXT);
CREATE TABLE notification (id INTEGER PRIMARY KEY, name TEXT, config TEXT, active INTEGER DEFAULT 1,
  user_id INTEGER, is_default INTEGER DEFAULT 0);
CREATE TABLE monitor_notification (id INTEGER PRIMARY KEY, monitor_id INTEGER, notification_id INTEGER);
CREATE TABLE heartbeat (id INTEGER PRIMARY KEY, important INTEGER DEFAULT 0, monitor_id INTEGER,
  status INTEGER, msg TEXT, time DATETIME, ping INTEGER, duration INTEGER DEFAULT 0, down_count INTEGER DEFAULT 0);
CREATE INDEX hb_monitor_time ON heartbeat (monitor_id, time);
CREATE INDEX hb_important ON heartbeat (important);
"""

# (name, type, parent, url, base_ping, reliability, tags, notifications)
GROUPS = {1: "Websites", 2: "Infrastructure", 3: "Customers", 4: "Acme Corp", 5: "Globex", 6: "Mail"}
GROUP_PARENT = {4: 3, 5: 3, 6: 2}
MONITORS = [
    ("www.example.com", "http", 1, "https://www.example.com", 120, 0.995, [(1, ""), (3, "")], [1]),
    ("shop.example.com", "keyword", 1, "https://shop.example.com", 240, 0.98, [(1, "")], [1]),
    ("blog.example.com", "http", 1, "https://blog.example.com", 180, 0.99, [], [1]),
    ("api.example.com", "http", 1, "https://api.example.com/health", 60, 0.97, [(1, "")], [1]),
    ("Core router", "ping", 2, "10.0.0.1", 2, 0.999, [(1, "")], [1]),
    ("Hypervisor 01", "ping", 2, "10.0.0.11", 1, 0.998, [(1, "")], [1]),
    ("Hypervisor 02", "ping", 2, "10.0.0.12", 1, 0.995, [(1, "")], [1]),
    ("Backup job", "push", 2, "", 0, 0.96, [], [1]),
    ("DNS resolver", "dns", 2, "1.1.1.1", 12, 0.999, [], [1]),
    ("SMTP relay", "port", 6, "mail.example.com:25", 25, 0.99, [(1, "")], [1]),
    ("IMAP", "port", 6, "mail.example.com:993", 30, 0.995, [], [1]),
    ("Acme – website", "http", 4, "https://acme.example", 210, 0.985, [(2, "acme")], [1, 2]),
    ("Acme – webshop", "keyword", 4, "https://shop.acme.example", 320, 0.975, [(2, "acme")], [1, 2]),
    ("Acme – VPN", "ping", 4, "203.0.113.10", 18, 0.993, [(2, "acme")], [1, 2]),
    ("Globex – portal", "http", 5, "https://portal.globex.example", 150, 0.99, [(2, "globex")], [1, 3]),
    ("Globex – API", "http", 5, "https://api.globex.example", 90, 0.96, [(2, "globex")], [1, 3]),
    ("Globex – office line", "ping", 5, "198.51.100.20", 9, 0.997, [(2, "globex")], [1, 3]),
]
TAGS = [(1, "production", "#2563eb"), (2, "customer", "#7c3aed"), (3, "public", "#059669")]
NOTIFICATIONS = [
    (1, "Ops team (e-mail)", {"type": "smtp", "smtpHost": "smtp.example.com", "smtpPort": 587, "smtpSecure": False,
                              "smtpFrom": "\"Monitoring\" <monitoring@example.com>", "smtpTo": "ops@example.com",
                              "isDefault": True}),
    (2, "Acme Corp (e-mail)", {"type": "smtp", "smtpHost": "smtp.example.com", "smtpPort": 587, "smtpSecure": False,
                               "smtpFrom": "\"Monitoring\" <monitoring@example.com>", "smtpTo": "it@acme.example"}),
    (3, "Globex (e-mail)", {"type": "smtp", "smtpHost": "smtp.example.com", "smtpPort": 587, "smtpSecure": False,
                            "smtpFrom": "\"Monitoring\" <monitoring@example.com>", "smtpTo": "servicedesk@globex.example"}),
    (4, "On-call (Pushover)", {"type": "pushover"}),
]
ERRORS = {
    "http": ["timeout of 48000ms exceeded", "Request failed with status code 502", "Request failed with status code 503",
             "getaddrinfo ENOTFOUND", "certificate has expired"],
    "keyword": ["Keyword 'Add to cart' not found", "Request failed with status code 500", "timeout of 48000ms exceeded"],
    "ping": ["Host unreachable", "100% packet loss"],
    "port": ["connect ECONNREFUSED", "Connection timed out"],
    "dns": ["queryA ETIMEOUT"],
    "push": ["No heartbeat in the time window"],
}


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.000")


def plan_states(start: datetime, end: datetime, reliability: float, kind: str):
    """Return a list of (from, to, status, msg) for non-up periods."""
    events = []
    downtime_budget = (end - start).total_seconds() * (1 - reliability)
    t = start + timedelta(hours=rng.uniform(5, 60))
    while t < end and downtime_budget > 0:
        if rng.random() < 0.25:  # short flap
            dur = timedelta(minutes=rng.choice([5, 5, 10]))
        else:
            dur = timedelta(minutes=rng.choice([10, 15, 20, 35, 60, 90, 180, 300]))
        events.append((t, t + dur, 0, rng.choice(ERRORS[kind])))
        downtime_budget -= dur.total_seconds()
        t += dur + timedelta(hours=rng.expovariate(1 / 70))
    return events


def main(path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.executescript(SCHEMA)
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    start = (now - timedelta(days=DAYS)).replace(minute=0)

    for gid, name in GROUPS.items():
        db.execute("INSERT INTO monitor (id, name, type, parent, created_date) VALUES (?, ?, 'group', ?, ?)",
                   (gid, name, GROUP_PARENT.get(gid), fmt(start)))
    for tid, name, color in TAGS:
        db.execute("INSERT INTO tag (id, name, color, created_date) VALUES (?, ?, ?, ?)", (tid, name, color, fmt(start)))
    for nid, name, cfg in NOTIFICATIONS:
        db.execute("INSERT INTO notification (id, name, config, is_default) VALUES (?, ?, ?, ?)",
                   (nid, name, json.dumps({"name": name, **cfg}), int(cfg.get("isDefault", False))))

    rows = []
    hb_id = 0
    for idx, (name, kind, parent, url, ping, rel, tags, notifs) in enumerate(MONITORS, start=100):
        created = start if idx != 116 else now - timedelta(days=20)  # one monitor added recently
        db.execute("INSERT INTO monitor (id, name, type, parent, url, hostname, created_date, interval) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (idx, name, kind, parent, url if kind in ("http", "keyword") else None,
                                                       url if kind not in ("http", "keyword") else None, fmt(created), 300))
        for tid, value in tags:
            db.execute("INSERT INTO monitor_tag (monitor_id, tag_id, value) VALUES (?, ?, ?)", (idx, tid, value))
        for nid in notifs:
            db.execute("INSERT INTO monitor_notification (monitor_id, notification_id) VALUES (?, ?)", (idx, nid))

        states = plan_states(created, now, rel, kind)
        if name == "Hypervisor 02":  # planned maintenance window
            m = now - timedelta(days=9, hours=3)
            states.append((m, m + timedelta(hours=2), 3, "Maintenance"))
        if name == "Globex – API":   # still down right now
            states.append((now - timedelta(minutes=47), now + timedelta(hours=1), 0, "Request failed with status code 503"))
        states.sort()

        t, prev, si = created, None, 0
        while t <= now:
            while si < len(states) and states[si][1] <= t:
                si += 1
            status, msg = 1, "200 - OK" if kind in ("http", "keyword") else ""
            if si < len(states) and states[si][0] <= t < states[si][1]:
                status, msg = states[si][2], states[si][3]
            hb_id += 1
            p = max(1, int(rng.gauss(ping, ping * 0.15))) if status == 1 and ping else None
            rows.append((hb_id, int(status != prev), idx, status, msg, fmt(t), p))
            prev = status
            t += STEP
    db.executemany("INSERT INTO heartbeat (id, important, monitor_id, status, msg, time, ping) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    print(f"Wrote {out} with {len(MONITORS)} monitors and {len(rows)} heartbeats over {DAYS} days")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo/kuma.db")
