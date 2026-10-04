#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in aws kubectl jq; do require_command "$command"; done
export KUBECONFIG="$GENERATED_DIR/kubeconfig"

account_id="$(aws sts get-caller-identity --query Account --output text)"
cluster_json="$(aws eks describe-cluster --region "$AWS_REGION" --name "$CLUSTER_NAME")"

jq -e '
  .cluster.status == "ACTIVE" and
  .cluster.computeConfig.enabled == true and
  .cluster.storageConfig.blockStorage.enabled == true
' <<<"$cluster_json" >/dev/null

auto_nodes="$(kubectl get nodes -l eks.amazonaws.com/compute-type=auto -o json)"
jq -e '.items | length > 0' <<<"$auto_nodes" >/dev/null

kubectl -n "$OCE_NAMESPACE" wait \
  --for=condition=Available deployment/openclaw-enterprise-api --timeout=5m
kubectl -n "$OCE_NAMESPACE" wait \
  --for=condition=Available deployment/openclaw-enterprise-worker --timeout=5m

printf 'PASS account=%s cluster=%s autoNodes=%s\n' \
  "$account_id" "$CLUSTER_NAME" "$(jq '.items | length' <<<"$auto_nodes")"
kubectl get nodes \
  -L eks.amazonaws.com/compute-type,karpenter.sh/nodepool,oce-role
kubectl -n "$OCE_NAMESPACE" get pods,pvc -o wide
