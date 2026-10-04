#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in aws kubectl python3; do require_command "$command"; done
require_digest CONTROLLER_IMAGE "$CONTROLLER_IMAGE"
require_digest RUNTIME_IMAGE "$RUNTIME_IMAGE"
export KUBECONFIG="$GENERATED_DIR/kubeconfig"

postgres_ip="${POSTGRES_POD_IP:-}"
[[ -n "$postgres_ip" ]] || die "set POSTGRES_POD_IP after deploying PostgreSQL"
kubernetes_service_ip="$(kubectl get service kubernetes -o jsonpath='{.spec.clusterIP}')"
service_cidr="$(aws eks describe-cluster --region "$AWS_REGION" --name "$CLUSTER_NAME" \
  --query 'cluster.kubernetesNetworkConfig.serviceIpv4Cidr' --output text)"
node_local_dns_ip="$(python3 - "$service_cidr" <<'PY'
import ipaddress
import sys

network = ipaddress.ip_network(sys.argv[1])
print(network.network_address + 10)
PY
)"
export POSTGRES_POD_IP="$postgres_ip"
export KUBERNETES_SERVICE_IP="$kubernetes_service_ip"
export NODE_LOCAL_DNS_IP="$node_local_dns_ip"

python3 - "$SAMPLE_DIR" "$GENERATED_DIR" <<'PY'
from pathlib import Path
import os, sys

root, output = map(Path, sys.argv[1:3])
replacements = {
    "__AWS_REGION__": os.environ["AWS_REGION"],
    "__CLUSTER_NAME__": os.environ["CLUSTER_NAME"],
    "__CONTROLLER_IMAGE__": os.environ["CONTROLLER_IMAGE"],
    "__RUNTIME_IMAGE__": os.environ["RUNTIME_IMAGE"],
    "__MODEL_ID__": os.environ["MODEL_ID"],
    "__POSTGRES_POD_IP__": os.environ["POSTGRES_POD_IP"],
    "__KUBERNETES_SERVICE_IP__": os.environ["KUBERNETES_SERVICE_IP"],
    "__NODE_LOCAL_DNS_IP__": os.environ["NODE_LOCAL_DNS_IP"],
}
for source, destination in [
    ("oce/values.yaml.tmpl", "values.yaml"),
    ("oce/installation.yaml.tmpl", "installation.yaml"),
    ("oce/agent-configuration.json.tmpl", "agent-configuration.json"),
    ("manifests/platform-network-policies.yaml.tmpl", "platform-network-policies.yaml"),
]:
    text = (root / source).read_text()
    for key, value in replacements.items():
        text = text.replace(key, value)
    if "__" in text:
        raise SystemExit(f"unresolved placeholder in {source}")
    (output / destination).write_text(text)
PY

chmod 600 "$GENERATED_DIR/values.yaml" \
  "$GENERATED_DIR/installation.yaml" \
  "$GENERATED_DIR/agent-configuration.json" \
  "$GENERATED_DIR/platform-network-policies.yaml"
