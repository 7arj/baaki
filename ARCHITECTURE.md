# Architecture

```mermaid
flowchart LR
  subgraph Ledger
    D[(debtors + invoices)]
    R[risk model<br/>logistic, held-out eval]
  end
  subgraph Agent loop — one invoice, one day
    C[DecisionContext<br/>visible facts · allowed actions · bounds]
    B{{Brain}}
    B1[RuleBrain]
    B2[ClaudeBrain / OpenAIBrain<br/>structured JSON]
    G[Policy gate<br/>P-TIME · P-MSG · P-DISC · P-PLAN · P-LINK · P-CEASE …]
    T[Toolbox]
  end
  subgraph Money
    RZ[(Razorpay Payment Links<br/>test mode or fake)]
    W[Webhook handler<br/>HMAC verify · idempotent]
  end
  A[(hash-chained audit log)]
  H[Human exception list]
  UI[Dashboard]

  D --> R --> C --> B
  B --> B2 -- error/refusal/invalid --> B1
  B --> B1
  B2 --> G
  B1 --> G
  G -- allowed --> T
  G -- blocked --> A
  T --> RZ --> W --> D
  T -- escalate / stop --> H
  C & B & G & T & W --> A
  A --> UI
  D --> UI
```

## Design decisions

**One gate, no exceptions.** The LLM, the rules playbook, the baselines and the fault injector all go through `Toolbox.execute`, which calls `Policy.evaluate` on the *actual* parameters (after defaults are filled in). The brain's pre-filtered "allowed actions" list is a courtesy to save it a wasted turn, not the enforcement point. Defense in depth: even if the prompt is jailbroken, a 15% discount or a threatening message cannot leave the system.

**The LLM does what only an LLM can do.** Reading a reply like "20 cartons were short-shipped, not paying till corrected" and recognising it as a dispute; drafting a message that references the actual history; choosing between a plan and a discount for a specific debtor. It does not compute amounts (policy floors do), does not call Razorpay, does not decide *whether* the merchant is allowed to discount.

**Deterministic fallback, not a retry storm.** When the LLM is unreachable, slow, refuses, returns malformed JSON, or picks a blocked action, the rules brain answers *that one decision* and the audit records `brain_fallback` with the cause. If the client can't even be constructed (no key, bad config), the whole run degrades to rules and records `brain_unavailable` so the report never silently overstates the LLM. The run never stalls, and `decision_sources` tells you exactly how many decisions were LLM vs rules vs fallback.

**Provider-agnostic by construction.** `ClaudeBrain` and `OpenAIBrain` share the system prompt, the output schema, the allowed-action list and the `Decision` return type; each owns only its SDK's quirks (raw JSON schema vs Pydantic model, `cache_control` vs automatic caching plus a `prompt_cache_key`, `stop_reason == "refusal"` vs `message.refusal`, and OpenAI's `reasoning_effort` which non-reasoning models reject — dropped once and remembered). The simulated outage is a neutral `SimulatedOutage`, not an SDK exception type, so the fallback path can't accidentally depend on one vendor. Swapping providers touches one class and nothing downstream of the policy gate.

**Stopping rules are policy, not vibes.** A dispute, a hardship claim, a cease request, or four unanswered contacts each map to a terminal state with a reason string that lands on the human's exception list sorted by outstanding amount. The agent's job is to convert what it can and hand over the rest *with context* — not to grind.

**Paise, not floats.** Every amount is an integer in paise, matching Razorpay's API; rupee formatting is only for humans.

**Same code path for fake and real.** `FakeRazorpay` implements the documented `payment_links` entity shape and signs webhook bodies the way Razorpay does, so `handle_razorpay_webhook` — signature check, link→invoice lookup via `notes.invoice_id`, idempotency on payment id — is exercised on every simulated payment. Switching to test mode is an env var.

**Measured against two baselines.** "Do nothing" gives the organic floor; "naive reminders" is what SMEs actually do, run with the policy in *record-only* mode so you can see the 934 conduct violations it would commit (mostly contacting people after they said stop, or after a dispute). The agent's incremental recovery is reported against both.

**Honest risk metrics.** Labels come from the do-nothing run ("not paid in 30 days without intervention"), the split is by debtor hash so no debtor leaks between train and holdout, and the report prints TP/FP/FN/TN at two thresholds rather than a single flattering number.

## Data flow for one decision

1. `Simulation._queue` picks invoices whose `next_action_day <= today` or that have an unprocessed reply, ordered by `risk × outstanding`.
2. `DecisionContext.for_llm()` serialises only visible fields (never `archetype`), the last 6 timeline events, the new inbound message, the policy's stop reason, the allowed-action map with block reasons, and the bounds.
3. The brain returns a `Decision(action, params, message, rationale, reply_intent, source)`.
4. The runner applies `reply_intent` to the invoice state (dispute/hardship/cease/promise) **before** the gate, so a cease request blocks the very action proposed alongside it.
5. `Toolbox.execute` fills defaults, evaluates policy, records `policy_check`, then runs the handler; contact handlers increment counters and set the next eligible day; link handlers call Razorpay with retries.
6. The simulator schedules the debtor's reaction; payments later arrive as signed webhooks.

## What I'd build next

- Transport adapters (WhatsApp Business API, email) behind the same `message_sent` event.
- Per-debtor negotiation memory across invoices (a debtor who accepted a plan once should be offered one first).
- A/B the LLM's drafted messages against templates on real reply rates.
- Razorpay Smart Collect virtual accounts for debtors who pay by NEFT so those payments reconcile automatically too.
