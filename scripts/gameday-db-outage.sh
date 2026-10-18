#!/usr/bin/env bash
# Game day: take Postgres away for a few minutes and record how the system and its alerts behave.
# Every 5s: postgres ready replicas, ready web pods, HTTP status of a user page through the
# ingress, and which baaki alerts Prometheus has firing.
#   scripts/gameday-db-outage.sh [outage_seconds]
set -uo pipefail
OUTAGE=${1:-180}
BASE=https://baaki.localhost

kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null' EXIT
sleep 3

alerts() {
  curl -s localhost:9090/api/v1/alerts | python3 -c '
import json, sys
alerts = json.load(sys.stdin)["data"]["alerts"]
names = sorted(a["labels"]["alertname"] + "=" + a["state"] for a in alerts if a["labels"]["alertname"].startswith("Baaki"))
print(",".join(names) or "-")'
}
sample() {
  local pg web code
  pg=$(kubectl -n baaki get sts postgres -o jsonpath='{.status.readyReplicas}')
  web=$(kubectl -n baaki get deploy baaki-web -o jsonpath='{.status.readyReplicas}')
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/login")
  printf '%s  postgres_ready=%-2s web_ready=%-2s GET /login=%s  alerts=%s\n' \
    "$(date -u +%H:%M:%S)" "${pg:-0}" "${web:-0}" "$code" "$(alerts)"
}

echo "# Game day: Postgres outage for ${OUTAGE}s, $(date -u +%FT%TZ)"
echo "# running version: $(curl -sk $BASE/version)"
sample
echo "## pause Argo CD self-heal (it would restore Postgres within seconds), then stop Postgres"
kubectl -n argocd patch app baaki --type merge -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":false}}}}' >/dev/null
kubectl -n baaki scale statefulset postgres --replicas=0
END=$(( $(date +%s) + OUTAGE ))
while [ "$(date +%s)" -lt "$END" ]; do sample; sleep 5; done
echo "## recovery: re-enable self-heal; Argo CD restores replicas=1 from git"
kubectl -n argocd patch app baaki --type merge -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":true}}}}' >/dev/null
for _ in $(seq 36); do sample; [ "$(kubectl -n baaki get deploy baaki-web -o jsonpath='{.status.readyReplicas}')" -ge 2 ] 2>/dev/null && break; sleep 5; done
sleep 5; sample
echo "## data after recovery"
"$(dirname "$0")/smoke-journey.sh" "$BASE"
