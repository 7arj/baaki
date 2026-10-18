#!/usr/bin/env bash
# The core user journey, end to end: sign in to the seeded demo tenant and open the invoice ledger.
# Used by CI against the freshly built image and by hand against the cluster:
#   scripts/smoke-journey.sh https://baaki.localhost
# Requires the demo tenant (python -m baaki demo). The demo login is the public one in baaki/app/demo.py.
set -euo pipefail
BASE=${1:-https://baaki.localhost}
cd "$(dirname "$0")/.."
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
EMAIL=$(sed -nE 's/^EMAIL = "([^"]+)"/\1/p' baaki/app/demo.py)
PASSWORD=$(sed -nE 's/^PASSWORD = "([^"]+)"/\1/p' baaki/app/demo.py)

curl -sfk -c "$JAR" "$BASE/login" -o /dev/null
CSRF=$(awk '$6 == "baaki_csrf" {print $7}' "$JAR")
STATUS=$(curl -sk -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code} %{redirect_url}' -X POST "$BASE/login" \
  --data-urlencode "email=$EMAIL" --data-urlencode "password=$PASSWORD" --data-urlencode "csrf_token=$CSRF")
echo "sign in: $STATUS"
[[ $STATUS == "303 "*"/app" ]] || { echo "FAIL: sign-in did not reach /app"; exit 1; }

PAGE=$(curl -sfk -b "$JAR" "$BASE/app/invoices")
COUNT=$(grep -oE 'INV-[0-9]+' <<<"$PAGE" | sort -u | wc -l | tr -d ' ')
echo "invoice ledger: $COUNT invoices visible"
[[ $COUNT -gt 0 ]] || { echo "FAIL: ledger empty"; exit 1; }
echo "PASS"
