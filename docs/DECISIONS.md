# Design decisions and trade-offs

Each entry gives the choice, why it fits baaki, the credible alternative, and when that alternative
would be the better call.

## Runtime (Kubernetes)

**Local kind cluster, 1 control plane + 2 workers labelled `zone-a` / `zone-b`.**
Free and repeatable, and multi-node means pod spreading, node drains and PDBs actually happen. A
single-node Minikube couldn't show any of that. *Alternative:* managed EKS/GKE, once there are real
users, an HA control plane, or a managed database to integrate with.

**Web = Deployment.** Sessions are stored in Postgres, not in process memory, so any pod can serve
any request and pods are disposable. That's what makes rolling updates and the HPA safe.

**Postgres = StatefulSet + PVC, one replica.** A stable name (`postgres-0`) and storage that survives
the pod. *Why not SQLite (the app's default)?* A SQLite file can't be shared between pods, so the web
tier would be stuck at one replica and the HPA would be pointless. *Why not more replicas?* Postgres
replicas aren't interchangeable (one primary), so it needs an operator (CloudNativePG) or a managed
DB. Out of scope; the postmortem calls it out as the single point of failure.

**Migrations = a Job that runs once per sync, between Postgres and web (Argo CD sync waves 0 → 1 → 2).**
The app migrated itself at startup; with 2–5 pods starting together they would race through Alembic.
It's now switched off in-cluster (`BAAKI_MIGRATE_ON_START=false`). *Alternative:* an initContainer per
pod, which is simpler but still races; or a separate migration pipeline step when migrations are long.

**Worker = CronJob with `concurrencyPolicy: Forbid`.** The product documents it as "run from cron,
daily". Forbid stops two runs overlapping; the app's per-org lock row in Postgres stops double-sends
even across a manual run. *Alternative:* a queue + long-running workers at higher volume.

**Three probes, three questions.**
- startup: has it finished booting? (Up to 60 s, so liveness doesn't kill a slow first start.)
- liveness `/healthz`: is the process stuck? It never touches the DB, because a DB outage must not
  restart healthy pods (proven in the game day).
- readiness `/readyz`: can it serve right now (DB reachable, schema migrated)? Failing removes the pod
  from the Service without restarting it.
*Trade-off:* readiness checking a dependency means a DB outage empties the Service (503 at the
ingress). That's honest: nothing works without the DB anyway. Checking several downstream services
this way would cascade failures, so only the hard dependency is checked.

**Resources.** web: request 100m/192Mi, limit 500m/384Mi → Burstable (cheap to schedule, can burst).
Postgres: request = limit → Guaranteed (last to be evicted under node pressure). A LimitRange gives
defaults so nothing runs as BestEffort, and a ResourceQuota caps the namespace. *Measured:* under
load, web CPU ran at ~4.5× its request with all 5 pods, so the request is set low and a real
deployment would raise it (and `maxReplicas`) from these numbers.

**HPA on CPU, 2–5 replicas, 120 s scale-down stabilization; no VPA.** CPU tracks load for this
request/response app (measured: 957 req/s needed all 5 pods). Min 2 keeps one pod alive through a
failure or drain. Running VPA on the same CPU signal would fight the HPA. `replicas` is left out of
the Deployment so Argo CD self-heal doesn't fight the HPA either. *Alternative:* scale on request
rate or latency via a custom metric, when CPU stops reflecting user load.

**Placement.** Topology spread across zones plus preferred anti-affinity by hostname: losing one node
or zone leaves a pod running. *Soft* rules (`ScheduleAnyway` / preferred) so pods still schedule if a
zone is gone. *Hard* rules would rather stay Pending than co-locate.

**PDB `minAvailable: 1`.** Voluntary disruptions (drains, upgrades) can't take the last web pod.
Measured: 60/60 requests succeeded during a drain.

**Graceful shutdown.** `preStop` sleeps 5 s so the endpoint is removed from routing before SIGTERM;
uvicorn then finishes in-flight requests within `terminationGracePeriodSeconds: 30`.

## Networking

**Traefik Ingress (L7) terminating TLS; ClusterIP Services inside.** One public entry point with
host-based routing; nothing else is exposed. The app runs with `BAAKI_ENV=production`, which marks
session cookies `Secure`, so HTTPS is required, not optional. Self-signed cert with
SAN=`baaki.localhost` locally. *Alternative:* cert-manager + Let's Encrypt for a public hostname.

**Default-deny NetworkPolicies.** Like security groups: only Traefik/Prometheus → web:8080,
app → postgres:5432, DNS, and HTTPS/SMTP to *public* IPs (needed for Razorpay, the LLM APIs, WhatsApp
and email; private ranges stay blocked). Verified with allow/deny tests.

## Configuration

**Kustomize (base + `overlays/local`) for the app; Helm for third-party platform pieces.**
One environment, plain readable YAML, built into `kubectl`; the overlay holds only what differs (the
image tag), so there are no drifting copies. Traefik, metrics-server and kube-prometheus-stack are
installed from pinned Helm charts, because that's how they're packaged and their values files are
the configuration. *Alternative:* a Helm chart for the app, once it's packaged for others or there
are several environments with many differing values.

**ConfigMap for settings, Secret for credentials, generated in-cluster by a script.** Nothing secret
is ever in git. *Alternative:* Sealed Secrets or External Secrets Operator, so secrets are declarative
and survive cluster rebuilds; in a team, that is the right choice.

## Delivery

**GitHub Actions: checks → image → push → promote.** ruff (bug-class only: the existing code has 200+
style findings, and fixing them isn't this project), bandit, pip-audit, pytest, gitleaks over full
history, then build → smoke test of the *real image* against *real Postgres* → Trivy → push. Shift
left: pip-audit found real advisories in pyjwt and urllib3 on day one. PRs get every check but
deploy nothing.

**Image tag = commit SHA, never `latest`.** The running version is provable: tag, `GIT_SHA` env,
OCI label and `/version` all equal the commit. `latest` can't say what's running or roll back.

**Argo CD (pull) instead of `kubectl apply` from CI (push).** GitHub's runners can't reach a laptop
cluster, and pull keeps git as the single source of truth. Self-heal reverts manual changes (drift)
and prune deletes what git deleted. *Alternative:* push-based deploys to a reachable cluster when
GitOps tooling is overkill.

**A CI-written `deploy/local` branch.** The deploy job pushes "source commit + new tag" to that branch
and Argo CD tracks it. Source branches never get bot commits, so every source commit keeps its own CI
result. *Trade-off:* the branch is force-pushed, so history of *deploys* lives in CI runs and Argo CD
history rather than in that branch.

**Automatic promotion, no manual approval.** One local environment and a strong automated gate
(tests, scans, smoke test, readiness-gated rollout). Where approval belongs: before a production
environment. A second Argo CD app with manual sync, or a GitHub Environment with required reviewers
on the deploy job, would add it without changing anything else.

**Actions pinned to commit SHAs; `permissions: contents: read` by default; push with `GITHUB_TOKEN`.**
A moved tag can't inject code; no long-lived registry credential exists.

## Release control

**Rolling update, `maxSurge: 1`, `maxUnavailable: 0`, gated by readiness.** A bad build that can't
become Ready never replaces a good pod. Right-sized for one stateless service with low traffic.
- *Blue-green:* instant switch and instant rollback, at the cost of 2× capacity during the release.
- *Canary (Argo Rollouts):* route 5% of traffic and watch the error rate before going further. Worth
  it once there's enough traffic for 5% to be statistically meaningful, which there isn't here.
- *Feature flags:* the LLM brain already falls back to rules per decision; a ConfigMap kill switch
  would be the next step.

**Rollback = re-run an earlier run's deploy job** (measured ~50 s, no rebuild), or `git revert`.
`kubectl rollout undo` would be undone by self-heal: with GitOps, rollback happens in git.
**Database caveat:** migrations only go forward, so schema changes must stay backward compatible for
one release (expand, then contract) for rollbacks to be safe.

## Observability and reliability

**RED metrics from the app, USE from the platform.** Request counter + latency histogram labelled by
route *template* (bounded cardinality: `/app/invoices/{inv_id}`, never the raw id). Percentiles, not
averages, because averages hide the slow tail. node-exporter, kube-state-metrics and cAdvisor give
CPU/memory vs requests, restarts, HPA and job status.

**JSON logs with a request id** echoed as `X-Request-ID`: one grep follows a user's request.

**No distributed tracing.** One service + one DB; the request id and latency histogram answer "where
did the time go". OpenTelemetry becomes worth its cost when a second service joins the request path.

**SLOs: 99.5% non-5xx, 95% under 500 ms.** User-facing and measurable from the app's own metrics.
99.5% (≈3.6 h/month) matches a single-replica database; promising 99.9% would be dishonest with this
architecture.

**Multi-window burn-rate alerts (14.4×/6×/3×).** Page only when the budget is burning fast *and*
still burning now; ticket for slow burns. Plus symptom alerts (no ready pods, Postgres down, worker
failed). The game day proved why both are needed: a total outage produces 503s at the ingress that
the app's own error counter never sees.
