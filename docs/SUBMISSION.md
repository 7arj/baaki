# Submission kit — Razorpay AI Buildathon (Track 3: AI Revenue Recovery)

**Applications close 5 September.** Form: https://forms.gle/d9r2gvxp8cmoZhon9

## Checklist
- [ ] Push this repo to public GitHub (`gh repo create baaki --public --source=. --push`)
- [ ] Record the 5-minute pitch (script below), upload unlisted to YouTube/Drive
- [ ] Put the video link at the top of README
- [ ] Optional but strong: run once with `--brain claude` and paste `reports/REPORT_claude.md` numbers into README
- [ ] Optional: create Razorpay test keys, set env, run `serve`, and screenshot a real `plink_…` in the dashboard
- [ ] Fill the form: track = AI Revenue Recovery, repo link, video link, one-paragraph architecture (copy "What makes it safe" from README)

## 5-minute pitch script

**0:00 – 0:40 · The itch.** "The #1 problem on Razorpay's own Fix My Itch board is SMEs unable to get paid on their terms. Every small supplier I know chases invoices on WhatsApp for hours a week and writes off what they can't chase. Baaki is a recovery agent that does that work — and, more importantly, *can't* do it badly."

**0:40 – 1:40 · Live demo, terminal.** Run `uv run python -m baaki run --demo-faults`. Walk the table: same ledger three ways. "Do nothing: 32%. Naive nagging: 52%, but 1,265 messages and 934 conduct violations. Baaki: 74%, 294 messages, zero violations. ₹18.7 lakh more than nagging with a quarter of the messages." Point at the exceptions list: "34 invoices it deliberately *stopped* working on, each with a reason and an amount for a human."

**1:40 – 2:50 · Dashboard.** `serve`, open an escalated invoice (e.g. the ₹2.4L Mehta Traders dispute). Show timeline: link created day 0 → reply "20 cartons short-shipped" → intent classified dispute → escalated → human resolves → payment arrives day 9 via bank transfer. Scroll the audit trail: `policy_check` with verdict, `razorpay_call` with the `plink_` id, hash-chained.

**2:50 – 3:50 · The bar: bounded, gated, explainable.** Open `policy.py` for 20 seconds: rule ids, RBI 8-to-7 window, discount cap, forbidden phrases. Then the fault demo in the audit log: the injected rogue decision (15% discount + "inform your family") blocked as `P-DISC-01`; Razorpay 503 → three attempts → deferred without a contact; (if Claude key) day-3 LLM outage → `brain_fallback` → run continues. "Every money action goes through one gate. The LLM proposes; it never executes."

**3:50 – 4:30 · Measured honestly.** Risk model: trained on the do-nothing run, evaluated on debtors it never saw — precision 0.74, recall 1.0. "Debtors are simulated; I'm claiming the mechanics work, not a forecast. The Razorpay path is real: test-mode SDK, signed webhooks, idempotent credit — flip an env var."

**4:30 – 5:00 · Why me.** "I built the guardrails before the model, measured against two baselines, and made failure boring. That's how I'd want to build inside Razorpay."

## Likely interview questions
- *Why not let the LLM set amounts?* Policy floors are the merchant's commercial decision; the LLM picks within them. Also keeps every number auditable.
- *How do you know the 74% isn't the simulator flattering you?* Archetype is hidden; naive baseline runs on the same simulator and gets 52%; ordering is stable across seeds (`--seed`). Absolute numbers need a pilot.
- *What happens on a duplicate webhook?* Idempotent on `payment.id` — test covers it.
- *What about RBI applicability?* The FPC formally binds regulated lenders; B2B trade receivables aren't regulated the same way, but the norms (hours, no threats, no third parties) are the right bar for any automated contact, and merchants can tighten `PolicyBounds`.
