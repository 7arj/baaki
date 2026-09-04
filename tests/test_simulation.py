import pytest
from baaki.brain import RuleBrain, classify_intent
from baaki.domain import ActionType, Intent, InvoiceStatus
from baaki.runner import Faults, RunConfig, Simulation


def test_intent_classifier_keywords():
    assert classify_intent("Stop messaging us. Speak to our lawyer.") == Intent.CEASE_CONTACT
    assert classify_intent("We already paid this in July") == Intent.DISPUTE
    assert classify_intent("Business is shut since June, we have no funds") == Intent.HARDSHIP
    assert classify_intent("Can we pay half now?") == Intent.PARTIAL_OFFER
    assert classify_intent(None) == Intent.NONE


def test_deterministic_and_agent_beats_baselines():
    m1 = Simulation(RunConfig(mode="agent", horizon_days=30)).run()
    m2 = Simulation(RunConfig(mode="agent", horizon_days=30)).run()
    assert m1["recovered_paise"] == m2["recovered_paise"]
    naive = Simulation(RunConfig(mode="naive", horizon_days=30)).run()
    none = Simulation(RunConfig(mode="none", horizon_days=30)).run()
    assert m1["recovered_paise"] > naive["recovered_paise"] > none["recovered_paise"]
    assert m1["policy_violations_unenforced"] == 0
    assert naive["policy_violations_unenforced"] > 0


def test_faults_are_handled_not_fatal():
    sim = Simulation(RunConfig(mode="agent", horizon_days=20, faults=Faults(razorpay_fail_creates=2, rogue_day=2)))
    m = sim.run()
    assert m["gateway_failures_handled"] == 2
    assert m["policy_denials"] >= 1
    rogue = [e for e in sim.audit.entries if e["event"] == "policy_check" and "rogue" in e.get("source", "")]
    assert rogue and not rogue[0]["verdict"]["allowed"]
    # the rogue message was never sent
    assert not any("inform your family" in e.get("text", "") for e in sim.audit.entries if e["event"] == "message_sent")


def test_stopping_rules_hand_over_to_humans():
    sim = Simulation(RunConfig(mode="agent", horizon_days=45))
    sim.run()
    escalated = [i for i in sim.invoices.values() if i.status == InvoiceStatus.ESCALATED]
    stopped = [i for i in sim.invoices.values() if i.status == InvoiceStatus.STOPPED]
    assert escalated and stopped
    assert all(i.escalation_reason for i in escalated) and all(i.stop_reason for i in stopped)
    # nobody who asked us to stop was contacted afterwards
    for i in sim.invoices.values():
        if i.cease_requested:
            cease_day = i.last_inbound_day
            assert all(d <= cease_day for d in i.contact_days)


def test_a_reminder_always_carries_a_payment_link():
    """An LLM may pick send_reminder before any link exists. "Pay here: {link}" with nothing to
    fill in is a broken message, so the reminder must create the link itself."""
    from baaki.audit import AuditLog
    from baaki.domain import Decision, Debtor, Archetype, Invoice, sim_datetime
    from baaki.policy import Policy
    from baaki.razorpay_client import FakeRazorpay
    from baaki.tools import Toolbox

    inv = Invoice(id="inv_1", debtor_id="c1", amount_paise=100_000_00, issue_day=-40,
                  due_day=-10, description="test")
    debtor = Debtor(id="c1", name="Test Co", email="a@b.in", contact="+910000000000", city="Pune",
                    prior_invoices=3, prior_late_count=1, avg_days_late=5.0,
                    prior_partial_payments=0, archetype=Archetype.FORGETFUL)
    tools = Toolbox(Policy(), FakeRazorpay(), AuditLog(), {"c1": debtor})

    assert inv.payment_link_url is None
    result = tools.execute(
        Decision(ActionType.SEND_REMINDER, message="Please pay here: {link}. Thanks."),
        inv, 0, sim_datetime(0))

    assert result.ok and result.contacted
    assert inv.payment_link_url and inv.payment_link_url.startswith("https://")
    sent = [e for e in tools.audit.entries if e["event"] == "message_sent"]
    assert sent and inv.payment_link_url in sent[0]["text"]
    assert "{link}" not in sent[0]["text"] and "(link pending)" not in sent[0]["text"]


def test_an_unfilled_placeholder_can_never_reach_a_customer():
    """Defence in depth at the send boundary, independent of which brain composed the message."""
    from baaki.audit import AuditLog
    from baaki.domain import Archetype, Debtor, Invoice
    from baaki.policy import Policy
    from baaki.razorpay_client import FakeRazorpay
    from baaki.tools import Toolbox

    inv = Invoice(id="inv_1", debtor_id="c1", amount_paise=1000, issue_day=-40, due_day=-10, description="t")
    debtor = Debtor(id="c1", name="T", email="a@b.in", contact="+91", city="Pune", prior_invoices=1,
                    prior_late_count=0, avg_days_late=0.0, prior_partial_payments=0,
                    archetype=Archetype.FORGETFUL)
    tools = Toolbox(Policy(), FakeRazorpay(), AuditLog(), {"c1": debtor})

    with pytest.raises(ValueError, match="refusing to send"):
        tools._contacted(inv, 0, "Pay here: (link pending)")
    assert tools.audit.filter("message_blocked_unfilled_placeholder")
    assert not tools.audit.filter("message_sent")
