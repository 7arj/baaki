"""Clerk identity path, exercised without a Clerk account or a network call.

`clerk_auth.verify` and `fetch_user` are the only two seams that touch Clerk; stubbing them
leaves provisioning, account linking, invites, onboarding and webhooks running for real.
"""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from baaki.app import accounts, clerk_auth, db as db_mod
from baaki.app.db import init_db, make_engine
from baaki.app.models import Org, Role, Session as SessionRow, TokenPurpose, User, utcnow
from baaki.app.security import hash_password
from baaki.app.web import create_app


@pytest.fixture
def eng(tmp_path, monkeypatch):
    e = make_engine(f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(db_mod, "_engine", e)
    init_db(e)
    return e


def _clerk_user(uid="user_abc", email="priya@verma.in", verified=True, first="Priya", last="Verma"):
    """Mirrors the fields clerk_auth reads off a Clerk User object."""
    addr = SimpleNamespace(id="idn_1", email_address=email,
                           verification=SimpleNamespace(status="verified" if verified else "unverified"))
    return SimpleNamespace(id=uid, primary_email_address_id="idn_1", email_addresses=[addr],
                           first_name=first, last_name=last, username=None, image_url="https://img/a.png")


@pytest.fixture
def clerk(eng, monkeypatch):
    """Turn Clerk on and hand back a handle for controlling who is signed in."""
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_stub")
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", "pk_test_" + base64.b64encode(b"clerk.example.com$").decode())
    state = {"claims": None, "user": _clerk_user()}
    monkeypatch.setattr(clerk_auth, "verify", lambda request: state["claims"])
    monkeypatch.setattr(clerk_auth, "fetch_user", lambda uid: state["user"])

    def sign_in(uid="user_abc", **kw):
        state["user"] = _clerk_user(uid=uid, **kw)
        state["claims"] = {"sub": uid, "sid": "sess_1"}

    def sign_out():
        state["claims"] = None

    return SimpleNamespace(sign_in=sign_in, sign_out=sign_out, state=state)


@pytest.fixture
def client(eng):
    return TestClient(create_app(), follow_redirects=True)


# ---- toggle -----------------------------------------------------------------------------------
def test_disabled_without_a_secret_key(eng, monkeypatch):
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    assert clerk_auth.enabled() is False
    assert clerk_auth.verify(None) is None


def test_local_password_auth_still_works_when_clerk_is_off(client, eng, monkeypatch):
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    client.get("/signup")
    r = client.post("/signup", data={"company": "Local Co", "name": "A", "email": "a@local.in",
                                     "password": "a-good-passphrase", "csrf_token": client.cookies.get("baaki_csrf")})
    assert r.status_code == 200
    assert client.get("/app").status_code == 200


def test_login_page_swaps_the_form_for_the_widget(client, clerk):
    html = client.get("/login").text
    assert 'id="clerk-mount"' in html
    assert 'name="password"' not in html      # the local form is not rendered alongside


# ---- provisioning -------------------------------------------------------------------------------
def test_first_sign_in_creates_an_org_parked_for_onboarding(client, clerk, eng):
    clerk.sign_in()
    r = client.get("/app")
    assert "One last thing" in r.text          # gated on naming the business

    with Session(eng) as s:
        user = s.exec(select(User)).one()
        org = s.exec(select(Org)).one()
        assert user.clerk_user_id == "user_abc"
        assert user.password_hash is None      # no local credential exists
        assert user.email == "priya@verma.in" and user.role == Role.OWNER
        assert user.email_verified_at is not None
        assert user.avatar_url == "https://img/a.png"
        assert org.onboarding_complete is False
        assert org.plan.value == "trial"


def test_onboarding_sets_the_name_customers_will_see(client, clerk, eng):
    clerk.sign_in()
    client.get("/app")
    r = client.post("/app/welcome", data={"company": "Verma Textiles",
                                          "csrf_token": client.cookies.get("baaki_csrf")})
    assert "Welcome, Verma Textiles" in r.text
    with Session(eng) as s:
        org = s.exec(select(Org)).one()
        assert org.name == "Verma Textiles" and org.legal_name == "Verma Textiles"
        assert org.onboarding_complete is True
    assert "Dashboard" in client.get("/app").text


def test_the_gate_holds_until_onboarding_is_done(client, clerk):
    clerk.sign_in()
    for path in ("/app", "/app/invoices", "/app/import"):
        assert "One last thing" in client.get(path).text


def test_returning_sign_in_reuses_the_same_user(client, clerk, eng):
    clerk.sign_in()
    client.get("/app")
    client.post("/app/welcome", data={"company": "Verma Textiles", "csrf_token": client.cookies.get("baaki_csrf")})
    clerk.sign_out()
    clerk.sign_in()
    client.get("/app")
    with Session(eng) as s:
        assert len(s.exec(select(User)).all()) == 1
        assert len(s.exec(select(Org)).all()) == 1


def test_signed_out_means_signed_out(client, clerk):
    clerk.sign_out()
    assert "Sign in" in client.get("/app").text


# ---- account linking ------------------------------------------------------------------------------
def test_a_verified_address_adopts_the_existing_local_account(client, clerk, eng):
    """Someone who signed up with a password and later clicks Google keeps their org and role."""
    with Session(eng) as s:
        org = Org(name="Existing Co", slug="existing"); s.add(org); s.commit(); s.refresh(org)
        s.add(User(org_id=org.id, email="priya@verma.in", name="Priya",
                   password_hash=hash_password("original-passphrase"), role=Role.OWNER))
        s.commit()

    clerk.sign_in()
    client.get("/app")
    with Session(eng) as s:
        users = s.exec(select(User)).all()
        assert len(users) == 1, "should link, not duplicate"
        assert users[0].clerk_user_id == "user_abc"
        assert users[0].password_hash is not None, "the local credential is left intact"
        assert len(s.exec(select(Org)).all()) == 1


def test_an_unverified_address_is_refused_rather_than_linked(clerk, eng):
    """Otherwise anyone could sign up at the provider with someone else's email and walk in."""
    with Session(eng) as s:
        org = Org(name="Victim Co", slug="victim"); s.add(org); s.commit(); s.refresh(org)
        s.add(User(org_id=org.id, email="priya@verma.in", password_hash=hash_password("x" * 12)))
        s.commit()

    clerk.sign_in(verified=False)
    with Session(eng) as s:
        with pytest.raises(clerk_auth.ClerkProvisionError, match="Verify that address"):
            clerk_auth.provision(s, {"sub": "user_abc"})
        assert s.exec(select(User).where(User.clerk_user_id.is_not(None))).first() is None


def test_a_disabled_user_cannot_sign_in_through_clerk(client, clerk, eng):
    clerk.sign_in()
    client.get("/app")
    with Session(eng) as s:
        u = s.exec(select(User)).one()
        u.disabled = True
        s.add(u); s.commit()
    assert "Sign in" in client.get("/app").text


# ---- invitations -------------------------------------------------------------------------------
def test_an_invited_person_joins_that_org_instead_of_creating_one(client, clerk, eng):
    with Session(eng) as s:
        org = Org(name="Sharma Supplies", slug="sharma", onboarding_complete=True)
        s.add(org); s.commit(); s.refresh(org)
        s.add(User(org_id=org.id, email="owner@sharma.in", password_hash=hash_password("x" * 12)))
        s.commit()
        raw = accounts.issue_token(s, TokenPurpose.INVITE, org_id=org.id,
                                   email="priya@verma.in", role=Role.MEMBER)
        org_id = org.id

    client.get(f"/invite?token={raw}")          # sets the invite cookie
    clerk.sign_in()
    assert "Dashboard" in client.get("/app").text   # no onboarding gate: the org already exists

    with Session(eng) as s:
        joined = s.exec(select(User).where(User.email == "priya@verma.in")).one()
        assert joined.org_id == org_id and joined.role == Role.MEMBER
        assert len(s.exec(select(Org)).all()) == 1, "must not create a second org"


def test_an_invite_for_a_different_address_does_not_grant_access(client, clerk, eng):
    with Session(eng) as s:
        org = Org(name="Sharma Supplies", slug="sharma", onboarding_complete=True)
        s.add(org); s.commit(); s.refresh(org)
        raw = accounts.issue_token(s, TokenPurpose.INVITE, org_id=org.id,
                                   email="someone.else@sharma.in", role=Role.OWNER)
        victim_org = org.id

    client.get(f"/invite?token={raw}")
    clerk.sign_in()                              # signs in as priya@verma.in, not the invitee
    client.get("/app")
    with Session(eng) as s:
        priya = s.exec(select(User).where(User.email == "priya@verma.in")).one()
        assert priya.org_id != victim_org, "an invite must not admit a different address"
        assert priya.role == Role.OWNER and len(s.exec(select(Org)).all()) == 2


# ---- webhooks ---------------------------------------------------------------------------------
def _svix(body: bytes, secret: str, age: int = 0):
    ts = str(int(time.time()) - age)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": "msg_1", "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


SECRET = "whsec_" + base64.b64encode(b"a-webhook-signing-secret").decode()


def test_webhook_signature_is_enforced(client, clerk, monkeypatch):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    body = json.dumps({"type": "user.updated", "data": {"id": "user_abc"}}).encode()
    assert client.post("/webhooks/clerk", content=body,
                       headers={"svix-id": "m", "svix-timestamp": str(int(time.time())),
                                "svix-signature": "v1,bogus"}).status_code == 400
    assert client.post("/webhooks/clerk", content=body, headers=_svix(body, SECRET)).status_code == 200


def test_old_deliveries_are_rejected_as_replays(clerk, monkeypatch):
    body = b'{"type":"user.updated","data":{"id":"user_abc"}}'
    assert clerk_auth.verify_webhook(body, _svix(body, SECRET, age=600), SECRET) is False
    assert clerk_auth.verify_webhook(body, _svix(body, SECRET), SECRET) is True


def test_user_deleted_disables_the_account_and_kills_sessions(client, clerk, eng, monkeypatch):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    clerk.sign_in()
    client.get("/app")
    with Session(eng) as s:
        u = s.exec(select(User)).one()
        s.add(SessionRow(token_hash="h", user_id=u.id, expires_at=utcnow()))
        s.commit()

    body = json.dumps({"type": "user.deleted", "data": {"id": "user_abc"}}).encode()
    assert client.post("/webhooks/clerk", content=body, headers=_svix(body, SECRET)).json()["status"] == "user.deleted"
    with Session(eng) as s:
        u = s.exec(select(User)).one()
        assert u.disabled is True
        assert all(x.revoked for x in s.exec(select(SessionRow)).all())
        # the row survives so past approvals stay attributable
        assert u.email == "priya@verma.in"


def test_user_updated_syncs_email_and_name(client, clerk, eng, monkeypatch):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    clerk.sign_in()
    client.get("/app")
    body = json.dumps({"type": "user.updated", "data": {
        "id": "user_abc", "primary_email_address_id": "idn_2", "first_name": "Priya", "last_name": "Sharma",
        "image_url": "https://img/b.png",
        "email_addresses": [{"id": "idn_2", "email_address": "Priya.Sharma@Verma.in"}],
    }}).encode()
    client.post("/webhooks/clerk", content=body, headers=_svix(body, SECRET))
    with Session(eng) as s:
        u = s.exec(select(User)).one()
        assert u.email == "priya.sharma@verma.in"    # normalised to lowercase
        assert u.name == "Priya Sharma" and u.avatar_url == "https://img/b.png"


def test_webhook_without_a_configured_secret_is_unavailable(client, clerk, monkeypatch):
    monkeypatch.delenv("CLERK_WEBHOOK_SECRET", raising=False)
    assert client.post("/webhooks/clerk", content=b"{}").status_code == 503
