"""The guardrails. Every money or contact action passes through here, no matter which brain proposed it.

Rules encode the merchant's commercial bounds plus India-specific collection conduct norms
(RBI Fair Practices Code: no contact outside 08:00-19:00, no threats, no third-party disclosure,
respect cease requests).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .domain import ActionType, Invoice, InvoiceStatus, CONTACT_ACTIONS, MONEY_ACTIONS, IST


@dataclass(frozen=True)
class PolicyBounds:
    contact_window_start_hour: int = 8
    contact_window_end_hour: int = 19
    min_gap_days_between_contacts: int = 3
    max_contacts_per_invoice: int = 6
    max_early_settlement_discount_pct: float = 5.0
    min_days_overdue_for_discount: int = 21
    max_installments: int = 3
    max_plan_interval_days: int = 30
    min_first_installment_pct: float = 25.0
    max_payment_link_expiry_days: int = 14
    min_partial_payment_pct: float = 20.0
    escalate_on_dispute: bool = True
    escalate_on_hardship: bool = True
    forbidden_phrases: tuple[str, ...] = (
        "police",
        "arrest",
        "jail",
        "criminal",
        "fir ",
        "your family",
        "your employer",
        "your wife",
        "your husband",
        "your parents",
        "we will visit",
        "goons",
        "last warning",
        "blacklist",
        "defaulter list",
        "consequences",
    )


@dataclass
class Verdict:
    allowed: bool
    rule_id: str
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


_ALLOW = Verdict(True, "ok", "within policy")


class Policy:
    def __init__(self, bounds: PolicyBounds | None = None):
        self.bounds = bounds or PolicyBounds()

    # ---- contact-time ------------------------------------------------------------------
    def contact_time_ok(self, when: datetime) -> Verdict:
        local = when.astimezone(IST)
        b = self.bounds
        if local.hour < b.contact_window_start_hour or local.hour >= b.contact_window_end_hour:
            return Verdict(
                False,
                "P-TIME-01",
                f"contact at {local.strftime('%H:%M')} IST is outside the {b.contact_window_start_hour:02d}:00-"
                f"{b.contact_window_end_hour:02d}:00 window (RBI FPC)",
            )
        return _ALLOW

    def next_allowed_time(self, when: datetime) -> datetime:
        local = when.astimezone(IST)
        b = self.bounds
        if local.hour < b.contact_window_start_hour:
            return local.replace(hour=b.contact_window_start_hour, minute=0, second=0)
        if local.hour >= b.contact_window_end_hour:
            nxt = local.replace(hour=b.contact_window_start_hour, minute=0, second=0)
            from datetime import timedelta

            return nxt + timedelta(days=1)
        return local

    # ---- message content ----------------------------------------------------------------
    def message_ok(self, text: str | None) -> Verdict:
        if not text:
            return _ALLOW
        low = " " + text.lower() + " "
        for phrase in self.bounds.forbidden_phrases:
            if phrase in low:
                return Verdict(False, "P-MSG-01", f"message contains prohibited phrase '{phrase.strip()}'")
        if re.search(r"\b(within|in)\s+\d+\s*(hours?|hrs?)\b.*\b(legal|court|action)\b", low):
            return Verdict(False, "P-MSG-02", "message contains a coercive legal ultimatum")
        return _ALLOW

    # ---- stopping rules ---------------------------------------------------------------------
    def stop_reason(self, inv: Invoice) -> str | None:
        """Returns a reason if the agent must stop automated contact for this invoice."""
        if inv.status == InvoiceStatus.PAID:
            return "invoice fully paid"
        if inv.cease_requested:
            return "debtor asked us to stop contacting them (cease request honoured)"
        if inv.dispute_open and self.bounds.escalate_on_dispute:
            return "dispute raised — automated recovery must not continue until a human resolves it"
        if inv.hardship_flagged and self.bounds.escalate_on_hardship and inv.plan is None:
            return "debtor reported financial hardship — needs a human decision"
        if inv.contact_count >= self.bounds.max_contacts_per_invoice:
            return f"reached max {self.bounds.max_contacts_per_invoice} automated contacts without resolution"
        return None

    # ---- the gate -----------------------------------------------------------------------------
    def evaluate(self, action: ActionType, params: dict, inv: Invoice, day: int, when: datetime, message: str | None) -> Verdict:
        b = self.bounds

        if action in (ActionType.WAIT, ActionType.ESCALATE_TO_HUMAN, ActionType.PAUSE_CONTACT):
            return _ALLOW

        if action in CONTACT_ACTIONS:
            if not inv.contact_allowed_state:
                return Verdict(False, "P-STATE-01", f"invoice is {inv.status.value}; automated contact is not permitted")
            if inv.cease_requested:
                return Verdict(False, "P-CEASE-01", "debtor requested no further contact")
            if inv.dispute_open:
                return Verdict(False, "P-DISPUTE-01", "dispute open; contact only via human")
            if inv.contact_count >= b.max_contacts_per_invoice:
                return Verdict(False, "P-CAP-01", f"contact cap of {b.max_contacts_per_invoice} reached")
            if inv.contact_days and day - inv.contact_days[-1] < b.min_gap_days_between_contacts:
                return Verdict(
                    False, "P-GAP-01", f"last contact was {day - inv.contact_days[-1]}d ago; minimum gap is {b.min_gap_days_between_contacts}d"
                )
            t = self.contact_time_ok(when)
            if not t.allowed:
                return t
            m = self.message_ok(message)
            if not m.allowed:
                return m

        if action == ActionType.CREATE_PAYMENT_LINK:
            amount = int(params.get("amount_paise", inv.outstanding_paise))
            if amount <= 0 or amount > inv.outstanding_paise:
                return Verdict(False, "P-LINK-01", f"link amount {amount} must be within (0, outstanding={inv.outstanding_paise}]")
            expiry = int(params.get("expire_days", 7))
            if expiry < 1 or expiry > b.max_payment_link_expiry_days:
                return Verdict(False, "P-LINK-02", f"link expiry {expiry}d outside 1..{b.max_payment_link_expiry_days}d")
            if params.get("accept_partial"):
                min_partial = int(params.get("first_min_partial_paise", 0))
                floor = int(inv.outstanding_paise * b.min_partial_payment_pct / 100)
                if min_partial < floor:
                    return Verdict(False, "P-LINK-03", f"minimum partial {min_partial} below policy floor {floor} ({b.min_partial_payment_pct}% of outstanding)")

        if action == ActionType.OFFER_INSTALLMENT_PLAN:
            n = int(params.get("installments", 0))
            if n < 2 or n > b.max_installments:
                return Verdict(False, "P-PLAN-01", f"{n} installments outside 2..{b.max_installments}")
            interval = int(params.get("interval_days", 30))
            if interval < 7 or interval > b.max_plan_interval_days:
                return Verdict(False, "P-PLAN-02", f"interval {interval}d outside 7..{b.max_plan_interval_days}d")
            first = int(params.get("first_amount_paise", 0))
            floor = int(inv.outstanding_paise * b.min_first_installment_pct / 100)
            if first < floor:
                return Verdict(False, "P-PLAN-03", f"first installment {first} below {b.min_first_installment_pct}% floor ({floor})")
            if inv.plan is not None and inv.plan.accepted:
                return Verdict(False, "P-PLAN-04", "an accepted plan already exists")

        if action == ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT:
            pct = float(params.get("discount_pct", 0))
            if pct <= 0 or pct > b.max_early_settlement_discount_pct:
                return Verdict(False, "P-DISC-01", f"discount {pct}% exceeds cap of {b.max_early_settlement_discount_pct}%")
            if inv.days_overdue(day) < b.min_days_overdue_for_discount:
                return Verdict(False, "P-DISC-02", f"discounts only after {b.min_days_overdue_for_discount}d overdue (now {inv.days_overdue(day)}d)")
            if inv.discount_pct_offered > 0:
                return Verdict(False, "P-DISC-03", "a discount has already been offered; no stacking")

        return _ALLOW

    def allowed_actions(self, inv: Invoice, day: int, when: datetime) -> dict[str, str]:
        """Pre-filter shown to the brain: action -> 'allowed' or the reason it isn't."""
        out: dict[str, str] = {}
        probes = {
            ActionType.WAIT: {},
            ActionType.SEND_REMINDER: {},
            ActionType.CREATE_PAYMENT_LINK: {"amount_paise": inv.outstanding_paise, "expire_days": 7},
            ActionType.OFFER_INSTALLMENT_PLAN: {
                "installments": 2,
                "interval_days": 30,
                "first_amount_paise": int(inv.outstanding_paise * self.bounds.min_first_installment_pct / 100) + 1,
            },
            ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT: {"discount_pct": self.bounds.max_early_settlement_discount_pct},
            ActionType.ESCALATE_TO_HUMAN: {},
            ActionType.PAUSE_CONTACT: {},
        }
        for action, params in probes.items():
            v = self.evaluate(action, params, inv, day, when, None)
            out[action.value] = "allowed" if v.allowed else f"blocked: {v.reason} [{v.rule_id}]"
        return out

    def describe(self) -> dict:
        d = asdict(self.bounds)
        d["forbidden_phrases"] = list(self.bounds.forbidden_phrases)
        return d
