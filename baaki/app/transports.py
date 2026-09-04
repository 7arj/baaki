"""Message delivery. The Outbox is the queue; a transport is the wire.

Every outbound message is persisted before a send is attempted, so a transport failure is a
retry, never a lost reminder — and never a duplicate, because `sent_at` gates the send.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Protocol

from sqlmodel import Session as DBSession, select

from .models import Outbox, OutboxStatus, utcnow

MAX_ATTEMPTS = 5


class Transport(Protocol):
    name: str

    def send(self, to: str, subject: str, body: str) -> str: ...


class ConsoleTransport:
    """Default. Renders the message to stdout so a merchant can dry-run before connecting SMTP."""

    name = "console"

    def send(self, to: str, subject: str, body: str) -> str:
        print(f"\n─── to {to} ───\n{subject}\n\n{body}\n───────────────\n", flush=True)
        return "console"


class SMTPTransport:
    name = "smtp"

    def __init__(self):
        self.host = os.environ["SMTP_HOST"]
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("SMTP_FROM", self.user)

    def send(self, to: str, subject: str, body: str) -> str:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)
        return f"smtp:{self.host}"


def build_transport() -> Transport:
    return SMTPTransport() if os.environ.get("SMTP_HOST") else ConsoleTransport()


def dispatch_outbox(db: DBSession, org_id: int | None = None, transport: Transport | None = None, limit: int = 100) -> dict:
    """Send everything QUEUED. Messages awaiting approval are untouched by design."""
    transport = transport or build_transport()
    q = select(Outbox).where(Outbox.status == OutboxStatus.QUEUED)
    if org_id is not None:
        q = q.where(Outbox.org_id == org_id)
    sent = failed = skipped = 0
    for msg in db.exec(q.limit(limit)).all():
        if msg.sent_at:
            continue
        if not msg.to_address:
            msg.status, msg.last_error = OutboxStatus.FAILED, "no email or phone on file for this customer"
            failed += 1
            db.add(msg)
            continue
        try:
            msg.attempts += 1
            transport.send(msg.to_address, msg.subject, msg.body)
            msg.status, msg.sent_at, msg.last_error = OutboxStatus.SENT, utcnow(), ""
            sent += 1
        except Exception as e:
            msg.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            if msg.attempts >= MAX_ATTEMPTS:
                msg.status = OutboxStatus.FAILED
                failed += 1
            else:
                skipped += 1  # stays QUEUED for the next pass
        db.add(msg)
    db.commit()
    return {"sent": sent, "failed": failed, "retrying": skipped, "transport": transport.name}
