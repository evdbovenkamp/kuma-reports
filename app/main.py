"""Web GUI."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, engine, i18n, importer, kuma, mailer, render, runner, scheduler

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kuma-reports")
HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    with db.SessionLocal() as session:
        render.seed_builtin_templates(session)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Kuma Reports", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(
    base=config.BASE_PATH,
    version=config.VERSION, PERIODS=engine.PERIODS, LANGUAGES=i18n.LANGUAGES,
    SCHEDULE_PRESETS=scheduler.SCHEDULE_PRESETS, WEEKDAYS=i18n.WEEKDAYS_SHORT["en"],
    ADMIN_USERNAME=config.ADMIN_USERNAME,
)
templates.env.filters.update(
    local=lambda dt: i18n.fmt_dt(dt, "en", True) if dt else "–",
    duration=lambda s: i18n.fmt_duration(s, "en"),
    pct=i18n.fmt_pct,
)


# ---------------------------------------------------------------- auth & helpers

PUBLIC_PATHS = ("/login", "/setup", "/static/", "/healthz")


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        return hmac.compare_digest(_hash_password(password, bytes.fromhex(salt)).split("$")[2], digest)
    except ValueError:
        return False


def _stored_hash() -> str | None:
    with db.SessionLocal() as s:
        return db.kv_get(s, "admin_password_hash")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if config.AUTH_MODE != "none" and not path.startswith(PUBLIC_PATHS):
        if not request.session.get("user"):
            if not config.ADMIN_PASSWORD and not _stored_hash():
                return redirect("/setup")
            return redirect(f"/login?next={path}")
    return await call_next(request)


# Added after the auth middleware so it wraps it (the last added middleware runs first).
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="kuma_reports",
                   max_age=14 * 86400, same_site="lax", https_only=config.BASE_URL.startswith("https"),
                   path=config.BASE_PATH or "/")


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(24)
    return tok


async def form_data(request: Request):
    form = await request.form()
    if config.AUTH_MODE != "none" and not hmac.compare_digest(form.get("csrf", ""), csrf_token(request)):
        raise HTTPException(400, "Invalid form token, please reload the page")
    return form


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {
        "csrf": csrf_token(request), "flash": flash, "auth": config.AUTH_MODE != "none",
        "user": request.session.get("user"), "nav": name.split("_")[0].split(".")[0], **ctx})


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def redirect(url: str) -> RedirectResponse:
    """Redirect to an app-relative path ("/reports/1") or a full URL (e.g. a Referer)."""
    if url.startswith("/") and not url.startswith("//"):
        url = config.BASE_PATH + url
    return RedirectResponse(url, 303)


@app.get("/healthz")
def healthz():
    """Health for Docker and for monitoring from Uptime Kuma itself.

    503 when Kuma Reports cannot do its job (Kuma database unreachable, import stale, scheduler stopped);
    200 with status "degraded" when a report delivery failed in the last 24 hours.
    Deliberately contains no monitor names, recipients or other details.
    """
    checks = {}
    try:
        kuma.reader().ping()
        checks["kuma_db"] = {"ok": True}
    except Exception:
        checks["kuma_db"] = {"ok": False}

    since = db.utcnow() - timedelta(hours=24)
    with db.SessionLocal() as s:
        last_import = db.kv_get(s, "last_import_at")
        import_error = db.kv_get(s, "last_import_error")
        runs = dict(s.execute(select(db.ReportRun.status, func.count()).where(
            db.ReportRun.started_at >= since, db.ReportRun.trigger != "test").group_by(db.ReportRun.status)).all())
    age = (db.utcnow() - datetime.fromisoformat(last_import)).total_seconds() if last_import else None
    checks["import"] = {"ok": age is not None and age <= 3 * config.IMPORT_INTERVAL + 60,
                        "last_import_age_s": int(age) if age is not None else None,
                        "error": import_error.split(" ", 1)[-1].splitlines()[0][:200] if import_error else None}
    checks["scheduler"] = {"ok": scheduler.is_healthy()}
    checks["deliveries"] = {"ok": not runs.get("failed"), "failed_24h": runs.get("failed", 0),
                            "sent_24h": runs.get("sent", 0)}

    failing = not (checks["kuma_db"]["ok"] and checks["import"]["ok"] and checks["scheduler"]["ok"])
    status = "fail" if failing else ("degraded" if not checks["deliveries"]["ok"] else "ok")
    return JSONResponse({"status": status, "version": config.VERSION, "checks": checks},
                        status_code=503 if failing else 200)


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    if config.AUTH_MODE == "none" or config.ADMIN_PASSWORD or _stored_hash():
        return redirect("/")
    return page(request, "login.html", setup=True)


@app.post("/setup")
async def setup_save(request: Request):
    if config.AUTH_MODE == "none" or config.ADMIN_PASSWORD or _stored_hash():
        return redirect("/")
    form = await request.form()
    pw, pw2 = form.get("password", ""), form.get("password2", "")
    if len(pw) < 10 or pw != pw2:
        flash(request, "Passwords must match and be at least 10 characters.", "error")
        return redirect("/setup")
    with db.SessionLocal() as s:
        db.kv_set(s, "admin_password_hash", _hash_password(pw))
        s.commit()
    request.session["user"] = config.ADMIN_USERNAME
    return redirect("/")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return page(request, "login.html", setup=False, next=request.query_params.get("next", "/"))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    user, pw = form.get("username", ""), form.get("password", "")
    ok = hmac.compare_digest(user, config.ADMIN_USERNAME or "admin") and (
        hmac.compare_digest(pw, config.ADMIN_PASSWORD) if config.ADMIN_PASSWORD
        else bool(_stored_hash()) and _check_password(pw, _stored_hash()))
    if not ok:
        log.warning("Failed login for %r from %s", user, request.client.host if request.client else "?")
        flash(request, "Invalid username or password.", "error")
        return redirect("/login")
    request.session.clear()
    request.session["user"] = user
    nxt = form.get("next") or "/"
    if config.BASE_PATH and nxt.startswith(config.BASE_PATH + "/"):
        nxt = nxt[len(config.BASE_PATH):]
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")


@app.post("/logout")
async def logout(request: Request):
    await form_data(request)
    request.session.clear()
    return redirect("/login")


# ---------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db.SessionLocal() as s:
        info = {k: db.kv_get(s, k) for k in ("last_import_at", "last_import_error", "kuma_version", "last_heartbeat_id")}
        counts = {
            "monitors": s.scalar(select(func.count()).select_from(db.Monitor).where(
                db.Monitor.deleted.is_(False), db.Monitor.type != "group")),
            "groups": s.scalar(select(func.count()).select_from(db.Monitor).where(
                db.Monitor.deleted.is_(False), db.Monitor.type == "group")),
            "tags": s.scalar(select(func.count()).select_from(db.Tag)),
            "state_changes": s.scalar(select(func.count()).select_from(db.StateChange)),
            "since": s.scalar(select(func.min(db.StateChange.time))),
        }
        reports = list(s.scalars(select(db.Report).order_by(db.Report.name)))
        runs = list(s.scalars(select(db.ReportRun).order_by(db.ReportRun.started_at.desc()).limit(10)))
        # Last 7 days at a glance
        cfg = engine.merged_config({"scope": {"all": True}, "period": {"type": "last_7d"},
                                    "sections": {"compare": False}})
        week = engine.build_context(s, "", cfg, engine.resolve_scope(s, cfg["scope"]))
    try:
        source = kuma.reader().source.description
    except Exception as exc:
        source = f"error: {exc}"
    next_runs = {r.id: scheduler.next_run(r.id) for r in reports}
    return page(request, "dashboard.html", info=info, counts=counts, reports=reports, runs=runs,
                next_runs=next_runs, source=source, week=week, config=config)


@app.post("/import")
async def import_now(request: Request):
    await form_data(request)
    result = importer.run_import()
    if "error" in result:
        flash(request, f"Import failed: {result['error']}", "error")
    else:
        flash(request, f"Import done: {result}")
    return redirect(request.headers.get("referer") or "/")


# ---------------------------------------------------------------- monitors

def _tree_rows(session):
    monitors, children = engine.monitor_tree(session)
    rows = []

    def walk(parent, depth):
        kids = sorted(children.get(parent, []), key=lambda i: (monitors[i].type != "group", monitors[i].name.lower()))
        for mid in kids:
            m = monitors[mid]
            if m.deleted:
                continue
            rows.append({"m": m, "depth": depth})
            walk(mid, depth + 1)
    walk(None, 0)
    return rows


@app.get("/monitors", response_class=HTMLResponse)
def monitors_page(request: Request):
    with db.SessionLocal() as s:
        rows = _tree_rows(s)
        since = db.utcnow() - timedelta(days=30)
        inc30 = dict(s.execute(select(db.StateChange.monitor_id, func.count()).where(
            db.StateChange.status == 0, db.StateChange.time >= since).group_by(db.StateChange.monitor_id)).all())
        latest = dict(s.execute(select(db.StateChange.monitor_id, db.StateChange.status).where(
            db.StateChange.id.in_(select(func.max(db.StateChange.id)).group_by(db.StateChange.monitor_id)))).all())
    try:
        notif_names = {n["id"]: n["name"] for n in kuma.reader().notifications()}
    except Exception:
        notif_names = {}
    return page(request, "monitors.html", rows=rows, inc30=inc30, latest=latest, notif_names=notif_names)


# ---------------------------------------------------------------- reports

def _report_form_ctx(session, report: db.Report | None):
    cfg = engine.merged_config(report.config if report else {})
    try:
        smtp_notifs = kuma.smtp_notifications()
    except Exception:
        smtp_notifs = []
    types = sorted({m.type for m in session.scalars(select(db.Monitor)) if m.type != "group"})
    return {
        "report": report, "cfg": cfg, "tree": _tree_rows(session),
        "tags": list(session.scalars(select(db.Tag).order_by(db.Tag.name))),
        "tag_rules": {int(t["id"]): t.get("value", "") for t in cfg["scope"].get("tags", [])},
        "types": types,
        "mail_templates": list(session.scalars(select(db.MailTemplate).order_by(db.MailTemplate.name))),
        "transports": mailer.transport_options(session),
        "smtp_notifs": smtp_notifs,
    }


def _ints(form, name) -> list[int]:
    return [int(v) for v in form.getlist(name) if str(v).strip().isdigit()]


def _config_from_form(form) -> tuple[dict, str, str | None]:
    tags = []
    for tid in _ints(form, "tags"):
        tags.append({"id": tid, "value": (form.get(f"tagval_{tid}") or "").strip()})
    preset = form.get("schedule_preset", "")
    schedule = (form.get("schedule_custom") or "").strip() if preset == "custom" else preset
    num = lambda k, d: float(form.get(k)) if (form.get(k) or "").replace(".", "", 1).isdigit() else d  # noqa: E731
    cfg = {
        "title": form.get("title", "").strip(),
        "intro": form.get("intro", "").strip(),
        "language": form.get("language", "en"),
        "scope": {
            "all": bool(form.get("scope_all")),
            "monitors": _ints(form, "monitors"),
            "groups": _ints(form, "groups"),
            "tags": tags,
            "tag_match": form.get("tag_match", "any"),
            "exclude": _ints(form, "exclude"),
            "types": form.getlist("types"),
            "include_paused": bool(form.get("include_paused")),
        },
        "period": {"type": form.get("period_type", "prev_month"), "days": int(num("period_days", 7)),
                   "start": form.get("period_start", ""), "end": form.get("period_end", "")},
        "filters": {
            "min_duration_min": num("min_duration_min", 0),
            "maintenance_as_down": bool(form.get("maintenance_as_down")),
            "business_hours": {"enabled": bool(form.get("bh_enabled")), "days": _ints(form, "bh_days"),
                               "start": form.get("bh_start", "08:00"), "end": form.get("bh_end", "18:00")},
        },
        "sla_target": num("sla_target", 99.9),
        "sections": {k: bool(form.get(f"sec_{k}")) for k in engine.DEFAULT_CONFIG["sections"]},
        "group_by": form.get("group_by", "none"),
        "top_n": int(num("top_n", 10)),
        "max_incidents": int(num("max_incidents", 200)),
        "delivery": {
            "transport": form.get("transport", ""),
            "recipients": form.get("recipients", "").strip(),
            "cc": form.get("cc", "").strip(),
            "kuma_recipients": bool(form.get("kuma_recipients")),
            "split": form.get("split", "none"),
            "only_if_incidents": bool(form.get("only_if_incidents")),
            "attach_pdf": bool(form.get("attach_pdf")),
            "attach_csv": bool(form.get("attach_csv")),
            "attach_xlsx": bool(form.get("attach_xlsx")),
        },
    }
    tpl = form.get("template_id")
    return cfg, schedule, int(tpl) if tpl and tpl.isdigit() else None


@app.get("/reports", response_class=HTMLResponse)
def reports_list(request: Request):
    with db.SessionLocal() as s:
        reports = list(s.scalars(select(db.Report).order_by(db.Report.name)))
        last = {}
        for r in reports:
            last[r.id] = s.scalars(select(db.ReportRun).where(db.ReportRun.report_id == r.id)
                                   .order_by(db.ReportRun.started_at.desc()).limit(1)).first()
        tpl_names = {t.id: t.name for t in s.scalars(select(db.MailTemplate))}
    return page(request, "reports.html", reports=reports, last=last, tpl_names=tpl_names,
                next_runs={r.id: scheduler.next_run(r.id) for r in reports},
                describe=lambda r: _describe_scope(r))


def _describe_scope(r: db.Report) -> str:
    sc = engine.merged_config(r.config)["scope"]
    parts = []
    if sc["all"]:
        parts.append("all monitors")
    if sc["groups"]:
        parts.append(f"{len(sc['groups'])} group(s)")
    if sc["monitors"]:
        parts.append(f"{len(sc['monitors'])} monitor(s)")
    if sc["tags"]:
        parts.append(f"{len(sc['tags'])} tag(s)")
    if sc["exclude"]:
        parts.append(f"−{len(sc['exclude'])}")
    return ", ".join(parts) or "nothing selected"


@app.get("/reports/new", response_class=HTMLResponse)
def report_new(request: Request):
    with db.SessionLocal() as s:
        return page(request, "report_edit.html", **_report_form_ctx(s, None))


@app.get("/reports/{rid}", response_class=HTMLResponse)
def report_edit(request: Request, rid: int):
    with db.SessionLocal() as s:
        r = s.get(db.Report, rid)
        if not r:
            raise HTTPException(404)
        return page(request, "report_edit.html", **_report_form_ctx(s, r))


@app.post("/reports/save")
async def report_save(request: Request):
    form = await form_data(request)
    cfg, schedule, tpl_id = _config_from_form(form)
    if schedule:
        try:
            scheduler.parse_cron(schedule)
        except ValueError as exc:
            flash(request, f"Invalid schedule: {exc}", "error")
            schedule = ""
    with db.SessionLocal() as s:
        rid = form.get("id")
        r = s.get(db.Report, int(rid)) if rid and rid.isdigit() else db.Report()
        r.name = (form.get("name") or "").strip() or "Untitled report"
        r.enabled = bool(form.get("enabled"))
        r.schedule = schedule
        r.template_id = tpl_id
        r.config = cfg
        s.add(r)
        s.commit()
        rid = r.id
    scheduler.sync_report_jobs()
    if form.get("action") == "preview":
        return redirect(f"/reports/{rid}/preview")
    flash(request, "Report saved.")
    return redirect(f"/reports/{rid}")


@app.post("/reports/{rid}/delete")
async def report_delete(request: Request, rid: int):
    await form_data(request)
    with db.SessionLocal() as s:
        r = s.get(db.Report, rid)
        if r:
            s.delete(r)
            s.commit()
    scheduler.sync_report_jobs()
    flash(request, "Report deleted.")
    return redirect("/reports")


@app.post("/reports/{rid}/duplicate")
async def report_duplicate(request: Request, rid: int):
    await form_data(request)
    with db.SessionLocal() as s:
        r = s.get(db.Report, rid)
        if not r:
            raise HTTPException(404)
        copy = db.Report(name=f"{r.name} (copy)", enabled=False, schedule=r.schedule,
                         template_id=r.template_id, config=dict(r.config))
        s.add(copy)
        s.commit()
        new_id = copy.id
    flash(request, "Copy created (disabled).")
    return redirect(f"/reports/{new_id}")


@app.post("/api/scope-preview")
async def scope_preview(request: Request):
    form = await form_data(request)
    cfg, _, _ = _config_from_form(form)
    with db.SessionLocal() as s:
        monitors = engine.resolve_scope(s, cfg["scope"])
        all_monitors, _ = engine.monitor_tree(s)
        names = [f"{engine.group_path(m, all_monitors) + ' / ' if m.parent_id else ''}{m.name}" for m in monitors]
    lang = cfg.get("language", "en")
    period = engine.resolve_period(cfg["period"], lang)
    return JSONResponse({"count": len(monitors), "names": names, "period": period.label})


def _jobs_or_404(s, rid: int):
    r = s.get(db.Report, rid)
    if not r:
        raise HTTPException(404)
    jobs = runner.plan_jobs(s, r, resolve_transport=False)
    return r, jobs


@app.get("/reports/{rid}/preview", response_class=HTMLResponse)
def report_preview(request: Request, rid: int, job: int = 0, template: int | None = None):
    with db.SessionLocal() as s:
        r, jobs = _jobs_or_404(s, rid)
        error, subject, ctx = None, "", None
        job_obj = jobs[min(job, len(jobs) - 1)] if jobs else None
        tpl = s.get(db.MailTemplate, template) if template else runner.template_for(s, r)
        if job_obj:
            try:
                ctx, subject, _, _ = runner.build(s, r, job_obj, tpl)
            except Exception as exc:
                error = str(exc)
        templates_list = list(s.scalars(select(db.MailTemplate).order_by(db.MailTemplate.name)))
        try:
            transport = mailer.resolve(s, None if (r.config.get("delivery") or {}).get("transport") == "notification"
                                       else (r.config.get("delivery") or {}).get("transport")).label
        except Exception as exc:
            transport = f"⚠ {exc}"
    return page(request, "report_preview.html", report=r, jobs=jobs, job=job, subject=subject, error=error,
                ctx=ctx, tpl=tpl, templates_list=templates_list, transport=transport,
                next_run=scheduler.next_run(r.id))


@app.get("/reports/{rid}/render")
def report_render(rid: int, job: int = 0, template: int | None = None, fmt: str = "html"):
    with db.SessionLocal() as s:
        r, jobs = _jobs_or_404(s, rid)
        if not jobs:
            return HTMLResponse("<p style='font-family:sans-serif'>No monitors in scope.</p>")
        job_obj = jobs[min(job, len(jobs) - 1)]
        tpl = s.get(db.MailTemplate, template) if template else runner.template_for(s, r)
        try:
            ctx, subject, html, text = runner.build(s, r, job_obj, tpl)
        except render.TemplateError as exc:
            return HTMLResponse(f"<pre style='color:#b91c1c;white-space:pre-wrap'>Template error: {exc}</pre>")
    name = render.slug(f"{r.name}-{ctx['report']['period']}")
    if fmt == "text":
        return Response(text, media_type="text/plain; charset=utf-8")
    if fmt == "pdf":
        try:
            pdf = render.to_pdf(html)
        except RuntimeError as exc:
            return Response(str(exc), status_code=501, media_type="text/plain")
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
    if fmt == "xlsx":
        return Response(render.to_xlsx(ctx), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    if fmt in ("csv", "monitors.csv"):
        return Response(render.to_csv(ctx["monitors"], render.MONITOR_COLUMNS), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}-monitors.csv"'})
    if fmt == "incidents.csv":
        return Response(render.to_csv(ctx["incidents"], render.INCIDENT_COLUMNS), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}-incidents.csv"'})
    return HTMLResponse(html)


@app.post("/reports/{rid}/send")
async def report_send(request: Request, rid: int):
    form = await form_data(request)
    test_to = (form.get("test_to") or "").strip()
    runs = runner.run_report(rid, "test" if test_to else "manual", override_to=test_to or None)
    summary = ", ".join(f"{r.report_name}: {r.status}" + (f" ({r.message})" if r.status != "sent" else "") for r in runs)
    flash(request, summary or "Nothing to send.", "ok" if all(r.status != "failed" for r in runs) else "error")
    return redirect(f"/reports/{rid}/preview")


# ---------------------------------------------------------------- mail templates

TEMPLATE_HELP = [
    ("report", "title, name, intro, period, generated, language, kuma_url, filters, period_start, period_end"),
    ("summary", "monitors, monitors_with_data, affected, incidents, ongoing, uptime(_str), uptime_min_str, downtime(_str), "
                "longest_str, mttr_str, sla_met, sla_missed, ping_avg_str, color"),
    ("monitors[]", "id, name, group, type, url, link, tags, description, uptime(_str), color, sla_met, incidents, "
                   "downtime(_str), longest_str, mttr_str, mtbf_str, maintenance_str, checks_up, checks_down, "
                   "ping_avg_str, ping_max_str, status_now"),
    ("incidents[]", "monitor_id, monitor, group, start, end, start_str, end_str, ongoing, duration_str, "
                    "downtime_str, kind, message, note, link"),
    ("series[]", "label, date, uptime(_str), incidents, downtime_str, color, height (0–100)"),
    ("top[] / groups[]", "top: monitors with most downtime; groups: name, monitors[], summary (when grouping)"),
    ("previous", "summary of the previous period + label, uptime_delta_str, incidents_delta (if enabled)"),
    ("other", "t (translated labels, e.g. t.incidents), sla_target_str, sections.*, messages[], "
              "has_incidents, incidents_total, incidents_truncated, series_title"),
    ("filters", "duration (seconds → text), pct, dt, nl2br"),
    ("macros", '{% import "macros.html" as k %} → k.page, k.header, k.tiles, k.tile, k.chart, '
               "k.monitor_table, k.incident_table, k.message_table, k.section_title, k.footer"),
]


@app.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request):
    with db.SessionLocal() as s:
        tpls = list(s.scalars(select(db.MailTemplate).order_by(db.MailTemplate.key.is_(None), db.MailTemplate.name)))
        usage = dict(s.execute(select(db.Report.template_id, func.count()).group_by(db.Report.template_id)).all())
    return page(request, "templates.html", tpls=tpls, usage=usage)


@app.get("/templates/{tid}", response_class=HTMLResponse)
def template_edit(request: Request, tid: int):
    with db.SessionLocal() as s:
        tpl = s.get(db.MailTemplate, tid)
        if not tpl:
            raise HTTPException(404)
        reports = list(s.scalars(select(db.Report).order_by(db.Report.name)))
    return page(request, "template_edit.html", tpl=tpl, reports=reports, help=TEMPLATE_HELP,
                macros=render.macros_source())


@app.post("/templates/{tid}")
async def template_save(request: Request, tid: int):
    form = await form_data(request)
    action = form.get("action", "save")
    with db.SessionLocal() as s:
        tpl = s.get(db.MailTemplate, tid)
        if not tpl:
            raise HTTPException(404)
        if action == "duplicate":
            copy = db.MailTemplate(name=f"{tpl.name} (copy)", description=tpl.description, subject=tpl.subject,
                                   body_html=tpl.body_html, body_text=tpl.body_text)
            s.add(copy)
            s.commit()
            flash(request, "Template copied. Built-in templates stay untouched; edit this copy.")
            return redirect(f"/templates/{copy.id}")
        if action == "reset" and tpl.key:
            render.reset_builtin(s, tpl)
            s.commit()
            flash(request, "Template reset to the built-in version.")
            return redirect(f"/templates/{tid}")
        if action == "delete" and not tpl.key:
            s.delete(tpl)
            s.commit()
            flash(request, "Template deleted. Reports using it fall back to the standard template.")
            return redirect("/templates")
        tpl.name = form.get("name", tpl.name).strip() or tpl.name
        tpl.description = form.get("description", "")
        tpl.subject = form.get("subject", "")
        tpl.body_html = form.get("body_html", "")
        tpl.body_text = form.get("body_text", "") or None
        # Validate syntax before accepting.
        try:
            render._html_env.parse(tpl.body_html)
            render._text_env.parse(tpl.subject)
            if tpl.body_text:
                render._text_env.parse(tpl.body_text)
        except Exception as exc:
            s.rollback()
            flash(request, f"Not saved — template syntax error: {exc}", "error")
            return redirect(f"/templates/{tid}")
        s.commit()
    flash(request, "Template saved.")
    return redirect(f"/templates/{tid}")


@app.post("/templates")
async def template_new(request: Request):
    await form_data(request)
    with db.SessionLocal() as s:
        base = s.scalars(select(db.MailTemplate).where(db.MailTemplate.key == "standard")).first()
        tpl = db.MailTemplate(name="New template", description="", subject=base.subject if base else "{{ report.title }}",
                              body_html=base.body_html if base else "", body_text=base.body_text if base else None)
        s.add(tpl)
        s.commit()
        return redirect(f"/templates/{tpl.id}")


@app.post("/templates/{tid}/try")
async def template_try(request: Request, tid: int):
    """Render unsaved template text against a report, for the live preview in the editor."""
    form = await form_data(request)
    rid = form.get("report_id")
    tpl = db.MailTemplate(name="preview", subject=form.get("subject", ""), body_html=form.get("body_html", ""),
                          body_text=form.get("body_text") or None)
    with db.SessionLocal() as s:
        r = s.get(db.Report, int(rid)) if rid and rid.isdigit() else None
        if r is None:
            r = db.Report(name="Preview (all monitors, previous month)",
                          config={"scope": {"all": True}, "period": {"type": "prev_month"}})
        jobs = runner.plan_jobs(s, r, resolve_transport=False)
        if not jobs:
            return JSONResponse({"subject": "", "html": "<p>No monitors in scope.</p>"})
        try:
            _, subject, html, _ = runner.build(s, r, jobs[0], tpl)
        except render.TemplateError as exc:
            return JSONResponse({"error": str(exc)})
    return JSONResponse({"subject": subject, "html": html})


# ---------------------------------------------------------------- mail transports

@app.get("/mail", response_class=HTMLResponse)
def mail_page(request: Request):
    error = None
    try:
        notifs = kuma.smtp_notifications()
    except Exception as exc:
        notifs, error = [], str(exc)
    with db.SessionLocal() as s:
        transports = list(s.scalars(select(db.SmtpTransport).order_by(db.SmtpTransport.name)))
    edit_id = request.query_params.get("edit")
    return page(request, "mail.html", notifs=notifs, transports=transports, error=error,
                edit=next((t for t in transports if str(t.id) == edit_id), None))


@app.post("/mail/transport")
async def transport_save(request: Request):
    form = await form_data(request)
    with db.SessionLocal() as s:
        tid = form.get("id")
        t = s.get(db.SmtpTransport, int(tid)) if tid and tid.isdigit() else db.SmtpTransport()
        if form.get("action") == "delete" and t.id:
            s.delete(t)
            s.commit()
            flash(request, "Transport deleted.")
            return redirect("/mail")
        t.name = form.get("name", "").strip() or form.get("host", "SMTP")
        t.host = form.get("host", "").strip()
        t.port = int(form.get("port") or 587)
        t.security = form.get("security", "starttls")
        t.username = form.get("username", "").strip() or None
        if form.get("password") or not t.id:
            t.password = form.get("password") or None
        t.from_addr = form.get("from_addr", "").strip()
        t.verify_tls = bool(form.get("verify_tls"))
        s.add(t)
        s.commit()
    flash(request, "Transport saved.")
    return redirect("/mail")


@app.post("/mail/test")
async def transport_test(request: Request):
    form = await form_data(request)
    to = (form.get("to") or "").strip()
    try:
        with db.SessionLocal() as s:
            settings = mailer.resolve(s, form.get("transport"))
        msg = mailer.build_message(settings, [to], [], "Kuma Reports test mail",
                                   "<p>This is a test mail from <b>Kuma Reports</b>.</p>",
                                   "This is a test mail from Kuma Reports.")
        mailer.send(settings, msg, [to])
        flash(request, f"Test mail sent to {to} via {settings.label}.")
    except Exception as exc:
        flash(request, f"Test mail failed: {type(exc).__name__}: {exc}", "error")
    return redirect("/mail")


# ---------------------------------------------------------------- incidents

@app.get("/incidents", response_class=HTMLResponse)
def incidents_page(request: Request, days: int = 30, q: str = "", min_min: float = 0, group: int | None = None):
    days = max(1, min(days, 3660))
    with db.SessionLocal() as s:
        cfg = engine.merged_config({
            "scope": {"all": not group, "groups": [group] if group else [], "include_paused": True},
            "period": {"type": "last_n_days", "days": days},
            "filters": {"min_duration_min": min_min}, "sections": {"compare": False}, "max_incidents": 5000})
        now = datetime.now(timezone.utc)
        period = engine.Period(now.astimezone(config.TIMEZONE) - timedelta(days=days), now.astimezone(config.TIMEZONE), "")
        monitors = engine.resolve_scope(s, cfg["scope"])
        stats = engine.compute_monitors(s, monitors, period, cfg)
        all_mon, _ = engine.monitor_tree(s)
        rows = []
        for st in stats:
            for i in st.incidents:
                rows.append({"i": i, "m": st.monitor, "group": st.group})
        excluded = []
        for n in s.scalars(select(db.IncidentNote).where(db.IncidentNote.excluded.is_(True))):
            sc = s.get(db.StateChange, n.start_id)
            if sc and sc.time >= period.start_utc:
                excluded.append({"note": n, "sc": sc, "m": all_mon.get(sc.monitor_id)})
        groups = [m for m in all_mon.values() if m.type == "group" and not m.deleted]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["m"].name.lower() or ql in r["group"].lower() or ql in (r["i"].msg or "").lower()]
    rows.sort(key=lambda r: r["i"].start, reverse=True)
    now_naive = db.utcnow()
    return page(request, "incidents.html", rows=rows[:1000], total=len(rows), days=days, q=q, min_min=min_min,
                group=group, groups=sorted(groups, key=lambda g: g.name.lower()), now=now_naive,
                excluded=excluded, to_local=i18n.to_local)


@app.post("/incidents/{start_id}")
async def incident_note(request: Request, start_id: int):
    form = await form_data(request)
    with db.SessionLocal() as s:
        n = s.get(db.IncidentNote, start_id) or db.IncidentNote(start_id=start_id)
        n.note = (form.get("note") or "").strip() or None
        n.excluded = bool(form.get("excluded"))
        if not n.note and not n.excluded:
            if n in s:
                s.delete(n)
        else:
            s.add(n)
        s.commit()
    flash(request, "Incident updated.")
    return redirect(request.headers.get("referer") or "/incidents")


# ---------------------------------------------------------------- history

@app.get("/history", response_class=HTMLResponse)
def history(request: Request, report: int | None = None):
    with db.SessionLocal() as s:
        q = select(db.ReportRun).order_by(db.ReportRun.started_at.desc()).limit(300)
        if report:
            q = q.where(db.ReportRun.report_id == report)
        runs = list(s.scalars(q))
    return page(request, "history.html", runs=runs)


@app.get("/history/{run_id}")
def history_view(run_id: int):
    with db.SessionLocal() as s:
        run = s.get(db.ReportRun, run_id)
        if not run or not run.html:
            raise HTTPException(404, "Archived report not available")
        return HTMLResponse(run.html)


class _BasePath:
    """Serve the app under config.BASE_PATH: strip the prefix before routing, redirect "/" to it.
    Paths without the prefix keep working (e.g. /healthz for Docker and Uptime Kuma)."""

    def __init__(self, inner, prefix: str):
        self.inner, self.prefix = inner, prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == self.prefix or path.startswith(self.prefix + "/"):
                new = path[len(self.prefix):] or "/"
                scope = dict(scope, path=new, raw_path=new.encode())
            elif path == "/" and scope["type"] == "http":
                await RedirectResponse(self.prefix + "/", 307)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


if config.BASE_PATH:
    app = _BasePath(app, config.BASE_PATH)  # noqa: F811 - uvicorn serves this wrapper
