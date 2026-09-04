"""Org-scoped business logic: the audit chain, ledger import, and the recovery engine.

The engine deliberately reuses the simulation's `Policy`, `Toolbox` and brains verbatim — DB rows
are converted to domain objects, run through the identical gate, and written back. There is one
policy implementation in this codebase, not a "demo" one and a "real" one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import replace
from datetime import date, datetime, timedelta

from sqlmodel import Session as DBSession, func, select

from ..brain import ClaudeBrain, DecisionContext, OpenAIBrain, ResilientBrain, RuleBrain
from ..domain import IST, ActionType, Archetype, Debtor, Intent, Invoice, InvoiceStatus, rupees
from ..policy import Policy, PolicyBounds
from ..razorpay_client import FakeRazorpay, RealRazorpay
from ..risk import FEATURES, RiskModel, features, is_holdout
from ..tools import Toolbox
from .models import (
    AuditRow, Customer, Event, InvoiceRow, Org, Outbox, OutboxStatus, PaymentRow, PolicySettings,
    RiskModelRow, RunLock, utcnow,
)
from .security import decrypt_secret
from .transports import channel_for

# Weights from the held-out training run in `reports/summary_rules.json` (precision 0.74 /
# recall 1.00). A new merchant has no payment history to train on, so these ship as the prior;
# `baaki-worker retrain` refits them per org once there are enough closed invoices.
DEFAULT_RISK_WEIGHTS = [-0.652, -0.397, 2.829, 1.677, 0.862, 0.325]


# ============================================================================================
class DbAudit:
    """Hash-chained audit, one chain per org. Duck-types `audit.AuditLog` for the Toolbox."""

    GENESIS = "0" * 64

    def __init__(self, db: DBSession, org_id: int, actor: str = "agent"):
        self.db, self.org_id, self.actor = db, org_id, actor
        last = db.exec(select(AuditRow).where(AuditRow.org_id == org_id).order_by(AuditRow.seq.desc())).first()
        self._prev = last.hash if last else self.GENESIS
        self._seq = (last.seq + 1) if last else 0
        self.entries: list[dict] = []

    def record(self, event: str, **fields) -> dict:
        invoice_id = fields.pop("invoice_pk", None)
        payload = {"seq": self._seq, "event": event, **fields}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        h = hashlib.sha256((self._prev + body).encode()).hexdigest()
        self.db.add(AuditRow(
            org_id=self.org_id, seq=self._seq, event=event, invoice_id=invoice_id, actor=self.actor,
            payload_json=json.dumps(fields, default=str), prev=self._prev, hash=h,
        ))
        self._prev, self._seq = h, self._seq + 1
        self.entries.append(payload)
        return payload

    def filter(self, event: str | None = None, **match) -> list[dict]:
        return [e for e in self.entries if (event is None or e["event"] == event) and all(e.get(k) == v for k, v in match.items())]


def verify_chain(db: DBSession, org_id: int) -> tuple[bool, str]:
    prev, n = DbAudit.GENESIS, 0
    for row in db.exec(select(AuditRow).where(AuditRow.org_id == org_id).order_by(AuditRow.seq)):
        n += 1
        payload = {"seq": row.seq, "event": row.event, **json.loads(row.payload_json)}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        if row.prev != prev:
            return False, f"chain break at seq {row.seq}"
        if hashlib.sha256((prev + body).encode()).hexdigest() != row.hash:
            return False, f"tampered entry at seq {row.seq}"
        prev = row.hash
    return True, f"{n} entries verified; chain intact" + (f" (head {prev[:12]}…)" if n else "")


# ============================================================================================
def bounds_for(db: DBSession, org_id: int) -> PolicyBounds:
    s = db.exec(select(PolicySettings).where(PolicySettings.org_id == org_id)).first()
    if not s:
        return PolicyBounds()
    return replace(
        PolicyBounds(),
        contact_window_start_hour=s.contact_window_start_hour,
        contact_window_end_hour=s.contact_window_end_hour,
        min_gap_days_between_contacts=s.min_gap_days_between_contacts,
        max_contacts_per_invoice=s.max_contacts_per_invoice,
        max_early_settlement_discount_pct=s.max_early_settlement_discount_pct,
        min_days_overdue_for_discount=s.min_days_overdue_for_discount,
        max_installments=s.max_installments,
        max_plan_interval_days=s.max_plan_interval_days,
        min_first_installment_pct=s.min_first_installment_pct,
        max_payment_link_expiry_days=s.max_payment_link_expiry_days,
        min_partial_payment_pct=s.min_partial_payment_pct,
    )


def razorpay_for(org: Org):
    """Real client when the merchant has connected test-mode keys; otherwise a sandbox."""
    key_id, secret = org.rzp_key_id, decrypt_secret(org.rzp_key_secret_enc)
    if key_id and secret:
        return RealRazorpay(key_id, secret)
    return FakeRazorpay(webhook_secret=decrypt_secret(org.rzp_webhook_secret_enc) or "baaki-sandbox")


def brain_for(org: Org, audit) -> object:
    rules = RuleBrain()
    cls = {"openai": OpenAIBrain, "claude": ClaudeBrain}.get(org.llm_provider)
    if not cls:
        return rules
    try:
        return ResilientBrain(cls(effort="low"), rules, audit)
    except Exception as e:
        audit.record("brain_unavailable", provider=org.llm_provider, error=f"{type(e).__name__}: {str(e)[:200]}")
        return rules


# ============================================================================================
CSV_COLUMNS = ["invoice_number", "customer_name", "customer_email", "customer_phone", "amount_inr", "issued_on", "due_on", "description"]


class ImportError_(Exception):
    pass


def import_csv(db: DBSession, org: Org, raw: bytes) -> dict:
    """Import a receivables CSV. Rejects the whole file if any row is malformed — a half-imported
    ledger is worse than none, because the agent would chase invoices that don't reconcile."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("File must be UTF-8 encoded CSV.")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportError_("The file appears to be empty.")
    missing = [c for c in CSV_COLUMNS[:7] if c not in reader.fieldnames]
    if missing:
        raise ImportError_(f"Missing required column(s): {', '.join(missing)}. Expected: {', '.join(CSV_COLUMNS)}")

    parsed, errors = [], []
    for i, row in enumerate(reader, start=2):
        try:
            number = (row["invoice_number"] or "").strip()
            name = (row["customer_name"] or "").strip()
            if not number or not name:
                raise ValueError("invoice_number and customer_name are required")
            amount = str(row["amount_inr"]).replace(",", "").replace("₹", "").strip()
            paise = int(round(float(amount) * 100))
            if paise <= 0:
                raise ValueError("amount_inr must be positive")
            issued = date.fromisoformat(row["issued_on"].strip())
            due = date.fromisoformat(row["due_on"].strip())
            if due < issued:
                raise ValueError("due_on is before issued_on")
            parsed.append(dict(number=number, name=name, email=(row.get("customer_email") or "").strip(),
                               phone=(row.get("customer_phone") or "").strip(), paise=paise, issued=issued, due=due,
                               description=(row.get("description") or "").strip()))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            errors.append(f"row {i}: {e}")
    if errors:
        raise ImportError_(f"{len(errors)} row(s) rejected, nothing imported:\n" + "\n".join(errors[:10]))

    open_count = db.exec(select(func.count(InvoiceRow.id)).where(InvoiceRow.org_id == org.id, InvoiceRow.status.in_(("open", "partially_paid")))).one()
    if open_count + len(parsed) > org.invoice_limit:
        raise ImportError_(f"Your {org.plan.value} plan allows {org.invoice_limit} open invoices "
                           f"({open_count} in use, {len(parsed)} in this file). Upgrade on the Billing page.")

    created = updated = 0
    audit = DbAudit(db, org.id, actor="user")
    for r in parsed:
        cust = db.exec(select(Customer).where(Customer.org_id == org.id, Customer.name == r["name"])).first()
        if not cust:
            cust = Customer(org_id=org.id, name=r["name"], email=r["email"], phone=r["phone"], external_id=r["name"][:40])
            db.add(cust); db.commit(); db.refresh(cust)
        elif r["email"] and not cust.email:
            cust.email = r["email"]; db.add(cust)

        inv = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id, InvoiceRow.number == r["number"])).first()
        if inv:
            inv.amount_paise, inv.due_on, inv.description = r["paise"], r["due"], r["description"] or inv.description
            inv.updated_at = utcnow(); db.add(inv); updated += 1
        else:
            db.add(InvoiceRow(org_id=org.id, customer_id=cust.id, number=r["number"], description=r["description"],
                              amount_paise=r["paise"], issued_on=r["issued"], due_on=r["due"], next_action_on=date.today()))
            created += 1
    audit.record("ledger_imported", created=created, updated=updated, rows=len(parsed))
    db.commit()
    return {"created": created, "updated": updated, "rows": len(parsed)}


