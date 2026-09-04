"""The OpenAI path, exercised without a network call or an API key.

The stub returns exactly what `chat.completions.parse` returns, so the code under test is the
real parsing, validation and fallback logic — only the transport is faked.
"""

import pytest

from baaki.audit import AuditLog
from baaki.brain import (
    DecisionContext,
    DecisionOutput,
    DecisionParams,
    OpenAIBrain,
    ResilientBrain,
    RuleBrain,
    SimulatedOutage,
)
from baaki.domain import ActionType, Archetype, Debtor, Intent, Invoice, sim_datetime
from baaki.policy import Policy


def _ctx(day=0):
    # 5 days overdue: a payment link is permitted, an early-settlement discount is not (P-DISC-02).
    inv = Invoice(id="inv_1", debtor_id="cust_1", amount_paise=100_000_00, issue_day=-35, due_day=-5, description="test")
    d = Debtor(id="cust_1", name="Test Traders", email="a@b.in", contact="+910000000000", city="Pune",
               prior_invoices=5, prior_late_count=3, avg_days_late=9.0, prior_partial_payments=1, archetype=Archetype.FORGETFUL)
    p = Policy()
    when = sim_datetime(day)
    return DecisionContext(day=day, inv=inv, debtor=d, allowed=p.allowed_actions(inv, day, when), bounds=p.describe(), stop_reason=p.stop_reason(inv))


class _Msg:
    def __init__(self, parsed, refusal=None):
        self.parsed, self.refusal = parsed, refusal


class _Resp:
    def __init__(self, parsed, refusal=None):
        self.choices = [type("C", (), {"message": _Msg(parsed, refusal)})()]
        self.usage = None


class _StubClient:
    """Stands in for openai.OpenAI(). Records kwargs so we can assert on the request shape."""

    def __init__(self, resp=None, raise_exc=None):
        self.resp, self.raise_exc, self.calls = resp, raise_exc, []
        self.chat = type("Chat", (), {"completions": self})()

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return self.resp


@pytest.fixture
def brain(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    return OpenAIBrain()


def _output(action=ActionType.CREATE_PAYMENT_LINK, **params):
    base = dict(accept_partial=True, expire_days=7, installments=None, interval_days=None,
                first_amount_paise=None, discount_pct=None, days=None, reason=None)
    base.update(params)
    return DecisionOutput(reply_intent=Intent.NONE, action=action, params=DecisionParams(**base),
                          message="Hello, please pay via {link}", rationale="first contact")


def test_parsed_output_becomes_a_decision_with_nulls_dropped(brain):
    brain.client = _StubClient(_Resp(_output()))
    d = brain.decide(_ctx())
    assert d.action == ActionType.CREATE_PAYMENT_LINK
    assert d.source == "openai"
    assert d.params == {"accept_partial": True, "expire_days": 7}  # None-valued keys stripped
    assert "{link}" in d.message


def test_request_carries_schema_and_a_stable_cache_key(brain):
    stub = _StubClient(_Resp(_output()))
    brain.client = stub
    brain.decide(_ctx())
    sent = stub.calls[0]
    assert sent["response_format"] is DecisionOutput
    assert sent["prompt_cache_key"] == "baaki-recovery-agent-v1"
    assert sent["reasoning_effort"] == "low"
    assert sent["messages"][0]["role"] == "system"
    # the hidden archetype must never reach the model
    assert "archetype" not in sent["messages"][1]["content"]


def test_blocked_action_is_rejected_not_executed(brain):
    # A discount is blocked this early (P-DISC-02); the brain must refuse to return it.
    brain.client = _StubClient(_Resp(_output(ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT, discount_pct=3.0)))
    with pytest.raises(ValueError, match="blocked"):
        brain.decide(_ctx())


def test_refusal_and_missing_output_raise(brain):
    brain.client = _StubClient(_Resp(None, refusal="I can't help with that"))
    with pytest.raises(RuntimeError, match="refused"):
        brain.decide(_ctx())
    brain.client = _StubClient(_Resp(None))
    with pytest.raises(ValueError, match="structured output missing"):
        brain.decide(_ctx())


def test_reasoning_effort_is_dropped_once_for_non_reasoning_models(brain, monkeypatch):
    import openai

    class _Once(_StubClient):
        def parse(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise openai.BadRequestError(
                    message="Unsupported parameter: 'reasoning_effort'",
                    response=type("R", (), {"status_code": 400, "headers": {}, "request": None})(),
                    body=None,
                )
            return _Resp(_output())

    stub = _Once()
    brain.client = stub
    d = brain.decide(_ctx())
    assert d.action == ActionType.CREATE_PAYMENT_LINK
    assert "reasoning_effort" in stub.calls[0] and "reasoning_effort" not in stub.calls[1]
    assert brain._send_effort is False  # remembered, so later calls skip it


def test_any_failure_degrades_to_the_rules_brain(brain):
    audit = AuditLog()
    resilient = ResilientBrain(brain, RuleBrain(), audit)
    brain.outage_days = {0}
    d = resilient.decide(_ctx())
    assert d.source == "openai->rules"
    assert resilient.fallbacks == 1
    assert audit.filter("brain_fallback")
    assert "SimulatedOutage" in resilient.errors[0]
    assert resilient.name == "openai+rules"


def test_outage_is_provider_neutral(brain):
    brain.outage_days = {0}
    with pytest.raises(SimulatedOutage):
        brain.decide(_ctx())
