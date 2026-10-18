# Evidence

Captured from the running system on 18 Oct 2026 (local kind cluster) with the commands shown in
each file. Nothing here is hand-edited output. Files 00, 02, 05 and 07 were captured with commit
`b77f874` deployed; the commit after it only adds these evidence files (plus the overlay tag).

| File | What it shows |
|------|---------------|
| [00-traceability-and-state.txt](00-traceability-and-state.txt) | Commit → green CI run → `deploy/local` commit → Argo CD synced revision → image tag, `GIT_SHA` env and OCI label in the running pods: all the same sha. Plus every workload object and the zone-labelled nodes |
| [01-drift-selfheal.txt](01-drift-selfheal.txt) | A hand-edited ConfigMap value and a deleted Service were reverted or recreated by Argo CD within about 6 s |
| [02-gameday-db-outage.txt](02-gameday-db-outage.txt) | Postgres stopped for 200 s: readiness pulls every web pod (503 at the ingress), `BaakiPostgresDown` and `BaakiNoReadyWebPods` go pending at ~15 s and fire at ~2 min; users served again 5 s after Postgres returns; data intact. Write-up: [postmortem](../postmortem-2026-10-18-db-outage.md) |
| [03-node-drain-pdb.txt](03-node-drain-pdb.txt) | `kubectl drain` of a worker while serving traffic: 60/60 requests returned 200 |
| [04-hpa-autoscaling.txt](04-hpa-autoscaling.txt) | Scale-in 5→2 after the stabilization window; under load (≈950 req/s) CPU jumped to ~470% of request, the HPA scaled 2→5 in ~30 s, and all 191,420 requests succeeded |
| [05-rollback.txt](05-rollback.txt) | Rollback by re-running an earlier run's deploy job: the old version was live in ~53 s with no rebuild, then rolled forward again |
| [06-security.txt](06-security.txt) | RBAC `can-i` matrix, no SA token, NetworkPolicy allow/deny tests, Pod Security rejecting a privileged pod, read-only root fs, TLS + redirect + `Secure` cookie, no secrets in git |
| [07-observability.txt](07-observability.txt) | Prometheus targets up, RED numbers (p50/p95/p99), alert rules loaded, JSON access-log line, `X-Request-ID` traced from response to log |

CI runs for every commit: https://github.com/7arj/baaki/actions?query=branch%3Adevops-improvements

All of it is repeatable: `scripts/cluster-up.sh`, then `scripts/smoke-journey.sh`,
`scripts/gameday-db-outage.sh` and `scripts/load.py`.
