"""SMTP delivery, using either own transports or SMTP settings of Uptime Kuma notifications."""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from . import db, kuma


@dataclass
class SmtpSettings:
    label: str
    host: str
    port: int
    security: str          # none | starttls | ssl | auto (starttls when offered)
    username: str | None
    password: str | None
    from_addr: str
    verify_tls: bool = True
    default_to: str = ""
    default_cc: str = ""
    default_bcc: str = ""


def from_kuma_notification(n: dict) -> SmtpSettings:
    c = n["config"]
    secure = bool(c.get("smtpSecure"))
    return SmtpSettings(
        label=f"Kuma: {n['name']}",
        host=c.get("smtpHost") or "localhost",
        port=int(c.get("smtpPort") or (465 if secure else 25)),
        security="ssl" if secure else ("none" if c.get("smtpIgnoreSTARTTLS") else "auto"),
        username=c.get("smtpUsername") or None,
        password=c.get("smtpPassword") or None,
        from_addr=c.get("smtpFrom") or "uptime-kuma@localhost",
        verify_tls=not c.get("smtpIgnoreTLSError"),
        default_to=c.get("smtpTo") or "",
        default_cc=c.get("smtpCC") or "",
        default_bcc=c.get("smtpBCC") or "",
    )


def from_transport(t: db.SmtpTransport) -> SmtpSettings:
    return SmtpSettings(label=t.name, host=t.host, port=t.port, security=t.security, username=t.username or None,
                        password=t.password or None, from_addr=t.from_addr, verify_tls=t.verify_tls)


def transport_options(session) -> list[tuple[str, str]]:
    """(value, label) pairs for transport selects."""
    opts = []
    try:
        for n in kuma.smtp_notifications():
            to = n["config"].get("smtpTo") or ""
            opts.append((f"kuma:{n['id']}", f"Kuma notification: {n['name']}" + (f" ({to})" if to else "")))
    except Exception:
        pass
    for t in session.query(db.SmtpTransport).order_by(db.SmtpTransport.name):
        opts.append((f"smtp:{t.id}", f"SMTP: {t.name} ({t.host})"))
    return opts


def resolve(session, value: str | None) -> SmtpSettings:
    value = value or ""
    if value.startswith("smtp:"):
        t = session.get(db.SmtpTransport, int(value[5:]))
        if t:
            return from_transport(t)
    notifs = kuma.smtp_notifications()
    if value.startswith("kuma:"):
        for n in notifs:
            if str(n["id"]) == value[5:]:
                return from_kuma_notification(n)
    # Fallback: first own transport, else Kuma's default SMTP notification, else any.
    t = session.query(db.SmtpTransport).order_by(db.SmtpTransport.id).first()
    if t:
        return from_transport(t)
    for n in sorted(notifs, key=lambda n: (not n["config"].get("isDefault"), n["id"])):
        return from_kuma_notification(n)
    raise RuntimeError("No mail transport configured: add an SMTP transport or an SMTP notification in Kuma")


def build_message(s: SmtpSettings, to: list[str], cc: list[str], subject: str, html: str, text: str,
                  attachments: list[tuple[str, str, bytes]] = ()) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.from_addr
    msg["To"] = ", ".join(to) if to else "undisclosed-recipients:;"
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(s.from_addr.rsplit("@", 1)[-1].strip(" >") or None))
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for filename, mime, data in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def send(s: SmtpSettings, msg: EmailMessage, recipients: list[str]) -> None:
    ctx = ssl.create_default_context()
    if not s.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if s.security == "ssl":
        server = smtplib.SMTP_SSL(s.host, s.port, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(s.host, s.port, timeout=30)
    with server:
        server.ehlo()
        if s.security == "starttls" or (s.security == "auto" and server.has_extn("starttls")):
            server.starttls(context=ctx)
            server.ehlo()
        if s.username:
            server.login(s.username, s.password or "")
        server.send_message(msg, to_addrs=[parseaddr(r)[1] or r for r in recipients])
