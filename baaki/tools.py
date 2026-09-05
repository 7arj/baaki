"""Bounded tools. A brain proposes; the Toolbox re-checks policy, executes, and audits every step.

No brain (LLM or rules) can move money or contact a debtor except through `Toolbox.execute`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .audit import AuditLog
from .domain import ActionType, CONTACT_ACTIONS, Debtor, Decision, InstallmentPlan, Invoice, InvoiceStatus, rupees
from .policy import Policy, Verdict
from .razorpay_client import PaymentLinkClient, RazorpayUnavailable

MERCHANT = "Arjun Industrial Supplies"  # simulation default; the product passes the org's legal name


@dataclass
class ExecResult:
    ok: bool
    verdict: Verdict
    detail: str = ""
    contacted: bool = False
    params: dict[str, Any] = field(default_factory=dict)


class Toolbox:
    def __init__(self, policy: Policy, rzp: PaymentLinkClient, audit: AuditLog, debtors: dict[str, Debtor], enforce: bool = True, on_message=None, merchant: str | None = None):
        self.policy = policy
        self.rzp = rzp
        self.audit = audit
        self.debtors = debtors
        # Optional sink for real delivery: fn(invoice, day, text, channel, action, decision).
        # The simulation leaves it None and the message only reaches the audit log.
        self.on_message = on_message
        self.merchant = merchant or MERCHANT
        self.enforce = enforce  # False = baseline mode: record violations but don't block
        self.violations = 0
        self.denials = 0
        self.gateway_failures = 0
        self.link_index: dict[str, str] = {}  # link_id -> invoice_id

    # -----------------------------------------------------------------------------------
    def execute(self, decision: Decision, inv: Invoice, day: int, when: datetime) -> ExecResult:
        params = self._defaults(decision, inv)
        verdict = self.policy.evaluate(decision.action, params, inv, day, when, decision.message)
        self.audit.record(
            "policy_check",
            day=day,
            invoice=inv.id,
            action=decision.action.value,
            params=params,
            source=decision.source,
            verdict=verdict.as_dict(),
            rationale=decision.rationale,
        )
        if not verdict.allowed:
            if self.enforce:
                self.denials += 1
                inv.log(day, "system", f"blocked {decision.action.value}: {verdict.reason} [{verdict.rule_id}]")
                if verdict.rule_id == "P-TIME-01":
                    inv.next_action_day = day + 1  # defer to the next contact window
                else:
                    inv.next_action_day = day + 1
                return ExecResult(False, verdict, "blocked by policy", params=params)
            self.violations += 1
            inv.log(day, "system", f"VIOLATION (unenforced baseline) {decision.action.value}: {verdict.reason} [{verdict.rule_id}]")

        handler = {
            ActionType.WAIT: self._wait,
            ActionType.SEND_REMINDER: self._send_reminder,
            ActionType.CREATE_PAYMENT_LINK: self._create_link,
            ActionType.OFFER_INSTALLMENT_PLAN: self._offer_plan,
            ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT: self._offer_discount,
            ActionType.ESCALATE_TO_HUMAN: self._escalate,
            ActionType.PAUSE_CONTACT: self._pause,
        }[decision.action]
        result = handler(decision, params, inv, day)
        result.verdict = verdict
        result.params = params
        self.audit.record(
            "action_executed" if result.ok else "action_failed",
            day=day,
            invoice=inv.id,
            action=decision.action.value,
            detail=result.detail,
            contacted=result.contacted,
        )
        return result

    # -----------------------------------------------------------------------------------
    def _defaults(self, d: Decision, inv: Invoice) -> dict[str, Any]:
        p = {k: v for k, v in d.params.items() if v is not None}
        b = self.policy.bounds
        if d.action == ActionType.CREATE_PAYMENT_LINK:
            p.setdefault("amount_paise", inv.outstanding_paise)
            p.setdefault("expire_days", 7)
            p.setdefault("accept_partial", False)
            if p["accept_partial"]:
                p.setdefault("first_min_partial_paise", int(inv.outstanding_paise * b.min_partial_payment_pct / 100))
        elif d.action == ActionType.OFFER_INSTALLMENT_PLAN:
            p.setdefault("installments", 2)
            p.setdefault("interval_days", 30)
            p.setdefault("first_amount_paise", int(inv.outstanding_paise * b.min_first_installment_pct / 100))
        elif d.action == ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT:
            p.setdefault("discount_pct", 3.0)
        elif d.action == ActionType.WAIT:
            p.setdefault("days", b.min_gap_days_between_contacts)
        return p

    def _contacted(self, inv: Invoice, day: int, text: str, channel: str = "whatsapp+email", action: str = "", decision=None) -> None:
        leaked = [m for m in self.PLACEHOLDER_MARKERS if m in text]
        if leaked:
            # Should be unreachable; recorded loudly rather than silently mailed to a customer.
            self.audit.record("message_blocked_unfilled_placeholder", day=day, invoice=inv.id,
                              markers=leaked, text=text[:400])
            raise ValueError(f"refusing to send a message containing {leaked}")
        inv.contact_count += 1
        inv.contact_days.append(day)
        inv.next_action_day = day + self.policy.bounds.min_gap_days_between_contacts
        inv.log(day, "outbound", text, channel=channel)
        self.audit.record("message_sent", day=day, invoice=inv.id, channel=channel, text=text)
        if self.on_message:
            self.on_message(inv, day, text, channel, action, decision)

    PLACEHOLDER_MARKERS = ("{link}", "(link pending)", "{{link}}")

    def _fill(self, text: str | None, inv: Invoice, fallback: str) -> str:
        text = text or fallback
        if inv.payment_link_url:
            return text.replace("{link}", inv.payment_link_url)
        # No link and none obtainable: drop the sentence that promised one rather than ship a
        # placeholder. Callers that need a link create it before composing.
        cleaned = re.sub(r"[^.!?\n]*\{link\}[^.!?\n]*[.!?]?", "", text)
        return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    # ---- handlers -----------------------------------------------------------------------
    def _wait(self, d: Decision, p: dict, inv: Invoice, day: int) -> ExecResult:
        inv.next_action_day = day + int(p["days"])
        inv.log(day, "system", f"wait {p['days']}d — {d.rationale}")
        return ExecResult(True, Verdict(True, "ok", ""), f"wait until day {inv.next_action_day}")

    def _send_reminder(self, d: Decision, p: dict, inv: Invoice, day: int) -> ExecResult:
        debtor = self.debtors[inv.debtor_id]
        # A reminder has to be actionable. An LLM may reasonably pick a reminder before ever
        # creating a link, and "pay here: {link}" with nothing to fill in is a broken message,
        # so create one now rather than sending a placeholder.
        if not inv.payment_link_url:
            if self._new_link(inv, day, inv.outstanding_paise, False, 0, 7, "reminder") is None:
                return ExecResult(False, Verdict(True, "ok", ""), "gateway unavailable; deferred")
        text = self._fill(
            d.message,
            inv,
            f"Hello {debtor.name}, a gentle reminder that invoice {inv.id} for {rupees(inv.outstanding_paise)} "
            f"({inv.description}) is {inv.days_overdue(day)} days past due. Pay securely here: {{link}}. — {self.merchant}",
        )
        self._contacted(inv, day, text, action="send_reminder", decision=d)
        return ExecResult(True, Verdict(True, "ok", ""), "reminder sent", contacted=True)

    def _new_link(self, inv: Invoice, day: int, amount: int, accept_partial: bool, min_partial: int, expire_days: int, note: str) -> dict | None:
        debtor = self.debtors[inv.debtor_id]
        if inv.payment_link_id:
            try:
                self.rzp.cancel(inv.payment_link_id)
                self.audit.record("razorpay_call", day=day, invoice=inv.id, op="payment_link.cancel", id=inv.payment_link_id)
            except Exception as e:  # already paid/cancelled — nothing to do
                self.audit.record("razorpay_call", day=day, invoice=inv.id, op="payment_link.cancel", id=inv.payment_link_id, error=str(e))
        payload = {
            "amount": amount,
            "currency": "INR",
            "accept_partial": accept_partial,
            "first_min_partial_amount": min_partial if accept_partial else 0,
            "expire_by": int((datetime.now().timestamp()) + expire_days * 86400),
            # Razorpay rejects duplicate reference ids. The product engine always passes day 0,
            # so "invoice-day" collides the moment an invoice gets a second link (a plan after a
            # reminder, a settlement link). notes.invoice_id carries the exact mapping; this only
            # needs to be unique and searchable.
            "reference_id": f"{inv.id}-{uuid.uuid4().hex[:8]}"[:40],
            "description": f"{self.merchant}: {inv.description} ({inv.id})",
            "customer": {"name": debtor.name, "contact": debtor.contact, "email": debtor.email},
            "notify": {"sms": False, "email": False},  # Baaki composes its own messages
            "reminder_enable": False,
            "notes": {"invoice_id": inv.id, "debtor_id": debtor.id, "purpose": note},
        }
        for attempt in range(3):
            try:
                link = self.rzp.create(payload)
                break
            except RazorpayUnavailable as e:
                self.gateway_failures += 1
                self.audit.record("razorpay_call", day=day, invoice=inv.id, op="payment_link.create", attempt=attempt + 1, error=str(e))
                link = None
                if getattr(self.rzp, "mode", "") != "simulated":
                    import time as _time

                    _time.sleep(1.5 * (attempt + 1))   # rate limits clear in seconds, not instantly
        if link is None:
            inv.log(day, "system", "Razorpay unavailable after 3 attempts; deferring one day (no contact made)")
            inv.next_action_day = day + 1
            return None
        self.audit.record("razorpay_call", day=day, invoice=inv.id, op="payment_link.create", id=link["id"], amount=amount, accept_partial=accept_partial, short_url=link["short_url"])
        inv.payment_link_id = link["id"]
        inv.payment_link_url = link["short_url"]
        self.link_index[link["id"]] = inv.id
        return link

    def _create_link(self, d: Decision, p: dict, inv: Invoice, day: int) -> ExecResult:
        debtor = self.debtors[inv.debtor_id]
        link = self._new_link(inv, day, int(p["amount_paise"]), bool(p["accept_partial"]), int(p.get("first_min_partial_paise", 0)), int(p["expire_days"]), "recovery")
        if link is None:
            return ExecResult(False, Verdict(True, "ok", ""), "gateway unavailable; deferred")
        partial_note = f" You can also pay in parts (minimum {rupees(int(p.get('first_min_partial_paise', 0)))} now)." if p["accept_partial"] else ""
        text = self._fill(
            d.message,
            inv,
            f"Hello {debtor.name}, invoice {inv.id} for {rupees(inv.outstanding_paise)} ({inv.description}) was due "
            f"{inv.days_overdue(day)} days ago. You can pay securely via UPI/card here: {{link}}.{partial_note} "
            f"Reply if anything is wrong with the invoice. — {self.merchant}",
        )
        self._contacted(inv, day, text, action="create_payment_link", decision=d)
        return ExecResult(True, Verdict(True, "ok", ""), f"link {link['id']} created ({link['short_url']})", contacted=True)

    def _offer_plan(self, d: Decision, p: dict, inv: Invoice, day: int) -> ExecResult:
        debtor = self.debtors[inv.debtor_id]
        n, interval, first = int(p["installments"]), int(p["interval_days"]), int(p["first_amount_paise"])
        link = self._new_link(inv, day, inv.outstanding_paise, True, first, min(interval, self.policy.bounds.max_payment_link_expiry_days), "installment_plan")
        if link is None:
            return ExecResult(False, Verdict(True, "ok", ""), "gateway unavailable; deferred")
        inv.plan = InstallmentPlan(total_paise=inv.outstanding_paise, installments=n, first_amount_paise=first, interval_days=interval)
        text = self._fill(
            d.message,
            inv,
            f"Hello {debtor.name}, we understand cash flow can be tight. For invoice {inv.id} ({rupees(inv.outstanding_paise)}) we can accept "
            f"{n} installments: {rupees(first)} now and the balance over the next {(n - 1) * interval} days. First installment: {{link}}. — {self.merchant}",
        )
        self._contacted(inv, day, text, action="offer_installment_plan", decision=d)
        return ExecResult(True, Verdict(True, "ok", ""), f"plan offered: {n} x every {interval}d, first {rupees(first)}", contacted=True)

    def _offer_discount(self, d: Decision, p: dict, inv: Invoice, day: int) -> ExecResult:
        debtor = self.debtors[inv.debtor_id]
        pct = float(p["discount_pct"])
        settle = int(inv.outstanding_paise * (100 - pct) / 100)
        link = self._new_link(inv, day, settle, False, 0, 5, "early_settlement")
        if link is None:
            return ExecResult(False, Verdict(True, "ok", ""), "gateway unavailable; deferred")
        inv.discount_pct_offered = pct
        inv.settlement_amount_paise = settle
        text = self._fill(
            d.message,
            inv,
            f"Hello {debtor.name}, to close invoice {inv.id} quickly we can offer a {pct:g}% early-settlement discount if paid within 5 days: "
            f"{rupees(settle)} instead of {rupees(inv.outstanding_paise)}. Pay here: {{link}}. — {self.merchant}",
        )
        self._contacted(inv, day, text, action="offer_early_settlement_discount", decision=d)
        return ExecResult(True, Verdict(True, "ok", ""), f"{pct:g}% settlement offered ({rupees(settle)})", contacted=True)

    def _escalate(self, d: Decision, p: dict, inv: Invoice, day: int) -> ExecResult:
        inv.status = InvoiceStatus.ESCALATED
        inv.escalation_reason = p.get("reason") or d.rationale or "needs human review"
        inv.next_action_day = 10**6
        inv.log(day, "system", f"escalated to human: {inv.escalation_reason}")
        self.audit.record("escalation", day=day, invoice=inv.id, reason=inv.escalation_reason, outstanding_paise=inv.outstanding_paise)
        return ExecResult(True, Verdict(True, "ok", ""), f"escalated: {inv.escalation_reason}")

    def _pause(self, d: Decision, p: dict, inv: Invoice, day: int) -> ExecResult:
        inv.status = InvoiceStatus.STOPPED
        inv.stop_reason = p.get("reason") or d.rationale or "stopped by policy"
        inv.next_action_day = 10**6
        inv.log(day, "system", f"automated contact stopped: {inv.stop_reason}")
        self.audit.record("stopped", day=day, invoice=inv.id, reason=inv.stop_reason, outstanding_paise=inv.outstanding_paise)
        return ExecResult(True, Verdict(True, "ok", ""), f"stopped: {inv.stop_reason}")
