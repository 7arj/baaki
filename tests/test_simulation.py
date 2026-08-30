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
