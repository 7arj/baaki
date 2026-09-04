# Baaki (बाकी) — bounded, auditable AI recovery of overdue SME receivables

> **Razorpay AI Buildathon · Track 3: AI Revenue Recovery.**
> Built for the #1 problem on Razorpay's Fix My Itch board — *"Why can't SMEs negotiate favourable payment terms with large buyers?"* (Itch Score 82.8) — and its neighbours: *"micro-SMEs waste 10+ hours a week on invoice management"* (67.5) and *"freelancers/vendors get ghosted after partial payments"* (76).

Indian SMEs are owed lakhs in overdue B2B invoices and chase them by hand: WhatsApp nagging, awkward calls, and eventually giving up. Baaki is an agent that works the overdue ledger the way a good collections person would — a friendly reminder with a Razorpay Payment Link, then partial payments or a bounded installment plan, a small early-settlement discount when the cap allows, and a clean hand-off to a human the moment a debtor disputes, reports hardship, or asks to be left alone — and it **can't** step outside the merchant's policy or RBI collection-conduct norms, because every action is gated and every step is hash-chain audited.

## Measured result (same 120-invoice ledger, same simulated debtors, 45 days)

| | do nothing | naive reminders every 3 days | **Baaki** |
|---|---:|---:|---:|
| recovered of ₹82.95L receivable | ₹26.58L (32.0%) | ₹42.99L (51.8%) | **₹61.72L (74.4%)** |
| via Razorpay Payment Links | – | – | ₹53.21L |
| via human after escalation | – | – | ₹3.32L |
| discount written off to settle | – | – | ₹0.22L |
| messages sent | 0 | 1,265 | **294** |
| messages per ₹1L recovered | – | 29.4 | **4.8** |
| collection-conduct violations | 0 | **934** | **0** |
| out-of-policy proposals blocked at the gate | – | – | 1 (injected) |
| gateway outages handled | – | – | 2 (injected) |
| handed to a human (escalated / stopped) | – | – | 9 / 25, each with a reason |

Incremental cash vs doing nothing: **₹35.14L**. Vs the nagging baseline: **₹18.73L, with 77% fewer messages and zero violations.**
Risk model (predicts "won't pay in 30 days without intervention", trained/evaluated on disjoint debtors): **precision 0.74, recall 1.00, F1 0.85** on 41 held-out invoices (base rate 0.49).

Reproduce in ~5 seconds, no keys needed: `uv run python -m baaki run --demo-faults` → `reports/REPORT_rules.md`.

**Be clear about what these numbers are.** The debtors are simulated (six behavioural archetypes with fixed probabilities, [`baaki/simulator.py`](baaki/simulator.py)); the agent never sees the archetype, only the visible ledger history. The numbers prove the *mechanics* — prioritisation, bounded negotiation, stopping rules, resilience — not real-world lift. Real-world lift needs a pilot; the code path is already the real one (Razorpay test-mode APIs, real webhook verification).

## What makes it safe to let loose on money

**Every money action is bounded, gated, and explained.**

```
brain (LLM or rules) ────proposes──▶ Policy gate ──allowed──▶ Toolbox ──▶ Razorpay ──webhook──▶ ledger
        │                                │ blocked                 │                         │
        └── rationale ───────────────────┴──── verdict + rule id ──┴─── every call ──────────┴──▶ hash-chained audit log
```

- **Policy bounds** ([`policy.py`](baaki/policy.py)): contact only 08:00–19:00 IST, ≥3 days between contacts, ≤6 automated contacts per invoice, discount ≤5% and only after 21 days overdue, ≤3 installments with ≥25% upfront, link expiry ≤14 days, no partial below 20%, and a prohibited-phrase check (police, family, employer, ultimatums…). Each rule has an id (`P-DISC-01`) that appears in the audit log when it fires.
- **Stopping rules**: fully paid → stop; dispute → escalate, no more automated contact; hardship → plan if allowed else escalate; "stop contacting us" → honoured immediately; 4 contacts with no resolution → stop and put on the human's exception list with the outstanding amount.
- **The LLM cannot bypass any of this.** The model receives the pre-filtered list of allowed actions and returns structured JSON (action, params, message, rationale, reply intent). The Toolbox re-runs the policy on whatever comes back. Try it: `--demo-faults` injects a rogue decision (15% discount + a threatening message) — it's blocked with `P-DISC-01` and never sent.
- **Graceful failure**, demonstrated and tested:
  - Razorpay returns 503 → 3 retries, then the invoice is deferred a day *without* counting a contact; the audit shows each attempt.
  - LLM errors / timeouts / refusals / schema-invalid or out-of-policy output → falls back to the deterministic rules brain for that decision, recorded as `brain_fallback` with the cause. A missing or invalid key degrades the whole run to rules and says so. Nothing stalls.
  - Duplicate webhook delivery → idempotent on payment id. Bad signature → rejected and logged.
- **Auditability**: `reports/audit_*.jsonl` is append-only and hash-chained; `python -m baaki audit <file>` proves it hasn't been edited. The dashboard shows, per invoice, every decision, verdict, API call and payment.

