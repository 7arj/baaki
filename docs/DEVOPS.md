# Running and operating baaki

How baaki is built, delivered and run on Kubernetes. This is the Advanced DevOps project work. The
baseline before any of it is commit `3c2eadf8734fc3c254748945e8342500732e7647`.

Also see: [DECISIONS.md](DECISIONS.md) (why each choice, and the alternatives),
[evidence/](evidence/README.md) (captured runs), [runbooks.md](runbooks.md),
[postmortem](postmortem-2026-10-18-db-outage.md).

## Architecture

```
 git push ─► GitHub Actions ─► ghcr.io/7arj/baaki:<commit-sha> ─► commit tag to branch deploy/local
                                                                          │
                     Argo CD (in the cluster) polls deploy/local, syncs ◄─┘
                                         │
 browser ─https─► Traefik Ingress ─► Service baaki-web ─► web pods (Deployment, HPA 2–5)
 (baaki.localhost)  TLS ends here      ClusterIP           │ /healthz /readyz /version /metrics
                                                           ▼
                        migrate Job ───────────────► postgres-0 (StatefulSet + 1Gi PVC)
                        worker CronJob (daily) ─────►
```

| Component | Kubernetes object | Why that object |
|-----------|-------------------|-----------------|
| Web app | Deployment + ClusterIP Service | Stateless (sessions are in Postgres), so pods are interchangeable and can scale out |
| Database | StatefulSet + PVC + headless Service | Needs a stable identity and storage that survives restarts |
| Schema migrations | Job (Argo CD Sync hook, wave 1) | Runs once per deploy, after Postgres, before new web pods; never raced by several pods |
| Daily recovery pass | CronJob, `concurrencyPolicy: Forbid` | A batch run on a schedule, never two at once |
| Entry point | Ingress (Traefik) + TLS Secret | One L7 entry point with HTTPS; everything else stays internal |

Request path: browser → `baaki.localhost` (resolves to 127.0.0.1) → kind maps host port 443 to
Traefik on the control-plane node → Traefik ends TLS and routes by host to Service `baaki-web` →
kube-proxy picks a Ready web pod → the app queries `postgres:5432` (CoreDNS resolves the headless
Service to `postgres-0`).

## Prerequisites

macOS or Linux with Docker running (about 4 CPUs / 6 GB for Docker), plus:

```bash
brew install kind kubectl helm uv
```

## Bring it up

```bash
git clone https://github.com/7arj/baaki && cd baaki
git switch devops-improvements
scripts/cluster-up.sh
```

`cluster-up.sh` is idempotent. It:
1. creates a 3-node kind cluster (`k8s/kind-config.yaml`): control plane + workers in `zone-a` and `zone-b`
2. installs Traefik (ingress, ports 80/443), metrics-server (for the HPA) and kube-prometheus-stack
   (Prometheus, Alertmanager, Grafana) with pinned Helm charts
3. installs Argo CD v3.5.5
4. generates the Secrets in the cluster: a random DB password and app key, plus a self-signed TLS cert
   for `baaki.localhost` (`scripts/create-secrets.sh`). **No secret is stored in git.**
5. applies `argocd/project.yaml` and `argocd/application.yaml`. From then on Argo CD deploys the app
   from git.

Watch it come up:

```bash
kubectl -n baaki get pods -w
```

Seed the demo tenant (once), then run the user journey:

```bash
kubectl -n baaki exec deploy/baaki-web -- python -m baaki demo
scripts/smoke-journey.sh https://baaki.localhost
```

## Access

| What | Where |
|------|-------|
| App | https://baaki.localhost (self-signed cert: accept the browser warning, or `curl -k`). Demo login: `demo@baaki.app` / `baaki-demo-2026` (public demo account from `baaki/app/demo.py`) |
| Running version | https://baaki.localhost/version (returns the git commit) |
| Health | `/healthz` (liveness), `/readyz` (readiness) |
| Metrics | `kubectl -n baaki port-forward deploy/baaki-web 8080` then http://localhost:8080/metrics |
| Grafana | `kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80` then http://localhost:3000, user `admin`, password from `kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' \| base64 -d`. Dashboard: **baaki: service health** |
| Prometheus / alerts | `kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090` then http://localhost:9090/alerts |
| Argo CD UI | `kubectl -n argocd port-forward svc/argocd-server 8443:443` then https://localhost:8443, user `admin`, password from `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' \| base64 -d` |

## Delivery: from commit to running pod

`.github/workflows/ci.yml`:

1. **checks**: ruff (bug-class lint), bandit (SAST), pip-audit (dependency CVEs), pytest
2. **secrets**: gitleaks over the full git history
3. **image** (needs 1 + 2): build with `GIT_SHA` baked in → smoke test of the real image against real
   Postgres (migrate, seed, sign in, `/version` must equal the commit) → Trivy (fails on fixable
   HIGH/CRITICAL) → push `ghcr.io/7arj/baaki:<sha>` using the run's own `GITHUB_TOKEN`
4. **deploy** (push events only): take the source commit, set `newTag` in
   `k8s/overlays/local/kustomization.yaml` to its sha, and push the result to the branch
   **`deploy/local`**. That branch is only ever written by CI, so source branches carry no bot commits
   and every source commit keeps its own CI result.

Argo CD notices the new commit (polls about every 3 minutes) and syncs in waves: Postgres (0) →
migrate Job (1) → web Deployment (2). The web rollout is `maxSurge: 1, maxUnavailable: 0`: a new pod
must pass readiness before an old one is removed.

Pull requests run steps 1–3 without pushing: checks and a scanned image, but nothing deployed.

**Why pull-based CD:** GitHub's runners can't reach a laptop's kind cluster, and git stays the single
source of truth. Argo CD `selfHeal` reverts manual `kubectl` edits (drift), and `prune` removes
objects deleted from git.

