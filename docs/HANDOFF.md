# Baaki — session handoff

Paste this into a new conversation to continue work.

## What this is

**Baaki**: AI agent that recovers overdue B2B invoices for Indian SMEs. Razorpay AI Buildathon
submission, Track 3 (AI Revenue Recovery), student hiring program, deadline 5 Sept 2026.
Repo: **https://github.com/7arj/baaki** (public, branch `main`, all work committed and pushed).
Local checkout: `~/Desktop/buildathon/baaki`. Python 3.12 via uv. **92 tests, all passing.**

Two things live in the repo:
1. **The simulation** (the measured submission): `uv run python -m baaki run --demo-faults`
   compares do-nothing vs naive reminders vs the agent on a 120-invoice synthetic ledger.
   Headline: 74.4% recovered vs 51.8% naive, zero conduct violations. Runs in ~5s, no keys.
2. **The product**: multi-tenant FastAPI web app (accounts, CSV import, approval queue, per-org
   guardrails, Razorpay subscription billing) on the SAME policy engine.

## Architecture in one paragraph

Any brain (RuleBrain / OpenAIBrain / ClaudeBrain, all sharing one prompt+schema) proposes a
`Decision`; `Toolbox.execute` re-validates it against `Policy` (contact hours 8-19 IST, gaps,
caps, discount limits, forbidden phrases — rule ids like P-DISC-01) and only then acts (Razorpay
Payment Links). Everything lands in a hash-chained audit log (file for sim, per-org DB table for
product). Stopping rules: dispute/hardship/cease/max-contacts → escalate or stop with a reason.
`ResilientBrain` falls back to rules on any LLM failure. Key files: `baaki/policy.py`,
`baaki/tools.py`, `baaki/brain.py`, `baaki/runner.py` (sim), `baaki/app/*` (product:
models/service/web/clerk_auth/billing/transports/demo), `migrations/` (alembic).

## How to run

```bash
cd ~/Desktop/buildathon/baaki
uv run pytest -q                          # 92 tests
uv run python -m baaki run --demo-faults  # simulation table (~5s, free, uses fake Razorpay)
uv run python -m baaki demo               # reseed demo tenant (see gotchas!)
uv run python -m baaki app                # product at http://127.0.0.1:8080
uv run python -m baaki doctor             # checks all keys/config
```

Demo login: `demo@baaki.app` / `baaki-demo-2026` — with Clerk enabled use
**http://127.0.0.1:8080/login?local=1** (Clerk's widget doesn't know local accounts).

## Credentials (all in gitignored `.env`, never committed)

- `OPENAI_API_KEY` — valid; default model `gpt-5-mini` ($0.40/full run; gpt-5 costs $3, avoid).
  `--max-llm-calls N` is a hard spend cap. User budget-conscious: ~$2.2 spent total.
- `RAZORPAY_KEY_ID`/`SECRET` — valid **test-mode** keys (live keys refused by code). Account has
  **no KYC → payment links over ₹50,000 are rejected**; demo ledger amounts sit under it.
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` + `CLERK_SECRET_KEY` — valid; Google sign-in works.
  Instance: enabling-basilisk-8884.clerk.accounts.dev.
- User should **rotate Razorpay+OpenAI keys after the buildathon** (they passed through chat).

## Live-payment demo wiring (currently TORN DOWN — servers/tunnel killed)

To restore the pay-a-real-link-and-watch-it-reconcile flow:
1. `uv run python -m baaki app` (port 8080)
2. `cloudflared tunnel --url http://127.0.0.1:8080` (installed via brew; free quick tunnel)
3. Re-point Razorpay webhook **TYMB29Zf4E9GIB** to `<tunnel-url>/webhooks/razorpay/sharma-industrial-supplies`
   via PUT https://api.razorpay.com/v1/webhooks/{id} (basic auth = API keys; body JSON:
   url, secret, events {"payment_link.paid":true,"payment_link.partially_paid":true}).
   The secret MUST be the org's stored one: decrypt `org.rzp_webhook_secret_enc` with
   `baaki.app.security.decrypt_secret`. Keep the same secret; only the URL changes.
4. Test card: 4111 1111 1111 1111, any future expiry/CVV; UPI `success@razorpay`.
- Note: user's ISP blocks `*.trycloudflare.com` on port-53 DNS; Razorpay resolves it fine
  (verified via DoH). Local curl needs `--resolve` pinning to 104.16.230.132.
- Delete webhook TYMB29Zf4E9GIB from the Razorpay dashboard after the buildathon.

## Critical gotchas (each cost real debugging time)

1. **Reseeding wipes the org's webhook secret.** Before `rm data/baaki.db* && baaki demo`,
   decrypt+save the secret from the org row, reseed, re-encrypt it back — else the registered
   webhook signature-fails silently. (Pattern used 3x this session.)
2. **Contact-hours guardrail vs clock**: engine takes `at=` datetime; simulated days run at
   10:00 IST, live runs use wall clock. tests/conftest.py pins tests to 10:00. Don't remove.
3. **`.env` had no trailing newline once** — an append glued keys into one line. Check after edits.
4. **`audit.record(event=...)` kwarg collides** with the positional param → 500s. Use
   `webhook_event=` etc. Bit twice.
5. `baaki run` (sim) always uses FakeRazorpay regardless of env keys — safe/fast for demos.
6. Razorpay SDK reports HTTP 429 as BadRequestError("Too many requests") — mapped to retryable
   in `razorpay_client.py`; keep backoff (real mode only).
7. Old real payment links from previous seeds may still be `created` on Razorpay — cancel them
   when reseeding or they can pay-credit same-numbered new invoices via notes.invoice_id.

## Demo-state surgery (for re-recording scenes)

To undo a recorded action (e.g. a test reply on an invoice): delete its inbound Event row, reset
`inbound_pending/last_inbound_text/next_action_on` on InvoiceRow, and delete the trailing
`inbound_recorded` AuditRow **only if it's the org-chain tail** (then the hash chain stays intact
— verify with `baaki.app.service.verify_chain`). Repair dead links: create a new payment link
with notes.invoice_id and update the row.

## Submission status (form: https://forms.gle/d9r2gvxp8cmoZhon9)

Done: repo public+clean (no Claude co-author, single author identity), README with measured
numbers incl. honest gpt-5-mini-lost-to-rules comparison (64% vs 74%), PRODUCT.md, ARCHITECTURE.md,
docs/SUBMISSION.md (pitch script + form answers were drafted in-chat).
Remaining: record video (script in docs/SUBMISSION.md; simpler spoken version was provided in
chat), put video link atop README, submit form.
Video notes: sim demo = rules brain (LLM run takes 25min, not demoable live); payment scene needs
tunnel+webhook restored first; scenes that mutate state (live reply, payment) are one-take.

## Known open gaps (documented in PRODUCT.md)

Live billing + WhatsApp untested against production (need registered business); background work
is locked-cron not a queue; no SSO/retention/bulk-export; single currency/region (INR/IST).
