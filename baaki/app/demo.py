"""Seed a realistic demo tenant: a ledger with history, payments, replies and escalations.

Used for `python -m baaki demo`. Everything it creates goes through the same import, engine and
webhook paths a real merchant uses — nothing is written straight into the tables to fake a state.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta

from sqlmodel import Session, delete, select

from ..razorpay_client import sign_webhook
from .db import engine, init_db
from .models import (
    AuditRow, Customer, Event, InvoiceRow, Org, Outbox, PaymentRow, Plan, PolicySettings,
    SubscriptionStatus, User, utcnow,
)
from .security import hash_password
from .service import DbAudit, RecoveryEngine, import_csv, record_payment
from .transports import dispatch_outbox

EMAIL = "demo@baaki.app"
PASSWORD = "baaki-demo-2026"
COMPANY = "Sharma Industrial Supplies"

# Amounts stay under Rs 50,000: Razorpay refuses larger payment links until KYC is complete,
# so this ledger produces real checkouts even on a fresh test account.
LEDGER = [
    # (number, customer, email, amount ₹, days overdue, description)
    ("INV-2041", "Mehta Traders", "accounts@mehtatraders.in", 48866.00, 46, "Printed labels — PO 1647"),
    ("INV-2042", "Chawla Hardware", "accounts@chawlahardware.in", 34965.06, 39, "MS pipes — PO 2213"),
    ("INV-2043", "Nair Logistics", "ap@nairlogistics.in", 45253.00, 34, "Pallets — PO 2288"),
    ("INV-2044", "Iyer Hardware", "accounts@iyerhardware.in", 18187.00, 28, "Fasteners — PO 2301"),
    ("INV-2045", "Bose Electricals", "finance@boseelectricals.in", 42240.00, 25, "Packaging film — PO 2344"),
    ("INV-2046", "Kavya Agro", "accounts@kavyaagro.in", 32039.00, 21, "Corrugated boxes — PO 2350"),
    ("INV-2047", "Reddy Logistics", "ap@reddylogistics.in", 40066.00, 19, "CNC job work — PO 2361"),
    ("INV-2048", "Joshi Enterprises", "accounts@joshient.in", 45372.00, 16, "Dyes & chemicals — PO 2370"),
    ("INV-2049", "Mishra Ceramics", "finance@mishraceramics.in", 12229.00, 14, "Spare parts — PO 2381"),
    ("INV-2050", "Patel Electricals", "accounts@patelelec.in", 35624.00, 12, "Cotton yarn lot — PO 2390"),
    ("INV-2051", "Das Hardware", "ap@dashardware.in", 9316.00, 9, "Office fit-out — PO 2401"),
    ("INV-2052", "Verma Pharma Distributors", "accounts@vermapharma.in", 47400.00, 7, "Packaging film — PO 2410"),
    ("INV-2053", "Singh Textiles", "finance@singhtextiles.in", 23487.00, 5, "Printed labels — PO 2418"),
    ("INV-2054", "Pillai Traders", "accounts@pillaitraders.in", 36800.00, 3, "Fasteners — PO 2425"),
]

# Replies that arrive during the demo run, keyed by invoice number.
REPLIES = {
    "INV-2041": "This invoice is wrong — 20 cartons were short-shipped. Not paying till corrected.",
    "INV-2043": "Our client payment comes on the 10th, will clear by then.",
    "INV-2046": "Can we pay half now and the rest next month?",
    "INV-2050": "Business is shut since June, we have no funds right now.",
    "INV-2052": "Stop messaging us. Speak to our lawyer.",
}
# Invoices whose customers simply pay.
PAYS_IN_FULL = ["INV-2044", "INV-2049", "INV-2053"]
PAYS_PARTIAL = {"INV-2042": 0.4}


def _wipe(db: Session, org: Org) -> None:
    for model in (AuditRow, Event, Outbox, PaymentRow, InvoiceRow, Customer, PolicySettings):
        db.exec(delete(model).where(model.org_id == org.id))
    db.exec(delete(User).where(User.org_id == org.id))
    db.delete(org)
    db.commit()


def seed(reset: bool = True) -> dict:
    init_db()
    rng = random.Random(4)
    with Session(engine()) as db:
        existing = db.exec(select(Org).where(Org.slug == "sharma-industrial-supplies")).first()
        if existing:
            if not reset:
                return {"org": existing.slug, "note": "already seeded"}
            _wipe(db, existing)

        org = Org(name=COMPANY, slug="sharma-industrial-supplies", legal_name=COMPANY,
                  reply_to_email="accounts@sharmaindustrial.in", support_phone="+91 80 4718 2200",
                  plan=Plan.GROWTH, subscription_status=SubscriptionStatus.ACTIVE,
                  rzp_subscription_id="sub_demo_growth", agent_enabled=True, approval_required=False)
        # When the operator has test-mode keys in the environment, the demo tenant collects on
        # them: every link the seed creates is then a real rzp.io checkout. Live keys never.
        import os

        from .security import encrypt_secret

        if os.environ.get("RAZORPAY_KEY_ID", "").startswith("rzp_test_") and os.environ.get("RAZORPAY_KEY_SECRET"):
            org.rzp_key_id = os.environ["RAZORPAY_KEY_ID"]
            org.rzp_key_secret_enc = encrypt_secret(os.environ["RAZORPAY_KEY_SECRET"])
        db.add(org); db.commit(); db.refresh(org)
        db.add(PolicySettings(org_id=org.id))
        # Verified at seed time: it is not a real inbox, and the banner nagging to confirm it is
        # noise on an account whose whole purpose is demonstration.
        db.add(User(org_id=org.id, email=EMAIL, name="Arjun Sharma", password_hash=hash_password(PASSWORD),
                    email_verified_at=utcnow()))
        db.commit()

        today = date.today()
        rows = ["invoice_number,customer_name,customer_email,customer_phone,amount_inr,issued_on,due_on,description"]
        for num, cust, email, amt, overdue, desc in LEDGER:
            due = today - timedelta(days=overdue)
            issued = due - timedelta(days=30)
            phone = f"+9198{rng.randint(10000000, 99999999)}"
            rows.append(f"{num},{cust},{email},{phone},{amt:.2f},{issued},{due},{desc}")
        import_csv(db, org, "\n".join(rows).encode())
        # import_csv schedules new invoices for the real today; the seed's first pass is
        # backdated, so pull them into its day or it finds nothing due.
        for inv in db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id)).all():
            inv.next_action_on = today - timedelta(days=8)
            db.add(inv)
        db.commit()

        # Day 1: first contact for everything due.
        RecoveryEngine(db, org, today=today - timedelta(days=8)).run()
        dispatch_outbox(db, org.id, transport=_Silent())

        # Customers respond.
        audit = DbAudit(db, org.id, actor="razorpay")
        for number, text in REPLIES.items():
            inv = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.number == number)).first()
            inv.last_inbound_text, inv.inbound_pending = text, True
            inv.next_action_on = today - timedelta(days=6)
            db.add(inv)
            db.add(Event(org_id=org.id, invoice_id=inv.id, kind="inbound", summary=text))
        for number in PAYS_IN_FULL:
            inv = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.number == number)).first()
            record_payment(db, org.id, inv, f"pay_demo_{number}", inv.outstanding_paise, "razorpay_link", audit)
        for number, frac in PAYS_PARTIAL.items():
            inv = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.number == number)).first()
            record_payment(db, org.id, inv, f"pay_demo_{number}", int(inv.outstanding_paise * frac), "razorpay_link", audit)
        db.commit()

        # Day 2: the agent reads the replies — disputes escalate, cease requests stop contact.
        RecoveryEngine(db, org, today=today - timedelta(days=5)).run()
        dispatch_outbox(db, org.id, transport=_Silent())

        # Day 3: today's pass leaves a few messages awaiting a human, to show the approval queue.
        # Pinned to mid-morning: a seed run in the evening must not produce an empty queue just
        # because the agent correctly refuses to draft outside contact hours.
        org.approval_required = True
        db.add(org); db.commit()
        from datetime import datetime, time as dtime

        from ..domain import IST

        RecoveryEngine(db, org, today=today, at=datetime.combine(today, dtime(10, 0), IST)).run()

        counts = {
            "invoices": len(db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id)).all()),
            "paid": len(db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.status == "paid")).all()),
            "escalated": len(db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.status == "escalated")).all()),
            "stopped": len(db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.status == "stopped")).all()),
            "pending_approval": len(db.exec(select(Outbox).where(Outbox.org_id == org.id, Outbox.status == "pending_approval")).all()),
            "audit_entries": len(db.exec(select(AuditRow).where(AuditRow.org_id == org.id)).all()),
        }
        return {"org": org.slug, "email": EMAIL, "password": PASSWORD, **counts}


class _Silent:
    """Seeding shouldn't spam the console with a hundred rendered emails."""

    name = "silent"

    def send(self, to: str, subject: str, body: str) -> str:
        return "silent"