SAMPLE_CSV = """invoice_number,customer_name,customer_email,customer_phone,amount_inr,issued_on,due_on,description
INV-2041,Mehta Traders,accounts@mehtatraders.in,+919812345671,240866.00,2026-06-20,2026-07-20,Printed labels — PO 1647
INV-2042,Chawla Hardware,accounts@chawlahardware.in,+919812345672,54965.06,2026-07-01,2026-07-31,MS pipes — PO 2213
INV-2043,Nair Logistics,ap@nairlogistics.in,+919812345673,85253.00,2026-07-05,2026-08-04,Pallets — PO 2288
INV-2044,Iyer Hardware,accounts@iyerhardware.in,+919812345674,18187.00,2026-07-11,2026-08-10,Fasteners — PO 2301
INV-2045,Bose Electricals,finance@boseelectricals.in,+919812345675,120240.00,2026-07-14,2026-08-13,Packaging film — PO 2344
"""


# ============================================================================================
def risk_score(inv: InvoiceRow, cust: Customer, history: dict, today: date,
               weights: list[float] | None = None) -> float | None:
    """Probability this invoice won't be paid in 30 days without intervention — or None.

    Four of the six features describe how this customer has paid *before*. With no settled
    invoices those are all zero and the model collapses to its bias term, returning the same
    number for everyone; a uniform value dressed as a prediction is worse than none, so we
    return None and the UI ranks by ageing instead. Once an org has enough of its own history,
    `fit_org_model` replaces the shipped prior with weights fitted on their actual payers.
    """
    if history.get("total", 0) == 0:
        return None
    dom = _to_domain_invoice(inv, today)
    debtor = _to_domain_debtor(cust, history)
    return round(model_predict(weights or DEFAULT_RISK_WEIGHTS,
                               features(dom, debtor, _day_index(today, inv.due_on))), 3)


