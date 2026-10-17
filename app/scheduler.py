"""Background jobs: periodic import from Kuma and cron-scheduled reports."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from . import config, db, importer, memory, runner

log = logging.getLogger(__name__)

SCHEDULE_PRESETS = {
    "": "Manual only",
    "0 7 * * *": "Daily at 07:00",
    "0 7 * * 1-5": "Weekdays at 07:00",
    "0 7 * * 1": "Weekly, Monday 07:00",
    "0 7 1 * *": "Monthly, 1st at 07:00",
    "0 7 1 1,4,7,10 *": "Quarterly, 1st at 07:00",
    "0 7 1 1 *": "Yearly, January 1st 07:00",
}

scheduler = BackgroundScheduler(timezone=config.TIMEZONE, job_defaults={"coalesce": True, "misfire_grace_time": 3600})


def parse_cron(expr: str) -> CronTrigger:
    return CronTrigger.from_crontab(expr.strip(), timezone=config.TIMEZONE)


def _run_scheduled(report_id: int) -> None:
    importer.run_import()  # make sure the latest state changes are in
    runner.run_report(report_id, "schedule")
    memory.release()


def _run_import() -> None:
    importer.run_import()
    memory.release()


def sync_report_jobs() -> None:
    wanted = {}
    with db.SessionLocal() as session:
        for r in session.scalars(select(db.Report).where(db.Report.enabled.is_(True))):
            if r.schedule and r.schedule.strip():
                wanted[f"report-{r.id}"] = (r.id, r.schedule)
    for job in scheduler.get_jobs():
        if job.id.startswith("report-") and job.id not in wanted:
            job.remove()
    for job_id, (rid, expr) in wanted.items():
        try:
            trigger = parse_cron(expr)
        except ValueError:
            log.warning("Report %s has invalid schedule %r", rid, expr)
            continue
        existing = scheduler.get_job(job_id)
        if existing and str(existing.trigger) == str(trigger):
            continue
        scheduler.add_job(_run_scheduled, trigger, id=job_id, args=[rid], replace_existing=True)


def next_run(report_id: int) -> datetime | None:
    job = scheduler.get_job(f"report-{report_id}")
    return job.next_run_time if job else None


def is_healthy() -> bool:
    job = scheduler.get_job("import") if scheduler.running else None
    return bool(job and job.next_run_time)


def start() -> None:
    scheduler.add_job(_run_import, "interval", seconds=config.IMPORT_INTERVAL, id="import",
                      next_run_time=datetime.now(config.TIMEZONE), max_instances=1)
    scheduler.add_job(_cleanup, "cron", hour=3, minute=17, id="cleanup")
    scheduler.start()
    sync_report_jobs()


def _cleanup() -> None:
    """Drop archived HTML of old runs (keeps the run records)."""
    cutoff = db.utcnow() - timedelta(days=config.HISTORY_KEEP_DAYS)
    with db.SessionLocal() as session:
        for run in session.scalars(select(db.ReportRun).where(db.ReportRun.started_at < cutoff,
                                                               db.ReportRun.html.is_not(None))):
            run.html = None
        session.commit()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
