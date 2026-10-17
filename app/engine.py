"""Turns imported state changes into report data: incidents, availability, SLA, series."""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select

from . import config, db
from .i18n import fmt_date_long, fmt_dt, fmt_duration, fmt_month, fmt_pct, strings, to_local

DOWN, UP, PENDING, MAINTENANCE = 0, 1, 2, 3

PERIODS = {
    "last_24h": "Last 24 hours",
    "today": "Today (so far)",
    "yesterday": "Yesterday",
    "last_7d": "Last 7 days",
    "this_week": "This week (so far)",
    "prev_week": "Previous week (Mon–Sun)",
    "last_30d": "Last 30 days",
    "this_month": "This month (so far)",
    "prev_month": "Previous month",
    "this_quarter": "This quarter (so far)",
    "prev_quarter": "Previous quarter",
    "this_year": "This year (so far)",
    "prev_year": "Previous year",
    "last_n_days": "Last N days",
    "custom": "Fixed dates",
}

DEFAULT_CONFIG = {
    "title": "",
    "intro": "",
    "language": "en",
    "scope": {"all": False, "monitors": [], "groups": [], "tags": [], "tag_match": "any",
              "exclude": [], "types": [], "include_paused": False},
    "period": {"type": "prev_month", "days": 7, "start": "", "end": ""},
    "filters": {"min_duration_min": 0, "maintenance_as_down": False,
                "business_hours": {"enabled": False, "days": [0, 1, 2, 3, 4], "start": "08:00", "end": "18:00"}},
    "sla_target": 99.9,
    "sections": {"summary": True, "chart": True, "monitors": True, "incidents": True,
                 "top": True, "messages": False, "response": True, "compare": True},
    "group_by": "none",
    "top_n": 10,
    "max_incidents": 200,
    "delivery": {"transport": "", "recipients": "", "cc": "", "kuma_recipients": False,
                 "split": "none", "only_if_incidents": False,
                 "attach_pdf": False, "attach_csv": False, "attach_xlsx": False},
}


def merged_config(cfg: dict | None) -> dict:
    """Deep-merge a stored report config over the defaults, so new options get sane values."""
    def merge(base, over):
        out = dict(base)
        for k, v in (over or {}).items():
            out[k] = merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
        return out
    return merge(DEFAULT_CONFIG, cfg or {})


# ---------------------------------------------------------------- periods

@dataclass
class Period:
    start: datetime   # local aware
    end: datetime     # local aware, exclusive
    label: str

    @property
    def start_utc(self) -> datetime:
        return self.start.astimezone(timezone.utc).replace(tzinfo=None)

    @property
    def end_utc(self) -> datetime:
        return self.end.astimezone(timezone.utc).replace(tzinfo=None)

    def previous(self) -> "Period":
        length = self.end - self.start
        # Calendar months/quarters/years: shift by the same calendar unit, not by a fixed length.
        if self.start.day == 1 and self.start.time() == time(0) and self.end.day == 1 and length.days >= 28:
            months = (self.end.year - self.start.year) * 12 + self.end.month - self.start.month
            start = _add_months(self.start, -months)
            return Period(start, self.start, "")
        return Period(self.start - length, self.start, "")


def _local_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=config.TIMEZONE)