MIN_ROWS_TO_FIT = 40      # below this a fitted model is noise dressed as precision
MIN_POSITIVES_TO_FIT = 8


def active_model(db: DBSession, org_id: int) -> RiskModelRow | None:
    return db.exec(select(RiskModelRow).where(RiskModelRow.org_id == org_id, RiskModelRow.active == True)  # noqa: E712
                   .order_by(RiskModelRow.fitted_at.desc())).first()


def fit_org_model(db: DBSession, org: Org, audit: "DbAudit | None" = None) -> dict:
    """Refit the risk model on this org's own settled invoices.

    Label: the invoice was not paid within 30 days of falling due. Only resolved invoices carry a
    label — an open invoice's outcome isn't known yet, and including it would leak the present
    into the training set. The split is by customer id, so no customer appears in both train and
    holdout; a per-invoice split would let the same payer's behaviour teach and then grade.
    """
    rows = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org.id,
                                            InvoiceRow.status.in_(("paid", "stopped")))).all()
    customers = {c.id: c for c in db.exec(select(Customer).where(Customer.org_id == org.id)).all()}
    train, hold = [], []
    for r in rows:
        cust = customers.get(r.customer_id)
        if not cust:
            continue
        settled = r.updated_at.date()
        label = 1 if (r.status != "paid" or (settled - r.due_on).days > 30) else 0
        # History as it stood before this invoice settled, so the label can't feed its own features.
        hist = _history_excluding(rows, r, cust.id)
        x = features(_to_domain_invoice(r, r.due_on), _to_domain_debtor(cust, hist), 0)
        (hold if is_holdout(str(cust.id)) else train).append((x, label))

    positives = sum(y for _, y in train + hold)
    if len(train) + len(hold) < MIN_ROWS_TO_FIT or positives < MIN_POSITIVES_TO_FIT or not hold:
        result = {"fitted": False,
                  "reason": f"needs {MIN_ROWS_TO_FIT}+ settled invoices with {MIN_POSITIVES_TO_FIT}+ late ones "
                            f"(have {len(train) + len(hold)} and {positives})"}
        if audit:
            audit.record("risk_model_skipped", **result)
        return result

    model = RiskModel()
    model.fit(train)
    preds = [(model_predict(model.weights, x), y) for x, y in hold]
    metrics = RiskModel.metrics(preds, 0.5)
    for old in db.exec(select(RiskModelRow).where(RiskModelRow.org_id == org.id)).all():
        old.active = False
        db.add(old)
    row = RiskModelRow(org_id=org.id, weights_json=json.dumps(model.weights), train_rows=len(train),
                       holdout_rows=len(hold), positives=metrics["positives"], precision=metrics["precision"],
                       recall=metrics["recall"], f1=metrics["f1"],
                       base_rate=round(sum(y for _, y in hold) / len(hold), 3))
    db.add(row)
    db.commit()
    result = {"fitted": True, "train_rows": len(train), "holdout_rows": len(hold), **metrics}
    if audit:
        audit.record("risk_model_fitted", **result)
    return result


