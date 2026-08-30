from baaki.domain import ActionType, Invoice, sim_datetime
from baaki.policy import Policy


def inv(**kw):
    base = dict(id="inv_1", debtor_id="cust_1", amount_paise=100_000_00, issue_day=-40, due_day=-10, description="test")
    base.update(kw)
    return Invoice(**base)


def test_contact_outside_window_is_blocked_and_deferred():
    p = Policy()
    v = p.evaluate(ActionType.SEND_REMINDER, {}, inv(), 0, sim_datetime(0, hour=21), "hi")
    assert not v.allowed and v.rule_id == "P-TIME-01"
    nxt = p.next_allowed_time(sim_datetime(0, hour=21))
    assert nxt.hour == 8 and nxt.day == sim_datetime(1).day


def test_threatening_language_is_blocked():
    p = Policy()
    v = p.evaluate(ActionType.SEND_REMINDER, {}, inv(), 0, sim_datetime(0), "Pay now or we inform the police and your family")
    assert not v.allowed and v.rule_id == "P-MSG-01"


def test_discount_cap_and_timing():
    p = Policy()
    i = inv()
    assert p.evaluate(ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT, {"discount_pct": 15}, i, 30, sim_datetime(30), None).rule_id == "P-DISC-01"
    assert p.evaluate(ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT, {"discount_pct": 3}, i, 5, sim_datetime(5), None).rule_id == "P-DISC-02"
    assert p.evaluate(ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT, {"discount_pct": 3}, i, 30, sim_datetime(30), None).allowed


def test_cease_and_dispute_block_all_contact_but_allow_escalation():
    p = Policy()
    i = inv(cease_requested=True)
    for a in (ActionType.SEND_REMINDER, ActionType.CREATE_PAYMENT_LINK, ActionType.OFFER_INSTALLMENT_PLAN):
        assert p.evaluate(a, {"amount_paise": 1, "expire_days": 7, "installments": 2, "interval_days": 30, "first_amount_paise": 50_000_00}, i, 0, sim_datetime(0), None).rule_id == "P-CEASE-01"
    assert p.evaluate(ActionType.ESCALATE_TO_HUMAN, {}, i, 0, sim_datetime(0), None).allowed
    assert p.stop_reason(inv(dispute_open=True))


def test_gap_and_cap():
    p = Policy()
    i = inv(contact_count=1, contact_days=[0])
    assert p.evaluate(ActionType.SEND_REMINDER, {}, i, 1, sim_datetime(1), None).rule_id == "P-GAP-01"
    i2 = inv(contact_count=6, contact_days=[0, 3, 6, 9, 12, 15])
    assert p.evaluate(ActionType.SEND_REMINDER, {}, i2, 30, sim_datetime(30), None).rule_id == "P-CAP-01"


def test_link_and_plan_bounds():
    p = Policy()
    i = inv()
    assert p.evaluate(ActionType.CREATE_PAYMENT_LINK, {"amount_paise": 200_000_00, "expire_days": 7}, i, 0, sim_datetime(0), None).rule_id == "P-LINK-01"
    assert p.evaluate(ActionType.CREATE_PAYMENT_LINK, {"amount_paise": 10, "expire_days": 60}, i, 0, sim_datetime(0), None).rule_id == "P-LINK-02"
    assert p.evaluate(ActionType.OFFER_INSTALLMENT_PLAN, {"installments": 6, "interval_days": 30, "first_amount_paise": 50_000_00}, i, 0, sim_datetime(0), None).rule_id == "P-PLAN-01"
    assert p.evaluate(ActionType.OFFER_INSTALLMENT_PLAN, {"installments": 3, "interval_days": 30, "first_amount_paise": 1000}, i, 0, sim_datetime(0), None).rule_id == "P-PLAN-03"
