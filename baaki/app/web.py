"""The operator application: auth, ledger, approvals, settings, billing, webhooks."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session as DBSession, func, select

from ..domain import rupees
from ..razorpay_client import verify_webhook
from . import accounts, billing as billing_mod, clerk_auth
from .db import get_session, init_db
from .models import (
    AuditRow, Customer, Event, InvoiceRow, Org, Outbox, OutboxStatus, PaymentRow,
    Plan, PolicySettings, Role, SubscriptionStatus, TokenPurpose, User, utcnow,
)
from .models import Session as SessionRow
from .security import (
    CSRF_COOKIE, INVITE_COOKIE, SESSION_COOKIE, Principal, create_session, current_principal, encrypt_secret,
    hash_password, issue_csrf, mask, optional_principal, password_problem, require_csrf,
    revoke_session, set_session_cookie, verify_password,
)
from .service import (DbAudit, ImportError_, RecoveryEngine, SAMPLE_CSV, active_model, fit_org_model,
                      import_csv, record_payment, verify_chain, work_priority)
from .transports import dispatch_outbox

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["inr"] = lambda p: rupees(int(p or 0))
templates.env.filters["ago"] = lambda d: _ago(d)

router = APIRouter()


def _ago(d) -> str:
    if not d:
        return "—"
    if isinstance(d, date) and not isinstance(d, datetime):
        days = (date.today() - d).days
    else:
        days = (utcnow() - (d if d.tzinfo else d.replace(tzinfo=utcnow().tzinfo))).days
    return "today" if days == 0 else ("yesterday" if days == 1 else (f"in {-days}d" if days < 0 else f"{days}d ago"))


def render(request: Request, name: str, principal: Principal | None = None, **ctx) -> HTMLResponse:
    csrf = request.cookies.get(CSRF_COOKIE)
    resp = templates.TemplateResponse(request, name, {
        "principal": principal, "org": principal.org if principal else None,
        "clerk_enabled": clerk_auth.enabled(), "clerk_key": clerk_auth.publishable_key(),
        "csrf_token": csrf or "", "ok": request.query_params.get("ok"),
        "err": request.query_params.get("err"), "entitlement": billing_mod.entitlement_problem(principal.org) if principal else None,
        **ctx,
    })
    if not csrf:
        issue_csrf(resp)
    return resp


def require_onboarded(p: Principal = Depends(current_principal)) -> Principal:
    """An org provisioned by an identity provider has no business name yet, and the org name is
    what customers see on reminders. Everything is blocked until one is supplied."""
    if not p.org.onboarding_complete:
        raise HTTPException(307, "/app/welcome")
    return p


def require_owner(p: Principal) -> None:
    """Billing, team, credentials and guardrails are owner-only; the rest of the app isn't."""
    if not p.user.role.can_administer:
        raise HTTPException(403, "Only an owner can change this. Ask the account owner.")


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))[:64]


