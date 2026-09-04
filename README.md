# Baaki (बाकी)

**Bounded, auditable AI recovery of overdue SME receivables.**
Razorpay AI Buildathon, Track 3: AI Revenue Recovery.

Indian SMEs are owed lakhs in overdue B2B invoices and chase them by hand. Baaki works the overdue
ledger the way a good collections person would: a reminder with a Razorpay Payment Link, then
partial payments or a bounded installment plan, a small settlement discount when the cap allows,
and a clean hand-off to a human the moment a customer disputes, reports hardship, or asks to be
left alone.

It cannot step outside the merchant's policy, because every action passes one gate and every step
is hash-chain audited.

Built for the top problem on Razorpay's Fix My Itch board, *"Why can't SMEs negotiate favourable
payment terms with large buyers?"* (score 82.8), and its neighbours on invoice-management time
(67.5) and vendors ghosted after partial payments (76).

## Measured result

Same 120-invoice ledger, same simulated debtors, 45 days, three strategies.

| | do nothing | naive reminders | **Baaki** |
|---|---:|---:|---:|
| recovered of ₹82.95L | ₹26.58L (32.0%) | ₹42.99L (51.8%) | **₹61.72L (74.4%)** |
| via Razorpay Payment Links | – | – | ₹53.21L |
| via human after escalation | – | – | ₹3.32L |
| discount written off | – | – | ₹0.22L |
| messages sent | 0 | 1,265 | **294** |
| messages per ₹1L recovered | – | 29.4 | **4.8** |
| conduct violations | 0 | **934** | **0** |
| out-of-policy proposals blocked | – | – | 1 (injected) |
| gateway outages handled | – | – | 2 (injected) |
| handed to a human | – | – | 34, each with a reason |

**₹18.73L more than nagging, with 77% fewer messages and zero violations.**

Risk model, trained and evaluated on disjoint debtors: precision 0.74, recall 1.00, F1 0.85 on 41
held-out invoices (base rate 0.49).

Reproduce in five seconds, no keys needed:

```bash
uv run python -m baaki run --demo-faults
```

**What these numbers are.** The debtors are simulated ([`simulator.py`](baaki/simulator.py)) using
six behavioural archetypes; the agent never sees the archetype, only visible ledger history. This
proves the mechanics, not real-world lift. The Razorpay path is already real: test-mode SDK, signed
webhooks, idempotent credit.

### Rules playbook vs an LLM brain

The same run on `gpt-5-mini`, one live API call per decision, 335 calls, $0.40.

| | rules playbook | gpt-5-mini |
|---|---:|---:|
| recovered | **74.4%** | 64.0% |
| messages sent | 294 | 301 |
| conduct violations | 0 | 0 |
| handed to a human | 34 | 40 |

**The deterministic playbook won.** The LLM sent a comparable number of messages and recovered
₹8.7L less. The reason is in its action mix: 198 reminders and 52 escalations, against only 30
payment links, 2 installment plans and 2 discounts. It reaches for a reminder where the playbook
reaches for a link or a plan. And 20 of its 52 escalations cite the three-day contact gap as the
reason: it read "this action is blocked today" as "a human should take this", where the playbook
simply waits a day.

What it is clearly better at is explaining itself. Every escalation carries a specific rationale a
human can act on, quoting the customer and the rule, rather than a template string. That is the
argument for using it on reply handling and message drafting while leaving sequencing to the
playbook.

Both ran zero conduct violations through the same gate, and both absorbed the injected day-3 LLM
outage: 69 decisions fell back to the rules brain and the run continued.

Swapping the brain also found a real bug that the rules path could not have. The playbook always
creates a payment link first, so `{link}` was always fillable. The LLM often sends a reminder first,
which exposed messages reading *"pay securely here: (link pending)"*. Reminders now create a link,
and the send boundary refuses any message with an unfilled placeholder.

## Why it is safe to let loose on money

```
brain (LLM or rules) ──▶ Policy gate ──▶ Toolbox ──▶ Razorpay ──▶ ledger
       │                     │ blocked      │                       │
       └── rationale ────────┴── verdict ───┴─── every call ────────┴──▶ hash-chained audit
```

**Policy bounds** ([`policy.py`](baaki/policy.py)). Contact only 08:00 to 19:00 IST. Minimum 3 days
between contacts, maximum 6 per invoice. Discounts capped at 5% and only after 21 days overdue.
At most 3 installments with 25% upfront. Prohibited phrases (police, family, employer, ultimatums)
blocked unconditionally. Every rule has an id like `P-DISC-01` that appears in the audit log when
it fires.