## Run it

```bash
uv sync                                   # Python 3.12+, installs openai, anthropic, razorpay, fastapi
uv run python -m baaki run --demo-faults  # do-nothing vs naive vs agent, rules brain, injected faults
uv run python -m baaki serve              # dashboard at http://127.0.0.1:8000
uv run pytest                             # 19 tests: policy, audit chain, webhooks, simulation, LLM brain
```

With an LLM (optional) — the agent is provider-agnostic, so pick either:

```bash
export OPENAI_API_KEY=sk-...        # then --brain openai   (default model gpt-5)
export ANTHROPIC_API_KEY=sk-ant-... # then --brain claude   (default model claude-opus-5)

uv run python -m baaki run --brain openai --demo-faults
uv run python -m baaki run --brain openai --model gpt-5.1 --effort medium
```

Both brains get the **same** system prompt, the same JSON schema, and the same allowed-action list, and both are re-checked by the same policy gate — only the transport differs. `--demo-faults` blacks out the LLM on day 3 so you can watch the fallback happen. If the key is missing, invalid, or the model name doesn't exist, the run completes on the rules brain and says so rather than crashing. To see which models your key can reach: `curl https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY"`.

With Razorpay test mode (optional): set `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` (`rzp_test_*` only — live keys are refused) and the Toolbox creates real Payment Links through the official SDK; point a Dashboard webhook at `POST /webhooks/razorpay` and the same signature-verified handler ingests `payment_link.paid` / `payment_link.partially_paid`.

## How it works, briefly

1. **Ledger** — 120 overdue invoices across 40 debtors, generated deterministically ([`data.py`](baaki/data.py)). Debtor history (late ratio, average days late, partial-payment history) is visible; the behavioural archetype is not.
2. **Risk model** ([`risk.py`](baaki/risk.py)) — a 6-feature logistic regression trained on labels from the do-nothing baseline, split by debtor. Used to work the riskiest rupees first and to allow partial payment on the first link for high-risk debtors.
3. **Daily loop** ([`runner.py`](baaki/runner.py)) — deliver payments and replies, then for each invoice that's due for attention build a context (visible facts + allowed actions + policy bounds), ask the brain, apply the reply intent, gate, execute, audit.
4. **Brains** ([`brain.py`](baaki/brain.py)) — `RuleBrain` (deterministic playbook), `ClaudeBrain` and `OpenAIBrain` (read the reply and history, draft the message, pick the action — same prompt, same schema, different SDK), `ResilientBrain` (an LLM with rules fallback), plus the two baselines.
5. **Toolbox** ([`tools.py`](baaki/tools.py)) — the only code that contacts debtors or calls Razorpay. Creates links (`accept_partial`, `first_min_partial_amount`, `expire_by`, `notes.invoice_id`), cancels superseded links, composes messages from templates or the LLM draft with the `{link}` placeholder filled in.
6. **Webhooks** ([`webhooks.py`](baaki/webhooks.py)) — HMAC-SHA256 verification of `X-Razorpay-Signature`, idempotent credit, settlement write-off handling.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagram and the design decisions, and [docs/SUBMISSION.md](docs/SUBMISSION.md) for the pitch script.

## Honest limitations

- Debtor behaviour is simulated; the archetype mix and response probabilities are my assumptions, not data. The relative ordering (agent > naive > nothing) is robust across seeds; the absolute percentages are not a forecast.
- Messages are "sent" to the audit log; wiring WhatsApp/SMS/email providers is a transport concern I deliberately left out.
- The risk model is tiny by design (6 features, pure Python) so it's inspectable; recall 1.0 at threshold 0.5 comes with 26% false positives, which only costs a partial-payment option being offered early.
- The rules-brain run is what's in the tables above. Both LLM brains (`--brain openai`, `--brain claude`) are fully wired and fall back cleanly — verified against a stubbed client and against a deliberately invalid key (335 calls, 335 fallbacks, run still completed) — but I had no live API key on this machine, so LLM numbers aren't reported here.
- GST invoices can't be created through the Invoices API; Baaki uses Payment Links (which carry `reference_id` and `notes`) instead.

## Layout

```
baaki/
  domain.py       types; paise everywhere
  policy.py       the guardrails and stopping rules (rule ids)
  tools.py        gated actions + Razorpay calls
  brain.py        RuleBrain, ClaudeBrain, OpenAIBrain, ResilientBrain, baselines, fault injection
  razorpay_client.py  real SDK wrapper + faithful fake with signed webhooks
  webhooks.py     signature verification, idempotent credit
  simulator.py    hidden debtor behaviour (the only reader of archetype)
  risk.py         logistic regression, held-out metrics
  runner.py       daily loop, metrics, baseline comparison
  server.py       FastAPI dashboard + webhook endpoint; static/index.html
tests/            19 tests
reports/          generated: REPORT_*.md, summary_*.json, run_*.json, audit_*.jsonl
```
