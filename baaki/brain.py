"""Decision makers. Every brain sees the same context and returns the same `Decision` shape.

- RuleBrain      : deterministic playbook. Always available. The fallback.
- ClaudeBrain    : Claude reads the debtor's reply + history and picks the next bounded action
                   with a rationale and a drafted message (structured JSON output).
- OpenAIBrain    : the same contract against OpenAI. Identical prompt and schema; only the
                   transport differs, which is the point — the policy gate does not care.
- ResilientBrain : an LLM first; any API error, timeout, refusal or invalid output falls back
                   to RuleBrain and records that it did.
- NoneBrain / NaiveBrain : the two baselines we measure against.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .domain import ActionType, Debtor, Decision, Intent, Invoice, rupees
from .policy import Policy
from .tools import MERCHANT


@dataclass
class DecisionContext:
    day: int
    inv: Invoice
    debtor: Debtor
    allowed: dict[str, str]
    bounds: dict[str, Any]
    stop_reason: str | None

    def for_llm(self) -> dict[str, Any]:
        """Only visible fields. Never the archetype."""
        return {
            "today_day_index": self.day,
            "merchant": MERCHANT,
            "invoice": self.inv.visible(self.day),
            "debtor": self.debtor.visible(),
            "recent_timeline": [f"day {e.day} [{e.kind}] {e.summary}" for e in self.inv.timeline[-6:]],
            "new_inbound_message": self.inv.last_inbound if self.inv.inbound_unprocessed else None,
            "policy_stop_reason": self.stop_reason,
            "actions": self.allowed,
            "policy_bounds": self.bounds,
        }


# ---------------------------------------------------------------------------------------------
_INTENT_RULES: list[tuple[Intent, re.Pattern]] = [
    (Intent.CEASE_CONTACT, re.compile(r"stop (messaging|contacting)|do not contact|no more messages|lawyer|enough\.", re.I)),
    (Intent.DISPUTE, re.compile(r"wrong|short[- ]shipped|already paid|rejected|credit[- ]not|dispute|not paying till", re.I)),
    (Intent.HARDSHIP, re.compile(r"shut|no funds|flooded|winding up|cannot pay|closed down", re.I)),
    (Intent.PARTIAL_OFFER, re.compile(r"half|split|\d+%|balance|in parts|instal", re.I)),
    (Intent.PROMISE_TO_PAY, re.compile(r"will (pay|clear)|next week|by then|give us \d+ days|after gst", re.I)),
    (Intent.WILL_PAY, re.compile(r"paying today|clearing it now|done from our side|noted", re.I)),
]


def classify_intent(text: str | None) -> Intent:
    if not text:
        return Intent.NONE
    for intent, pat in _INTENT_RULES:
        if pat.search(text):
            return intent
    return Intent.UNCLEAR


def _ok(allowed: dict[str, str], a: ActionType) -> bool:
    return allowed.get(a.value) == "allowed"


class RuleBrain:
    name = "rules"

    def decide(self, ctx: DecisionContext) -> Decision:
        inv, d, day, allowed = ctx.inv, ctx.debtor, ctx.day, ctx.allowed
        intent = classify_intent(inv.last_inbound) if inv.inbound_unprocessed else Intent.NONE
        out = inv.outstanding_paise

        if intent == Intent.CEASE_CONTACT:
            return Decision(ActionType.PAUSE_CONTACT, {"reason": "debtor requested no contact"}, rationale="cease request; stop automated contact and hand to human", reply_intent=intent)
        if intent == Intent.DISPUTE:
            return Decision(ActionType.ESCALATE_TO_HUMAN, {"reason": f"dispute raised: '{inv.last_inbound}'"}, rationale="dispute must be resolved by a human before any further recovery", reply_intent=intent)
        if intent == Intent.HARDSHIP:
            if _ok(allowed, ActionType.OFFER_INSTALLMENT_PLAN) and d.prior_partial_payments == 0:
                return Decision(ActionType.OFFER_INSTALLMENT_PLAN, {"installments": 3, "interval_days": 30}, rationale="hardship reported; offer a bounded 3-part plan before escalating", reply_intent=intent)
            return Decision(ActionType.ESCALATE_TO_HUMAN, {"reason": f"financial hardship reported: '{inv.last_inbound}'"}, rationale="hardship needs a human decision (write-off / legal / restructure)", reply_intent=intent)
        if intent == Intent.PROMISE_TO_PAY:
            return Decision(ActionType.WAIT, {"days": 7}, rationale="debtor promised to pay; wait 7 days before following up", reply_intent=intent)
        if intent == Intent.WILL_PAY:
            return Decision(ActionType.WAIT, {"days": 4}, rationale="debtor says payment is in progress; give it 4 days", reply_intent=intent)
        if intent == Intent.PARTIAL_OFFER:
            if _ok(allowed, ActionType.OFFER_INSTALLMENT_PLAN):
                return Decision(ActionType.OFFER_INSTALLMENT_PLAN, {"installments": 2, "interval_days": 21, "first_amount_paise": out // 2}, rationale="debtor asked to split; offer 2 installments, 50% now", reply_intent=intent)
            if _ok(allowed, ActionType.CREATE_PAYMENT_LINK):
                return Decision(ActionType.CREATE_PAYMENT_LINK, {"accept_partial": True}, rationale="debtor asked to split; allow partial payment on the link", reply_intent=intent)
        if intent == Intent.UNCLEAR:
            if _ok(allowed, ActionType.SEND_REMINDER):
                return Decision(ActionType.SEND_REMINDER, rationale="unclear reply; restate invoice details and link", reply_intent=intent)

        if inv.promised_pay_day and day < inv.promised_pay_day:
            return Decision(ActionType.WAIT, {"days": inv.promised_pay_day - day}, rationale="waiting for promised payment date")

        if ctx.stop_reason:
            if inv.dispute_open or inv.hardship_flagged:
                return Decision(ActionType.ESCALATE_TO_HUMAN, {"reason": ctx.stop_reason}, rationale=ctx.stop_reason)
            return Decision(ActionType.PAUSE_CONTACT, {"reason": ctx.stop_reason}, rationale=ctx.stop_reason)

        n = inv.contact_count
        high_risk = (inv.risk_score or 0) >= 0.5
        if n == 0 and _ok(allowed, ActionType.CREATE_PAYMENT_LINK):
            return Decision(ActionType.CREATE_PAYMENT_LINK, {"accept_partial": high_risk, "expire_days": 7}, rationale=f"first contact: friendly reminder with a payment link{' (partial allowed: high risk)' if high_risk else ''}")
        if n == 1 and _ok(allowed, ActionType.SEND_REMINDER):
            return Decision(ActionType.SEND_REMINDER, rationale="second touch: firmer factual reminder, same link")
        if n == 2:
            if _ok(allowed, ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT) and out >= 50_000_00:
                return Decision(ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT, {"discount_pct": 3.0}, rationale="large, long-overdue invoice with no response: 3% early-settlement nudge")
            if _ok(allowed, ActionType.OFFER_INSTALLMENT_PLAN):
                return Decision(ActionType.OFFER_INSTALLMENT_PLAN, {"installments": 2, "interval_days": 30}, rationale="no response to two reminders; lower the barrier with a 2-part plan")
        if n == 3 and _ok(allowed, ActionType.SEND_REMINDER):
            return Decision(ActionType.SEND_REMINDER, rationale="final automated reminder before handing over")
        if n >= 4:
            return Decision(ActionType.PAUSE_CONTACT, {"reason": f"no resolution after {n} automated contacts; recommend a human call or write-off review"}, rationale="stopping rule: automated recovery exhausted")
        if _ok(allowed, ActionType.SEND_REMINDER):
            return Decision(ActionType.SEND_REMINDER, rationale="default follow-up")
        return Decision(ActionType.WAIT, {"days": 1}, rationale="nothing permitted today; re-check tomorrow")


class NoneBrain:
    name = "none"

    def decide(self, ctx: DecisionContext) -> Decision:
        return Decision(ActionType.WAIT, {"days": 10**6}, rationale="baseline: do nothing", source="none")


class NaiveBrain:
    """What most SMEs actually do: nag every few days, ignore replies, never stop."""

    name = "naive"

    def decide(self, ctx: DecisionContext) -> Decision:
        inv, d = ctx.inv, ctx.debtor
        return Decision(
            ActionType.SEND_REMINDER,
            message=f"Dear {d.name}, payment of {rupees(inv.outstanding_paise)} for invoice {inv.id} is overdue. Kindly pay immediately. — {MERCHANT}",
            rationale="baseline: reminder every 3 days regardless of replies",
            reply_intent=classify_intent(inv.last_inbound) if inv.inbound_unprocessed else Intent.NONE,
            source="naive",
        )


# ---------------------------------------------------------------------------------------------
SYSTEM_PROMPT = f"""You are the receivables-recovery agent for {MERCHANT}, an Indian SME that sells to other businesses.
Your job: recover overdue B2B invoices while keeping the customer relationship and staying inside policy.