def model_predict(weights: list[float], x: list[float]) -> float:
    return 1.0 / (1.0 + math.exp(-sum(w * xi for w, xi in zip(weights, x))))


def _history_excluding(rows: list[InvoiceRow], target: InvoiceRow, customer_id: int) -> dict:
    prior = [r for r in rows if r.customer_id == customer_id and r.id != target.id and r.due_on < target.due_on]
    late = [r for r in prior if r.updated_at.date() > r.due_on]
    days = [(r.updated_at.date() - r.due_on).days for r in late]
    return {"total": len(prior), "late": len(late),
            "partials": sum(1 for r in prior if 0 < r.amount_paid_paise < r.amount_paise),
            "avg_days_late": round(sum(days) / len(days), 1) if days else 0.0}


def work_priority(row: InvoiceRow, today: date) -> float:
    """Rupees at stake, weighted by risk when known and by ageing when it isn't."""
    ageing = 1.0 + max(0, (today - row.due_on).days) / 90.0
    return row.outstanding_paise * (row.risk_score if row.risk_score is not None else 0.5) * ageing


def _day_index(today: date, due: date) -> int:
    """The engine works in the simulation's relative-day space: day 0 = today."""
    return 0


def _to_domain_invoice(row: InvoiceRow, today: date) -> Invoice:
    due_offset = (row.due_on - today).days
    inv = Invoice(
        id=row.number, debtor_id=str(row.customer_id), amount_paise=row.amount_paise,
        issue_day=(row.issued_on - today).days, due_day=due_offset, description=row.description or row.number,
        status=InvoiceStatus(row.status), amount_paid_paise=row.amount_paid_paise,
        contact_count=row.contact_count, dispute_open=row.dispute_open, hardship_flagged=row.hardship_flagged,
        cease_requested=row.cease_requested, discount_pct_offered=row.discount_pct_offered,
        settlement_amount_paise=row.settlement_amount_paise, payment_link_id=row.payment_link_id,
        payment_link_url=row.payment_link_url, risk_score=row.risk_score,
        escalation_reason=row.escalation_reason, stop_reason=row.stop_reason,
    )
    if row.last_contact_on:
        inv.contact_days = [(row.last_contact_on - today).days]
    if row.inbound_pending and row.last_inbound_text:
        inv.last_inbound, inv.inbound_unprocessed, inv.last_inbound_day = row.last_inbound_text, True, 0
    inv.next_action_day = (row.next_action_on - today).days if row.next_action_on else 0
    inv.promised_pay_day = (row.promised_pay_on - today).days if row.promised_pay_on else None
    return inv


