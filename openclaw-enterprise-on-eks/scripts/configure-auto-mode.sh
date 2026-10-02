#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in aws kubectl jq sed; do require_command "$command"; done
export KUBECONFIG="$GENERATED_DIR/kubeconfig"

cluster_json="$(aws eks describe-cluster --region "$AWS_REGION" --name "$CLUSTER_NAME")"
node_role_arn="$(jq -er '.cluster.computeConfig.nodeRoleArn' <<<"$cluster_json")"
node_role_name="${node_role_arn##*/}"
cluster_sg="$(jq -er '.cluster.resourcesVpcConfig.clusterSecurityGroupId' <<<"$cluster_json")"

private_subnets="$(
  jq -r '.cluster.resourcesVpcConfig.subnetIds[]' <<<"$cluster_json" |
  xargs aws ec2 describe-subnets --region "$AWS_REGION" --subnet-ids |
  jq -r '.Subnets[] | select(.MapPublicIpOnLaunch == false) | .SubnetId'
)"
subnet_count="$(printf '%s\n' "$private_subnets" | sed '/^$/d' | wc -l | tr -d ' ')"
[[ "$subnet_count" -ge 2 ]] || die "expected at least two private cluster subnets"

subnet_terms=
while IFS= read -r subnet_id; do
  [[ -n "$subnet_id" ]] || continue
  subnet_terms+="    - id: $subnet_id"$'\n'
done <<<"$private_subnets"

export NODE_ROLE_NAME="$node_role_name"
export CLUSTER_SECURITY_GROUP_ID="$cluster_sg"
export PRIVATE_SUBNET_TERMS="$subnet_terms"
python3 - "$SAMPLE_DIR/manifests/nodeclass.yaml.tmpl" "$GENERATED_DIR/nodeclass.yaml" <<'PY'
from pathlib import Path
import os
import sys

source = Path(sys.argv[1]).read_text()
source = source.replace("__NODE_ROLE_NAME__", os.environ["NODE_ROLE_NAME"])
source = source.replace(
    "__CLUSTER_SECURITY_GROUP_ID__",
    os.environ["CLUSTER_SECURITY_GROUP_ID"],
)
source = source.replace(
    "__PRIVATE_SUBNET_TERMS__",
    os.environ["PRIVATE_SUBNET_TERMS"].rstrip(),
)
Path(sys.argv[2]).write_text(source)
PY

# Enable policy enforcement before any custom workload node can launch.
kubectl apply -f "$SAMPLE_DIR/manifests/network-policy-controller.yaml"
kubectl apply -f "$GENERATED_DIR/nodeclass.yaml"
kubectl apply -f "$SAMPLE_DIR/manifests/nodepools/"
kubectl apply -f "$SAMPLE_DIR/manifests/storage-classes/"

kubectl wait --for=condition=Ready nodeclass/oce-private --timeout=5m
kubectl wait --for=condition=Ready nodepool/oce-control --timeout=5m
kubectl wait --for=condition=Ready nodepool/oce-agents --timeout=5m
kubectl get nodeclass,nodepool