**Stopping rules.** Paid, stop. Dispute, escalate. Hardship, offer a plan or escalate. "Stop
contacting us", honoured immediately. Four contacts without resolution, stop and hand over with the
amount outstanding.

**The LLM cannot bypass any of it.** The model gets a pre-filtered list of allowed actions and
returns structured JSON. The Toolbox re-runs the policy on whatever comes back. `--demo-faults`
injects a rogue decision (15% discount plus a threatening message); it is blocked as `P-DISC-01` and
never sent.

**Failure is boring.** Razorpay 503 retries three times, then defers a day without counting a
contact. LLM errors, timeouts, refusals or invalid output fall back to the deterministic rules brain
for that decision and log the cause. Duplicate webhooks are idempotent on payment id. Bad signatures
are rejected.

**Auditability.** `reports/audit_*.jsonl` is append-only and hash-chained. `python -m baaki audit
<file>` proves it has not been edited.

## Two things live here

**The submission**, a measured simulation, above.

**The product**, a multi-tenant web app with accounts, CSV import, an approval queue, per-org
guardrails and Razorpay subscription billing, on the same policy engine. See
[PRODUCT.md](PRODUCT.md).

```bash
uv run python -m baaki demo   # seed a tenant with a realistic ledger
uv run python -m baaki app    # http://127.0.0.1:8080 · demo@baaki.app / baaki-demo-2026
```

## Run it

```bash
uv sync
uv run python -m baaki run --demo-faults   # three strategies, injected faults
uv run python -m baaki serve               # simulation dashboard on :8000
uv run pytest                              # 90 tests
```

Optional LLM. The agent is provider-agnostic; both brains get the same prompt, schema and
allowed-action list, and both are re-checked by the same gate.

```bash
export OPENAI_API_KEY=sk-...          # --brain openai, default gpt-5-mini
export ANTHROPIC_API_KEY=sk-ant-...   # --brain claude, default claude-opus-5

uv run python -m baaki run --brain openai --demo-faults
uv run python -m baaki run --brain openai --model gpt-5-nano --max-llm-calls 50
```

A missing or invalid key completes the run on the rules brain and says so. `--max-llm-calls` is a
hard spend cap: past it the run finishes deterministically rather than stopping, so a capped run
still produces complete numbers. Every run reports tokens and an estimated cost.
`uv run python -m baaki doctor` checks which keys and models are actually reachable before you spend
anything.

Optional Razorpay. Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` (test mode only, live keys are
refused) and the Toolbox creates real Payment Links. Point a webhook at `/webhooks/razorpay`.

## How it works

1. **Ledger.** 120 overdue invoices across 40 debtors, generated deterministically
   ([`data.py`](baaki/data.py)). Payment history is visible; behavioural archetype is not.
2. **Risk model** ([`risk.py`](baaki/risk.py)). Six-feature logistic regression trained on the
   do-nothing baseline, split by debtor. Ranks which rupees to work first.
3. **Daily loop** ([`runner.py`](baaki/runner.py)). Deliver payments and replies, build a context,
   ask the brain, apply reply intent, gate, execute, audit.
4. **Brains** ([`brain.py`](baaki/brain.py)). `RuleBrain`, `ClaudeBrain`, `OpenAIBrain`,
   `ResilientBrain` (LLM with rules fallback), plus two baselines.
5. **Toolbox** ([`tools.py`](baaki/tools.py)). The only code that contacts a debtor or calls
   Razorpay.
6. **Webhooks** ([`webhooks.py`](baaki/webhooks.py)). HMAC verification, idempotent credit,
   settlement write-off.

[ARCHITECTURE.md](ARCHITECTURE.md) has the diagram and the design decisions.
[docs/SUBMISSION.md](docs/SUBMISSION.md) has the pitch script.

## Layout

```
baaki/
  domain.py       types; paise everywhere
  policy.py       the guardrails and stopping rules
  tools.py        gated actions and Razorpay calls
  brain.py        rules, Claude, OpenAI, resilient wrapper, baselines
  razorpay_client.py  real SDK wrapper and a faithful fake with signed webhooks
  webhooks.py     signature verification, idempotent credit
  simulator.py    hidden debtor behaviour
  risk.py         logistic regression, held-out metrics
  runner.py       daily loop, metrics, baseline comparison
  server.py       simulation dashboard
  app/            the product: models, auth, service, billing, web, templates
migrations/       alembic
tests/            90 tests
```