def _to_domain_debtor(c: Customer, history: dict) -> Debtor:
    return Debtor(
        id=str(c.id), name=c.name, email=c.email, contact=c.phone, city=c.city,
        prior_invoices=history.get("total", 0), prior_late_count=history.get("late", 0),
        avg_days_late=history.get("avg_days_late", 0.0), prior_partial_payments=history.get("partials", 0),
        archetype=Archetype.FORGETFUL,  # unused outside the simulator; never shown to a model
    )


def customer_history(db: DBSession, org_id: int, customer_id: int) -> dict:
    """Payment behaviour on *resolved* invoices only.

    Counting currently-open invoices as "late history" would make every customer look like a
    100% late payer on the day a merchant first imports their ledger, which tells the risk model
    nothing. History is what has already closed; the invoice being scored contributes its own
    ageing through the `days_overdue` feature instead.

    `updated_at` is a proxy for the settlement date — the row is last written when the final
    payment lands. It is approximate for invoices edited after payment, which is rare and only
    shifts one feature of six.
    """
    rows = db.exec(select(InvoiceRow).where(InvoiceRow.org_id == org_id, InvoiceRow.customer_id == customer_id)).all()
    resolved = [r for r in rows if r.status in ("paid", "stopped")]
    late = [r for r in resolved if r.updated_at.date() > r.due_on]
    days_late = [(r.updated_at.date() - r.due_on).days for r in late]
    partials = sum(1 for r in rows if 0 < r.amount_paid_paise < r.amount_paise)
    return {"total": len(resolved), "late": len(late), "partials": partials,
            "avg_days_late": round(sum(days_late) / len(days_late), 1) if days_late else 0.0}


