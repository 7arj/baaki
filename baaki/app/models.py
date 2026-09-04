"""Multi-tenant schema. Every business row carries `org_id`; nothing is queried without it.

SQLite by default so a merchant can self-host with zero infrastructure; the schema is plain
SQLAlchemy so `DATABASE_URL=postgresql://…` works unchanged.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(StrEnum):
    TRIAL = "trial"
    STARTER = "starter"      # ₹1,499/mo — up to 100 open invoices
    GROWTH = "growth"        # ₹4,999/mo — up to 1,000
    SCALE = "scale"          # ₹14,999/mo — unlimited


PLAN_LIMITS: dict[Plan, int] = {Plan.TRIAL: 25, Plan.STARTER: 100, Plan.GROWTH: 1000, Plan.SCALE: 10**9}
PLAN_PRICE_PAISE: dict[Plan, int] = {Plan.TRIAL: 0, Plan.STARTER: 149900, Plan.GROWTH: 499900, Plan.SCALE: 1499900}


class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"


class Role(StrEnum):
    OWNER = "owner"     # billing, team, credentials, guardrails
    MEMBER = "member"   # ledger, approvals, audit — everything except the four above

    @property
    def can_administer(self) -> bool:
        return self is Role.OWNER


class TokenPurpose(StrEnum):
    VERIFY_EMAIL = "verify_email"
    PASSWORD_RESET = "password_reset"
    INVITE = "invite"


class Org(SQLModel, table=True):
    """A merchant. The tenant boundary.

    No ORM relationships by design: every read states its `org_id` explicitly, so a missing
    tenancy filter is a visible omission at the call site rather than a silent lazy-load.
    """

    __tablename__ = "orgs"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)

    # Commercial identity used in outbound messages
    legal_name: str = ""
    reply_to_email: str = ""
    support_phone: str = ""

    # Billing
    plan: Plan = Field(default=Plan.TRIAL)
    subscription_status: SubscriptionStatus = Field(default=SubscriptionStatus.TRIALING)
    trial_ends_on: Optional[date] = None
    rzp_subscription_id: Optional[str] = None
    rzp_customer_id: Optional[str] = None

    # Razorpay credentials for *collecting* (test mode only unless explicitly allowed)
    rzp_key_id: Optional[str] = None
    rzp_key_secret_enc: Optional[str] = None
    rzp_webhook_secret_enc: Optional[str] = None

    # Agent control
    agent_enabled: bool = False
    approval_required: bool = True   # human approves each outbound until the merchant trusts it
    llm_provider: str = "rules"      # rules | openai | claude

    @property
    def invoice_limit(self) -> int:
        return PLAN_LIMITS[self.plan]


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    email: str = Field(index=True)
    name: str = ""
    password_hash: str
    role: Role = Field(default=Role.OWNER)
    email_verified_at: Optional[datetime] = None
    disabled: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: Optional[datetime] = None


class Session(SQLModel, table=True):
    """Server-side sessions: revocable, unlike a stateless token."""

    __tablename__ = "sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    revoked: bool = False
    user_agent: str = ""
    ip: str = ""


class PolicySettings(SQLModel, table=True):
    """Per-org guardrails. Mirrors PolicyBounds; editable in the UI, versioned by updated_at."""

    __tablename__ = "policy_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True, unique=True)
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
    updated_at: datetime = Field(default_factory=utcnow)
    updated_by: Optional[int] = Field(default=None, foreign_key="users.id")


class Customer(SQLModel, table=True):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("org_id", "external_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    # Nullable, not "": the unique constraint below pairs it with org_id, and SQL treats NULLs
    # as distinct but empty strings as equal — so a default of "" would let only one customer
    # per org lack a code.
    external_id: Optional[str] = None
    name: str
    email: str = ""
    phone: str = ""
    city: str = ""
    do_not_contact: bool = False   # a cease request is permanent and customer-wide
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class InvoiceRow(SQLModel, table=True):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("org_id", "number"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    customer_id: int = Field(foreign_key="customers.id", index=True)
    number: str                      # merchant's invoice number
    description: str = ""
    amount_paise: int
    amount_paid_paise: int = 0
    issued_on: date
    due_on: date = Field(index=True)
    currency: str = "INR"

    status: str = Field(default="open", index=True)   # mirrors domain.InvoiceStatus
    contact_count: int = 0
    last_contact_on: Optional[date] = None
    next_action_on: Optional[date] = Field(default=None, index=True)
    promised_pay_on: Optional[date] = None

    inbound_pending: bool = False        # an unprocessed customer reply is waiting
    last_inbound_text: str = ""
    dispute_open: bool = False
    hardship_flagged: bool = False
    cease_requested: bool = False
    escalation_reason: Optional[str] = None
    stop_reason: Optional[str] = None

    risk_score: Optional[float] = None
    discount_pct_offered: float = 0.0
    settlement_amount_paise: Optional[int] = None
    payment_link_id: Optional[str] = None
    payment_link_url: Optional[str] = None
    plan_json: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def outstanding_paise(self) -> int:
        return max(0, self.amount_paise - self.amount_paid_paise)


class Event(SQLModel, table=True):
    """The invoice timeline a human reads: outbound, inbound, payment, system."""

    __tablename__ = "events"

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    invoice_id: int = Field(foreign_key="invoices.id", index=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    kind: str                      # outbound | inbound | payment | system
    summary: str
    channel: str = ""
    payload_json: str = "{}"


class OutboxStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    REJECTED = "rejected"


class Outbox(SQLModel, table=True):
    """Every outbound message. When approval_required is on, a human releases these.

    Also the delivery retry queue — a transport failure never loses the message.
    """

    __tablename__ = "outbox"

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    invoice_id: int = Field(foreign_key="invoices.id", index=True)
    channel: str = "email"
    to_address: str = ""
    subject: str = ""
    body: str
    status: OutboxStatus = Field(default=OutboxStatus.QUEUED, index=True)
    action: str = ""               # the ActionType that produced it
    rationale: str = ""
    decided_by: str = "rules"      # rules | openai | claude | openai->rules …
    attempts: int = 0
    last_error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    approved_by: Optional[int] = Field(default=None, foreign_key="users.id")
    sent_at: Optional[datetime] = None


class AuditRow(SQLModel, table=True):
    """Hash-chained per org. Same guarantee as the file log, queryable."""

    __tablename__ = "audit"

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    seq: int = Field(index=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    event: str = Field(index=True)
    invoice_id: Optional[int] = Field(default=None, index=True)
    actor: str = "agent"           # agent | user:<id> | razorpay | system
    payload_json: str = "{}"
    prev: str
    hash: str


class PaymentRow(SQLModel, table=True):
    """Idempotency ledger: a Razorpay payment id is credited at most once per org."""

    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("org_id", "external_payment_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    invoice_id: int = Field(foreign_key="invoices.id", index=True)
    external_payment_id: str
    amount_paise: int
    source: str = "razorpay_link"
    received_at: datetime = Field(default_factory=utcnow)


class Token(SQLModel, table=True):
    """Single-use, expiring, hashed. Covers email verification, password reset and invites.

    Only the SHA-256 of the token is stored, so a database leak doesn't hand over live links.
    """

    __tablename__ = "tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    purpose: TokenPurpose = Field(index=True)
    org_id: Optional[int] = Field(default=None, foreign_key="orgs.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    email: str = ""                 # for invites, the address being invited
    role: Role = Field(default=Role.MEMBER)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    used_at: Optional[datetime] = None
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")


class LoginAttempt(SQLModel, table=True):
    """Failed sign-in attempts, for throttling. Kept in the database so limits hold across workers."""

    __tablename__ = "login_attempts"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True)    # "email:<addr>" or "ip:<addr>"
    at: datetime = Field(default_factory=utcnow, index=True)


class RiskModelRow(SQLModel, table=True):
    """A logistic model fitted on one org's own settled invoices, with its held-out metrics.

    Absent or stale rows mean the shipped prior is used and scores stay `None` until an org has
    enough of its own history — see `service.risk_score`.
    """

    __tablename__ = "risk_models"

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True)
    fitted_at: datetime = Field(default_factory=utcnow, index=True)
    weights_json: str
    train_rows: int
    holdout_rows: int
    positives: int
    precision: float
    recall: float
    f1: float
    base_rate: float
    threshold: float = 0.5
    active: bool = Field(default=True, index=True)


class RunLock(SQLModel, table=True):
    """Advisory lock so two workers can't run the same org concurrently and double-send."""

    __tablename__ = "run_locks"

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(foreign_key="orgs.id", index=True, unique=True)
    holder: str
    acquired_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
