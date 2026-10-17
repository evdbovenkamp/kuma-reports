"""Rendering of report contexts into mail subject/HTML/text and attachments (PDF, CSV, XLSX)."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader, Undefined
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup, escape
from sqlalchemy import select

from . import db
from .i18n import fmt_dt, fmt_duration, fmt_pct

TEMPLATE_DIR = Path(__file__).parent / "mail_templates"


def _nl2br(value) -> Markup:
    return Markup("<br>".join(escape(str(value or "")).split("\n")))


def _env(autoescape: bool, extra: dict[str, str] | None = None) -> SandboxedEnvironment:
    loader = ChoiceLoader([DictLoader(extra or {}), FileSystemLoader(str(TEMPLATE_DIR))])
    env = SandboxedEnvironment(loader=loader, autoescape=autoescape, undefined=Undefined,
                               trim_blocks=True, lstrip_blocks=True)
    env.filters.update({"nl2br": _nl2br, "duration": fmt_duration, "pct": fmt_pct, "dt": fmt_dt})
    return env


_html_env = _env(True)
_text_env = _env(False)


class TemplateError(Exception):
    pass


def render_mail(tpl: db.MailTemplate, ctx: dict) -> tuple[str, str, str]:
    """Returns (subject, html, text)."""
    try:
        subject = _text_env.from_string(tpl.subject or "{{ report.title }}").render(ctx)
        html = _html_env.from_string(tpl.body_html or "").render(ctx)
        text = _text_env.from_string(tpl.body_text).render(ctx) if tpl.body_text else html_to_text(html)
    except Exception as exc:  # syntax and runtime errors from user templates
        lineno = getattr(exc, "lineno", None)
        raise TemplateError(f"{type(exc).__name__}: {exc}" + (f" (line {lineno})" if lineno else "")) from exc
    return " ".join(subject.split()), html, text.strip() + "\n"


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    import html as html_mod
    text = html_mod.unescape(text)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


# ---------------------------------------------------------------- built-in templates

def builtin_definitions() -> list[dict]:
    return json.loads((TEMPLATE_DIR / "builtin.json").read_text())


def builtin_content(defn: dict) -> dict:
    return {
        "key": defn["key"], "name": defn["name"], "description": defn.get("description", ""),
        "subject": defn["subject"],
        "body_html": (TEMPLATE_DIR / defn["html"]).read_text(),
        "body_text": (TEMPLATE_DIR / defn["text"]).read_text() if defn.get("text") else None,
    }


def seed_builtin_templates(session) -> None:
    existing = {t.key for t in session.scalars(select(db.MailTemplate).where(db.MailTemplate.key.is_not(None)))}
    for defn in builtin_definitions():
        if defn["key"] not in existing:
            session.add(db.MailTemplate(**builtin_content(defn)))
    session.commit()


def reset_builtin(session, tpl: db.MailTemplate) -> None:
    for defn in builtin_definitions():
        if defn["key"] == tpl.key:
            for k, v in builtin_content(defn).items():
                setattr(tpl, k, v)


def macros_source() -> str:
    return (TEMPLATE_DIR / "macros.html").read_text()


# ---------------------------------------------------------------- attachments

MONITOR_COLUMNS = [("id", "ID"), ("name", "Monitor"), ("group", "Group"), ("type", "Type"), ("url", "URL"),
                   ("uptime", "Availability %"), ("incidents", "Incidents"), ("downtime", "Downtime (s)"),
                   ("longest", "Longest (s)"), ("maintenance", "Maintenance (s)"), ("checks_up", "Checks up"),
                   ("checks_down", "Checks down"), ("ping_avg", "Avg response (ms)"), ("sla_met", "SLA met")]
INCIDENT_COLUMNS = [("monitor_id", "Monitor ID"), ("monitor", "Monitor"), ("group", "Group"), ("kind", "Kind"),
                    ("start", "Start"), ("end", "End"), ("duration", "Duration (s)"),
                    ("downtime", "Downtime in period (s)"), ("message", "Message"), ("note", "Note")]


def _cell(v):
    if hasattr(v, "isoformat"):
        return v.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    if isinstance(v, float):
        return round(v, 4)
    return v


def to_csv(rows: list[dict], columns) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([label for _, label in columns])
    for r in rows:
        w.writerow([_cell(r.get(k)) if r.get(k) is not None else "" for k, _ in columns])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM: Excel opens UTF-8 correctly


def to_xlsx(ctx: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    s = ctx["summary"]
    for row in [("Report", ctx["report"]["title"]), ("Period", ctx["report"]["period"]),
                ("Monitors", s["monitors"]), ("Availability %", s["uptime"]), ("SLA target %", ctx["sla_target"]),
                ("Incidents", s["incidents"]), ("Downtime (s)", s["downtime"]), ("MTTR (s)", s["mttr"]),
                ("Longest incident (s)", s["longest"]), ("SLA met", s["sla_met"]), ("SLA missed", s["sla_missed"])]:
        ws.append([row[0], _cell(row[1])])
    for name, rows, cols in [("Monitors", ctx["monitors"], MONITOR_COLUMNS),
                             ("Incidents", ctx["incidents"], INCIDENT_COLUMNS),
                             ("Series", ctx["series"], [("label", "Bucket"), ("uptime", "Availability %"),
                                                        ("incidents", "Incidents"), ("downtime", "Downtime (s)")])]:
        sh = wb.create_sheet(name)
        sh.append([label for _, label in cols])
        for c in sh[1]:
            c.font = Font(bold=True)
        for r in rows:
            sh.append([_cell(r.get(k)) for k, _ in cols])
        sh.freeze_panes = "A2"
        for col in sh.columns:
            width = max(len(str(c.value or "")) for c in col[:200])
            sh.column_dimensions[col[0].column_letter].width = min(60, max(10, width + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(html: str) -> bytes:
    try:
        from weasyprint import CSS, HTML
    except ImportError as exc:  # pragma: no cover - depends on system libraries
        raise RuntimeError("PDF output needs WeasyPrint (included in the Docker image)") from exc
    # Mail layouts use a grey canvas and a ~760px column; drop the canvas and scale to A4.
    html = html.replace("background:#f3f4f6", "background:#ffffff")
    page_css = CSS(string="@page { size: A4; margin: 10mm 8mm; } .kr-canvas, .kr-content { padding: 0 !important; } "
                          "tr { page-break-inside: avoid; }")
    return HTML(string=html).write_pdf(stylesheets=[page_css], zoom=0.85, presentational_hints=True)


def attachments(ctx: dict, html: str, delivery: dict, basename: str) -> list[tuple[str, str, bytes]]:
    out = []
    if delivery.get("attach_pdf"):
        out.append((f"{basename}.pdf", "application/pdf", to_pdf(html)))
    if delivery.get("attach_csv"):
        out.append((f"{basename}-monitors.csv", "text/csv", to_csv(ctx["monitors"], MONITOR_COLUMNS)))
        out.append((f"{basename}-incidents.csv", "text/csv", to_csv(ctx["incidents"], INCIDENT_COLUMNS)))
    if delivery.get("attach_xlsx"):
        out.append((f"{basename}.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", to_xlsx(ctx)))
    return out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "report"
