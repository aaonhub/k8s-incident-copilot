#!/usr/bin/env bash
# Mint short-lived kubeconfigs for the two copilot identities (see deploy/rbac.yaml).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .kube
kubectl apply -f deploy/rbac.yaml >/dev/null
server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
ca=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
for sa in copilot-reader copilot-remediator; do
  token=$(kubectl -n copilot create token "$sa" --duration=24h)
  cat > ".kube/$sa.yaml" <<YAML
apiVersion: v1
kind: Config
clusters: [{name: k3d, cluster: {server: "$server", certificate-authority-data: "$ca"}}]
users: [{name: $sa, user: {token: "$token"}}]
contexts: [{name: $sa, context: {cluster: k3d, user: $sa, namespace: shop}}]
current-context: $sa
YAML
  chmod 600 ".kube/$sa.yaml"
done
echo "wrote .kube/copilot-reader.yaml and .kube/copilot-remediator.yaml (24h tokens)"
