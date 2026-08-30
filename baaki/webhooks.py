"""Razorpay webhook ingestion. Same code path for the simulator and for real test-mode webhooks."""

from __future__ import annotations

import json
from typing import Any

from .audit import AuditLog
from .domain import Invoice, InvoiceStatus, rupees
from .razorpay_client import verify_webhook


class WebhookError(Exception):
    pass


def apply_payment(inv: Invoice, payment_id: str, amount_paise: int, day: int, audit: AuditLog, source: str, **extra: Any) -> int:
    """Credit a payment to an invoice idempotently. Returns paise actually credited."""
    if payment_id in inv.payment_ids_seen:
        audit.record("payment_duplicate_ignored", day=day, invoice=inv.id, payment_id=payment_id)
        return 0
    inv.payment_ids_seen.add(payment_id)
    credited = min(amount_paise, inv.outstanding_paise)
    inv.amount_paid_paise += credited
    if inv.plan and inv.plan.accepted:
        inv.plan.paid_installments += 1
    written_off = 0
    if inv.settlement_amount_paise and inv.amount_paid_paise >= inv.settlement_amount_paise and inv.outstanding_paise > 0:
        written_off = inv.outstanding_paise
        inv.amount_paid_paise = inv.amount_paise  # settle in full; the discount is a write-off
    if inv.outstanding_paise == 0:
        inv.status = InvoiceStatus.PAID
        inv.next_action_day = 10**6
    elif inv.status in (InvoiceStatus.OPEN,):
        inv.status = InvoiceStatus.PARTIALLY_PAID
    inv.log(day, "payment", f"received {rupees(credited)} via {source}" + (f" (settlement; {rupees(written_off)} written off)" if written_off else ""), payment_id=payment_id)
    audit.record("payment_received", day=day, invoice=inv.id, payment_id=payment_id, amount_paise=credited, source=source, written_off_paise=written_off, status=inv.status.value, **extra)
    return credited


def handle_razorpay_webhook(body: bytes, signature: str, secret: str, invoices: dict[str, Invoice], link_index: dict[str, str], audit: AuditLog, day: int) -> dict[str, Any]:
    if not verify_webhook(body, signature, secret):
        audit.record("webhook_rejected", day=day, reason="bad signature")
        raise WebhookError("invalid X-Razorpay-Signature")
    event = json.loads(body)
    kind = event.get("event")
    if kind not in ("payment_link.paid", "payment_link.partially_paid"):
        audit.record("webhook_ignored", day=day, event=kind)
        return {"status": "ignored", "event": kind}
    link = event["payload"]["payment_link"]["entity"]
    payment = event["payload"]["payment"]["entity"]
    inv_id = link_index.get(link["id"]) or (link.get("notes") or {}).get("invoice_id")
    inv = invoices.get(inv_id) if inv_id else None
    if inv is None:
        audit.record("webhook_unmatched", day=day, event=kind, link_id=link["id"])
        return {"status": "unmatched", "link_id": link["id"]}
    credited = apply_payment(inv, payment["id"], int(payment["amount"]), day, audit, source="razorpay_link", link_id=link["id"], webhook_event=kind)
    return {"status": "ok", "invoice": inv.id, "credited_paise": credited, "invoice_status": inv.status.value}