**Traceability:** source commit sha = image tag = `GIT_SHA` env = `/version` response = the
`deploy: baaki <sha> (branch, run id)` commit on `deploy/local`, which links back to the CI run.

## Releasing and rolling back

- **Release:** merge or push → CI → tag bump → Argo CD rolling update.
- **Bad release, caught automatically:** if new pods fail readiness, the rollout stalls with old pods
  still serving (`maxUnavailable: 0`). No user impact.
- **Bad release, found later (fast path):** in GitHub Actions, open the last known-good run and
  *Re-run* its `deploy` job. It re-points `deploy/local` at that commit's already-scanned image, and
  Argo CD rolls back with no rebuild.
- **Proper fix:** `git revert` the bad change on the source branch; CI builds and promotes the revert.
- `kubectl rollout undo` would also work, but Argo CD self-heal would immediately put back what
  `deploy/local` says. With GitOps, the rollback has to happen in git.
- **Migrations:** Alembic upgrades run forward only. A rollback that needs an older schema needs a
  down-migration, so schema changes should be backward compatible for one release (expand, then contract).

## Observability

| Signal | How | Where |
|--------|-----|-------|
| Metrics (RED) | `baaki_http_requests_total{method,route,status}` counter and `baaki_http_request_duration_seconds` histogram from `baaki/app/ops.py`. Labels are route *templates*, so cardinality stays bounded | Prometheus via `ServiceMonitor`; Grafana dashboard |
| Metrics (USE) | node-exporter + kube-state-metrics + cAdvisor: CPU and memory vs requests, restarts, HPA replicas, Postgres readiness, CronJob outcomes | same dashboard |
| Logs | One JSON line per request: `request_id` (also returned as `X-Request-ID`), `route`, `status`, `duration_ms`, `version`. Traefik access logs are JSON too | `kubectl logs`, filterable with grep/jq |
| Health | `/healthz` liveness, `/readyz` readiness (DB and schema), `/version` | probes, curl |
| Traces | Not implemented. One service plus one database: the request id in logs and the latency histogram cover where time goes. OpenTelemetry would be the next step once a second service exists | — |

**SLOs** (`k8s/base/monitoring.yaml`), measured on user requests only (probes and scrapes are excluded):

| SLI | SLO | Error budget |
|-----|-----|--------------|
| Availability: share of requests that are not 5xx | 99.5% over 30 days | 0.5% (about 3.6 h of full outage a month) |
| Latency: share of requests under 500 ms | 95% | 5% |

**Alerts** are multi-window burn-rate alerts, so a short blip doesn't page but a real outage pages
within minutes:

| Alert | Condition | Severity |
|-------|-----------|----------|
| BaakiErrorBudgetFastBurn | error ratio > 14.4× budget over 1h **and** 5m | page |
| BaakiErrorBudgetSlowBurn | > 6× over 6h **and** 30m | page |
| BaakiErrorBudgetTicket | > 3× over 1d **and** 2h | ticket |
| BaakiLatencySLOBreach | < 95% of requests under 500 ms for 15m | ticket |
| BaakiNoReadyWebPods / BaakiPostgresDown | no Ready replica for 2m (works even with zero traffic) | page |
| BaakiWorkerJobFailed | the daily CronJob run failed | ticket |

Alertmanager routes `severity=page` to an on-call receiver and `severity=ticket` to a queue
(receivers are placeholders on a laptop). Every alert links to its section in
[runbooks.md](runbooks.md). Error-budget policy: while the 30-day budget is exhausted, only
reliability fixes are released.

## Security controls

| Layer | Control |
|-------|---------|
| Source | gitleaks on full history; no secrets in git (`.env` ignored, Secrets generated in-cluster) |
| Dependencies | pip-audit in CI (found and fixed advisories in pyjwt and urllib3); `uv.lock` pinned |
| Code | bandit SAST |
| Pipeline | Actions pinned to commit SHAs; `permissions: contents: read` by default; registry push with the short-lived `GITHUB_TOKEN` |
| Image | Multi-stage build, slim base, non-root uid 10001, Trivy scan gate |
| Pod | Pod Security `restricted` on the namespace; `runAsNonRoot`, read-only root filesystem, all capabilities dropped, seccomp `RuntimeDefault`, no service-account token |
| Network | Default-deny NetworkPolicies; only Traefik → web:8080, app → postgres:5432, DNS, and HTTPS/SMTP to public IPs |
| Access | Read-only `developer` Role (no Secrets, no exec, no writes); Argo CD AppProject limited to this repo and namespace |
| Transport | TLS at the ingress; http → https redirect; `BAAKI_ENV=production` makes session cookies `Secure` |
| Capacity | ResourceQuota + LimitRange on the namespace |

## Known limitations

- Local kind cluster only: a single control plane and a single Postgres replica (no DB failover).
  Postgres data lives on the node's local disk (kind `standard` StorageClass), so the pod is tied to
  that node.
- Self-signed TLS certificate; a real deployment would use cert-manager with a public CA.
- Secrets are created by a script rather than managed (Sealed Secrets / External Secrets in a team).
- Argo CD polls git; there's no webhook, because GitHub can't reach the laptop.
- Outbound email/WhatsApp/Razorpay need real credentials and aren't exercised here; the app falls back
  to its console transport and simulator.
- Availability is measured inside the app, so a total outage (503 from the ingress) doesn't burn the
  error budget; symptom alerts cover that case (see the postmortem).
- No distributed tracing (single service); no canary analysis or manual approval gate (see DECISIONS.md).
- Web CPU requests are deliberately small; the load test saturated 5 pods at ~950 req/s.