def _add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    year, month = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def resolve_period(pcfg: dict, lang: str = "en", now: datetime | None = None) -> Period:
    now = (now or datetime.now(timezone.utc)).astimezone(config.TIMEZONE)
    today = _local_midnight(now.date())
    ptype = pcfg.get("type", "prev_month")
    if ptype == "last_24h":
        start, end = now - timedelta(hours=24), now
    elif ptype == "today":
        start, end = today, now
    elif ptype == "yesterday":
        start, end = _local_midnight(now.date() - timedelta(days=1)), today
    elif ptype == "last_7d":
        start, end = now - timedelta(days=7), now
    elif ptype == "this_week":
        start, end = _local_midnight(now.date() - timedelta(days=now.weekday())), now
    elif ptype == "prev_week":
        end = _local_midnight(now.date() - timedelta(days=now.weekday()))
        start = _local_midnight(end.date() - timedelta(days=7))
    elif ptype == "last_30d":
        start, end = now - timedelta(days=30), now
    elif ptype == "this_month":
        start, end = today.replace(day=1), now
    elif ptype == "prev_month":
        end = today.replace(day=1)
        start = _add_months(end, -1)
    elif ptype in ("this_quarter", "prev_quarter"):
        q_start = today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
        start, end = (q_start, now) if ptype == "this_quarter" else (_add_months(q_start, -3), q_start)
    elif ptype == "this_year":
        start, end = today.replace(month=1, day=1), now
    elif ptype == "prev_year":
        end = today.replace(month=1, day=1)
        start = end.replace(year=end.year - 1)
    elif ptype == "custom" and pcfg.get("start") and pcfg.get("end"):
        start = _local_midnight(date.fromisoformat(pcfg["start"]))
        end = _local_midnight(date.fromisoformat(pcfg["end"]) + timedelta(days=1))
    else:  # last_n_days
        days = max(1, int(pcfg.get("days") or 7))
        start, end = _local_midnight(now.date() - timedelta(days=days)), today
    # Normalise through UTC so DST transitions produce correct wall-clock offsets.
    start = start.astimezone(timezone.utc).astimezone(config.TIMEZONE)
    end = end.astimezone(timezone.utc).astimezone(config.TIMEZONE)
    return Period(start, end, period_label(start, end, ptype, lang))


def period_label(start: datetime, end: datetime, ptype: str, lang: str) -> str:
    last_day = (end - timedelta(seconds=1)).date()
    if ptype == "prev_month" or (start.day == 1 and end.day == 1 and start.time() == time(0)
                                 and _add_months(start, 1) == end):
        return fmt_month(start, lang).capitalize()
    if ptype in ("last_24h", "last_7d", "last_30d", "today", "this_week", "this_month", "this_quarter", "this_year"):
        return f"{fmt_dt(start, lang, True)} – {fmt_dt(end, lang, True)}"
    if start.date() == last_day:
        return fmt_date_long(start, lang)
    return f"{fmt_date_long(start, lang)} – {fmt_date_long(last_day, lang)}"


# ---------------------------------------------------------------- business hours

def _parse_hhmm(value: str, default: time) -> time:
    try:
        h, m = value.split(":")
        return time(int(h) % 24, int(m))
    except (AttributeError, ValueError):
        return default


class Clock:
    """Measures seconds of an interval, optionally only counting business hours."""

    def __init__(self, bh: dict | None):
        self.enabled = bool(bh and bh.get("enabled"))
        if self.enabled:
            self.days = {int(d) for d in bh.get("days", [0, 1, 2, 3, 4])}
            self.start = _parse_hhmm(bh.get("start"), time(8))
            self.end = _parse_hhmm(bh.get("end"), time(18))

    def seconds(self, a: datetime, b: datetime) -> float:
        """a, b: UTC naive."""
        if b <= a:
            return 0.0
        if not self.enabled:
            return (b - a).total_seconds()
        la, lb = to_local(a), to_local(b)
        total = 0.0
        day = la.date()
        while day <= lb.date():
            if day.weekday() in self.days:
                ws = datetime.combine(day, self.start, tzinfo=config.TIMEZONE)
                we = (datetime.combine(day, self.end, tzinfo=config.TIMEZONE) if self.end > self.start
                      else datetime.combine(day + timedelta(days=1), self.end, tzinfo=config.TIMEZONE))
                s, e = max(ws, la), min(we, lb)
                if e > s:
                    total += (e - s).total_seconds()
            day += timedelta(days=1)
        return total


# ---------------------------------------------------------------- scope

def monitor_tree(session) -> tuple[dict[int, db.Monitor], dict[int | None, list[int]]]:
    monitors = {m.id: m for m in session.scalars(select(db.Monitor))}
    children: dict[int | None, list[int]] = defaultdict(list)
    for m in monitors.values():
        children[m.parent_id if m.parent_id in monitors else None].append(m.id)
    return monitors, children


def group_path(m: db.Monitor, monitors: dict[int, db.Monitor]) -> str:
    names, seen, pid = [], set(), m.parent_id
    while pid and pid in monitors and pid not in seen:
        seen.add(pid)
        names.append(monitors[pid].name)
        pid = monitors[pid].parent_id
    return " / ".join(reversed(names))


