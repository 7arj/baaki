#!/usr/bin/env bash
# Create the two Secrets the app needs, directly in the cluster. Nothing is written to git.
#   baaki-secrets  random Postgres password, DATABASE_URL built from it, random app secret key
#   baaki-tls      self-signed certificate for baaki.localhost (SAN set, valid 1 year)
# Re-running keeps the existing values, so the database password never changes under Postgres.
set -euo pipefail
NS=baaki
kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

if kubectl -n "$NS" get secret baaki-secrets >/dev/null 2>&1; then
  echo "baaki-secrets already exists, keeping it"
else
  PG_PASSWORD=$(openssl rand -hex 24)
  kubectl -n "$NS" create secret generic baaki-secrets \
    --from-literal=POSTGRES_PASSWORD="$PG_PASSWORD" \
    --from-literal=DATABASE_URL="postgresql+psycopg://baaki:${PG_PASSWORD}@postgres:5432/baaki" \
    --from-literal=BAAKI_SECRET_KEY="$(openssl rand -base64 32)"
  echo "created baaki-secrets"
fi

if kubectl -n "$NS" get secret baaki-tls >/dev/null 2>&1; then
  echo "baaki-tls already exists, keeping it"
else
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "$TMP/tls.key" -out "$TMP/tls.crt" \
    -subj "/CN=baaki.localhost" -addext "subjectAltName=DNS:baaki.localhost" 2>/dev/null
  kubectl -n "$NS" create secret tls baaki-tls --cert="$TMP/tls.crt" --key="$TMP/tls.key"
  echo "created baaki-tls (self-signed; browsers will warn, curl needs -k)"
fi
