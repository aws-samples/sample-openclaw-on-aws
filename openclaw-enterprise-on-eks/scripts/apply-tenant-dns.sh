#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in aws kubectl python3 sed; do require_command "$command"; done
[[ "$#" -gt 0 ]] || die "pass each generated tenant and gateway namespace"
export KUBECONFIG="$GENERATED_DIR/kubeconfig"

service_cidr="$(aws eks describe-cluster --region "$AWS_REGION" --name "$CLUSTER_NAME" \
  --query 'cluster.kubernetesNetworkConfig.serviceIpv4Cidr' --output text)"
node_local_dns_ip="$(python3 - "$service_cidr" <<'PY'
import ipaddress
import sys

network = ipaddress.ip_network(sys.argv[1])
print(network.network_address + 10)
PY
)"

for namespace in "$@"; do
  [[ "$namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
    die "invalid Kubernetes namespace: $namespace"
  sed \
    -e "s/__TENANT_NAMESPACE__/$namespace/g" \
    -e "s/__NODE_LOCAL_DNS_IP__/$node_local_dns_ip/g" \
    "$SAMPLE_DIR/manifests/tenant-dns-policy.yaml.tmpl" |
    kubectl apply -f -
done
