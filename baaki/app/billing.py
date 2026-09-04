"""Subscription billing, on Razorpay Subscriptions.

Baaki charges merchants through the same rails it helps them collect on. Without platform
credentials this runs in sandbox mode: plans and subscriptions are created locally so the flow
is demonstrable end to end, and the code path is identical when keys are present.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from sqlmodel import Session as DBSession

from .models import Org, Plan, PLAN_PRICE_PAISE, SubscriptionStatus, utcnow

TRIAL_DAYS = 14


@dataclass(frozen=True)
class PlanInfo:
    plan: Plan
    name: str
    price_paise: int
    invoice_limit: int
    blurb: str
    features: tuple[str, ...]


CATALOGUE: tuple[PlanInfo, ...] = (
    PlanInfo(Plan.STARTER, "Starter", PLAN_PRICE_PAISE[Plan.STARTER], 100,
             "For a single-owner business chasing a few dozen invoices.",
             ("100 open invoices", "Email reminders + Razorpay Payment Links", "Policy guardrails & audit trail", "1 user")),
    PlanInfo(Plan.GROWTH, "Growth", PLAN_PRICE_PAISE[Plan.GROWTH], 1000,
             "For a finance team that wants the agent to run unattended.",
             ("1,000 open invoices", "LLM-drafted messages (OpenAI or Claude)", "Auto-send without per-message approval", "5 users", "Priority support")),
    PlanInfo(Plan.SCALE, "Scale", PLAN_PRICE_PAISE[Plan.SCALE], 10**9,
             "For distributors and NBFC-adjacent books.",
             ("Unlimited invoices", "Custom policy bounds & approval workflows", "SSO and audit export", "Unlimited users", "Dedicated onboarding")),
)


def plan_info(plan: Plan) -> PlanInfo | None:
    return next((p for p in CATALOGUE if p.plan == plan), None)


class BillingUnavailable(RuntimeError):
    pass


def _platform_client():
    key, secret = os.environ.get("BAAKI_RZP_KEY_ID"), os.environ.get("BAAKI_RZP_KEY_SECRET")
    if not (key and secret):
        return None
    import razorpay

    c = razorpay.Client(auth=(key, secret))
    c.enable_retry(True)
    return c


def start_trial(org: Org) -> None:
    from datetime import date, timedelta

    org.plan = Plan.TRIAL
    org.subscription_status = SubscriptionStatus.TRIALING
    org.trial_ends_on = date.today() + timedelta(days=TRIAL_DAYS)


def create_subscription(db: DBSession, org: Org, plan: Plan) -> dict:
    """Returns a dict the checkout page renders. Sandbox mode short-circuits to an active sub."""
    info = plan_info(plan)
    if not info:
        raise BillingUnavailable("Unknown plan.")
    client = _platform_client()
    if client is None:
        org.plan, org.subscription_status = plan, SubscriptionStatus.ACTIVE
        org.rzp_subscription_id = f"sub_sandbox_{int(time.time())}"
        db.add(org)
        db.commit()
        return {"mode": "sandbox", "subscription_id": org.rzp_subscription_id, "plan": plan.value,
                "message": "Sandbox billing: subscription activated locally. Set BAAKI_RZP_KEY_ID/SECRET for live checkout."}

    plan_id = os.environ.get(f"BAAKI_RZP_PLAN_{plan.value.upper()}")
    if not plan_id:
        raise BillingUnavailable(f"No Razorpay plan id configured for {plan.value} (set BAAKI_RZP_PLAN_{plan.value.upper()}).")
    sub = client.subscription.create({
        "plan_id": plan_id,
        "total_count": 12,
        "customer_notify": 1,
        "notes": {"org_id": str(org.id), "org_slug": org.slug},
    })
    org.rzp_subscription_id = sub["id"]
    org.subscription_status = SubscriptionStatus.PAST_DUE  # not active until the first charge
    db.add(org)
    db.commit()
    return {"mode": "live", "subscription_id": sub["id"], "short_url": sub.get("short_url"), "plan": plan.value}


def apply_subscription_event(db: DBSession, org: Org, event: str, payload: dict) -> str:
    """Maps Razorpay subscription webhooks onto entitlement."""
    mapping = {
        "subscription.activated": SubscriptionStatus.ACTIVE,
        "subscription.charged": SubscriptionStatus.ACTIVE,
        "subscription.pending": SubscriptionStatus.PAST_DUE,
        "subscription.halted": SubscriptionStatus.PAST_DUE,
        "subscription.cancelled": SubscriptionStatus.CANCELLED,
        "subscription.completed": SubscriptionStatus.CANCELLED,
    }
    if event not in mapping:
        return "ignored"
    org.subscription_status = mapping[event]
    if org.subscription_status == SubscriptionStatus.CANCELLED:
        org.agent_enabled = False  # stop acting on a merchant's behalf once they stop paying
    db.add(org)
    db.commit()
    return org.subscription_status.value


def entitlement_problem(org: Org) -> str | None:
    """Why the agent may not run. Returned to the UI verbatim."""
    from datetime import date

    if org.subscription_status == SubscriptionStatus.CANCELLED:
        return "Your subscription is cancelled. Reactivate on the Billing page to resume recovery."
    if org.subscription_status == SubscriptionStatus.PAST_DUE:
        return "Your last payment did not go through. Update billing to resume recovery."
    if org.plan == Plan.TRIAL and org.trial_ends_on and org.trial_ends_on < date.today():
        return f"Your free trial ended on {org.trial_ends_on:%d %b %Y}. Choose a plan to resume recovery."
    return None