def redirect(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import quote
    if ok:
        url += ("&" if "?" in url else "?") + "ok=" + quote(ok)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "org"


# ============================================================================================
# Marketing + auth
# ============================================================================================
@router.get("/", response_class=HTMLResponse)
def landing(request: Request, p: Principal | None = Depends(optional_principal)):
    if p:
        return RedirectResponse("/app", status_code=303)
    return render(request, "landing.html", plans=billing_mod.CATALOGUE)


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return render(request, "signup.html")


@router.post("/signup")
def signup(request: Request, db: DBSession = Depends(get_session), company: str = Form(...), name: str = Form(""),
           email: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return redirect("/signup", err="That email address doesn't look right.")
    if problem := password_problem(password):
        return redirect("/signup", err=problem)
    if db.exec(select(User).where(User.email == email)).first():
        return redirect("/login", err="An account with that email already exists — sign in instead.")

    slug, n = _slugify(company), 1
    while db.exec(select(Org).where(Org.slug == slug)).first():
        n += 1
        slug = f"{_slugify(company)}-{n}"
    org = Org(name=company.strip(), slug=slug, legal_name=company.strip(), reply_to_email=email)
    billing_mod.start_trial(org)
    db.add(org); db.commit(); db.refresh(org)
    db.add(PolicySettings(org_id=org.id))
    user = User(org_id=org.id, email=email, name=name.strip(), password_hash=hash_password(password), role=Role.OWNER)
    db.add(user); db.commit(); db.refresh(user)
    DbAudit(db, org.id, actor=f"user:{user.id}").record("org_created", org=org.slug, by=email)
    db.commit()

    try:
        accounts.send_verification(db, user, org)
    except Exception as e:      # a mail outage must not block signup
        DbAudit(db, org.id).record("verification_email_failed", error=str(e)[:200])
        db.commit()

    token = create_session(db, user, request)
    resp = redirect("/app/import", ok=f"Welcome to Baaki. Your {billing_mod.TRIAL_DAYS}-day trial has started — "
                                      f"check {email} to confirm your address.")
    set_session_cookie(resp, token)
    issue_csrf(resp)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, db: DBSession = Depends(get_session), email: str = Form(...),
          password: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    email, ip = email.strip().lower(), client_ip(request)
    if problem := accounts.throttle_problem(db, email, ip):
        return redirect("/login", err=problem)

    user = db.exec(select(User).where(User.email == email)).first()
    # Same message and comparable work either way — don't leak which emails exist.
    if not user or user.disabled or not verify_password(password, user.password_hash):
        if not user:
            hash_password(password)
        accounts.record_failure(db, email, ip)
        return redirect("/login", err="Email or password is incorrect.")
    accounts.clear_failures(db, email, ip)
    token = create_session(db, user, request)
    resp = redirect("/app")
    set_session_cookie(resp, token)
    issue_csrf(resp)
    return resp


@router.get("/verify", response_class=HTMLResponse)
def verify_email(token: str, request: Request, db: DBSession = Depends(get_session)):
    row = accounts.consume_token(db, token, TokenPurpose.VERIFY_EMAIL)
    if not row:
        return redirect("/login", err="That confirmation link has expired or was already used. Sign in to request another.")
    user = db.get(User, row.user_id)
    if user:
        user.email_verified_at = utcnow()
        db.add(user)
        DbAudit(db, user.org_id, actor=f"user:{user.id}").record("email_verified", email=user.email)
        db.commit()
    return redirect("/app", ok="Email confirmed.")


@router.post("/app/resend-verification")
def resend_verification(request: Request, p: Principal = Depends(current_principal),
                        db: DBSession = Depends(get_session), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    if p.user.email_verified_at:
        return redirect("/app", ok="Your email is already confirmed.")
    accounts.send_verification(db, p.user, p.org)
    return redirect("/app", ok=f"Confirmation email sent to {p.user.email}.")


@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return render(request, "forgot.html")


@router.post("/forgot")
def forgot(request: Request, db: DBSession = Depends(get_session), email: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    user = db.exec(select(User).where(User.email == email.strip().lower())).first()
    if user and not user.disabled:
        try:
            accounts.send_password_reset(db, user)
        except Exception:
            pass
    # Always the same answer: whether an address has an account is not public information.
    return redirect("/login", ok="If that address has an account, a reset link is on its way. It expires in an hour.")


@router.get("/reset", response_class=HTMLResponse)
def reset_form(token: str, request: Request, db: DBSession = Depends(get_session)):
    if not accounts.peek_token(db, token, TokenPurpose.PASSWORD_RESET):
        return redirect("/forgot", err="That reset link has expired or was already used. Request a new one.")
    return render(request, "reset.html", token=token)


@router.post("/reset")
def reset(request: Request, db: DBSession = Depends(get_session), token: str = Form(...),
          password: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    if problem := password_problem(password):
        return redirect(f"/reset?token={token}", err=problem)
    row = accounts.consume_token(db, token, TokenPurpose.PASSWORD_RESET)
    if not row:
        return redirect("/forgot", err="That reset link has expired or was already used.")
    user = db.get(User, row.user_id)
    if not user:
        return redirect("/forgot", err="That account no longer exists.")
    user.password_hash = hash_password(password)
    db.add(user)
    # Changing a password invalidates every existing session — a reset is how you evict an intruder.
    for sess in db.exec(select(SessionRow).where(SessionRow.user_id == user.id, SessionRow.revoked == False)).all():  # noqa: E712
        sess.revoked = True
        db.add(sess)
    accounts.clear_failures(db, user.email, "")
    DbAudit(db, user.org_id, actor=f"user:{user.id}").record("password_reset", email=user.email)
    db.commit()
    return redirect("/login", ok="Password changed, and every other session was signed out. Sign in with your new password.")


@router.get("/invite", response_class=HTMLResponse)
def invite_form(token: str, request: Request, db: DBSession = Depends(get_session)):
    row = accounts.peek_token(db, token, TokenPurpose.INVITE)
    if not row:
        return redirect("/login", err="That invitation has expired or was already used.")
    org = db.get(Org, row.org_id)
    resp = render(request, "invite.html", token=token, invite=row, org_name=org.name if org else "")
    if clerk_auth.enabled():
        # Provisioning reads this on the first authenticated request and joins that org instead
        # of creating a new one. Short-lived, and consumed once the invite is redeemed.
        resp.set_cookie(INVITE_COOKIE, token, httponly=True, samesite="lax", max_age=1800,
                        secure=os.environ.get("BAAKI_ENV") == "production", path="/")
    return resp


@router.post("/invite")
def accept_invite(request: Request, db: DBSession = Depends(get_session), token: str = Form(...),
                  name: str = Form(""), password: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    if problem := password_problem(password):
        return redirect(f"/invite?token={token}", err=problem)
    row = accounts.peek_token(db, token, TokenPurpose.INVITE)
    if not row:
        return redirect("/login", err="That invitation has expired or was already used.")
    if db.exec(select(User).where(User.email == row.email)).first():
        return redirect("/login", err="An account with that email already exists — sign in instead.")
    accounts.consume_token(db, token, TokenPurpose.INVITE)
    user = User(org_id=row.org_id, email=row.email, name=name.strip(), role=row.role,
                password_hash=hash_password(password), email_verified_at=utcnow())
    db.add(user); db.commit(); db.refresh(user)
    DbAudit(db, row.org_id, actor=f"user:{user.id}").record("invite_accepted", email=user.email, role=user.role.value)
    db.commit()
    sess = create_session(db, user, request)
    resp = redirect("/app", ok="Welcome to the team.")
    set_session_cookie(resp, sess)
    issue_csrf(resp)
    return resp


@router.post("/logout")
def logout(request: Request, db: DBSession = Depends(get_session), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    if token := request.cookies.get(SESSION_COOKIE):
        revoke_session(db, token)
    resp = redirect("/", ok="Signed out.")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(INVITE_COOKIE, path="/")
    return resp


# ============================================================================================
# Dashboard
# ============================================================================================
def _totals(db: DBSession, org_id: int) -> dict:
    rows = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org_id)).all()
    open_rows = [r for r in rows if r.status in ("open", "partially_paid")]
    overdue = [r for r in open_rows if r.due_on < date.today()]
    recovered = sum(r.amount_paid_paise for r in rows)
    exceptions = [r for r in rows if r.status in ("escalated", "stopped")]
    buckets = {"0–30": 0, "31–60": 0, "61–90": 0, "90+": 0}
    for r in overdue:
        d = (date.today() - r.due_on).days
        key = "0–30" if d <= 30 else "31–60" if d <= 60 else "61–90" if d <= 90 else "90+"
        buckets[key] += r.outstanding_paise
    return {
        "invoices": len(rows), "open": len(open_rows), "overdue": len(overdue),
        "outstanding_paise": sum(r.outstanding_paise for r in open_rows),
        "overdue_paise": sum(r.outstanding_paise for r in overdue),
        "recovered_paise": recovered,
        "exceptions": sorted(exceptions, key=lambda r: -r.outstanding_paise),
        "buckets": buckets,
        "at_risk_paise": sum(r.outstanding_paise for r in overdue if (r.risk_score or 0) >= 0.6),
        "scored": sum(1 for r in open_rows if r.risk_score is not None),
        "oldest": max(overdue, key=lambda r: (date.today() - r.due_on).days) if overdue else None,
    }


@router.get("/app/welcome", response_class=HTMLResponse)
def welcome(request: Request, p: Principal = Depends(current_principal)):
    if p.org.onboarding_complete:
        return redirect("/app")
    return render(request, "welcome.html", p)


@router.post("/app/welcome")
def save_welcome(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                 company: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    company = company.strip()
    if len(company) < 2:
        return redirect("/app/welcome", err="Please enter your business name.")
    p.org.name = p.org.legal_name = company
    p.org.onboarding_complete = True
    db.add(p.org)
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("org_onboarded", name=company)
    db.commit()
    return redirect("/app/import", ok=f"Welcome, {company}. Import your overdue invoices to begin.")


@router.get("/app", response_class=HTMLResponse)
def dashboard(request: Request, p: Principal = Depends(require_onboarded), db: DBSession = Depends(get_session)):
    t = _totals(db, p.org_id)
    pending = db.exec(select(func.count(Outbox.id)).where(Outbox.org_id == p.org_id, Outbox.status == OutboxStatus.PENDING_APPROVAL)).one()
    recent = db.exec(select(Event).where(Event.org_id == p.org_id).order_by(Event.at.desc()).limit(12)).all()
    inv_by_id = {i.id: i for i in db.exec(select(InvoiceRow).where(InvoiceRow.org_id == p.org_id)).all()}
    cust_by_id = {c.id: c for c in db.exec(select(Customer).where(Customer.org_id == p.org_id)).all()}
    last_run = db.exec(select(AuditRow).where(AuditRow.org_id == p.org_id, AuditRow.event == "agent_run_finished").order_by(AuditRow.seq.desc())).first()
    return render(request, "dashboard.html", p, t=t, pending=pending, recent=recent, today=date.today(),
                  inv_by_id=inv_by_id, cust_by_id=cust_by_id, last_run=last_run, has_data=t["invoices"] > 0)


# ============================================================================================
# Ledger
# ============================================================================================
@router.get("/app/invoices", response_class=HTMLResponse)
def invoices(request: Request, p: Principal = Depends(require_onboarded), db: DBSession = Depends(get_session),
             status: str = "all", q: str = ""):
    stmt = select(InvoiceRow).where(InvoiceRow.org_id == p.org_id)
    if status != "all":
        stmt = stmt.where(InvoiceRow.status == status)
    rows = db.exec(stmt).all()
    customers = {c.id: c for c in db.exec(select(Customer).where(Customer.org_id == p.org_id)).all()}
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r.number.lower() or ql in customers.get(r.customer_id, Customer(org_id=0, name="")).name.lower()]
    rows.sort(key=lambda r: -work_priority(r, date.today()))
    counts = {"all": 0}
    for r in db.exec(select(InvoiceRow).where(InvoiceRow.org_id == p.org_id)).all():
        counts["all"] += 1
        counts[r.status] = counts.get(r.status, 0) + 1
    return render(request, "invoices.html", p, rows=rows, customers=customers, status=status, q=q, counts=counts)


@router.get("/app/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(invoice_id: int, request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session)):
    row = db.get(InvoiceRow, invoice_id)
    if not row or row.org_id != p.org_id:      # tenancy check on every single object read
        raise HTTPException(404, "Invoice not found")
    cust = db.get(Customer, row.customer_id)
    events = db.exec(select(Event).where(Event.org_id == p.org_id, Event.invoice_id == invoice_id).order_by(Event.at)).all()
    audit = db.exec(select(AuditRow).where(AuditRow.org_id == p.org_id, AuditRow.invoice_id == invoice_id).order_by(AuditRow.seq)).all()
    msgs = db.exec(select(Outbox).where(Outbox.org_id == p.org_id, Outbox.invoice_id == invoice_id).order_by(Outbox.created_at.desc())).all()
    return render(request, "invoice_detail.html", p, row=row, cust=cust, events=events, audit=audit, msgs=msgs,
                  audit_payloads=[json.loads(a.payload_json) for a in audit])


@router.post("/app/invoices/{invoice_id}/note")
def add_reply(invoice_id: int, request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
              text: str = Form(...), csrf_token: str = Form("")):
    """Log a customer's reply. The agent reads it on the next run and classifies the intent."""
    require_csrf(request, csrf_token)
    row = db.get(InvoiceRow, invoice_id)
    if not row or row.org_id != p.org_id:
        raise HTTPException(404)
    db.add(Event(org_id=p.org_id, invoice_id=invoice_id, kind="inbound", summary=text.strip()[:2000]))
    row.last_inbound_text, row.inbound_pending = text.strip()[:2000], True
    row.next_action_on = date.today()   # a reply pulls the invoice forward in the queue
    db.add(row)
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("inbound_recorded", invoice=row.number, invoice_pk=row.id, text=text[:500])
    db.commit()
    return redirect(f"/app/invoices/{invoice_id}", ok="Reply recorded — the agent will act on it in the next run.")


@router.post("/app/invoices/{invoice_id}/stop")
def stop_invoice(invoice_id: int, request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                 reason: str = Form("stopped by a human"), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    row = db.get(InvoiceRow, invoice_id)
    if not row or row.org_id != p.org_id:
        raise HTTPException(404)
    row.status, row.stop_reason, row.next_action_on = "stopped", reason, None
    db.add(row)
    db.add(Event(org_id=p.org_id, invoice_id=invoice_id, kind="system", summary=f"stopped by {p.user.email}: {reason}"))
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("stopped_by_user", invoice=row.number, invoice_pk=row.id, reason=reason)
    db.commit()
    return redirect(f"/app/invoices/{invoice_id}", ok="Automated contact stopped for this invoice.")


# ============================================================================================
# Import
# ============================================================================================
@router.get("/app/import", response_class=HTMLResponse)
def import_form(request: Request, p: Principal = Depends(require_onboarded), db: DBSession = Depends(get_session)):
    count = db.exec(select(func.count(InvoiceRow.id)).where(InvoiceRow.org_id == p.org_id)).one()
    return render(request, "import.html", p, count=count, sample=SAMPLE_CSV)


@router.get("/app/import/sample.csv")
def sample_csv(p: Principal = Depends(current_principal)):
    return PlainTextResponse(SAMPLE_CSV, headers={"Content-Disposition": 'attachment; filename="baaki-sample.csv"'})


@router.post("/app/import")
async def do_import(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                    file: UploadFile = File(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    raw = await file.read()
    if len(raw) > 5_000_000:
        return redirect("/app/import", err="File is larger than 5 MB.")
    try:
        res = import_csv(db, p.org, raw)
    except ImportError_ as e:
        return redirect("/app/import", err=str(e))
    return redirect("/app/invoices", ok=f"Imported {res['created']} new and updated {res['updated']} invoices.")


@router.post("/app/import/demo")
def load_demo(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
              csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    try:
        res = import_csv(db, p.org, SAMPLE_CSV.encode())
    except ImportError_ as e:
        return redirect("/app/import", err=str(e))
    return redirect("/app/invoices", ok=f"Loaded {res['created']} sample invoices so you can try a run.")


# ============================================================================================
# Agent runs & approvals
# ============================================================================================
@router.post("/app/run")
def run_agent(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
              csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    if problem := billing_mod.entitlement_problem(p.org):
        return redirect("/app/billing", err=problem)
    if not p.org.agent_enabled:
        return redirect("/app/settings", err="Turn the agent on in Settings before running it.")
    res = RecoveryEngine(db, p.org).run()
    if p.org.approval_required:
        return redirect("/app/approvals", ok=f"Run complete: {res.get('actioned', 0)} actions, {res.get('queued', 0)} messages waiting for your approval.")
    disp = dispatch_outbox(db, p.org_id)
    return redirect("/app", ok=f"Run complete: {res.get('actioned', 0)} actions, {disp['sent']} messages sent.")


@router.get("/app/approvals", response_class=HTMLResponse)
def approvals(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session)):
    msgs = db.exec(select(Outbox).where(Outbox.org_id == p.org_id, Outbox.status == OutboxStatus.PENDING_APPROVAL).order_by(Outbox.created_at)).all()
    invs = {i.id: i for i in db.exec(select(InvoiceRow).where(InvoiceRow.org_id == p.org_id)).all()}
    custs = {c.id: c for c in db.exec(select(Customer).where(Customer.org_id == p.org_id)).all()}
    recent = db.exec(select(Outbox).where(Outbox.org_id == p.org_id, Outbox.status != OutboxStatus.PENDING_APPROVAL).order_by(Outbox.created_at.desc()).limit(15)).all()
    return render(request, "approvals.html", p, msgs=msgs, invs=invs, custs=custs, recent=recent)


@router.post("/app/approvals/approve-all")
def approve_all(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    msgs = db.exec(select(Outbox).where(Outbox.org_id == p.org_id, Outbox.status == OutboxStatus.PENDING_APPROVAL)).all()
    audit = DbAudit(db, p.org_id, actor=f"user:{p.user.id}")
    for m in msgs:
        m.status, m.approved_by = OutboxStatus.QUEUED, p.user.id
        db.add(m)
    audit.record("messages_bulk_approved", count=len(msgs))
    db.commit()
    disp = dispatch_outbox(db, p.org_id)
    return redirect("/app/approvals", ok=f"Approved {len(msgs)}; {disp['sent']} sent.")


# Registered after the literal route above: FastAPI matches in order, so
# "/app/approvals/approve-all" must never fall through to this int-typed path.
@router.post("/app/approvals/{msg_id}")
def decide_message(msg_id: int, request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                   verdict: str = Form(...), body: str = Form(""), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    msg = db.get(Outbox, msg_id)
    if not msg or msg.org_id != p.org_id:
        raise HTTPException(404)
    audit = DbAudit(db, p.org_id, actor=f"user:{p.user.id}")
    if verdict == "approve":
        if body.strip() and body.strip() != msg.body:
            audit.record("message_edited", outbox=msg.id, invoice_pk=msg.invoice_id, before=msg.body, after=body.strip())
            msg.body = body.strip()
        msg.status, msg.approved_by = OutboxStatus.QUEUED, p.user.id
        audit.record("message_approved", outbox=msg.id, invoice_pk=msg.invoice_id)
        db.add(msg); db.commit()
        dispatch_outbox(db, p.org_id)
        return redirect("/app/approvals", ok="Approved and sent.")
    msg.status = OutboxStatus.REJECTED
    audit.record("message_rejected", outbox=msg.id, invoice_pk=msg.invoice_id)
    db.add(msg); db.commit()
    return redirect("/app/approvals", ok="Rejected — not sent.")




# ============================================================================================
# Settings
# ============================================================================================
@router.get("/app/settings", response_class=HTMLResponse)
def settings_page(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session)):
    s = db.exec(select(PolicySettings).where(PolicySettings.org_id == p.org_id)).first() or PolicySettings(org_id=p.org_id)
    ok, msg = verify_chain(db, p.org_id)
    return render(request, "settings.html", p, s=s, chain_ok=ok, chain_msg=msg,
                  rzp_key=mask(p.org.rzp_key_id), rzp_secret_set=bool(p.org.rzp_key_secret_enc),
                  model=active_model(db, p.org_id))


@router.post("/app/settings/profile")
def save_profile(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                 legal_name: str = Form(""), reply_to_email: str = Form(""), support_phone: str = Form(""),
                 csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    p.org.legal_name, p.org.reply_to_email, p.org.support_phone = legal_name.strip(), reply_to_email.strip(), support_phone.strip()
    db.add(p.org); db.commit()
    return redirect("/app/settings", ok="Business details saved.")


# Hard ceilings the UI cannot exceed. A merchant may tighten their own guardrails freely;
# loosening past these would put them outside accepted collection conduct, so the form refuses.
POLICY_LIMITS: dict[str, tuple[float, float, str]] = {
    "contact_window_start_hour": (6, 12, "Earliest contact hour"),
    "contact_window_end_hour": (17, 21, "Latest contact hour"),
    "min_gap_days_between_contacts": (1, 30, "Days between contacts"),
    "max_contacts_per_invoice": (1, 12, "Maximum contacts per invoice"),
    "max_early_settlement_discount_pct": (0, 25, "Maximum discount %"),
    "min_days_overdue_for_discount": (0, 180, "Days overdue before a discount"),
    "max_installments": (2, 12, "Maximum installments"),
    "max_plan_interval_days": (7, 90, "Installment interval (days)"),
    "min_first_installment_pct": (5, 100, "Minimum first installment %"),
    "max_payment_link_expiry_days": (1, 60, "Payment link expiry (days)"),
    "min_partial_payment_pct": (1, 100, "Minimum partial payment %"),
}


@router.post("/app/settings/policy")
async def save_policy(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session)):
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token", "")))
    require_owner(p)
    s = db.exec(select(PolicySettings).where(PolicySettings.org_id == p.org_id)).first()
    if not s:
        s = PolicySettings(org_id=p.org_id)

    before = {f: getattr(s, f) for f in POLICY_LIMITS}
    for field, (lo, hi, label) in POLICY_LIMITS.items():
        if field not in form:
            continue
        try:
            value = float(form[field])
        except (TypeError, ValueError):
            return redirect("/app/settings", err=f"{label} must be a number.")
        if not (lo <= value <= hi):
            return redirect("/app/settings", err=f"{label} must be between {lo:g} and {hi:g}.")
        setattr(s, field, value if isinstance(getattr(s, field), float) else int(value))

    if s.contact_window_start_hour >= s.contact_window_end_hour:
        return redirect("/app/settings", err="The contact window must start before it ends.")

    changed = {f: [before[f], getattr(s, f)] for f in POLICY_LIMITS if before[f] != getattr(s, f)}
    s.updated_at, s.updated_by = utcnow(), p.user.id
    db.add(s)
    if changed:
        DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("policy_changed", changes=changed)
    db.commit()
    return redirect("/app/settings", ok="Guardrails updated." if changed else "No changes.")


@router.post("/app/settings/agent")
def save_agent(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
               agent_enabled: str = Form(""), approval_required: str = Form(""), llm_provider: str = Form("rules"),
               csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    require_owner(p)
    if llm_provider not in ("rules", "openai", "claude"):
        return redirect("/app/settings", err="Unknown provider.")
    was = p.org.agent_enabled
    if agent_enabled == "on" and not p.user.email_verified_at:
        return redirect("/app/settings", err="Confirm your email address before switching the agent on — "
                                             "we won't contact your customers on behalf of an unverified account.")
    p.org.agent_enabled = agent_enabled == "on"
    p.org.approval_required = approval_required == "on"
    p.org.llm_provider = llm_provider
    db.add(p.org)
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record(
        "agent_settings_changed", enabled=p.org.agent_enabled, approval_required=p.org.approval_required, provider=llm_provider)
    db.commit()
    note = "Agent turned on." if p.org.agent_enabled and not was else "Settings saved."
    return redirect("/app/settings", ok=note)


@router.post("/app/settings/razorpay")
def save_razorpay(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                  key_id: str = Form(""), key_secret: str = Form(""), webhook_secret: str = Form(""),
                  csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    require_owner(p)
    key_id = key_id.strip()
    if key_id and not key_id.startswith("rzp_test_"):
        return redirect("/app/settings", err="Only test-mode keys (rzp_test_…) are accepted. Live keys are refused by design.")
    if key_id:
        p.org.rzp_key_id = key_id
    if key_secret.strip():
        p.org.rzp_key_secret_enc = encrypt_secret(key_secret.strip())
    if webhook_secret.strip():
        p.org.rzp_webhook_secret_enc = encrypt_secret(webhook_secret.strip())
    db.add(p.org)
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("razorpay_credentials_updated", key_id=mask(key_id))
    db.commit()
    return redirect("/app/settings", ok="Razorpay credentials saved (encrypted at rest).")


@router.get("/app/team", response_class=HTMLResponse)
def team_page(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session)):
    from .models import Token

    users = db.exec(select(User).where(User.org_id == p.org_id).order_by(User.created_at)).all()
    invites = db.exec(select(Token).where(Token.org_id == p.org_id, Token.purpose == TokenPurpose.INVITE,
                                          Token.used_at.is_(None)).order_by(Token.created_at.desc())).all()
    live = [i for i in invites if (i.expires_at if i.expires_at.tzinfo else i.expires_at.replace(tzinfo=utcnow().tzinfo)) > utcnow()]
    return render(request, "team.html", p, users=users, invites=live)


@router.post("/app/team/invite")
def invite_member(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
                  email: str = Form(...), role: str = Form("member"), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    require_owner(p)
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return redirect("/app/team", err="That email address doesn't look right.")
    if db.exec(select(User).where(User.email == email)).first():
        return redirect("/app/team", err="Someone with that email already has a Baaki account.")
    if role not in ("owner", "member"):
        return redirect("/app/team", err="Unknown role.")
    try:
        accounts.send_invite(db, p.org, p.user, email, Role(role))
    except Exception as e:
        return redirect("/app/team", err=f"Couldn't send the invitation: {str(e)[:120]}")
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("invite_sent", email=email, role=role)
    db.commit()
    return redirect("/app/team", ok=f"Invitation sent to {email}.")


@router.post("/app/team/revoke-invite/{token_id}")
def revoke_invite(token_id: int, request: Request, p: Principal = Depends(current_principal),
                  db: DBSession = Depends(get_session), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    require_owner(p)
    from .models import Token

    row = db.get(Token, token_id)
    if not row or row.org_id != p.org_id:
        raise HTTPException(404)
    row.used_at = utcnow()
    db.add(row)
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("invite_revoked", email=row.email)
    db.commit()
    return redirect("/app/team", ok="Invitation revoked.")


@router.post("/app/team/{user_id}")
def update_member(user_id: int, request: Request, p: Principal = Depends(current_principal),
                  db: DBSession = Depends(get_session), action: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    require_owner(p)
    target = db.get(User, user_id)
    if not target or target.org_id != p.org_id:
        raise HTTPException(404)

    owners = db.exec(select(User).where(User.org_id == p.org_id, User.role == Role.OWNER, User.disabled == False)).all()  # noqa: E712
    losing_last_owner = target.role == Role.OWNER and len(owners) <= 1 and action in ("demote", "disable")
    if losing_last_owner:
        return redirect("/app/team", err="An organisation must keep at least one active owner.")

    audit = DbAudit(db, p.org_id, actor=f"user:{p.user.id}")
    if action == "promote":
        target.role = Role.OWNER
    elif action == "demote":
        target.role = Role.MEMBER
    elif action == "disable":
        target.disabled = True
        for sess in db.exec(select(SessionRow).where(SessionRow.user_id == target.id, SessionRow.revoked == False)).all():  # noqa: E712
            sess.revoked = True   # revoke immediately; a disabled user shouldn't finish their session
            db.add(sess)
    elif action == "enable":
        target.disabled = False
    else:
        return redirect("/app/team", err="Unknown action.")
    db.add(target)
    audit.record("team_member_updated", target=target.email, action=action, role=target.role.value)
    db.commit()
    return redirect("/app/team", ok=f"{target.email} updated.")


@router.post("/app/settings/refit-risk")
def refit_risk(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
               csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    require_owner(p)
    audit = DbAudit(db, p.org_id, actor=f"user:{p.user.id}")
    res = fit_org_model(db, p.org, audit)
    db.commit()
    if not res["fitted"]:
        return redirect("/app/settings", err=f"Not enough settled history to fit a model yet — {res['reason']}.")
    return redirect("/app/settings", ok=f"Model refitted on your own ledger: precision {res['precision']}, "
                                        f"recall {res['recall']} on {res['holdout_rows']} held-out invoices.")


# ============================================================================================
# Billing
# ============================================================================================
@router.get("/app/billing", response_class=HTMLResponse)
def billing_page(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session)):
    used = db.exec(select(func.count(InvoiceRow.id)).where(InvoiceRow.org_id == p.org_id, InvoiceRow.status.in_(("open", "partially_paid")))).one()
    return render(request, "billing.html", p, plans=billing_mod.CATALOGUE, used=used, limit=p.org.invoice_limit)


@router.post("/app/billing/subscribe")
def subscribe(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session),
              plan: str = Form(...), csrf_token: str = Form("")):
    require_csrf(request, csrf_token)
    require_owner(p)
    try:
        res = billing_mod.create_subscription(db, p.org, Plan(plan))
    except (billing_mod.BillingUnavailable, ValueError) as e:
        return redirect("/app/billing", err=str(e))
    DbAudit(db, p.org_id, actor=f"user:{p.user.id}").record("subscription_created", plan=plan, mode=res["mode"], subscription_id=res["subscription_id"])
    db.commit()
    if res.get("short_url"):
        return RedirectResponse(res["short_url"], status_code=303)
    return redirect("/app/billing", ok=res.get("message", f"Subscribed to {plan}."))


# ============================================================================================
# Webhooks
# ============================================================================================
@router.post("/webhooks/razorpay/{slug}")
async def razorpay_webhook(slug: str, request: Request, db: DBSession = Depends(get_session)):
    """Per-org endpoint. The signature is verified against that org's own webhook secret."""
    from .security import decrypt_secret

    org = db.exec(select(Org).where(Org.slug == slug)).first()
    if not org:
        raise HTTPException(404, "Unknown organisation")
    secret = decrypt_secret(org.rzp_webhook_secret_enc) or "baaki-sandbox"
    body = await request.body()
    sig = request.headers.get("X-Razorpay-Signature", "")
    audit = DbAudit(db, org.id, actor="razorpay")
    if not verify_webhook(body, sig, secret):
        audit.record("webhook_rejected", reason="bad signature")
        db.commit()
        raise HTTPException(400, "invalid signature")

    event = json.loads(body)
    kind = event.get("event", "")
    if kind.startswith("subscription."):
        status = billing_mod.apply_subscription_event(db, org, kind, event)
        audit.record("subscription_event", event=kind, status=status)
        db.commit()
        return {"status": status}
    if kind not in ("payment_link.paid", "payment_link.partially_paid"):
        audit.record("webhook_ignored", event=kind)
        db.commit()
        return {"status": "ignored"}

    link = event["payload"]["payment_link"]["entity"]
    payment = event["payload"]["payment"]["entity"]
    number = (link.get("notes") or {}).get("invoice_id")
    row = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.number == number)).first() if number else None
    if not row:
        row = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.payment_link_id == link["id"])).first()
    if not row:
        audit.record("webhook_unmatched", event=kind, link_id=link["id"])
        db.commit()
        return {"status": "unmatched"}
    credited = record_payment(db, org.id, row, payment["id"], int(payment["amount"]), "razorpay_link", audit)
    db.commit()
    return {"status": "ok", "invoice": row.number, "credited_paise": credited}


@router.post("/webhooks/clerk")
async def clerk_webhook(request: Request, db: DBSession = Depends(get_session)):
    secret = os.environ.get("CLERK_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "Clerk webhooks are not configured")
    body = await request.body()
    if not clerk_auth.verify_webhook(body, request.headers, secret):
        raise HTTPException(400, "invalid signature")
    result = clerk_auth.apply_webhook(db, json.loads(body))
    return {"status": result}


# ============================================================================================
# Ops
# ============================================================================================
@router.get("/healthz")
def healthz(db: DBSession = Depends(get_session)):
    db.exec(select(func.count(Org.id))).one()
    return {"status": "ok", "time": utcnow().isoformat()}


@router.get("/app/audit", response_class=HTMLResponse)
def audit_page(request: Request, p: Principal = Depends(current_principal), db: DBSession = Depends(get_session), limit: int = 200):
    rows = db.exec(select(AuditRow).where(AuditRow.org_id == p.org_id).order_by(AuditRow.seq.desc()).limit(limit)).all()
    ok, msg = verify_chain(db, p.org_id)
    invs = {i.id: i for i in db.exec(select(InvoiceRow).where(InvoiceRow.org_id == p.org_id)).all()}
    return render(request, "audit.html", p, rows=rows, chain_ok=ok, chain_msg=msg, invs=invs,
                  payloads={r.id: json.loads(r.payload_json) for r in rows})


@router.get("/app/audit/export.jsonl")
def audit_export(p: Principal = Depends(current_principal), db: DBSession = Depends(get_session)):
    rows = db.exec(select(AuditRow).where(AuditRow.org_id == p.org_id).order_by(AuditRow.seq)).all()
    lines = "\n".join(json.dumps({"seq": r.seq, "at": r.at.isoformat(), "event": r.event, "actor": r.actor,
                                  **json.loads(r.payload_json), "prev": r.prev, "hash": r.hash}, default=str) for r in rows)
    return PlainTextResponse(lines, headers={"Content-Disposition": 'attachment; filename="baaki-audit.jsonl"'})


# ============================================================================================
def create_app() -> FastAPI:
    app = FastAPI(title="Baaki", docs_url=None, redoc_url=None)
    init_db()
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    app.include_router(router)

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401:
            return RedirectResponse("/login?err=Please+sign+in+to+continue.", status_code=303)
        # A dependency that needs to send the user elsewhere (the onboarding gate) raises a
        # redirect rather than returning one, since dependencies cannot short-circuit a response.
        if exc.status_code in (303, 307) and isinstance(exc.detail, str) and exc.detail.startswith("/"):
            return RedirectResponse(exc.detail, status_code=303)
        if request.headers.get("accept", "").startswith("application/json"):
            return Response(json.dumps({"detail": exc.detail}), exc.status_code, media_type="application/json")
        return templates.TemplateResponse(request, "error.html",
                                          {"code": exc.status_code, "detail": exc.detail, "principal": None}, status_code=exc.status_code)

    return app


app = create_app()
