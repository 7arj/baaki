"""End-to-end product tests: auth, tenancy isolation, import, agent run, approvals, billing, webhooks."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from baaki.app import accounts, db as db_mod
from baaki.app.db import init_db, make_engine
from baaki.app.models import InvoiceRow, Org, Outbox, OutboxStatus, Plan, Role, TokenPurpose, User
from baaki.app.service import DbAudit, verify_chain
from baaki.app.web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(db_mod, "_engine", eng)
    init_db(eng)
    return TestClient(create_app(), follow_redirects=True)


def _csrf(client) -> str:
    client.get("/signup")
    return client.cookies.get("baaki_csrf")


def signup(client, company="Sharma Supplies", email="owner@sharma.in", password="a-good-passphrase"):
    token = _csrf(client)
    return client.post("/signup", data={"company": company, "name": "Owner", "email": email,
                                        "password": password, "csrf_token": token})


def verify_email(client, email="owner@sharma.in"):
    """Click the confirmation link, the way a new user would."""
    with Session(db_mod.engine()) as s:
        user = s.exec(select(User).where(User.email == email)).first()
        raw = accounts.issue_token(s, TokenPurpose.VERIFY_EMAIL, org_id=user.org_id,
                                   user_id=user.id, email=user.email)
    return client.get(f"/verify?token={raw}")


# ---- auth ---------------------------------------------------------------------------------
def test_signup_login_logout_cycle(client):
    r = signup(client)
    assert r.status_code == 200 and "Import invoices" in r.text
    assert client.get("/app").status_code == 200

    csrf = client.cookies.get("baaki_csrf")
    client.post("/logout", data={"csrf_token": csrf})
    # Session revoked: the app redirects to the sign-in page
    assert "Please sign in" in client.get("/app").text or "Sign in" in client.get("/app").text

    r = client.post("/login", data={"email": "owner@sharma.in", "password": "a-good-passphrase", "csrf_token": csrf})
    assert r.status_code == 200 and "Dashboard" in r.text


def test_weak_password_and_duplicate_email_are_rejected(client):
    r = client.post("/signup", data={"company": "X", "name": "", "email": "a@b.in", "password": "short",
                                     "csrf_token": _csrf(client)})
    assert "at least 10 characters" in r.text
    signup(client, email="dup@x.in")
    r = signup(client, company="Other", email="dup@x.in")
    assert "already exists" in r.text


def test_wrong_password_gives_no_account_oracle(client):
    signup(client, email="real@x.in")
    csrf = client.cookies.get("baaki_csrf")
    a = client.post("/login", data={"email": "real@x.in", "password": "wrong-password", "csrf_token": csrf}).text
    b = client.post("/login", data={"email": "ghost@x.in", "password": "wrong-password", "csrf_token": csrf}).text
    assert "Email or password is incorrect" in a and "Email or password is incorrect" in b


def test_csrf_is_enforced(client):
    signup(client)
    r = client.post("/app/settings/profile", data={"legal_name": "Hacked", "csrf_token": "bogus"})
    assert r.status_code == 403


def test_anonymous_cannot_reach_the_app(client):
    assert "Sign in" in client.get("/app/invoices").text
    assert "Sign in" in client.get("/app/settings").text


# ---- tenancy ------------------------------------------------------------------------------
def test_one_org_cannot_read_anothers_invoice(client, tmp_path):
    signup(client, company="Org A", email="a@a.in")
    client.post("/app/import/demo", data={"csrf_token": client.cookies.get("baaki_csrf")})
    with Session(db_mod.engine()) as s:
        victim = s.exec(select(InvoiceRow)).first()
        victim_id = victim.id
    client.post("/logout", data={"csrf_token": client.cookies.get("baaki_csrf")})

    signup(client, company="Org B", email="b@b.in")
    assert client.get(f"/app/invoices/{victim_id}", follow_redirects=False).status_code == 404
    assert "0 invoice" in client.get("/app/import").text or "Import invoices" in client.get("/app/import").text
    # and Org B's ledger really is empty
    assert "No invoices match" in client.get("/app/invoices").text


# ---- import -------------------------------------------------------------------------------
def test_demo_import_then_ledger_visible(client):
    signup(client)
    r = client.post("/app/import/demo", data={"csrf_token": client.cookies.get("baaki_csrf")})
    assert "Loaded 5 sample invoices" in r.text
    page = client.get("/app/invoices").text
    assert "INV-2041" in page and "Mehta Traders" in page


def test_malformed_csv_imports_nothing(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    bad = (b"invoice_number,customer_name,customer_email,customer_phone,amount_inr,issued_on,due_on\n"
           b"INV-1,Good Co,a@b.in,+91,100.00,2026-01-01,2026-02-01\n"
           b"INV-2,Bad Co,a@b.in,+91,NOT_A_NUMBER,2026-01-01,2026-02-01\n")
    r = client.post("/app/import", data={"csrf_token": csrf}, files={"file": ("x.csv", bad, "text/csv")})
    assert "rejected, nothing imported" in r.text
    with Session(db_mod.engine()) as s:
        assert s.exec(select(InvoiceRow)).all() == []


def test_missing_columns_are_named(client):
    signup(client)
    r = client.post("/app/import", data={"csrf_token": client.cookies.get("baaki_csrf")},
                    files={"file": ("x.csv", b"invoice_number,customer_name\nA,B\n", "text/csv")})
    assert "Missing required column" in r.text


# ---- the agent ----------------------------------------------------------------------------
def _enable_agent(client, approval=True, email="owner@sharma.in"):
    verify_email(client, email)          # the agent can't be switched on before this
    csrf = client.cookies.get("baaki_csrf")
    data = {"agent_enabled": "on", "llm_provider": "rules", "csrf_token": csrf}
    if approval:
        data["approval_required"] = "on"
    return client.post("/app/settings/agent", data=data)


def test_agent_run_queues_messages_for_approval(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    _enable_agent(client, approval=True)
    r = client.post("/app/run", data={"csrf_token": csrf})
    assert "waiting for your approval" in r.text

    with Session(db_mod.engine()) as s:
        msgs = s.exec(select(Outbox)).all()
        assert msgs, "the agent should have drafted messages"
        assert all(m.status == OutboxStatus.PENDING_APPROVAL for m in msgs)
        assert all(m.sent_at is None for m in msgs)
        # the message names the merchant and carries a payment link
        assert any("Sharma Supplies" in m.body for m in msgs)


def test_agent_refuses_to_run_when_disabled(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    r = client.post("/app/run", data={"csrf_token": csrf})
    assert "Turn the agent on" in r.text
    with Session(db_mod.engine()) as s:
        assert s.exec(select(Outbox)).all() == []


def test_approve_sends_and_reject_does_not(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    _enable_agent(client)
    client.post("/app/run", data={"csrf_token": csrf})
    with Session(db_mod.engine()) as s:
        ids = [m.id for m in s.exec(select(Outbox)).all()]

    client.post(f"/app/approvals/{ids[0]}", data={"verdict": "approve", "body": "Edited body {link}", "csrf_token": csrf})
    client.post(f"/app/approvals/{ids[1]}", data={"verdict": "reject", "csrf_token": csrf})
    with Session(db_mod.engine()) as s:
        a, b = s.get(Outbox, ids[0]), s.get(Outbox, ids[1])
        assert a.status == OutboxStatus.SENT and a.sent_at is not None and a.body == "Edited body {link}"
        assert b.status == OutboxStatus.REJECTED and b.sent_at is None


def test_recorded_dispute_escalates_on_next_run(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    _enable_agent(client)
    client.post("/app/run", data={"csrf_token": csrf})
    with Session(db_mod.engine()) as s:
        inv_id = s.exec(select(InvoiceRow)).first().id

    client.post(f"/app/invoices/{inv_id}/note",
                data={"text": "This invoice is wrong — 20 cartons were short-shipped. Not paying till corrected.",
                      "csrf_token": csrf})
    client.post("/app/run", data={"csrf_token": csrf})
    with Session(db_mod.engine()) as s:
        inv = s.get(InvoiceRow, inv_id)
        assert inv.status == "escalated" and inv.dispute_open
        assert "dispute" in (inv.escalation_reason or "").lower()


def test_policy_bounds_are_validated_and_audited(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    r = client.post("/app/settings/policy", data={"max_early_settlement_discount_pct": "80", "csrf_token": csrf})
    assert "must be between" in r.text
    r = client.post("/app/settings/policy", data={"max_early_settlement_discount_pct": "2.5", "csrf_token": csrf})
    assert "Guardrails updated" in r.text
    assert "policy_changed" in client.get("/app/audit").text


def test_live_razorpay_keys_are_refused(client):
    signup(client)
    r = client.post("/app/settings/razorpay",
                    data={"key_id": "rzp_live_abc123", "key_secret": "s", "csrf_token": client.cookies.get("baaki_csrf")})
    assert "Only test-mode keys" in r.text


# ---- audit --------------------------------------------------------------------------------
def test_audit_chain_verifies_and_detects_tampering(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    _enable_agent(client)
    client.post("/app/run", data={"csrf_token": csrf})

    with Session(db_mod.engine()) as s:
        org_id = s.exec(select(Org)).first().id
        ok, msg = verify_chain(s, org_id)
        assert ok, msg
    assert "Chain intact" in client.get("/app/audit").text

    from baaki.app.models import AuditRow
    with Session(db_mod.engine()) as s:
        row = s.exec(select(AuditRow).where(AuditRow.org_id == org_id, AuditRow.seq == 2)).first()
        row.payload_json = json.dumps({"tampered": True})
        s.add(row); s.commit()
        ok, msg = verify_chain(s, org_id)
        assert not ok and "seq 2" in msg
    assert "Chain broken" in client.get("/app/audit").text


# ---- webhooks -----------------------------------------------------------------------------
def test_payment_webhook_credits_once_and_rejects_bad_signature(client):
    from baaki.razorpay_client import sign_webhook

    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    _enable_agent(client)
    client.post("/app/run", data={"csrf_token": csrf})

    with Session(db_mod.engine()) as s:
        org = s.exec(select(Org)).first()
        inv = s.exec(select(InvoiceRow).where(InvoiceRow.payment_link_id.is_not(None))).first()
        slug, number, amount, link_id = org.slug, inv.number, inv.outstanding_paise, inv.payment_link_id

    body = json.dumps({
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"id": link_id, "notes": {"invoice_id": number}}},
                    "payment": {"entity": {"id": "pay_TEST_1", "amount": amount}}},
    }).encode()
    sig = sign_webhook(body, "baaki-sandbox")

    assert client.post(f"/webhooks/razorpay/{slug}", content=body, headers={"X-Razorpay-Signature": "nope"}).status_code == 400
    r = client.post(f"/webhooks/razorpay/{slug}", content=body, headers={"X-Razorpay-Signature": sig})
    assert r.json()["credited_paise"] == amount
    # replay is idempotent
    r2 = client.post(f"/webhooks/razorpay/{slug}", content=body, headers={"X-Razorpay-Signature": sig})
    assert r2.json()["credited_paise"] == 0

    with Session(db_mod.engine()) as s:
        inv = s.exec(select(InvoiceRow).where(InvoiceRow.number == number)).first()
        assert inv.status == "paid" and inv.amount_paid_paise == inv.amount_paise


def test_webhook_for_unknown_org_is_404(client):
    assert client.post("/webhooks/razorpay/nobody", content=b"{}", headers={"X-Razorpay-Signature": "x"}).status_code == 404


# ---- billing ------------------------------------------------------------------------------
def test_plan_limit_blocks_import_and_subscribing_raises_it(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    rows = ["invoice_number,customer_name,customer_email,customer_phone,amount_inr,issued_on,due_on"]
    rows += [f"INV-{i},Cust {i},c{i}@x.in,+9199999{i:05d},1000.00,2026-01-01,2026-02-01" for i in range(30)]
    csv_bytes = "\n".join(rows).encode()

    r = client.post("/app/import", data={"csrf_token": csrf}, files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert "plan allows 25 open invoices" in r.text

    client.post("/app/billing/subscribe", data={"plan": "growth", "csrf_token": csrf})
    with Session(db_mod.engine()) as s:
        org = s.exec(select(Org)).first()
        assert org.plan == Plan.GROWTH and org.subscription_status.value == "active"
    r = client.post("/app/import", data={"csrf_token": csrf}, files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert "Imported 30 new" in r.text


def test_cancelled_subscription_stops_the_agent(client):
    from baaki.app import billing as billing_mod

    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    _enable_agent(client)
    with Session(db_mod.engine()) as s:
        org = s.exec(select(Org)).first()
        billing_mod.apply_subscription_event(s, org, "subscription.cancelled", {})
        assert not org.agent_enabled
    r = client.post("/app/run", data={"csrf_token": csrf})
    assert "cancelled" in r.text.lower()


def test_approve_all_route_is_not_shadowed_by_the_id_route(client):
    """`/app/approvals/approve-all` must not be parsed as `/app/approvals/{msg_id:int}`."""
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    client.post("/app/import/demo", data={"csrf_token": csrf})
    _enable_agent(client)
    client.post("/app/run", data={"csrf_token": csrf})

    r = client.post("/app/approvals/approve-all", data={"csrf_token": csrf})
    assert r.status_code == 200, f"route shadowed: {r.status_code}"
    assert re.search(r"Approved \d+;", r.text)
    with Session(db_mod.engine()) as s:
        msgs = s.exec(select(Outbox)).all()
        assert msgs and all(m.status == OutboxStatus.SENT for m in msgs)
        assert all(m.sent_at is not None for m in msgs)