# ============================================================================================
class RecoveryEngine:
    """One pass over an org's ledger. Idempotent per day: an invoice already actioned today
    has `next_action_on` in the future and is skipped."""

    def __init__(self, db: DBSession, org: Org, today: date | None = None, dry_run: bool = False):
        self.db, self.org = db, org
        self.today = today or datetime.now(IST).date()
        self.dry_run = dry_run
        self.audit = DbAudit(db, org.id, actor="agent")
        self.policy = Policy(bounds_for(db, org.id))
        self.rzp = razorpay_for(org)
        self.brain = brain_for(org, self.audit)
        fitted = active_model(db, org.id)
        self.weights = json.loads(fitted.weights_json) if fitted else DEFAULT_RISK_WEIGHTS
        self.model_source = "org" if fitted else "prior"
        self.queued: list[Outbox] = []
        self.actioned = 0
        self.blocked = 0

    def _due(self) -> list[InvoiceRow]:
        rows = self.db.exec(select(InvoiceRow).where(
            InvoiceRow.org_id == self.org.id,
            InvoiceRow.status.in_(("open", "partially_paid")),
        )).all()
        due = [r for r in rows if r.next_action_on is None or r.next_action_on <= self.today]
        due.sort(key=lambda r: -work_priority(r, self.today))
        return due

    def run(self) -> dict:
        if not self.org.agent_enabled:
            return {"skipped": "agent is paused for this organisation"}
        self.audit.record("agent_run_started", date=str(self.today), provider=self.org.llm_provider,
                          approval_required=self.org.approval_required, dry_run=self.dry_run,
                          risk_model=self.model_source)
        for row in self._due():
            try:
                self._step(row)
            except Exception as e:  # one bad invoice must never stop the batch
                self.audit.record("invoice_error", invoice=row.number, invoice_pk=row.id,
                                  error=f"{type(e).__name__}: {str(e)[:200]}")
        self.audit.record("agent_run_finished", actioned=self.actioned, blocked=self.blocked, queued=len(self.queued))
        self.db.commit()
        return {"date": str(self.today), "considered": len(self._due()) + self.actioned,
                "actioned": self.actioned, "blocked": self.blocked, "queued": len(self.queued)}

    def _step(self, row: InvoiceRow) -> None:
        cust = self.db.get(Customer, row.customer_id)
        if cust and cust.do_not_contact and not row.cease_requested:
            row.cease_requested = True  # a cease request applies to every invoice for that customer
        hist = customer_history(self.db, self.org.id, row.customer_id)
        row.risk_score = risk_score(row, cust, hist, self.today, self.weights)

        dom = _to_domain_invoice(row, self.today)
        debtor = _to_domain_debtor(cust, hist)
        now = datetime.now(IST)
        ctx = DecisionContext(day=0, inv=dom, debtor=debtor,
                              allowed=self.policy.allowed_actions(dom, 0, now),
                              bounds=self.policy.describe(), stop_reason=self.policy.stop_reason(dom))
        decision = self.brain.decide(ctx)
        self.audit.record("decision", invoice=row.number, invoice_pk=row.id, source=decision.source,
                          action=decision.action.value, params=decision.params, rationale=decision.rationale)

        # The reply is applied to state *before* the gate, so a cease request blocks the very
        # action proposed alongside it.
        if dom.inbound_unprocessed:
            self._apply_intent(row, dom, cust, decision.reply_intent)

        tools = Toolbox(self.policy, self.rzp, self.audit, {debtor.id: debtor},
                        merchant=self.org.legal_name or self.org.name,
                        on_message=lambda inv, day, text, channel, action, d: self._queue(row, cust, text, channel, action, d))
        result = tools.execute(decision, dom, 0, now)
        if result.ok:
            self.actioned += 1
        elif not result.verdict.allowed:
            self.blocked += 1
        self._write_back(row, dom, decision, result)

    def _apply_intent(self, row: InvoiceRow, dom: Invoice, cust: Customer | None, intent: Intent) -> None:
        dom.inbound_unprocessed = False
        row.inbound_pending = False
        if intent == Intent.CEASE_CONTACT:
            dom.cease_requested = True
            if cust:
                cust.do_not_contact = True   # honoured across every invoice for that customer
                self.db.add(cust)
        elif intent == Intent.DISPUTE:
            dom.dispute_open = True
        elif intent == Intent.HARDSHIP:
            dom.hardship_flagged = True
        elif intent == Intent.PROMISE_TO_PAY:
            dom.promised_pay_day = 7
            row.promised_pay_on = self.today + timedelta(days=7)
        self.audit.record("intent_classified", invoice=row.number, invoice_pk=row.id,
                          intent=intent.value, text=(dom.last_inbound or "")[:400])

    def _queue(self, row: InvoiceRow, cust: Customer, text: str, channel: str, action: str, decision) -> None:
        status = OutboxStatus.PENDING_APPROVAL if self.org.approval_required else OutboxStatus.QUEUED
        if self.dry_run:
            status = OutboxStatus.PENDING_APPROVAL
        chan, address = channel_for(cust.email if cust else "", cust.phone if cust else "")
        msg = Outbox(org_id=self.org.id, invoice_id=row.id, channel=chan,
                     to_address=address,
                     subject=f"Invoice {row.number} — payment reminder from {self.org.legal_name or self.org.name}",
                     body=text, status=status, action=action,
                     rationale=getattr(decision, "rationale", ""), decided_by=getattr(decision, "source", "rules"))
        self.db.add(msg)
        self.queued.append(msg)

    def _write_back(self, row: InvoiceRow, dom: Invoice, decision, result) -> None:
        row.status = dom.status.value
        row.contact_count = dom.contact_count
        row.dispute_open, row.hardship_flagged, row.cease_requested = dom.dispute_open, dom.hardship_flagged, dom.cease_requested
        row.inbound_pending = dom.inbound_unprocessed
        row.escalation_reason, row.stop_reason = dom.escalation_reason, dom.stop_reason
        row.discount_pct_offered, row.settlement_amount_paise = dom.discount_pct_offered, dom.settlement_amount_paise
        row.payment_link_id, row.payment_link_url = dom.payment_link_id, dom.payment_link_url
        row.risk_score = dom.risk_score
        if dom.plan:
            row.plan_json = json.dumps({"installments": dom.plan.installments, "first_amount_paise": dom.plan.first_amount_paise,
                                        "interval_days": dom.plan.interval_days, "accepted": dom.plan.accepted})
        if result.contacted:
            row.last_contact_on = self.today
        row.next_action_on = self.today + timedelta(days=max(1, dom.next_action_day)) if dom.next_action_day < 10**5 else None
        row.updated_at = utcnow()
        self.db.add(row)
        for ev in dom.timeline:
            self.db.add(Event(org_id=self.org.id, invoice_id=row.id, kind=ev.kind, summary=ev.summary,
                              channel=ev.data.get("channel", ""), payload_json=json.dumps(ev.data, default=str)))


