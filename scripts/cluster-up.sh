#!/usr/bin/env bash
# One-time local platform setup: kind cluster, ingress controller, metrics-server, app secrets.
# Idempotent: safe to re-run. The app itself is deployed by Argo CD (see README), not by this script.
set -euo pipefail
cd "$(dirname "$0")/.."

kind get clusters | grep -qx baaki || kind create cluster --name baaki --config k8s/kind-config.yaml
kubectl config use-context kind-baaki

helm repo add traefik https://traefik.github.io/charts >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null
helm repo update >/dev/null

helm upgrade --install traefik traefik/traefik --version 41.7.1 \
  --namespace traefik --create-namespace -f k8s/platform/traefik-values.yaml --wait

# kind's kubelets use self-signed serving certs, hence --kubelet-insecure-tls (local only).
helm upgrade --install metrics-server metrics-server/metrics-server --version 3.14.0 \
  --namespace kube-system --set 'args={--kubelet-insecure-tls}' --wait

scripts/create-secrets.sh
echo "platform ready"
