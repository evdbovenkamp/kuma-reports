"""Generates and delivers reports: works out recipients, splits, renders and sends."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from . import db, engine, kuma, mailer, render

log = logging.getLogger(__name__)


@dataclass
class Job:
    label: str                     # shown in history / preview selector
    monitors: list[db.Monitor]
    to: list[str]
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    transport: mailer.SmtpSettings | None = None
    title_suffix: str = ""


def _uniq(items):
    seen, out = set(), []
    for i in items:
        if i and i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out


def plan_jobs(session, report: db.Report, *, resolve_transport: bool = True) -> list[Job]:
    cfg = engine.merged_config(report.config)
    d = cfg["delivery"]
    monitors = engine.resolve_scope(session, cfg["scope"])
    manual_to = kuma.split_addresses(d.get("recipients"))
    manual_cc = kuma.split_addresses(d.get("cc"))
    transport_value = d.get("transport") or ""
    use_own_notification_smtp = transport_value == "notification"

    def transport(default=None):
        if not resolve_transport:
            return None
        if use_own_notification_smtp and default:
            return default
        return mailer.resolve(session, None if use_own_notification_smtp else transport_value)

    if d.get("split") == "notification" or d.get("kuma_recipients"):
        notifs = {n["id"]: n for n in kuma.smtp_notifications()}
    else:
        notifs = {}

    if d.get("split") == "notification":
        jobs = []
        for nid, n in sorted(notifs.items(), key=lambda kv: kv[1]["name"].lower()):
            subset = [m for m in monitors if nid in (m.notification_ids or [])]
            if not subset:
                continue
            s = mailer.from_kuma_notification(n)
            jobs.append(Job(label=n["name"], monitors=subset,
                            to=_uniq(kuma.split_addresses(s.default_to)),
                            cc=_uniq(kuma.split_addresses(s.default_cc) + manual_cc),
                            bcc=_uniq(manual_to),
                            transport=transport(s), title_suffix=n["name"]))
        return jobs

    to = list(manual_to)
    if d.get("kuma_recipients"):
        linked = {nid for m in monitors for nid in (m.notification_ids or [])}
        for nid in sorted(linked):
            if nid in notifs:
                to += kuma.split_addresses(notifs[nid]["config"].get("smtpTo"))
    return [Job(label="all", monitors=monitors, to=_uniq(to), cc=_uniq(manual_cc), transport=transport())]


def template_for(session, report: db.Report) -> db.MailTemplate:
    tpl = session.get(db.MailTemplate, report.template_id) if report.template_id else None
    if tpl is None:
        tpl = session.scalars(select(db.MailTemplate).where(db.MailTemplate.key == "standard")).first()
    if tpl is None:
        tpl = session.scalars(select(db.MailTemplate).order_by(db.MailTemplate.id)).first()
    return tpl


def build(session, report: db.Report, job: Job, template: db.MailTemplate | None = None):
    cfg = engine.merged_config(report.config)
    if job.title_suffix:
        cfg["title"] = f"{cfg.get('title') or report.name} — {job.title_suffix}"
    ctx = engine.build_context(session, report.name, cfg, job.monitors)
    subject, html, text = render.render_mail(template or template_for(session, report), ctx)
    return ctx, subject, html, text


def run_report(report_id: int, trigger: str = "schedule", override_to: str | None = None) -> list[db.ReportRun]:
    """Generate and send a report. With override_to, all mails go only to that address (test)."""
    runs = []
    with db.SessionLocal() as session:
        report = session.get(db.Report, report_id)
        if report is None:
            return []
        cfg = engine.merged_config(report.config)
        d = cfg["delivery"]
        try:
            jobs = plan_jobs(session, report)
        except Exception as exc:
            log.exception("Planning report %s failed", report.name)
            run = db.ReportRun(report_id=report.id, report_name=report.name, trigger=trigger,
                               status="failed", message=str(exc))
            session.add(run)
            session.commit()
            return [run]
        if not jobs:
            run = db.ReportRun(report_id=report.id, report_name=report.name, trigger=trigger, status="skipped",
                               message="No monitors / recipients in scope")
            session.add(run)
            session.commit()
            return [run]

        for job in jobs:
            run = db.ReportRun(report_id=report.id, report_name=report.name + (f" — {job.label}" if job.label != "all" else ""),
                               trigger=trigger, status="failed")
            session.add(run)
            try:
                ctx, subject, html, text = build(session, report, job)
                run.subject, run.html = subject, html
                run.period_start = ctx["report"]["period_start"].replace(tzinfo=None)
                run.period_end = ctx["report"]["period_end"].replace(tzinfo=None)
                to, cc, bcc = job.to, job.cc, job.bcc
                if override_to:
                    to, cc, bcc = kuma.split_addresses(override_to), [], []
                run.recipients = ", ".join(to + [f"cc:{a}" for a in cc] + [f"bcc:{a}" for a in bcc])
                if d.get("only_if_incidents") and not ctx["has_incidents"] and not override_to:
                    run.status, run.message = "skipped", "No incidents in period"
                elif not (to or cc or bcc):
                    run.status, run.message = "skipped", "No recipients"
                else:
                    files = render.attachments(ctx, html, d, render.slug(f"{report.name}-{ctx['report']['period']}"))
                    msg = mailer.build_message(job.transport, to, cc, subject, html, text, files)
                    mailer.send(job.transport, msg, _uniq(to + cc + bcc))
                    run.status = "sent"
                    run.message = f"via {job.transport.label}" + (f", {len(files)} attachment(s)" if files else "")
            except Exception as exc:
                log.exception("Report %s (%s) failed", report.name, job.label)
                run.status, run.message = "failed", f"{type(exc).__name__}: {exc}"
            session.commit()
            runs.append(run)
    return runs