# ============================================================================================
def record_payment(db: DBSession, org_id: int, invoice: InvoiceRow, payment_id: str, amount_paise: int, source: str, audit: DbAudit) -> int:
    """Idempotent credit. The unique (org_id, external_payment_id) index is the real guard;
    the pre-check just avoids a noisy rollback on the common replay case."""
    existing = db.exec(select(PaymentRow).where(PaymentRow.org_id == org_id, PaymentRow.external_payment_id == payment_id)).first()
    if existing:
        audit.record("payment_duplicate_ignored", invoice=invoice.number, invoice_pk=invoice.id, payment_id=payment_id)
        return 0
    credited = min(amount_paise, invoice.outstanding_paise)
    invoice.amount_paid_paise += credited
    written_off = 0
    if invoice.settlement_amount_paise and invoice.amount_paid_paise >= invoice.settlement_amount_paise and invoice.outstanding_paise > 0:
        written_off = invoice.outstanding_paise
        invoice.amount_paid_paise = invoice.amount_paise
    if invoice.outstanding_paise == 0:
        invoice.status, invoice.next_action_on = "paid", None
    elif invoice.status == "open":
        invoice.status = "partially_paid"
    invoice.updated_at = utcnow()
    db.add(invoice)
    db.add(PaymentRow(org_id=org_id, invoice_id=invoice.id, external_payment_id=payment_id,
                      amount_paise=credited, source=source))
    db.add(Event(org_id=org_id, invoice_id=invoice.id, kind="payment",
                 summary=f"received {rupees(credited)} via {source}" + (f" (settled; {rupees(written_off)} written off)" if written_off else "")))
    audit.record("payment_received", invoice=invoice.number, invoice_pk=invoice.id, payment_id=payment_id,
                 amount_paise=credited, source=source, written_off_paise=written_off, status=invoice.status)
    return credited


# ============================================================================================
LOCK_TTL = timedelta(minutes=30)


class LockBusy(RuntimeError):
    pass


class org_lock:
    """Advisory per-org lock so two workers can't run the same ledger and double-send.

    A stale lock (holder crashed mid-run) expires after LOCK_TTL and is reclaimed; the unique
    index on `org_id` is what actually serialises two racing acquirers.
    """

    def __init__(self, db: DBSession, org_id: int, holder: str):
        self.db, self.org_id, self.holder = db, org_id, holder
        self.row: RunLock | None = None

    def __enter__(self) -> "org_lock":
        from sqlalchemy.exc import IntegrityError

        existing = self.db.exec(select(RunLock).where(RunLock.org_id == self.org_id)).first()
        if existing:
            expires = existing.expires_at if existing.expires_at.tzinfo else existing.expires_at.replace(tzinfo=utcnow().tzinfo)
            if expires > utcnow():
                raise LockBusy(f"org {self.org_id} is already being processed by {existing.holder}")
            self.db.delete(existing)
            self.db.commit()
        self.row = RunLock(org_id=self.org_id, holder=self.holder, expires_at=utcnow() + LOCK_TTL)
        self.db.add(self.row)
        try:
            self.db.commit()
        except IntegrityError:      # another worker won the race between our check and insert
            self.db.rollback()
            raise LockBusy(f"org {self.org_id} is already being processed")
        return self

    def __exit__(self, *exc) -> None:
        if self.row is not None:
            self.db.delete(self.row)
            self.db.commit()
