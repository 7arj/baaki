# Baaki as a product

The buildathon submission is a measured simulation ([README](README.md)). This document covers the
layer that turns it into something a merchant can sign up for and pay for: multi-tenant persistence,
authentication, a human approval workflow, per-org guardrails, subscription billing and an operator UI.

```bash
uv run python -m baaki demo     # seed a tenant with a realistic ledger, payments and escalations
uv run python -m baaki app      # http://127.0.0.1:8080 → sign in as demo@baaki.app / baaki-demo-2026
```

The simulation is untouched and still runs: `uv run python -m baaki run --demo-faults`.

## What a merchant actually does

1. **Sign up** — business name, email, password. A 14-day trial starts; no card.
2. **Import a ledger** — CSV out of Tally/Zoho/Excel. The whole file is validated first: one bad row
   and nothing is imported, with the offending row numbers returned. (A half-imported ledger is worse
   than none — the agent would chase invoices that don't reconcile.)
3. **Set the guardrails** — contact window, gap between contacts, contact cap, discount ceiling and
   ageing, installment limits. Tighten freely; the form refuses to loosen past defensible limits, and
   prohibited language can't be switched off at all.
4. **Turn the agent on** — with *"I approve every message"* on by default.
5. **Review the approval queue** — each drafted message shows the invoice, the action, who decided it
   (templates, OpenAI or Claude) and *why*. Edit before sending; edits are audited against the original.
6. **Watch money arrive** — Razorpay payment-link webhooks credit invoices idempotently.
7. **Handle the exception list** — disputes, hardship and cease requests the agent deliberately
   stopped on, largest first, with the customer's own words.
8. **Turn approvals off** once it's trusted, and let `baaki worker` run it from cron.

## The parts that make it a product, not a demo

**Multi-tenancy.** Every business row carries `org_id`, and there are no ORM relationships anywhere —
each read states its tenancy filter explicitly, so a missing one is a visible omission at the call
site rather than a silent lazy-load. Object reads re-check ownership before returning
(`baaki/app/web.py` — `if not row or row.org_id != p.org_id: 404`). Covered by
`test_one_org_cannot_read_anothers_invoice`.

**Auth.** scrypt password hashing (stdlib, memory-hard, ~100 ms/hash). Opaque server-side session
tokens in an httponly/SameSite=Lax cookie — revocable on sign-out, unlike a stateless JWT.
Double-submit CSRF on every mutating form. Login failures are indistinguishable between "no such
account" and "wrong password", and the no-account branch still burns a hash so timing doesn't leak
either.

**Secrets.** A merchant's Razorpay key secret and webhook secret are Fernet-encrypted at rest and
never rendered back. Live keys (`rzp_live_…`) are refused outright. Set `BAAKI_SECRET_KEY` in
production — there is a derived dev fallback so nothing breaks locally, and it is clearly marked.

**Approval workflow.** Nothing reaches a customer without passing the policy gate *and*, by default,
a human. The Outbox is both the approval queue and the delivery retry queue: a message is persisted
before any send is attempted, retried up to 5 times, and `sent_at` prevents a double-send.

**Billing.** Razorpay Subscriptions — Baaki bills merchants on the same rails it helps them collect
on. Plan limits are enforced at import time. A cancelled subscription disables the agent immediately:
we stop acting on a merchant's behalf the moment they stop paying, while their ledger and audit trail
stay exportable.

**Audit.** Hash-chained per org, in the database, covering agent decisions, policy verdicts, Razorpay
calls, payments, message approvals and edits, policy changes and credential updates. Verifiable from
the Settings page and exportable as JSONL.

**Operations.** `baaki worker` runs the daily pass for every eligible org and flushes the outbox —
point cron at it. `/healthz` for liveness. SQLite by default (a merchant can self-host with zero
infrastructure) with WAL enabled; `DATABASE_URL=postgresql://…` switches to Postgres unchanged.

## One policy engine, not two

The engine converts database rows to the same `domain.Invoice` the simulation uses, runs them through
the **same** `Policy`, `Toolbox` and brains, and writes the results back. There is no "demo" policy and
"real" policy that could drift apart — `baaki/app/service.py` imports from `baaki.policy` and
`baaki.tools` directly. The only additions to the core were an optional outbound-message hook and a
per-instance merchant name, both of which the simulation leaves at their defaults.

## Deploying

```bash
export BAAKI_ENV=production          # switches cookies to Secure
export BAAKI_SECRET_KEY="$(openssl rand -base64 32)"
export DATABASE_URL=postgresql://…   # optional; SQLite otherwise
export SMTP_HOST=… SMTP_USER=… SMTP_PASSWORD=… SMTP_FROM=…   # optional; console transport otherwise
export BAAKI_RZP_KEY_ID=… BAAKI_RZP_KEY_SECRET=…             # platform keys, for subscription billing
export BAAKI_RZP_PLAN_STARTER=plan_… BAAKI_RZP_PLAN_GROWTH=plan_… BAAKI_RZP_PLAN_SCALE=plan_…

uv run uvicorn baaki.app.web:app --host 0.0.0.0 --port 8080
uv run python -m baaki worker        # from cron, once a day
```

Each merchant points a Razorpay webhook at `/webhooks/razorpay/<their-org-slug>`; the signature is
verified against that org's own secret.

## Honest gaps

These are real and I'd rather name them than have them found.

- **No schema migrations.** Tables are created with `create_all`. A second release that changes a
  column needs Alembic before it can be deployed over existing data.
- **Risk scores need history.** Four of the model's six features describe how a customer paid
  *previously*. On a freshly imported ledger there is none, the model collapses to its bias term, and
  a uniform number presented as a prediction would be worse than none — so it returns `None`, the UI
  shows "—", and work is ranked by outstanding × ageing until history exists. Per-org refitting is
  the obvious next step; the weights currently ship as a prior from the simulation's held-out run.
- **Email only.** WhatsApp is where Indian B2B collections actually happen. The transport interface
  is there; the WhatsApp Business API adapter is not.
- **No rate limiting** on login, and no email verification or password reset. Fine behind a trusted
  proxy for a pilot; not fine on the open internet.
- **Single-user orgs in practice.** The `Role` enum and `org_id` scoping support teams, but there is
  no invite flow yet.
- **Subscription billing is sandboxed** unless platform keys are configured, in which case it creates
  real Razorpay subscriptions. The webhook path that activates and cancels them is implemented and
  tested; it has not been run against live Razorpay.
- **Background work is a cron command**, not a queue. Fine to a few thousand invoices per org; beyond
  that the daily pass wants a real worker with per-org locking.
