"""Own (local) database: configuration, imported state changes and run history."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
    create_engine, event, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import config

engine = create_engine(
    f"sqlite:///{config.DATA_DIR / 'kuma-reports.db'}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class Monitor(Base):
    """Snapshot of Kuma monitors; kept after deletion in Kuma so history stays readable."""
    __tablename__ = "monitor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # Kuma monitor id
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    interval: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)       # UTC, from Kuma
    tags: Mapped[list] = mapped_column(JSON, default=list)            # [{id, name, color, value}]
    notification_ids: Mapped[list] = mapped_column(JSON, default=list)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Tag(Base):
    __tablename__ = "tag"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    color: Mapped[str | None] = mapped_column(String(32))


class StateChange(Base):
    """Kuma 'important' heartbeats: every transition between up/down/maintenance."""
    __tablename__ = "state_change"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # Kuma heartbeat id
    monitor_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[int] = mapped_column(Integer)                 # 0 down, 1 up, 2 pending, 3 maintenance
    time: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    msg: Mapped[str | None] = mapped_column(Text)


class DailyStat(Base):
    __tablename__ = "daily_stat"
    monitor_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[datetime] = mapped_column(Date, primary_key=True)  # UTC day
    up: Mapped[int] = mapped_column(Integer, default=0)
    down: Mapped[int] = mapped_column(Integer, default=0)
    ping_avg: Mapped[float | None] = mapped_column(Float)
    ping_max: Mapped[float | None] = mapped_column(Float)


class IncidentNote(Base):
    """Annotation on an incident, keyed by the heartbeat id that started it."""
    __tablename__ = "incident_note"
    start_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note: Mapped[str | None] = mapped_column(Text)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SmtpTransport(Base):
    __tablename__ = "smtp_transport"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=587)
    security: Mapped[str] = mapped_column(String(16), default="starttls")  # none|starttls|ssl
    username: Mapped[str | None] = mapped_column(String(255))
    password: Mapped[str | None] = mapped_column(String(255))
    from_addr: Mapped[str] = mapped_column(String(255))
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)


class MailTemplate(Base):
    __tablename__ = "mail_template"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str | None] = mapped_column(String(64), unique=True)  # set for built-in templates
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Report(Base):
    __tablename__ = "report"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule: Mapped[str | None] = mapped_column(String(64))  # crontab, empty = manual only
    template_id: Mapped[int | None] = mapped_column(ForeignKey("mail_template.id", ondelete="SET NULL"))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReportRun(Base):
    __tablename__ = "report_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("report.id", ondelete="SET NULL"), index=True)
    report_name: Mapped[str] = mapped_column(String(255))
    trigger: Mapped[str] = mapped_column(String(16))  # schedule|manual|test
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(16))  # sent|skipped|failed
    message: Mapped[str | None] = mapped_column(Text)
    recipients: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[datetime | None] = mapped_column(DateTime)
    period_end: Mapped[datetime | None] = mapped_column(DateTime)
    html: Mapped[str | None] = mapped_column(Text)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Minimal forward migration: add columns introduced in newer versions (all nullable)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    ddl = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))


def kv_get(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(KV, key)
    return row.value if row else default


def kv_set(session: Session, key: str, value: str | None) -> None:
    row = session.get(KV, key)
    if row:
        row.value = value
    else:
        session.add(KV(key=key, value=value))


__all__ = [n for n in dir() if not n.startswith("_")] + ["select"]
