# Runbooks

One section per alert in `k8s/base/monitoring.yaml`. Each one follows the same method:
**symptom → hypothesis → evidence → fix → verify**. Start with the cheapest checks.

Useful commands:

```bash
kubectl -n baaki get pods -o wide                     # what is running where, restarts
kubectl -n baaki describe pod <pod>                   # events: probe failures, OOMKilled, scheduling
kubectl -n baaki logs deploy/baaki-web --since=15m | grep '"status": 5'   # failing requests (JSON logs)
kubectl -n baaki get events --sort-by=.lastTimestamp | tail -20
curl -sk https://baaki.localhost/readyz               # can the app reach a migrated database?
curl -sk https://baaki.localhost/version              # which commit is live?
```

Grafana dashboard: **baaki: service health**.

## BaakiErrorBudgetFastBurn

**Page.** Many requests are failing right now (more than 7.2% over 1h and 5m).

1. **Did something just change?** Compare `/version` with the latest `deploy:` commit on `deploy/local`
   and the Argo CD app history. Most incidents follow a change.
2. **Which routes?** Dashboard → "Error ratio" and "Requests per second by route". Logs:
   `kubectl -n baaki logs deploy/baaki-web --since=15m | grep '"status": 5'`. Each line has
   `request_id`, `route` and `version`.
3. **Is the database the cause?** `kubectl -n baaki get pods -l app.kubernetes.io/name=postgres`,
   then `/readyz`.
4. **Mitigate first, then investigate.** If the errors started with a deploy, roll back by re-running
   the previous good run's `deploy` job (docs/DEVOPS.md → Releasing and rolling back).
5. **Verify:** the 5m error ratio drops below 0.5% and the alert resolves (the short window clears it within minutes).

## BaakiErrorBudgetTicket

**Ticket.** A low but steady error rate will use up the 30-day budget in about 10 days. Find the top
failing route in the logs and fix it during working hours. Under the error-budget policy, while the
budget is exhausted only reliability fixes ship.

## BaakiLatencySLOBreach

**Ticket.** Fewer than 95% of requests finish within 500 ms.

- CPU-bound? Dashboard → "CPU per pod vs request". If pods sit at their limit, check whether the HPA is
  at `maxReplicas` (`kubectl -n baaki get hpa`).
- Database-bound? Slow queries show up across every route at once. Check Postgres CPU and memory.
- One route only? Look at that handler and recent changes to it.

## BaakiNoReadyWebPods

**Page.** No web pod passes readiness, so the site is down.

- `kubectl -n baaki get pods`:
  - **CrashLoopBackOff**: read `logs --previous` for a startup error such as a bad config or missing secret.
  - **Running but 0/1**: readiness is failing, so `/readyz` will say why. Usually the DB is unreachable
    or the schema isn't migrated: check the `baaki-migrate` Job.
  - **Pending**: `describe` shows a scheduling problem (quota, resources, node down).
  - **ImagePullBackOff**: the tag on `deploy/local` doesn't exist in GHCR.
- Rollout stuck on a bad image? Old pods should still be serving (`maxUnavailable: 0`). Roll back as above.

## BaakiPostgresDown

**Page.** `postgres-0` is not Ready, so every web pod will fail readiness shortly.

- `kubectl -n baaki describe pod postgres-0` and `logs postgres-0`. Look for a full disk
  (`df -h` inside the pod), OOMKilled, or failing liveness.
- The PVC keeps the data across restarts: `kubectl -n baaki get pvc`. Deleting the pod is safe; deleting
  the PVC is not.
- **Verify:** `pg_isready` passes, `/readyz` returns 200, and web pods become Ready again by themselves.

## BaakiWorkerJobFailed

**Ticket.** The daily recovery pass failed, so no reminders went out today.

- `kubectl -n baaki get jobs`, then `kubectl -n baaki logs job/<failed job>`.
- `busy` lines mean another run held the per-org lock: harmless.
- After fixing, re-run it by hand:
  `kubectl -n baaki create job --from=cronjob/baaki-worker baaki-worker-manual-$(date +%s)`.
  The per-org lock and the outbox `sent_at` check prevent double-sending.
