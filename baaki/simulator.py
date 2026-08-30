"""Debtor environment. Given an outbound action, decides how the debtor reacts and when.

This is the only module that reads `Debtor.archetype`. Reactions are scheduled as future
outcomes (payments on the Razorpay link, inbound replies, or silence) and replayed by the runner.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .domain import ActionType, Archetype, Debtor, Intent, Invoice


@dataclass
class Outcome:
    day: int
    invoice_id: str
    kind: str  # pay | reply | accept_plan
    amount_paise: int = 0
    text: str = ""
    intent: Intent = Intent.NONE
    data: dict[str, Any] = field(default_factory=dict)


REPLIES = {
    Intent.WILL_PAY: ["Noted, paying today via the link.", "Sorry for the delay, clearing it now.", "Done from our side, please check."],
    Intent.PROMISE_TO_PAY: ["Our client payment comes on the 10th, will clear by then.", "Will pay next week after GST filing.", "Give us 5 days, funds are tight this week."],
    Intent.PARTIAL_OFFER: ["Can we pay half now and the rest next month?", "We can do 40% today, balance in 3 weeks.", "Cash flow is bad, can we split this?"],
    Intent.HARDSHIP: ["Business is shut since June, we have no funds right now.", "Factory flooded, operations stopped. Cannot pay currently.", "We are winding up the firm."],
    Intent.DISPUTE: ["This invoice is wrong — 20 cartons were short-shipped. Not paying till corrected.", "We already paid this in July, check your books.", "Quality was rejected by our QC, invoice should be credit-noted."],
    Intent.CEASE_CONTACT: ["Stop messaging us. Speak to our lawyer.", "Do not contact this number again.", "Enough. No more messages."],
    Intent.UNCLEAR: ["ok", "who is this?", "call later"],
}


class DebtorSim:
    def __init__(self, seed: int = 11):
        self.rng = random.Random(seed)

    # -- organic behaviour with no contact at all (the "do nothing" baseline) --------------------
    def organic(self, debtor: Debtor, inv: Invoice) -> list[Outcome]:
        r = self.rng
        a = debtor.archetype
        if a == Archetype.PROMPT:
            return [Outcome(day=r.randint(1, 8), invoice_id=inv.id, kind="pay", amount_paise=inv.amount_paise, data={"organic": True})]
        if a == Archetype.FORGETFUL and r.random() < 0.25:
            return [Outcome(day=r.randint(10, 30), invoice_id=inv.id, kind="pay", amount_paise=inv.amount_paise, data={"organic": True})]
        if a == Archetype.DISPUTER and r.random() < 0.3:
            return [Outcome(day=r.randint(5, 20), invoice_id=inv.id, kind="reply", text=r.choice(REPLIES[Intent.DISPUTE]), intent=Intent.DISPUTE)]
        return []

    def _reply(self, day: int, inv: Invoice, intent: Intent, delay: tuple[int, int] = (0, 2)) -> Outcome:
        return Outcome(day=day + self.rng.randint(*delay), invoice_id=inv.id, kind="reply", text=self.rng.choice(REPLIES[intent]), intent=intent)

    def _pay(self, day: int, inv: Invoice, amount: int, delay: tuple[int, int] = (0, 3)) -> Outcome:
        return Outcome(day=day + self.rng.randint(*delay), invoice_id=inv.id, kind="pay", amount_paise=amount)

    # -- reaction to an outbound action -------------------------------------------------------
    def react(self, debtor: Debtor, inv: Invoice, action: ActionType, params: dict[str, Any], day: int) -> list[Outcome]:
        r = self.rng
        a = debtor.archetype
        has_link = inv.payment_link_id is not None
        n = inv.contact_count  # already incremented for this contact
        out: list[Outcome] = []

        if action == ActionType.ESCALATE_TO_HUMAN:
            # A human resolves disputes/hardship offline. Disputers mostly pay once corrected.
            if a == Archetype.DISPUTER and r.random() < 0.7:
                out.append(self._pay(day, inv, inv.outstanding_paise, delay=(4, 9)))
                out[-1].data["via_human"] = True
            elif a == Archetype.CASH_STRAPPED and r.random() < 0.4:
                out.append(self._pay(day, inv, inv.outstanding_paise // 2, delay=(5, 12)))
                out[-1].data["via_human"] = True
            return out

        if action not in (ActionType.SEND_REMINDER, ActionType.CREATE_PAYMENT_LINK, ActionType.OFFER_INSTALLMENT_PLAN, ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT):
            return out

        # Every archetype: repeated automated contact after a dispute or cease request angers them.
        if inv.cease_requested or inv.dispute_open:
            out.append(self._reply(day, inv, Intent.CEASE_CONTACT, (0, 1)))
            return out

        if a == Archetype.PROMPT:
            out.append(self._reply(day, inv, Intent.WILL_PAY, (0, 1)))
            out.append(self._pay(day, inv, inv.outstanding_paise, (0, 2)))

        elif a == Archetype.FORGETFUL:
            p = 0.55 if has_link else 0.35
            p += 0.1 * min(n - 1, 2)
            if r.random() < p:
                out.append(self._pay(day, inv, inv.outstanding_paise, (0, 4)))
            elif r.random() < 0.3:
                out.append(self._reply(day, inv, Intent.PROMISE_TO_PAY, (0, 2)))
                out.append(self._pay(day, inv, inv.outstanding_paise, (5, 9)))

        elif a == Archetype.CASH_STRAPPED:
            if action == ActionType.OFFER_INSTALLMENT_PLAN:
                if r.random() < 0.8:
                    first = params["first_amount_paise"]
                    out.append(Outcome(day=day + r.randint(0, 2), invoice_id=inv.id, kind="accept_plan"))
                    out.append(self._pay(day, inv, first, (1, 4)))
                    # later installments follow with 85% adherence each
                    rest = inv.outstanding_paise - first
                    k = params["installments"] - 1
                    per = rest // k if k else 0
                    d = day
                    for i in range(k):
                        d += params["interval_days"]
                        if r.random() < 0.85:
                            out.append(self._pay(d, inv, per if i < k - 1 else rest - per * (k - 1), (0, 2)))
                        else:
                            break
                else:
                    out.append(self._reply(day, inv, Intent.HARDSHIP, (0, 2)))
            elif action == ActionType.OFFER_EARLY_SETTLEMENT_DISCOUNT:
                if r.random() < 0.5:
                    pct = params["discount_pct"]
                    out.append(self._pay(day, inv, int(inv.outstanding_paise * (100 - pct) / 100), (0, 3)))
                    out[-1].data["discount_pct"] = pct
                else:
                    out.append(self._reply(day, inv, Intent.PARTIAL_OFFER, (0, 2)))
            elif action == ActionType.CREATE_PAYMENT_LINK and params.get("accept_partial"):
                if r.random() < 0.65:
                    frac = r.uniform(0.3, 0.5)
                    amt = max(int(inv.outstanding_paise * frac), params.get("first_min_partial_paise", 0))
                    out.append(self._pay(day, inv, amt, (0, 3)))
                    out.append(self._reply(day, inv, Intent.PARTIAL_OFFER, (0, 1)))
                else:
                    out.append(self._reply(day, inv, Intent.PARTIAL_OFFER, (0, 2)))
            else:
                # plain reminder / full-only link: mostly a partial-offer reply, rarely silence
                if r.random() < 0.6:
                    out.append(self._reply(day, inv, Intent.PARTIAL_OFFER, (0, 2)))

        elif a == Archetype.DISPUTER:
            out.append(self._reply(day, inv, Intent.DISPUTE, (0, 1)))

        elif a == Archetype.GHOST:
            if n >= 3 and r.random() < 0.15:
                out.append(self._reply(day, inv, Intent.UNCLEAR, (0, 3)))

        elif a == Archetype.INSOLVENT:
            out.append(self._reply(day, inv, Intent.HARDSHIP, (0, 2)))

        return out
