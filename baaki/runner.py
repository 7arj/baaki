"""Day-stepped simulation harness. Runs a brain over the ledger and measures what it recovered."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .brain import (ClaudeBrain, DecisionContext, NaiveBrain, NoneBrain, OpenAIBrain, ResilientBrain,
                    RogueWrapper, RuleBrain, estimate_cost_usd)
from .data import generate
from .domain import ActionType, CONTACT_ACTIONS, Debtor, Intent, Invoice, InvoiceStatus, rupees, sim_datetime
from .policy import Policy, PolicyBounds
from .razorpay_client import FakeRazorpay
from .risk import RiskModel, features, is_holdout
from .simulator import DebtorSim, Outcome
from .tools import Toolbox
from .webhooks import apply_payment, handle_razorpay_webhook


@dataclass
class Faults:
    razorpay_fail_creates: int = 0  # first N payment_link.create calls return 503
    llm_outage_days: set[int] = field(default_factory=set)
    rogue_day: int | None = None  # brain proposes an out-of-policy action on this day


@dataclass
class RunConfig:
    mode: str = "agent"  # none | naive | agent
    brain: str = "rules"  # rules | claude | openai
    model: str | None = None  # override the provider's default model
    horizon_days: int = 45
    seed: int = 7
    sim_seed: int = 11
    faults: Faults = field(default_factory=Faults)
    risk_model: RiskModel | None = None
    out_dir: Path | None = None
    llm_effort: str = "low"
    max_llm_calls: int | None = None   # hard spend cap; the rest of the run uses the rules brain


class Simulation:
    def __init__(self, cfg: RunConfig, debtors: list[Debtor] | None = None, invoices: list[Invoice] | None = None):
        self.cfg = cfg
        if debtors is None or invoices is None:
            debtors, invoices = generate(cfg.seed)
        self.debtors = {d.id: d for d in debtors}
        self.invoices = {i.id: i for i in invoices}
        self.audit = AuditLog(cfg.out_dir / f"audit_{cfg.mode}{'_' + cfg.brain if cfg.mode == 'agent' else ''}.jsonl" if cfg.out_dir else None)
        self.policy = Policy(PolicyBounds())
        self.rzp = FakeRazorpay(fail_next_creates=cfg.faults.razorpay_fail_creates)
        self.tools = Toolbox(self.policy, self.rzp, self.audit, self.debtors, enforce=(cfg.mode == "agent"))
        self.sim = DebtorSim(cfg.sim_seed)
        self.pending: dict[int, list[Outcome]] = defaultdict(list)
        self.llm_unavailable: str | None = None
        self.brain = self._build_brain()
        self.decisions = 0
        self.decision_sources: dict[str, int] = defaultdict(int)
        self.day = 0

    def _build_brain(self):
        cfg = self.cfg
        if cfg.mode == "none":
            return NoneBrain()
        if cfg.mode == "naive":
            return NaiveBrain()
        rules = RuleBrain()
        brain = rules
        llm_cls = {"claude": ClaudeBrain, "openai": OpenAIBrain}.get(cfg.brain)
        if llm_cls:
            try:
                primary = llm_cls(model=cfg.model, effort=cfg.llm_effort, outage_days=cfg.faults.llm_outage_days)
                brain = ResilientBrain(primary, rules, self.audit, max_calls=cfg.max_llm_calls)
            except Exception as e:
                # No key, missing SDK, bad config: degrade to the deterministic brain for the whole
                # run rather than crashing. Recorded so the report never silently overstates the LLM.
                self.audit.record("brain_unavailable", provider=cfg.brain, error=f"{type(e).__name__}: {str(e)[:200]}")
                self.llm_unavailable = f"{cfg.brain} unavailable ({type(e).__name__}); ran on the rules brain"
        if cfg.faults.rogue_day is not None:
            brain = RogueWrapper(brain, cfg.faults.rogue_day)
        return brain

    # -----------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        t0 = time.time()
        self.audit.record("run_started", mode=self.cfg.mode, brain=getattr(self.brain, "name", "?"), horizon=self.cfg.horizon_days, policy=self.policy.describe(), gateway=self.rzp.mode)
        for inv in self.invoices.values():
            inv.next_action_day = 0
            if self.cfg.risk_model:
                inv.risk_score = round(self.cfg.risk_model.predict(inv, self.debtors[inv.debtor_id], 0), 3)
            for o in self.sim.organic(self.debtors[inv.debtor_id], inv):
                self.pending[o.day].append(o)

        for day in range(self.cfg.horizon_days + 1):
            self.day = day
            self._deliver(day)
            if self.cfg.mode == "none":
                continue
            for inv in self._queue(day):
                self._step(inv, day)
        self.audit.record("run_finished", mode=self.cfg.mode, seconds=round(time.time() - t0, 2))
        return self.metrics()

    def _queue(self, day: int) -> list[Invoice]:
        due = [i for i in self.invoices.values() if not i.is_terminal and i.status != InvoiceStatus.ESCALATED and (i.next_action_day <= day or i.inbound_unprocessed)]
        due.sort(key=lambda i: -((i.risk_score or 0.5) * i.outstanding_paise))
        return due

    # -----------------------------------------------------------------------------------------
    def _deliver(self, day: int) -> None:
        for o in self.pending.pop(day, []):
            inv = self.invoices[o.invoice_id]
            if o.kind == "pay":
                self._pay(inv, o, day)
            elif o.kind == "reply":
                if inv.status == InvoiceStatus.PAID:
                    continue
                inv.last_inbound, inv.last_inbound_day, inv.inbound_unprocessed = o.text, day, True
                inv.last_inbound_intent = Intent.NONE
                inv.log(day, "inbound", o.text)
                self.audit.record("message_received", day=day, invoice=inv.id, text=o.text)
                if inv.status in (InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID):
                    inv.next_action_day = min(inv.next_action_day, day)
            elif o.kind == "accept_plan" and inv.plan:
                inv.plan.accepted = True
                inv.log(day, "inbound", "accepted installment plan")
                self.audit.record("plan_accepted", day=day, invoice=inv.id)

    def _pay(self, inv: Invoice, o: Outcome, day: int) -> None:
        if inv.outstanding_paise <= 0:
            return
        amount = min(o.amount_paise, inv.outstanding_paise)
        link_id = inv.payment_link_id
        link = self.rzp.links.get(link_id) if link_id else None
        offline = o.data.get("via_human") or o.data.get("organic")
        if not offline and link and link["status"] in ("created", "partially_paid") and (amount >= link["amount"] - link["amount_paid"] or link["accept_partial"]):
            body, sig = self.rzp.simulate_payment(link_id, amount)
            res = handle_razorpay_webhook(body, sig, self.rzp.webhook_secret, self.invoices, self.tools.link_index, self.audit, day)
            self.audit.record("webhook_processed", day=day, **res)
        else:
            # No usable link (organic payer, human-negotiated settlement): a NEFT hits the bank and
            # accounts marks it manually. Same idempotent credit path.
            apply_payment(inv, f"neft_{inv.id}_{day}_{o.amount_paise}", amount, day, self.audit, source="bank_transfer", **o.data)

    # -----------------------------------------------------------------------------------------
    def _step(self, inv: Invoice, day: int) -> None:
        when = sim_datetime(day, hour=10)
        debtor = self.debtors[inv.debtor_id]
        ctx = DecisionContext(day=day, inv=inv, debtor=debtor, allowed=self.policy.allowed_actions(inv, day, when), bounds=self.policy.describe(), stop_reason=self.policy.stop_reason(inv))
        decision = self.brain.decide(ctx)
        self.decisions += 1
        self.decision_sources[decision.source] += 1
        self.audit.record("decision", day=day, invoice=inv.id, source=decision.source, action=decision.action.value, params=decision.params, reply_intent=decision.reply_intent.value, rationale=decision.rationale, message=decision.message)

        if inv.inbound_unprocessed:
            self._apply_intent(inv, decision.reply_intent, day)

        result = self.tools.execute(decision, inv, day, when)
        if result.ok and decision.action in CONTACT_ACTIONS and result.contacted or decision.action == ActionType.ESCALATE_TO_HUMAN:
            for o in self.sim.react(debtor, inv, decision.action, result.params, day):
                self.pending[o.day].append(o)

    def _apply_intent(self, inv: Invoice, intent: Intent, day: int) -> None:
        inv.inbound_unprocessed = False
        inv.last_inbound_intent = intent
        if intent == Intent.CEASE_CONTACT:
            inv.cease_requested = True
        elif intent == Intent.DISPUTE:
            inv.dispute_open = True
        elif intent == Intent.HARDSHIP:
            inv.hardship_flagged = True
        elif intent == Intent.PROMISE_TO_PAY:
            inv.promised_pay_day = day + 7
        self.audit.record("intent_classified", day=day, invoice=inv.id, intent=intent.value, text=inv.last_inbound)

    # -----------------------------------------------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        invs = list(self.invoices.values())
        total = sum(i.amount_paise for i in invs)
        recovered = sum(i.amount_paid_paise for i in invs)
        paid_events = self.audit.filter("payment_received")
        via_link = sum(e["amount_paise"] for e in paid_events if e["source"] == "razorpay_link")
        via_human = sum(e["amount_paise"] for e in paid_events if e.get("via_human"))
        organic = sum(e["amount_paise"] for e in paid_events if e.get("organic"))
        written_off = sum(e.get("written_off_paise", 0) for e in paid_events)
        contacts = sum(i.contact_count for i in invs)
        pay_days = [e["day"] for e in paid_events]
        by_status = defaultdict(int)
        for i in invs:
            by_status[i.status.value] += 1
        exceptions = [
            {
                "invoice": i.id,
                "debtor": self.debtors[i.debtor_id].name,
                "outstanding": rupees(i.outstanding_paise),
                "outstanding_paise": i.outstanding_paise,
                "status": i.status.value,
                "reason": i.escalation_reason or i.stop_reason,
                "last_inbound": i.last_inbound,
            }
            for i in invs
            if i.status in (InvoiceStatus.ESCALATED, InvoiceStatus.STOPPED)
        ]
        exceptions.sort(key=lambda x: -x["outstanding_paise"])
        unresolved_open = [i for i in invs if i.status in (InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID)]
        llm = None
        b = self.brain.inner if isinstance(self.brain, RogueWrapper) else self.brain
        if isinstance(b, ResilientBrain):
            p = b.primary
            llm = {"provider": p.provider, "model": p.model, "calls": p.calls, "fallbacks": b.fallbacks,
                   "input_tokens": p.input_tokens, "output_tokens": p.output_tokens,
                   "cache_read_tokens": p.cache_read_tokens,
                   "estimated_cost_usd": estimate_cost_usd(p.model, p.input_tokens, p.output_tokens, p.cache_read_tokens),
                   "budget_cap": b.max_calls, "budget_exhausted_at": b.budget_exhausted_at,
                   "errors": b.errors[:20]}
        return {
            "mode": self.cfg.mode,
            "brain": getattr(self.brain, "name", "?"),
            "horizon_days": self.cfg.horizon_days,
            "invoices": len(invs),
            "debtors": len(self.debtors),
            "total_receivable_paise": total,
            "total_receivable": rupees(total),
            "recovered_paise": recovered,
            "recovered": rupees(recovered),
            "recovery_rate_pct": round(100 * recovered / total, 1) if total else 0,
            "recovered_via_link_paise": via_link,
            "recovered_via_human_paise": via_human,
            "recovered_organic_paise": organic,
            "discount_written_off_paise": written_off,
            "invoices_by_status": dict(by_status),
            "contacts_sent": contacts,
            "contacts_per_lakh_recovered": round(contacts / (recovered / 100_000_00), 2) if recovered else None,
            "median_days_to_cash": sorted(pay_days)[len(pay_days) // 2] if pay_days else None,
            "policy_denials": self.tools.denials,
            "policy_violations_unenforced": self.tools.violations,
            "gateway_failures_handled": self.tools.gateway_failures,
            "decisions": self.decisions,
            "decision_sources": dict(self.decision_sources),
            "llm": llm,
            "llm_unavailable": self.llm_unavailable,
            "razorpay_calls": len(self.rzp.calls),
            "exceptions": exceptions,
            "still_open_unresolved": len(unresolved_open),
            "still_open_outstanding_paise": sum(i.outstanding_paise for i in unresolved_open),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics(),
            "invoices": [
                {
                    **inv.visible(self.day),
                    "debtor": self.debtors[inv.debtor_id].name,
                    "escalation_reason": inv.escalation_reason,
                    "stop_reason": inv.stop_reason,
                    "payment_link_url": inv.payment_link_url,
                    "timeline": [{"day": e.day, "kind": e.kind, "summary": e.summary} for e in inv.timeline],
                }
                for inv in self.invoices.values()
            ],
            "policy": self.policy.describe(),
        }


# ---------------------------------------------------------------------------------------------
def train_risk_model(seed: int, sim_seed: int, horizon: int = 30) -> tuple[RiskModel, dict[str, Any]]:
    """Labels = 'unpaid after `horizon` days with no intervention' from the do-nothing baseline."""
    debtors, invoices = generate(seed)
    sim = Simulation(RunConfig(mode="none", horizon_days=horizon, seed=seed, sim_seed=sim_seed), debtors, invoices)
    sim.run()
    dmap = sim.debtors
    train, hold = [], []
    for inv in sim.invoices.values():
        d = dmap[inv.debtor_id]
        # features are computed from the pristine day-0 view: reconstruct from a fresh copy
        fresh = Invoice(id=inv.id, debtor_id=inv.debtor_id, amount_paise=inv.amount_paise, issue_day=inv.issue_day, due_day=inv.due_day, description=inv.description)
        row = (features(fresh, d, 0), 1 if inv.status != InvoiceStatus.PAID else 0)
        (hold if is_holdout(d.id) else train).append(row)
    model = RiskModel()
    model.fit(train)
    preds = [(1.0 / (1.0 + __import__("math").exp(-sum(w * x for w, x in zip(model.weights, x)))), y) for x, y in hold]
    report = {
        "label": f"invoice NOT paid within {horizon} days with zero intervention",
        "train_rows": len(train),
        "holdout_rows": len(hold),
        "split": "by debtor id hash (40% held out); no debtor appears in both",
        "weights": {n: round(w, 3) for n, w in zip(["bias", "days_overdue", "late_ratio", "avg_days_late", "partial_hist", "log_amount"], model.weights)},
        "holdout": RiskModel.metrics(preds, 0.5),
        "holdout_at_0.7": RiskModel.metrics(preds, 0.7),
        "base_rate_holdout": round(sum(y for _, y in hold) / len(hold), 3) if hold else None,
    }
    return model, report


def run_all(out_dir: Path, brain: str = "rules", horizon: int = 45, seed: int = 7, sim_seed: int = 11, faults: Faults | None = None, llm_effort: str = "low", model: str | None = None, max_llm_calls: int | None = None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    faults = faults or Faults()
    risk, risk_report = train_risk_model(seed, sim_seed)
    results: dict[str, Any] = {"risk_model": risk_report, "runs": {}}
    for mode in ("none", "naive", "agent"):
        cfg = RunConfig(mode=mode, brain=brain, horizon_days=horizon, seed=seed, sim_seed=sim_seed, faults=faults if mode == "agent" else Faults(), risk_model=risk if mode == "agent" else None, out_dir=out_dir, llm_effort=llm_effort, model=model, max_llm_calls=max_llm_calls)
        sim = Simulation(cfg)
        m = sim.run()
        results["runs"][mode] = m
        key = f"{mode}{'_' + brain if mode == 'agent' else ''}"
        (out_dir / f"run_{key}.json").write_text(json.dumps(sim.snapshot(), indent=2, default=str))
    (out_dir / f"summary_{brain}.json").write_text(json.dumps(results, indent=2, default=str))
    return results