def _descendants(gid: int, children) -> set[int]:
    out, stack = set(), [gid]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _tag_matches(m: db.Monitor, rule: dict) -> bool:
    for t in m.tags or []:
        if int(t["id"]) == int(rule.get("id", -1)):
            want = (rule.get("value") or "").strip()
            if not want or (t.get("value") or "").strip().lower() == want.lower():
                return True
    return False


def resolve_scope(session, scope: dict) -> list[db.Monitor]:
    monitors, children = monitor_tree(session)
    selected: set[int] = set()
    if scope.get("all"):
        selected = {i for i, m in monitors.items() if not m.deleted}
    for mid in scope.get("monitors", []):
        if int(mid) in monitors:
            selected.add(int(mid))
    for gid in scope.get("groups", []):
        selected |= _descendants(int(gid), children)
    rules = scope.get("tags") or []
    if rules:
        match_all = scope.get("tag_match") == "all"
        for m in monitors.values():
            if m.deleted:
                continue
            hits = [_tag_matches(m, r) for r in rules]
            if all(hits) if match_all else any(hits):
                selected.add(m.id)
                if m.type == "group":  # a tagged group brings its members along
                    selected |= _descendants(m.id, children)
    for xid in scope.get("exclude", []):
        selected.discard(int(xid))
        selected -= _descendants(int(xid), children)
    types = set(scope.get("types") or [])
    out = []
    for mid in selected:
        m = monitors[mid]
        if m.type == "group" or (types and m.type not in types):
            continue
        if not m.active and not scope.get("include_paused"):
            continue
        out.append(m)
    return sorted(out, key=lambda m: (group_path(m, monitors).lower(), m.name.lower()))


# ---------------------------------------------------------------- incidents

@dataclass
class Incident:
    monitor_id: int
    start_id: int
    start: datetime            # UTC naive (real start, may be before the period)
    end: datetime | None       # UTC naive, None = ongoing
    msg: str
    kind: int = DOWN
    downtime: float = 0.0      # seconds within the period (business hours applied)
    note: str = ""
    excluded: bool = False

    def duration(self, now: datetime) -> float:
        return ((self.end or now) - self.start).total_seconds()


@dataclass
class MonitorStats:
    monitor: db.Monitor
    group: str
    monitored: float = 0.0
    downtime: float = 0.0
    maintenance: float = 0.0
    incidents: list[Incident] = field(default_factory=list)
    status_now: int | None = None
    first_seen: datetime | None = None
    checks_up: int = 0
    checks_down: int = 0
    ping_avg: float | None = None
    ping_max: float | None = None

    @property
    def uptime(self) -> float | None:
        if self.monitored <= 0:
            return None
        return max(0.0, 100.0 * (1 - self.downtime / self.monitored))


def _status_class(status: int) -> int:
    return DOWN if status == DOWN else MAINTENANCE if status == MAINTENANCE else UP


def _intervals(changes: list[db.StateChange], last_end: datetime | None):
    """Yield (status, first_change, run_end) for consecutive equal-status runs.
    last_end is the real end of the final run, if it ended after the period."""
    runs = []
    for c in changes:
        status = _status_class(c.status)
        if runs and runs[-1][0] == status:
            continue
        runs.append([status, c])
    for i, (status, c) in enumerate(runs):
        run_end = runs[i + 1][1].time if i + 1 < len(runs) else last_end
        yield status, c, run_end