You choose ONE next action for one invoice and, if it involves contacting the debtor, draft the message.

Principles
- Be factual, courteous and brief. Indian business English; no threats, no legal ultimatums, no mention of police, family or employers.
- Never invent invoice facts. Use only the numbers given.
- Prefer the cheapest action that plausibly gets paid: reminder with a link → partial/installments → small early-settlement discount → escalate.
- If the debtor disputes the invoice, escalate_to_human. If they ask you to stop, pause_contact. If they report hardship, offer a plan if allowed, else escalate.
- If they promised to pay, wait until that date. Don't nag people who are mid-payment.
- Only pick actions marked "allowed". The policy engine will block anything else and you will have wasted a turn.
- Include the literal placeholder {{link}} in the message where the payment link should go; the system fills it in.
- classify the new inbound message (if any) into reply_intent.

Return JSON only, matching the schema."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply_intent": {"type": "string", "enum": [i.value for i in Intent]},
        "action": {"type": "string", "enum": [a.value for a in ActionType]},
        "params": {
            "type": "object",
            "properties": {
                "accept_partial": {"type": ["boolean", "null"]},
                "expire_days": {"type": ["integer", "null"]},
                "installments": {"type": ["integer", "null"]},
                "interval_days": {"type": ["integer", "null"]},
                "first_amount_paise": {"type": ["integer", "null"]},
                "discount_pct": {"type": ["number", "null"]},
                "days": {"type": ["integer", "null"]},
                "reason": {"type": ["string", "null"]},
            },
            "required": ["accept_partial", "expire_days", "installments", "interval_days", "first_amount_paise", "discount_pct", "days", "reason"],
            "additionalProperties": False,
        },
        "message": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["reply_intent", "action", "params", "message", "rationale"],
    "additionalProperties": False,
}


