"""Auth hardening, team management, migrations, per-org risk fitting, locking and WhatsApp."""

import json
import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from baaki.app import accounts, db as db_mod
from baaki.app.db import current_revision, init_db, make_engine
from baaki.app.models import (
    Customer, InvoiceRow, LoginAttempt, Org, PolicySettings, RiskModelRow, Role, RunLock,
    Session as SessionRow, Token, TokenPurpose, User, utcnow,
)
from baaki.app.security import hash_password, verify_password
from baaki.app.service import LockBusy, active_model, fit_org_model, org_lock, risk_score
from baaki.app.web import create_app


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = make_engine(f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(db_mod, "_engine", e)
    init_db(e)
    return e


@pytest.fixture
def client(eng):
    return TestClient(create_app(), follow_redirects=True)


def _csrf(client):
    client.get("/signup")
    return client.cookies.get("baaki_csrf")


def signup(client, company="Sharma Supplies", email="owner@sharma.in", password="a-good-passphrase"):
    return client.post("/signup", data={"company": company, "name": "Owner", "email": email,
                                        "password": password, "csrf_token": _csrf(client)})


# ---- migrations ------------------------------------------------------------------------------
def test_fresh_database_is_stamped_at_head(eng):
    assert current_revision(eng) is not None


def test_init_db_is_idempotent(eng):
    before = current_revision(eng)
    assert init_db(eng) == "upgraded"
    assert current_revision(eng) == before


def test_a_pre_alembic_database_is_adopted_not_wrecked(tmp_path, monkeypatch):
    """An existing DB from before migrations existed must be adopted, keeping its data."""
    from sqlmodel import SQLModel

    e = make_engine(f"sqlite:///{tmp_path}/legacy.db")
    monkeypatch.setattr(db_mod, "_engine", e)
    SQLModel.metadata.create_all(e)                     # created the old way, no alembic_version
    with Session(e) as s:
        s.add(Org(name="Legacy", slug="legacy")); s.commit()
    assert current_revision(e) is None
    assert init_db(e) == "adopted"
    assert current_revision(e) is not None
    with Session(e) as s:
        assert s.exec(select(Org)).first().slug == "legacy"


# ---- sign-in throttling ------------------------------------------------------------------------
def test_repeated_failures_are_throttled_then_cleared_by_success(client, eng):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    for _ in range(accounts.MAX_PER_EMAIL):
        client.post("/login", data={"email": "owner@sharma.in", "password": "wrong", "csrf_token": csrf})
    r = client.post("/login", data={"email": "owner@sharma.in", "password": "wrong", "csrf_token": csrf})
    assert "Too many failed sign-in attempts" in r.text

    # Correct password is refused too while throttled — that's the point.
    r = client.post("/login", data={"email": "owner@sharma.in", "password": "a-good-passphrase", "csrf_token": csrf})
    assert "Too many failed sign-in attempts" in r.text

    with Session(eng) as s:
        for row in s.exec(select(LoginAttempt)).all():
            s.delete(row)
        s.commit()
    r = client.post("/login", data={"email": "owner@sharma.in", "password": "a-good-passphrase", "csrf_token": csrf})
    assert "Dashboard" in r.text
    with Session(eng) as s:
        assert s.exec(select(LoginAttempt)).all() == []   # a success clears the counter


def test_throttling_does_not_reveal_whether_an_account_exists(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    for _ in range(accounts.MAX_PER_EMAIL + 1):
        client.post("/login", data={"email": "ghost@nowhere.in", "password": "wrong", "csrf_token": csrf})
    r = client.post("/login", data={"email": "ghost@nowhere.in", "password": "wrong", "csrf_token": csrf})
    assert "Too many failed sign-in attempts" in r.text   # same treatment as a real account


def test_old_attempts_are_pruned(eng):
    with Session(eng) as s:
        s.add(LoginAttempt(key="email:a@b.in", at=utcnow() - timedelta(hours=2)))
        s.add(LoginAttempt(key="email:a@b.in"))
        s.commit()
        assert accounts.prune_attempts(s) == 1
        assert len(s.exec(select(LoginAttempt)).all()) == 1


# ---- email verification ------------------------------------------------------------------------
def test_agent_cannot_be_enabled_until_the_email_is_confirmed(client, eng):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    r = client.post("/app/settings/agent", data={"agent_enabled": "on", "llm_provider": "rules", "csrf_token": csrf})
    assert "Confirm your email" in r.text
    with Session(eng) as s:
        assert not s.exec(select(Org)).first().agent_enabled

    with Session(eng) as s:
        u = s.exec(select(User)).first()
        raw = accounts.issue_token(s, TokenPurpose.VERIFY_EMAIL, org_id=u.org_id, user_id=u.id, email=u.email)
    client.get(f"/verify?token={raw}")
    r = client.post("/app/settings/agent", data={"agent_enabled": "on", "llm_provider": "rules", "csrf_token": csrf})
    assert "Agent turned on" in r.text


def test_a_verification_token_works_once(client, eng):
    signup(client)
    with Session(eng) as s:
        u = s.exec(select(User)).first()
        raw = accounts.issue_token(s, TokenPurpose.VERIFY_EMAIL, org_id=u.org_id, user_id=u.id, email=u.email)
    assert "Email confirmed" in client.get(f"/verify?token={raw}").text
    assert "expired or was already used" in client.get(f"/verify?token={raw}").text


def test_expired_tokens_are_refused(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        raw = accounts.issue_token(s, TokenPurpose.PASSWORD_RESET, org_id=org.id, email="a@b.in")
        row = s.exec(select(Token)).first()
        row.expires_at = utcnow() - timedelta(minutes=1)
        s.add(row); s.commit()
        assert accounts.consume_token(s, raw, TokenPurpose.PASSWORD_RESET) is None


def test_a_token_is_only_valid_for_its_own_purpose(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        raw = accounts.issue_token(s, TokenPurpose.INVITE, org_id=org.id, email="a@b.in")
        assert accounts.consume_token(s, raw, TokenPurpose.PASSWORD_RESET) is None
        assert accounts.consume_token(s, raw, TokenPurpose.INVITE) is not None


def test_only_the_hash_of_a_token_is_stored(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        raw = accounts.issue_token(s, TokenPurpose.INVITE, org_id=org.id, email="a@b.in")
        stored = [t.token_hash for t in s.exec(select(Token)).all()]
        assert raw not in stored and len(stored[0]) == 64


# ---- password reset ------------------------------------------------------------------------------
def test_reset_changes_the_password_and_evicts_other_sessions(client, eng):
    signup(client)
    with Session(eng) as s:
        u = s.exec(select(User)).first()
        assert len(s.exec(select(SessionRow).where(SessionRow.revoked == False)).all()) == 1  # noqa: E712
        raw = accounts.issue_token(s, TokenPurpose.PASSWORD_RESET, org_id=u.org_id, user_id=u.id, email=u.email)

    csrf = client.cookies.get("baaki_csrf")
    r = client.post("/reset", data={"token": raw, "password": "a-brand-new-passphrase", "csrf_token": csrf})
    assert "every other session was signed out" in r.text
    with Session(eng) as s:
        u = s.exec(select(User)).first()
        assert verify_password("a-brand-new-passphrase", u.password_hash)
        assert not verify_password("a-good-passphrase", u.password_hash)
        assert all(x.revoked for x in s.exec(select(SessionRow)).all())

    assert "Dashboard" in client.post("/login", data={"email": "owner@sharma.in", "password": "a-brand-new-passphrase",
                                                     "csrf_token": _csrf(client)}).text


def test_forgot_never_reveals_whether_the_address_exists(client):
    signup(client)
    csrf = client.cookies.get("baaki_csrf")
    a = client.post("/forgot", data={"email": "owner@sharma.in", "csrf_token": csrf}).text
    b = client.post("/forgot", data={"email": "nobody@nowhere.in", "csrf_token": csrf}).text
    assert "a reset link is on its way" in a and "a reset link is on its way" in b


def test_reset_rejects_a_weak_password(client, eng):
    signup(client)
    with Session(eng) as s:
        u = s.exec(select(User)).first()
        raw = accounts.issue_token(s, TokenPurpose.PASSWORD_RESET, org_id=u.org_id, user_id=u.id, email=u.email)
    r = client.post("/reset", data={"token": raw, "password": "short", "csrf_token": client.cookies.get("baaki_csrf")})
    assert "at least 10 characters" in r.text


# ---- team ------------------------------------------------------------------------------------
def _invite(client, eng, email="colleague@sharma.in", role="member"):
    client.post("/app/team/invite", data={"email": email, "role": role, "csrf_token": client.cookies.get("baaki_csrf")})
    with Session(eng) as s:
        tok = s.exec(select(Token).where(Token.purpose == TokenPurpose.INVITE, Token.used_at.is_(None))).first()
    return tok


def test_invite_and_accept_puts_the_new_user_in_the_same_org(client, eng):
    signup(client)
    assert _invite(client, eng) is not None
    with Session(eng) as s:
        org_id = s.exec(select(Org)).first().id
        raw = accounts.issue_token(s, TokenPurpose.INVITE, org_id=org_id, email="colleague@sharma.in", role=Role.MEMBER)

    fresh = TestClient(create_app(), follow_redirects=True)
    fresh.get(f"/invite?token={raw}")
    r = fresh.post("/invite", data={"token": raw, "name": "Colleague", "password": "colleague-passphrase",
                                    "csrf_token": fresh.cookies.get("baaki_csrf")})
    assert "Welcome to the team" in r.text
    with Session(eng) as s:
        u = s.exec(select(User).where(User.email == "colleague@sharma.in")).first()
        assert u.org_id == org_id and u.role == Role.MEMBER and u.email_verified_at is not None


def test_members_cannot_touch_billing_credentials_or_guardrails(client, eng):
    signup(client)
    with Session(eng) as s:
        org_id = s.exec(select(Org)).first().id
        s.add(User(org_id=org_id, email="member@sharma.in", role=Role.MEMBER,
                   password_hash=hash_password("member-passphrase"), email_verified_at=utcnow()))
        s.commit()

    m = TestClient(create_app(), follow_redirects=True)
    m.get("/login")
    m.post("/login", data={"email": "member@sharma.in", "password": "member-passphrase",
                           "csrf_token": m.cookies.get("baaki_csrf")})
    csrf = m.cookies.get("baaki_csrf")
    for path, data in [("/app/billing/subscribe", {"plan": "growth"}),
                       ("/app/settings/razorpay", {"key_id": "rzp_test_x", "key_secret": "s"}),
                       ("/app/settings/policy", {"max_early_settlement_discount_pct": "1"}),
                       ("/app/team/invite", {"email": "x@y.in", "role": "owner"})]:
        r = m.post(path, data={**data, "csrf_token": csrf}, follow_redirects=False)
        assert r.status_code == 403, f"{path} should be owner-only, got {r.status_code}"
    # but a member can still do their job
    assert m.get("/app/approvals").status_code == 200
    assert m.get("/app/invoices").status_code == 200


def test_the_last_owner_cannot_be_demoted_or_disabled(client, eng):
    signup(client)
    with Session(eng) as s:
        org_id = s.exec(select(Org)).first().id
        s.add(User(org_id=org_id, email="second@sharma.in", role=Role.OWNER,
                   password_hash=hash_password("second-passphrase"), email_verified_at=utcnow()))
        s.commit()
        owner2 = s.exec(select(User).where(User.email == "second@sharma.in")).first().id
    csrf = client.cookies.get("baaki_csrf")
    # demoting the *other* owner is fine while one remains
    assert "updated" in client.post(f"/app/team/{owner2}", data={"action": "demote", "csrf_token": csrf}).text
    # now try to demote the only remaining owner (self) — blocked
    with Session(eng) as s:
        me = s.exec(select(User).where(User.email == "owner@sharma.in")).first().id
    r = client.post(f"/app/team/{me}", data={"action": "demote", "csrf_token": csrf})
    assert "at least one active owner" in r.text


def test_disabling_a_user_revokes_their_sessions_immediately(client, eng):
    signup(client)
    with Session(eng) as s:
        org_id = s.exec(select(Org)).first().id
        s.add(User(org_id=org_id, email="member@sharma.in", role=Role.MEMBER,
                   password_hash=hash_password("member-passphrase"), email_verified_at=utcnow()))
        s.commit()

    m = TestClient(create_app(), follow_redirects=True)
    m.get("/login")
    m.post("/login", data={"email": "member@sharma.in", "password": "member-passphrase",
                           "csrf_token": m.cookies.get("baaki_csrf")})
    assert m.get("/app/invoices").status_code == 200

    with Session(eng) as s:
        member_id = s.exec(select(User).where(User.email == "member@sharma.in")).first().id
    client.post(f"/app/team/{member_id}", data={"action": "disable", "csrf_token": client.cookies.get("baaki_csrf")})
    assert "Sign in" in m.get("/app/invoices").text          # session killed mid-flight
    m.post("/login", data={"email": "member@sharma.in", "password": "member-passphrase",
                           "csrf_token": m.cookies.get("baaki_csrf")})
    assert "Sign in" in m.get("/app/invoices").text          # and cannot sign back in


def test_invite_can_be_revoked_before_use(client, eng):
    signup(client)
    tok = _invite(client, eng)
    r = client.post(f"/app/team/revoke-invite/{tok.id}", data={"csrf_token": client.cookies.get("baaki_csrf")})
    assert "revoked" in r.text
    with Session(eng) as s:
        assert s.get(Token, tok.id).used_at is not None


# ---- per-org risk fitting -----------------------------------------------------------------------
def _settled_ledger(db, org_id: int, n_customers: int = 14, per: int = 4):
    """Customers alternate reliable / chronically late, so there is real signal to learn."""
    today = date.today()
    for c in range(n_customers):
        cust = Customer(org_id=org_id, name=f"Cust {c}", email=f"c{c}@x.in")
        db.add(cust); db.commit(); db.refresh(cust)
        late_payer = c % 2 == 0
        for i in range(per):
            due = today - timedelta(days=200 - i * 30)
            settled = due + timedelta(days=45 if late_payer else 3)
            db.add(InvoiceRow(org_id=org_id, customer_id=cust.id, number=f"H-{c}-{i}",
                              amount_paise=50_000_00, amount_paid_paise=50_000_00,
                              issued_on=due - timedelta(days=30), due_on=due, status="paid",
                              updated_at=settled))
    db.commit()


def test_model_refuses_to_fit_without_enough_history(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        res = fit_org_model(s, org)
        assert res["fitted"] is False and "settled invoices" in res["reason"]
        assert active_model(s, org.id) is None


def test_model_fits_on_org_history_and_reports_holdout_metrics(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        _settled_ledger(s, org.id)
        res = fit_org_model(s, org)
        assert res["fitted"] is True
        assert res["holdout_rows"] > 0 and res["train_rows"] > 0
        assert 0.0 <= res["precision"] <= 1.0 and 0.0 <= res["recall"] <= 1.0

        row = active_model(s, org.id)
        assert row is not None and len(json.loads(row.weights_json)) == 6

        # refitting supersedes rather than accumulating active models
        fit_org_model(s, org)
        assert len(s.exec(select(RiskModelRow).where(RiskModelRow.active == True)).all()) == 1  # noqa: E712


def test_scores_stay_none_without_customer_history(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        cust = Customer(org_id=org.id, name="New", email="n@x.in")
        s.add(cust); s.commit(); s.refresh(cust)
        inv = InvoiceRow(org_id=org.id, customer_id=cust.id, number="N-1", amount_paise=10000,
                         issued_on=date.today() - timedelta(days=40), due_on=date.today() - timedelta(days=10))
        assert risk_score(inv, cust, {"total": 0}, date.today()) is None
        assert risk_score(inv, cust, {"total": 3, "late": 2, "partials": 0, "avg_days_late": 20.0}, date.today()) is not None


# ---- run locking ----------------------------------------------------------------------------------
def test_a_second_worker_cannot_run_the_same_org(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        with org_lock(s, org.id, "worker-a"):
            with Session(eng) as s2:
                with pytest.raises(LockBusy):
                    with org_lock(s2, org.id, "worker-b"):
                        pass
        assert s.exec(select(RunLock)).all() == []        # released on exit


def test_a_stale_lock_is_reclaimed(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        s.add(RunLock(org_id=org.id, holder="crashed-worker", expires_at=utcnow() - timedelta(minutes=1)))
        s.commit()
        with org_lock(s, org.id, "worker-b") as lock:
            assert lock.row.holder == "worker-b"


def test_the_lock_is_released_even_when_the_run_raises(eng):
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        with pytest.raises(ValueError):
            with org_lock(s, org.id, "worker-a"):
                raise ValueError("boom")
        assert s.exec(select(RunLock)).all() == []


# ---- whatsapp --------------------------------------------------------------------------------------
def test_whatsapp_number_normalisation():
    from baaki.app.transports import WhatsAppTransport as W

    assert W.normalise("+91 98123 45671") == "919812345671"
    assert W.normalise("9812345671") == "919812345671"       # bare Indian mobile gets +91
    assert W.normalise("(080) 4718-2200") == "08047182200"


def test_channel_selection_prefers_whatsapp_only_when_configured(monkeypatch):
    from baaki.app.transports import channel_for

    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    assert channel_for("a@b.in", "+919812345671") == ("email", "a@b.in")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    assert channel_for("a@b.in", "+919812345671") == ("whatsapp", "+919812345671")
    assert channel_for("a@b.in", "") == ("email", "a@b.in")


def test_permanent_delivery_failures_are_not_retried(eng):
    from baaki.app.models import Outbox, OutboxStatus
    from baaki.app.transports import PermanentDeliveryError, dispatch_outbox

    class Rejecting:
        name = "rejecting"

        def send(self, to, subject, body):
            raise PermanentDeliveryError("not a valid WhatsApp number")

    class Flaky:
        name = "flaky"

        def send(self, to, subject, body):
            raise TimeoutError("network")

    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        cust = Customer(org_id=org.id, name="C"); s.add(cust); s.commit(); s.refresh(cust)
        inv = InvoiceRow(org_id=org.id, customer_id=cust.id, number="I-1", amount_paise=100,
                         issued_on=date.today(), due_on=date.today())
        s.add(inv); s.commit(); s.refresh(inv)
        s.add(Outbox(org_id=org.id, invoice_id=inv.id, to_address="+91", body="x"))
        s.commit()

        res = dispatch_outbox(s, org.id, transport=Rejecting())
        assert res["failed"] == 1 and res["retrying"] == 0
        msg = s.exec(select(Outbox)).first()
        assert msg.status == OutboxStatus.FAILED and msg.attempts == 1

        msg.status, msg.attempts = OutboxStatus.QUEUED, 0
        s.add(msg); s.commit()
        res = dispatch_outbox(s, org.id, transport=Flaky())
        assert res["retrying"] == 1 and res["failed"] == 0   # transient errors keep their place
        assert s.exec(select(Outbox)).first().status == OutboxStatus.QUEUED


def test_uncoded_customers_can_coexist(eng):
    """Regression: external_id defaulted to "" under UNIQUE(org_id, external_id), so a second
    customer without a code was rejected. NULLs are distinct in SQL; empty strings are not."""
    with Session(eng) as s:
        org = Org(name="X", slug="x"); s.add(org); s.commit(); s.refresh(org)
        s.add(Customer(org_id=org.id, name="Alpha"))
        s.add(Customer(org_id=org.id, name="Beta"))
        s.commit()
        assert len(s.exec(select(Customer)).all()) == 2

        # the constraint still applies to real codes
        from sqlalchemy.exc import IntegrityError

        s.add(Customer(org_id=org.id, name="Gamma", external_id="ACME"))
        s.commit()
        s.add(Customer(org_id=org.id, name="Delta", external_id="ACME"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()


def test_every_migration_upgrades_and_downgrades(tmp_path):
    """Walk the full chain both directions — a migration that can't be reversed is a trap."""
    import subprocess

    url = f"sqlite:///{tmp_path}/chain.db"
    env = {**os.environ, "DATABASE_URL": url}
    for args in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
        r = subprocess.run(["uv", "run", "alembic", *args], env=env, capture_output=True, text=True)
        assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr[-600:]}"
