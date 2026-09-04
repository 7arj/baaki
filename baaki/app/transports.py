"""Message delivery. The Outbox is the queue; a transport is the wire.

Every outbound message is persisted before a send is attempted, so a transport failure is a
retry, never a lost reminder — and never a duplicate, because `sent_at` gates the send.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Protocol

from sqlmodel import Session as DBSession, select

from .models import Outbox, OutboxStatus, utcnow

MAX_ATTEMPTS = 5


class PermanentDeliveryError(Exception):
    """Retrying will not help — a bad address, an unopted-in number, a rejected template."""


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


class WhatsAppTransport:
    """WhatsApp Business Cloud API.

    Business-initiated messages must use a template approved by Meta — you cannot send free text
    to someone who hasn't messaged you in the last 24 hours. So the reminder body is passed as
    template parameters, not as a message. Configure the approved template with
    BAAKI_WA_TEMPLATE (default `payment_reminder`), whose body should be a single placeholder,
    e.g. "{{1}}".
    """

    name = "whatsapp"
    API = "https://graph.facebook.com/v21.0"

    def __init__(self):
        self.phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        self.token = os.environ["WHATSAPP_TOKEN"]
        self.template = os.environ.get("BAAKI_WA_TEMPLATE", "payment_reminder")
        self.language = os.environ.get("BAAKI_WA_TEMPLATE_LANG", "en")

    @staticmethod
    def normalise(number: str) -> str:
        """Meta wants digits only, with country code and no +."""
        digits = re.sub(r"\D", "", number or "")
        if len(digits) == 10:          # a bare Indian mobile
            digits = "91" + digits
        return digits

    def send(self, to: str, subject: str, body: str) -> str:
        number = self.normalise(to)
        if len(number) < 11:
            raise PermanentDeliveryError(f"{to!r} is not a valid WhatsApp number")
        payload = {
            "messaging_product": "whatsapp",
            "to": number,
            "type": "template",
            "template": {
                "name": self.template,
                "language": {"code": self.language},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": body}]}],
            },
        }
        req = urllib.request.Request(
            f"{self.API}/{self.phone_number_id}/messages",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            # 4xx other than rate-limiting is a bad request; retrying sends the same thing again.
            if 400 <= e.code < 500 and e.code != 429:
                raise PermanentDeliveryError(f"WhatsApp rejected the message ({e.code}): {detail}") from e
            raise RuntimeError(f"WhatsApp API {e.code}: {detail}") from e
        return f"whatsapp:{data.get('messages', [{}])[0].get('id', 'sent')}"


def build_transport(channel: str = "") -> Transport:
    """Pick a transport. `channel` names one explicitly; otherwise use whatever is configured."""
    if channel == "whatsapp" or (not channel and os.environ.get("WHATSAPP_PHONE_NUMBER_ID")):
        if os.environ.get("WHATSAPP_PHONE_NUMBER_ID"):
            return WhatsAppTransport()
    if channel == "email" or os.environ.get("SMTP_HOST"):
        if os.environ.get("SMTP_HOST"):
            return SMTPTransport()
    return ConsoleTransport()


def channel_for(email: str, phone: str) -> tuple[str, str]:
    """Choose the channel and address for a customer.

    WhatsApp first when it's configured and we have a number — it's where Indian B2B collections
    actually happen and reply rates are far higher than email.
    """
    if phone and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"):
        return "whatsapp", phone
    if email:
        return "email", email
    return ("whatsapp", phone) if phone else ("email", "")


def dispatch_outbox(db: DBSession, org_id: int | None = None, transport: Transport | None = None, limit: int = 100) -> dict:
    """Send everything QUEUED. Messages awaiting approval are untouched by design.

    A transport is resolved per message channel unless one is passed in (tests, seeding).
    """
    forced = transport
    cache: dict[str, Transport] = {}
    q = select(Outbox).where(Outbox.status == OutboxStatus.QUEUED)
    if org_id is not None:
        q = q.where(Outbox.org_id == org_id)
    sent = failed = skipped = 0
    used: set[str] = set()
    for msg in db.exec(q.limit(limit)).all():
        if msg.sent_at:
            continue
        if not msg.to_address:
            msg.status, msg.last_error = OutboxStatus.FAILED, "no email or phone on file for this customer"
            failed += 1
            db.add(msg)
            continue
        if forced is not None:
            t = forced
        else:
            t = cache.get(msg.channel) or cache.setdefault(msg.channel, build_transport(msg.channel))
        used.add(t.name)
        try:
            msg.attempts += 1
            t.send(msg.to_address, msg.subject, msg.body)
            msg.status, msg.sent_at, msg.last_error = OutboxStatus.SENT, utcnow(), ""
            sent += 1
        except PermanentDeliveryError as e:
            # No amount of retrying fixes a rejected address — fail it now and surface it.
            msg.status, msg.last_error = OutboxStatus.FAILED, f"{type(e).__name__}: {str(e)[:200]}"
            failed += 1
        except Exception as e:
            msg.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            if msg.attempts >= MAX_ATTEMPTS:
                msg.status = OutboxStatus.FAILED
                failed += 1
            else:
                skipped += 1  # stays QUEUED for the next pass
        db.add(msg)
    db.commit()
    return {"sent": sent, "failed": failed, "retrying": skipped, "transport": "+".join(sorted(used)) or "none"}