class SimulatedOutage(RuntimeError):
    """Injected by --demo-faults to black out the LLM for a day. Provider-neutral on purpose:
    the fallback path must not depend on which SDK's exception type was raised."""


class DecisionParams(BaseModel):
    """Mirror of OUTPUT_SCHEMA's params. OpenAI structured outputs require every field present,
    so optionals are explicit `| None` rather than omitted."""

    accept_partial: bool | None
    expire_days: int | None
    installments: int | None
    interval_days: int | None
    first_amount_paise: int | None
    discount_pct: float | None
    days: int | None
    reason: str | None


class DecisionOutput(BaseModel):
    reply_intent: Intent
    action: ActionType
    params: DecisionParams
    message: str | None
    rationale: str


class ClaudeBrain:
    name = "claude"

    provider = "anthropic"

    def __init__(self, model: str | None = None, effort: str = "low", outage_days: set[int] | None = None):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("BAAKI_MODEL", "claude-opus-5")
        self.effort = effort
        self.outage_days = outage_days or set()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0

    def decide(self, ctx: DecisionContext) -> Decision:
        if ctx.day in self.outage_days:
            raise SimulatedOutage("injected LLM outage")
        self.calls += 1
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(ctx.for_llm(), ensure_ascii=False)}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        self.cache_read_tokens += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {getattr(resp.stop_details, 'category', None)}")
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
        action = ActionType(data["action"])
        if ctx.allowed.get(action.value) != "allowed":
            raise ValueError(f"model chose '{action.value}' which is {ctx.allowed.get(action.value)}")
        return Decision(
            action=action,
            params=data.get("params") or {},
            message=data.get("message"),
            rationale=data.get("rationale", ""),
            reply_intent=Intent(data.get("reply_intent", "none")),
            source="claude",
        )


