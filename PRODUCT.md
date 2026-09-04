# Baaki as a product

The submission is a measured simulation ([README](README.md)). This is the layer that makes it
something a merchant can sign up for and pay for.

```bash
uv run python -m baaki demo     # seed a tenant with a realistic ledger
uv run python -m baaki app      # http://127.0.0.1:8080 · demo@baaki.app / baaki-demo-2026
```

The simulation is untouched and still runs.

## What a merchant does

1. **Sign up.** Business name, email, password. 14-day trial, no card.
2. **Import a ledger.** CSV out of Tally, Zoho or Excel. The whole file is validated first: one bad
   row and nothing is imported, with the offending row numbers returned. A half-imported ledger is
   worse than none.
3. **Set guardrails.** Contact window, gap, cap, discount ceiling and ageing, installment limits.
   Tighten freely; the form refuses to loosen past defensible limits, and prohibited language cannot
   be switched off.
4. **Turn the agent on**, with "I approve every message" on by default.
5. **Review the approval queue.** Each drafted message shows the invoice, the action, who decided it
   and why. Edit before sending; edits are audited against the original.
6. **Watch money arrive.** Payment-link webhooks credit invoices idempotently.
7. **Work the exception list.** Disputes, hardship and cease requests the agent stopped on, largest
   first, with the customer's own words.
8. **Turn approvals off** once it is trusted, and let `baaki worker` run from cron.

## What makes it a product

**Multi-tenancy.** Every business row carries `org_id` and there are no ORM relationships, so each
read states its tenancy filter explicitly and a missing one is visible at the call site. Object
reads re-check ownership before returning.

**Auth.** scrypt passwords. Opaque server-side session tokens in an httponly SameSite=Lax cookie,
revocable on sign-out. Double-submit CSRF on every mutating form. Login failures are
indistinguishable between "no such account" and "wrong password", and the no-account branch still
burns a hash so timing does not leak either. Attempts are throttled per email (6) and per IP (20)
per 15 minutes, counted in the database so limits hold across workers.

Email verification and password reset use single-use expiring tokens stored only as SHA-256, so a
database leak hands over no live links. A reset revokes every other session. **The agent cannot be
switched on until the owner's email is confirmed**, because we will not contact a merchant's
customers on behalf of an unverified account.

**Teams.** Owners manage billing, credentials, guardrails and the team. Members work the ledger,
approvals and audit. Invitations expire in 7 days and can be revoked. An org cannot lose its last
active owner, and disabling someone revokes their live sessions immediately.

**Secrets.** Merchant Razorpay secrets are Fernet-encrypted at rest and never rendered back. Live
keys are refused outright. Set `BAAKI_SECRET_KEY` in production; there is a marked dev fallback.

**Approval workflow.** Nothing reaches a customer without passing the policy gate and, by default, a
human. The Outbox is both the approval queue and the delivery retry queue: persisted before any send
is attempted, retried up to five times, with `sent_at` preventing a double-send.

**Delivery.** WhatsApp first when configured and the customer has a number, otherwise email.
Business-initiated WhatsApp messages must use a Meta-approved template, so the reminder is passed as
template parameters rather than free text. Permanent failures such as a malformed number fail
immediately instead of burning five retries.

**Billing.** Razorpay Subscriptions, the same rails Baaki helps merchants collect on. Plan limits
are enforced at import. A cancelled subscription disables the agent immediately, while the ledger
and audit trail stay exportable.

**Risk scores that admit ignorance.** Four of six features describe how a customer paid previously.
On a fresh ledger there is none, the model collapses to its bias term, and a uniform number
presented as a prediction is worse than none. So it returns `None`, the UI shows a dash, and work is
ranked by outstanding times ageing. Once an org has 40+ settled invoices with 8+ late ones,
**Settings, Refit on my ledger** fits on their own payers, split by customer so nobody teaches and
grades, and shows held-out precision and recall next to the button.

**Migrations.** Alembic with batch mode for SQLite. `baaki migrate` creates and stamps a fresh
database, adopts a pre-Alembic one rather than wrecking it, or upgrades. A test walks the chain up,
down to base, and up again.

**Audit.** Hash-chained per org, covering decisions, policy verdicts, Razorpay calls, payments,
message approvals and edits, policy changes and credential updates. Verifiable in Settings,
exportable as JSONL.

**Operations.** `baaki worker` runs the daily pass for every eligible org and flushes the outbox.
It takes a per-org advisory lock so two workers cannot double-send, reclaims stale locks, and prunes
expired throttle rows. `/healthz` for liveness. SQLite by default with WAL, or
`DATABASE_URL=postgresql://...`.

## One policy engine, not two

The engine converts database rows to the same `domain.Invoice` the simulation uses, runs them
through the same `Policy`, `Toolbox` and brains, and writes back. There is no demo policy and real
policy that could drift apart. The only core additions were an optional outbound-message hook and a
per-instance merchant name.

## Deploying

```bash
export BAAKI_ENV=production
export BAAKI_SECRET_KEY="$(openssl rand -base64 32)"
export DATABASE_URL=postgresql://...                        # optional
export SMTP_HOST=... SMTP_USER=... SMTP_PASSWORD=...        # optional
export WHATSAPP_PHONE_NUMBER_ID=... WHATSAPP_TOKEN=...      # optional
export BAAKI_RZP_KEY_ID=... BAAKI_RZP_KEY_SECRET=...        # platform keys for billing

uv run python -m baaki migrate
uv run uvicorn baaki.app.web:app --host 0.0.0.0 --port 8080
uv run python -m baaki worker    # from cron, daily
```

Each merchant points a Razorpay webhook at `/webhooks/razorpay/<org-slug>`, verified against their
own secret.

## Known gaps

- **Live billing and WhatsApp are untested against production infrastructure.** Both need a
  registered business account rather than a test key. The logic is tested against synthetic events;
  the HTTP calls are not.
- **Background work is a locked cron command, not a queue.** Fine to a few thousand invoices per org.
- **No SSO, retention policy or bulk export** beyond audit JSONL and CSV re-import. All three are
  table stakes for the Scale tier as priced.
- **One region, one currency.** Amounts are paise, the contact window is IST, and the policy engine
  is not currency-aware.
