"""Core domain types. Money is always integer paise (₹1 = 100 paise), like Razorpay."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
SIM_EPOCH = datetime(2026, 9, 1, 10, 0, tzinfo=IST)


def rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    whole, frac = divmod(p, 100)
    # Indian digit grouping: 12,34,567
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{sign}₹{s}.{frac:02d}"


class Archetype(StrEnum):
    """Hidden debtor behaviour used ONLY by the simulator. The agent never sees this."""

    PROMPT = "prompt"
    FORGETFUL = "forgetful"
    CASH_STRAPPED = "cash_strapped"
    DISPUTER = "disputer"
    GHOST = "ghost"
    INSOLVENT = "insolvent"


class InvoiceStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    DISPUTED = "disputed"
    ESCALATED = "escalated"
    STOPPED = "stopped"  # agent has stopped all automated contact; a human owns it


class ActionType(StrEnum):
    WAIT = "wait"
    SEND_REMINDER = "send_reminder"
    CREATE_PAYMENT_LINK = "create_payment_link"
    OFFER_INSTALLMENT_PLAN = "offer_installment_plan"
    OFFER_EARLY_SETTLEMENT_DISCOUNT = "offer_early_settlement_discount"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    PAUSE_CONTACT = "pause_contact"


MONEY_ACTIONS = {
    ActionType.CREATE_PAYMENT_LINK,
    ActionType.OFFER_INSTALLMENT_PLAN,
    ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT,
}
CONTACT_ACTIONS = MONEY_ACTIONS | {ActionType.SEND_REMINDER}


class Intent(StrEnum):
    """What an inbound debtor message means."""

    NONE = "none"
    WILL_PAY = "will_pay"
    PROMISE_TO_PAY = "promise_to_pay"
    PARTIAL_OFFER = "partial_offer"
    HARDSHIP = "hardship"
    DISPUTE = "dispute"
    CEASE_CONTACT = "cease_contact"
    UNCLEAR = "unclear"


@dataclass
class Debtor:
    id: str
    name: str
    email: str
    contact: str
    city: str
    # Visible history the agent may reason over.
    prior_invoices: int
    prior_late_count: int
    avg_days_late: float
    prior_partial_payments: int
    archetype: Archetype  # hidden: excluded from every agent-facing context

    def visible(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "city": self.city,
            "prior_invoices": self.prior_invoices,
            "prior_late_count": self.prior_late_count,
            "avg_days_late": self.avg_days_late,
            "prior_partial_payments": self.prior_partial_payments,
        }


@dataclass
class InstallmentPlan:
    total_paise: int
    installments: int
    first_amount_paise: int
    interval_days: int
    accepted: bool = False
    paid_installments: int = 0


@dataclass
class TimelineEvent:
    day: int
    kind: str  # outbound | inbound | payment | system
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Invoice:
    id: str
    debtor_id: str
    amount_paise: int
    issue_day: int  # simulation day (relative to SIM_EPOCH)
    due_day: int
    description: str
    status: InvoiceStatus = InvoiceStatus.OPEN
    amount_paid_paise: int = 0
    contact_count: int = 0
    contact_days: list[int] = field(default_factory=list)
    next_action_day: int = 0
    last_inbound: str | None = None
    last_inbound_intent: Intent = Intent.NONE
    last_inbound_day: int | None = None
    inbound_unprocessed: bool = False
    settlement_amount_paise: int | None = None
    cease_requested: bool = False
    dispute_open: bool = False
    hardship_flagged: bool = False
    promised_pay_day: int | None = None
    payment_link_id: str | None = None
    payment_link_url: str | None = None
    plan: InstallmentPlan | None = None
    discount_pct_offered: float = 0.0
    escalation_reason: str | None = None
    stop_reason: str | None = None
    risk_score: float | None = None
    timeline: list[TimelineEvent] = field(default_factory=list)
    payment_ids_seen: set[str] = field(default_factory=set)

    @property
    def outstanding_paise(self) -> int:
        return max(0, self.amount_paise - self.amount_paid_paise)

    def days_overdue(self, day: int) -> int:
        return max(0, day - self.due_day)

    @property
    def is_terminal(self) -> bool:
        return self.status in (InvoiceStatus.PAID, InvoiceStatus.STOPPED)

    @property
    def contact_allowed_state(self) -> bool:
        return self.status in (InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID)

    def log(self, day: int, kind: str, summary: str, **data: Any) -> None:
        self.timeline.append(TimelineEvent(day=day, kind=kind, summary=summary, data=data))

    def visible(self, day: int) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "amount": rupees(self.amount_paise),
            "amount_paise": self.amount_paise,
            "paid_paise": self.amount_paid_paise,
            "outstanding": rupees(self.outstanding_paise),
            "outstanding_paise": self.outstanding_paise,
            "days_overdue": self.days_overdue(day),
            "status": self.status.value,
            "contact_count": self.contact_count,
            "days_since_last_contact": (day - self.contact_days[-1]) if self.contact_days else None,
            "last_inbound_message": self.last_inbound,
            "last_inbound_day": self.last_inbound_day,
            "promised_pay_day": self.promised_pay_day,
            "has_payment_link": self.payment_link_id is not None,
            "plan": None
            if not self.plan
            else {
                "installments": self.plan.installments,
                "first_amount": rupees(self.plan.first_amount_paise),
                "accepted": self.plan.accepted,
                "paid_installments": self.plan.paid_installments,
            },
            "discount_pct_offered": self.discount_pct_offered,
            "risk_score": self.risk_score,
        }


@dataclass
class Decision:
    """What a brain wants to do next. Always re-validated by the policy before execution."""

    action: ActionType
    params: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    rationale: str = ""
    reply_intent: Intent = Intent.NONE
    source: str = "rules"  # rules | claude | claude->rules (fallback)


def sim_datetime(day: int, hour: int = 10, minute: int = 0) -> datetime:
    return (SIM_EPOCH + timedelta(days=day)).replace(hour=hour, minute=minute)
