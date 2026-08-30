import json

from baaki.audit import AuditLog, verify
from baaki.domain import Invoice, InvoiceStatus
from baaki.razorpay_client import FakeRazorpay, sign_webhook
from baaki.webhooks import WebhookError, handle_razorpay_webhook


def test_audit_chain_detects_tampering(tmp_path):
    p = tmp_path / "a.jsonl"
    log = AuditLog(p)
    for i in range(5):
        log.record("x", i=i)
    ok, msg = verify(p)
    assert ok and "5 entries" in msg
    lines = p.read_text().splitlines()
    e = json.loads(lines[2])
    e["i"] = 99
    lines[2] = json.dumps(e)
    p.write_text("\n".join(lines) + "\n")
    ok, msg = verify(p)
    assert not ok and "seq 2" in msg


def test_webhook_signature_and_idempotency():
    rzp = FakeRazorpay()
    inv = Invoice(id="inv_1", debtor_id="c", amount_paise=10_000_00, issue_day=-30, due_day=-5, description="t")
    link = rzp.create({"amount": 10_000_00, "accept_partial": True, "first_min_partial_amount": 2_000_00, "notes": {"invoice_id": "inv_1"}})
    audit = AuditLog()
    body, sig = rzp.simulate_payment(link["id"], 4_000_00)
    res = handle_razorpay_webhook(body, sig, rzp.webhook_secret, {"inv_1": inv}, {link["id"]: "inv_1"}, audit, 1)
    assert res["credited_paise"] == 4_000_00 and inv.status == InvoiceStatus.PARTIALLY_PAID
    # replaying the same webhook must not double-credit
    res2 = handle_razorpay_webhook(body, sig, rzp.webhook_secret, {"inv_1": inv}, {link["id"]: "inv_1"}, audit, 1)
    assert res2["credited_paise"] == 0 and inv.amount_paid_paise == 4_000_00
    # tampered body -> rejected
    try:
        handle_razorpay_webhook(body + b" ", sig, rzp.webhook_secret, {"inv_1": inv}, {}, audit, 1)
        assert False, "should reject"
    except WebhookError:
        pass
    body2, sig2 = rzp.simulate_payment(link["id"], 6_000_00)
    handle_razorpay_webhook(body2, sig2, rzp.webhook_secret, {"inv_1": inv}, {link["id"]: "inv_1"}, audit, 3)
    assert inv.status == InvoiceStatus.PAID and rzp.fetch(link["id"])["status"] == "paid"
    assert sign_webhook(body2, rzp.webhook_secret) == sig2