class ResilientBrain:
    """An LLM brain with a deterministic safety net. Every fallback is counted and audited."""

    def __init__(self, primary, fallback: RuleBrain, audit):
        self.primary = primary
        self.name = f"{primary.name}+rules"
        self.fallback = fallback
        self.audit = audit
        self.fallbacks = 0
        self.errors: list[str] = []

    def decide(self, ctx: DecisionContext) -> Decision:
        try:
            return self.primary.decide(ctx)
        except Exception as e:  # network, auth, rate limit, refusal, invalid output — all degrade the same way
            self.fallbacks += 1
            self.errors.append(f"day {ctx.day} {ctx.inv.id}: {type(e).__name__}: {str(e)[:160]}")
            self.audit.record("brain_fallback", day=ctx.day, invoice=ctx.inv.id, error=f"{type(e).__name__}: {str(e)[:200]}")
            d = self.fallback.decide(ctx)
            d.source = f"{self.primary.name}->rules"
            return d


class RogueWrapper:
    """Fault injection: on a given day, proposes an out-of-policy action to prove the gate holds."""

    def __init__(self, inner, day: int):
        self.inner = inner
        self.day = day
        self.name = inner.name
        self.fired = False

    def decide(self, ctx: DecisionContext) -> Decision:
        if ctx.day == self.day and not self.fired and ctx.inv.contact_allowed_state:
            self.fired = True
            return Decision(
                ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT,
                {"discount_pct": 15.0},
                message="Final warning: pay within 24 hours or we will take legal action and inform your family.",
                rationale="(injected) rogue decision to demonstrate policy gating",
                source=f"{self.inner.name}(rogue)",
            )
        return self.inner.decide(ctx)


class OpenAIBrain:
    """Same contract as ClaudeBrain against OpenAI's Chat Completions API.

    Differences that matter, all contained here: structured output is a Pydantic model passed to
    `.parse()` rather than a raw JSON schema; prompt caching is automatic (we only supply a stable
    `prompt_cache_key` so repeated system prompts route to the same cache); a policy decline arrives
    as `message.refusal` rather than a stop reason; and `reasoning_effort` is only accepted by
    reasoning models, so a 400 naming it is retried once without it.
    """

    name = "openai"
    provider = "openai"
    # OpenAI accepts minimal|low|medium|high; the Anthropic-side levels above `high` collapse down.
    _EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

    def __init__(self, model: str | None = None, effort: str = "low", outage_days: set[int] | None = None):
        import openai

        self._openai = openai
        self.client = openai.OpenAI()
        self.model = model or os.environ.get("BAAKI_OPENAI_MODEL", "gpt-5")
        self.effort = self._EFFORT.get(effort, "low")
        self.outage_days = outage_days or set()
        self._send_effort = True
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0

    def _request(self, ctx: DecisionContext, with_effort: bool):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(ctx.for_llm(), ensure_ascii=False)},
            ],
            "response_format": DecisionOutput,
            # Stable key so every invoice's request reuses the cached system-prompt prefix.
            "prompt_cache_key": "baaki-recovery-agent-v1",
        }
        if with_effort:
            kwargs["reasoning_effort"] = self.effort
        return self.client.chat.completions.parse(**kwargs)

    def decide(self, ctx: DecisionContext) -> Decision:
        if ctx.day in self.outage_days:
            raise SimulatedOutage("injected LLM outage")
        self.calls += 1
        try:
            resp = self._request(ctx, self._send_effort)
        except self._openai.BadRequestError as e:
            # Non-reasoning models reject reasoning_effort. Drop it once, remember, carry on.
            if self._send_effort and "reasoning_effort" in str(e):
                self._send_effort = False
                resp = self._request(ctx, False)
            else:
                raise

        if resp.usage:
            self.input_tokens += resp.usage.prompt_tokens
            self.output_tokens += resp.usage.completion_tokens
            details = resp.usage.prompt_tokens_details
            self.cache_read_tokens += (details.cached_tokens or 0) if details else 0

        message = resp.choices[0].message
        if message.refusal:
            raise RuntimeError(f"model refused: {message.refusal[:120]}")
        data: DecisionOutput | None = message.parsed
        if data is None:
            raise ValueError("structured output missing (response was truncated or filtered)")
        if ctx.allowed.get(data.action.value) != "allowed":
            raise ValueError(f"model chose '{data.action.value}' which is {ctx.allowed.get(data.action.value)}")
        return Decision(
            action=data.action,
            params={k: v for k, v in data.params.model_dump().items() if v is not None},
            message=data.message,
            rationale=data.rationale,
            reply_intent=data.reply_intent,
            source="openai",
        )