def compute_monitors(session, monitors: list[db.Monitor], period: Period, cfg: dict,
                     now: datetime | None = None) -> list[MonitorStats]:
    now = now or db.utcnow()
    p_start, p_end = period.start_utc, min(period.end_utc, now)
    clock = Clock(cfg["filters"].get("business_hours"))
    min_dur = float(cfg["filters"].get("min_duration_min") or 0) * 60
    maint_as_down = bool(cfg["filters"].get("maintenance_as_down"))
    ids = [m.id for m in monitors]
    all_monitors, _ = monitor_tree(session)
    if not ids:
        return []

    prev_ids = session.execute(
        select(func.max(db.StateChange.id)).where(db.StateChange.monitor_id.in_(ids),
                                                  db.StateChange.time < p_start)
        .group_by(db.StateChange.monitor_id)).scalars().all()
    rows = list(session.scalars(select(db.StateChange).where(db.StateChange.id.in_(prev_ids))))
    rows += list(session.scalars(select(db.StateChange).where(
        db.StateChange.monitor_id.in_(ids), db.StateChange.time >= p_start, db.StateChange.time < p_end)))
    by_monitor: dict[int, list[db.StateChange]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r.time, r.id)):
        by_monitor[r.monitor_id].append(r)

    # For runs still open at the end of the period, find when they really ended.
    real_end: dict[int, datetime] = {}
    for mid, changes in by_monitor.items():
        last = _status_class(changes[-1].status)
        if last == UP or p_end >= now:
            continue
        nxt = session.scalars(select(db.StateChange.time).where(
            db.StateChange.monitor_id == mid, db.StateChange.time >= p_end,
            db.StateChange.status != last).order_by(db.StateChange.time).limit(1)).first()
        if nxt:
            real_end[mid] = nxt

    latest = dict(session.execute(
        select(db.StateChange.monitor_id, db.StateChange.status)
        .where(db.StateChange.id.in_(select(func.max(db.StateChange.id))
                                     .where(db.StateChange.monitor_id.in_(ids))
                                     .group_by(db.StateChange.monitor_id)))).all())

    notes = {}
    out = []
    for m in monitors:
        st = MonitorStats(m, group_path(m, all_monitors), status_now=latest.get(m.id))
        changes = by_monitor.get(m.id, [])
        if not changes or changes[0].time >= p_start:
            # No known state before the period (Kuma prunes old heartbeats, and the import only
            # starts at install time): assume the monitor was up since its creation.
            born = max(p_start, m.created_at or p_start)
            if born < p_end and (not changes or born < changes[0].time):
                changes = [db.StateChange(id=0, monitor_id=m.id, status=UP, time=born, msg="")] + changes
        if changes:
            st.first_seen = max(changes[0].time, p_start)
            st.monitored = clock.seconds(st.first_seen, p_end)
        for status, first, run_end in _intervals(changes, real_end.get(m.id)):
            seg_start, seg_end = max(first.time, p_start), min(run_end or p_end, p_end)
            secs = clock.seconds(seg_start, seg_end)
            if status == MAINTENANCE and not maint_as_down:
                st.maintenance += secs
                continue
            if status == UP:
                continue
            inc = Incident(m.id, first.id, first.time, run_end, (first.msg or "").strip(),
                           kind=status, downtime=secs)
            if inc.duration(now) < min_dur:
                continue
            st.incidents.append(inc)
        notes.update({i.start_id: i for i in st.incidents})
        out.append(st)

    if notes:
        for n in session.scalars(select(db.IncidentNote).where(db.IncidentNote.start_id.in_(list(notes)))):
            notes[n.start_id].note = n.note or ""
            notes[n.start_id].excluded = bool(n.excluded)
    for st in out:
        st.incidents = [i for i in st.incidents if not i.excluded]
        st.downtime = sum(i.downtime for i in st.incidents)
        st.monitored = max(0.0, st.monitored - st.maintenance)

    # Check counts and response times from daily aggregates (UTC days overlapping the period).
    d0, d1 = p_start.date(), (p_end - timedelta(seconds=1)).date()
    agg = defaultdict(lambda: [0, 0, 0.0, 0, None])  # up, down, ping_sum, ping_n, ping_max
    for r in session.scalars(select(db.DailyStat).where(db.DailyStat.monitor_id.in_(ids),
                                                        db.DailyStat.day >= d0, db.DailyStat.day <= d1)):
        a = agg[r.monitor_id]
        a[0] += r.up
        a[1] += r.down
        if r.ping_avg:
            a[2] += r.ping_avg * max(r.up, 1)
            a[3] += max(r.up, 1)
        if r.ping_max:
            a[4] = max(a[4] or 0, r.ping_max)
    for st in out:
        a = agg.get(st.monitor.id)
        if a:
            st.checks_up, st.checks_down = a[0], a[1]
            st.ping_avg = a[2] / a[3] if a[3] else None
            st.ping_max = a[4]
    return out


# ---------------------------------------------------------------- report context

def _buckets(period: Period, now_utc: datetime) -> tuple[str, list[tuple[datetime, datetime, datetime]]]:
    """Chart buckets as (label_dt local, start utc, end utc)."""
    start, end = period.start, min(period.end, now_utc.replace(tzinfo=timezone.utc).astimezone(config.TIMEZONE))
    days = (end - start).total_seconds() / 86400
    if days <= 2:
        unit = "hour"
        cur = start.replace(minute=0, second=0, microsecond=0)
    elif days <= 62:
        unit, cur = "day", _local_midnight(start.date())
    elif days <= 190:
        unit, cur = "week", _local_midnight(start.date() - timedelta(days=start.weekday()))
    else:
        unit, cur = "month", _local_midnight(start.date().replace(day=1))
    out = []
    while cur < end:
        if unit == "hour":
            nxt = (cur.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(config.TIMEZONE)
        elif unit == "day":
            nxt = _local_midnight(cur.date() + timedelta(days=1))
        elif unit == "week":
            nxt = _local_midnight(cur.date() + timedelta(days=7))
        else:
            nxt = _add_months(cur, 1)
        s, e = max(cur, start), min(nxt, end)
        out.append((cur, s.astimezone(timezone.utc).replace(tzinfo=None), e.astimezone(timezone.utc).replace(tzinfo=None)))
        cur = nxt
    return unit, out


def _bucket_label(dt: datetime, unit: str, lang: str) -> str:
    from .i18n import MONTHS, WEEKDAYS_SHORT
    if unit == "hour":
        return dt.strftime("%H:00")
    if unit == "day":
        return f"{WEEKDAYS_SHORT.get(lang, WEEKDAYS_SHORT['en'])[dt.weekday()]} {dt.day}"
    if unit == "week":
        return f"{dt.day} {MONTHS.get(lang, MONTHS['en'])[dt.month - 1][:3]}"
    return f"{MONTHS.get(lang, MONTHS['en'])[dt.month - 1][:3]} {dt.year}"


def _color_for(uptime: float | None, target: float) -> str:
    if uptime is None:
        return "#c8ccd2"
    if uptime >= target:
        return "#2f9e5b"
    if uptime >= target - 1:
        return "#e0a800"
    return "#d64545"


def _summary(stats: list[MonitorStats], target: float, now: datetime) -> dict:
    with_data = [s for s in stats if s.uptime is not None]
    incidents = [i for s in stats for i in s.incidents]
    mon_total = sum(s.monitored for s in with_data)
    down_total = sum(s.downtime for s in with_data)
    pings = [s.ping_avg for s in stats if s.ping_avg]
    longest = max(incidents, key=lambda i: i.duration(now), default=None)
    resolved = [i.duration(now) for i in incidents if i.end]
    return {
        "monitors": len(stats),
        "monitors_with_data": len(with_data),
        "affected": sum(1 for s in stats if s.incidents),
        "incidents": len(incidents),
        "ongoing": sum(1 for i in incidents if i.end is None),
        "downtime": down_total,
        "uptime": (100.0 * (1 - down_total / mon_total)) if mon_total else None,
        "uptime_min": min((s.uptime for s in with_data), default=None),
        "sla_met": sum(1 for s in with_data if s.uptime >= target),
        "sla_missed": sum(1 for s in with_data if s.uptime < target),
        "longest": longest.duration(now) if longest else None,
        "mttr": sum(resolved) / len(resolved) if resolved else None,
        "ping_avg": sum(pings) / len(pings) if pings else None,
        "checks_down": sum(s.checks_down for s in stats),
    }


def build_context(session, report_name: str, cfg: dict, monitors: list[db.Monitor],
                  now: datetime | None = None, period: Period | None = None) -> dict:
    cfg = merged_config(cfg)
    lang = cfg.get("language") or "en"
    t = strings(lang)
    now = now or db.utcnow()
    period = period or resolve_period(cfg["period"], lang, now.replace(tzinfo=timezone.utc))
    target = float(cfg.get("sla_target") or 0)
    stats = compute_monitors(session, monitors, period, cfg, now)
    summary = _summary(stats, target, now)

    def dur(s):
        return fmt_duration(s, lang)

    def kuma_link(mid):
        return f"{config.KUMA_BASE_URL}/dashboard/{mid}" if config.KUMA_BASE_URL else ""

    status_names = {DOWN: t.down, UP: t.up, PENDING: t.pending, MAINTENANCE: t.maintenance}
    mon_rows = []
    for s in stats:
        n = len(s.incidents)
        resolved = [i.duration(now) for i in s.incidents if i.end]
        up = s.uptime
        mon_rows.append({
            "id": s.monitor.id, "name": s.monitor.name, "type": s.monitor.type, "group": s.group,
            "url": s.monitor.url or "", "description": s.monitor.description or "", "link": kuma_link(s.monitor.id),
            "tags": [x["name"] + (f": {x['value']}" if x.get("value") else "") for x in s.monitor.tags or []],
            "uptime": up, "uptime_str": fmt_pct(up) if up is not None else t.no_data,
            "color": _color_for(up, target), "sla_met": up is not None and up >= target,
            "incidents": n, "downtime": s.downtime, "downtime_str": dur(s.downtime) if s.downtime else "–",
            "maintenance": s.maintenance, "maintenance_str": dur(s.maintenance) if s.maintenance else "–",
            "longest": max((i.duration(now) for i in s.incidents), default=0),
            "longest_str": dur(max((i.duration(now) for i in s.incidents), default=0)) if n else "–",
            "mttr_str": dur(sum(resolved) / len(resolved)) if resolved else "–",
            "mtbf_str": dur((s.monitored - s.downtime) / n) if n else "–",
            "checks_up": s.checks_up, "checks_down": s.checks_down,
            "ping_avg": s.ping_avg, "ping_avg_str": f"{s.ping_avg:.0f} ms" if s.ping_avg else "–",
            "ping_max_str": f"{s.ping_max:.0f} ms" if s.ping_max else "–",
            "status_now": status_names.get(s.status_now, t.unknown), "status_code": s.status_now,
        })

    inc_rows = []
    for s in stats:
        for i in s.incidents:
            inc_rows.append({
                "monitor_id": i.monitor_id, "monitor": s.monitor.name, "group": s.group,
                "start_id": i.start_id, "start": to_local(i.start), "end": to_local(i.end),
                "start_str": fmt_dt(i.start, lang, True), "end_str": fmt_dt(i.end, lang, True) if i.end else t.ongoing,
                "ongoing": i.end is None, "duration": i.duration(now), "duration_str": dur(i.duration(now)),
                "downtime": i.downtime, "downtime_str": dur(i.downtime),
                "kind": t.maintenance if i.kind == MAINTENANCE else t.down,
                "message": i.msg, "note": i.note, "link": kuma_link(i.monitor_id),
            })
    inc_rows.sort(key=lambda r: r["start"], reverse=True)

    # Chart series
    unit, buckets = _buckets(period, now)
    clock = Clock(cfg["filters"].get("business_hours"))
    series = []
    for label_dt, bs, be in buckets:
        mon = down = 0.0
        started = 0
        for s in stats:
            if s.first_seen is None or s.first_seen >= be:
                continue
            mon += clock.seconds(max(bs, s.first_seen), be)
            for i in s.incidents:
                down += clock.seconds(max(bs, i.start), min(be, i.end or now))
                if bs <= i.start < be:
                    started += 1
        up = 100.0 * (1 - down / mon) if mon else None
        series.append({"label": _bucket_label(label_dt, unit, lang), "date": label_dt, "uptime": up,
                       "uptime_str": fmt_pct(up, 2), "downtime": down, "downtime_str": dur(down) if down else "",
                       "incidents": started, "color": _color_for(up, target)})
    lo = min((b["uptime"] for b in series if b["uptime"] is not None), default=100)
    floor = max(0.0, min(target - 1, lo - (100 - lo) * 0.25)) if lo < 100 else max(0.0, target - 1)
    for b in series:
        b["height"] = 0 if b["uptime"] is None else max(3, round(100 * (b["uptime"] - floor) / max(0.001, 100 - floor)))

    top = sorted([r for r in mon_rows if r["incidents"]], key=lambda r: (-r["downtime"], -r["incidents"]))
    messages = Counter(r["message"][:140] for r in inc_rows if r["message"]).most_common(int(cfg.get("top_n") or 10))

    groups = []
    if cfg.get("group_by") in ("group", "tag"):
        buckets_g: dict[str, list] = defaultdict(list)
        for r, s in zip(mon_rows, stats):
            keys = ([r["group"] or "—"] if cfg["group_by"] == "group"
                    else sorted({x["name"] for x in s.monitor.tags or []}) or ["—"])
            for k in keys:
                buckets_g[k].append((r, s))
        for name in sorted(buckets_g, key=str.lower):
            rows = buckets_g[name]
            gs = _summary([s for _, s in rows], target, now)
            groups.append({"name": name, "monitors": [r for r, _ in rows], "summary": _fmt_summary(gs, lang, target)})

    previous = None
    if cfg["sections"].get("compare"):
        prev_period = period.previous()
        prev_stats = compute_monitors(session, monitors, prev_period, cfg, now)
        ps = _summary(prev_stats, target, now)
        previous = _fmt_summary(ps, lang, target)
        cur_up, prev_up = summary["uptime"], ps["uptime"]
        previous["uptime_delta"] = (cur_up - prev_up) if cur_up is not None and prev_up is not None else None
        previous["uptime_delta_str"] = (f"{previous['uptime_delta']:+.3f} pp".replace(".", "," if lang != "en" else ".")
                                        if previous["uptime_delta"] is not None else "–")
        previous["incidents_delta"] = summary["incidents"] - ps["incidents"]
        previous["label"] = period_label(prev_period.start, prev_period.end, "", lang)

    filters_desc = []
    if cfg["filters"].get("business_hours", {}).get("enabled"):
        bh = cfg["filters"]["business_hours"]
        from .i18n import WEEKDAYS_SHORT
        days = ", ".join(WEEKDAYS_SHORT.get(lang, WEEKDAYS_SHORT["en"])[int(d)] for d in sorted(bh.get("days", [])))
        filters_desc.append(f"{t.business_hours}: {days} {bh.get('start')}–{bh.get('end')}")
    if float(cfg["filters"].get("min_duration_min") or 0) > 0:
        filters_desc.append(t.ignored_shorter.format(n=cfg["filters"]["min_duration_min"]))
    filters_desc.append(t.maintenance_counted if cfg["filters"].get("maintenance_as_down") else t.maintenance_excluded)

    max_inc = int(cfg.get("max_incidents") or 200)
    return {
        "report": {"name": report_name, "title": cfg.get("title") or report_name, "intro": cfg.get("intro") or "",
                   "period": period.label, "period_start": period.start, "period_end": period.end,
                   "generated": fmt_dt(datetime.now(timezone.utc).astimezone(config.TIMEZONE), lang, True),
                   "language": lang, "kuma_url": config.KUMA_BASE_URL, "filters": filters_desc},
        "t": t,
        "sla_target": target, "sla_target_str": fmt_pct(target),
        "summary": _fmt_summary(summary, lang, target),
        "monitors": mon_rows,
        "incidents": inc_rows[:max_inc],
        "incidents_total": len(inc_rows),
        "incidents_truncated": max(0, len(inc_rows) - max_inc),
        "series": series, "series_unit": unit,
        "series_title": {"hour": t.availability, "day": t.per_day, "week": t.per_week, "month": t.per_month}[unit],
        "top": top[: int(cfg.get("top_n") or 10)],
        "messages": [{"message": m, "count": c} for m, c in messages],
        "groups": groups,
        "previous": previous,
        "sections": cfg["sections"],
        "has_incidents": bool(inc_rows),
    }


def _fmt_summary(s: dict, lang: str, target: float) -> dict:
    return {**s,
            "uptime_str": fmt_pct(s["uptime"]) if s["uptime"] is not None else "–",
            "uptime_min_str": fmt_pct(s["uptime_min"]) if s["uptime_min"] is not None else "–",
            "downtime_str": fmt_duration(s["downtime"], lang) if s["downtime"] else "0",
            "longest_str": fmt_duration(s["longest"], lang) if s["longest"] else "–",
            "mttr_str": fmt_duration(s["mttr"], lang) if s["mttr"] else "–",
            "ping_avg_str": f"{s['ping_avg']:.0f} ms" if s["ping_avg"] else "–",
            "color": _color_for(s["uptime"], target)}
