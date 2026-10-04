#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in aws eksctl kubectl sed; do require_command "$command"; done
[[ "$PUBLIC_ACCESS_CIDR" =~ ^[0-9.]+/32$ ]] || die "PUBLIC_ACCESS_CIDR must be one IPv4 /32"

account_id="$(aws sts get-caller-identity --query Account --output text)"
[[ "$account_id" =~ ^[0-9]{12}$ ]] || die "could not resolve the AWS account"

sed \
  -e "s/__CLUSTER_NAME__/$CLUSTER_NAME/g" \
  -e "s/__AWS_REGION__/$AWS_REGION/g" \
  -e "s/__KUBERNETES_VERSION__/$KUBERNETES_VERSION/g" \
  -e "s#__PUBLIC_ACCESS_CIDR__#$PUBLIC_ACCESS_CIDR#g" \
  "$SAMPLE_DIR/cluster/cluster.yaml.tmpl" > "$GENERATED_DIR/cluster.yaml"

eksctl create cluster -f "$GENERATED_DIR/cluster.yaml"
aws eks update-kubeconfig \
  --region "$AWS_REGION" \
  --name "$CLUSTER_NAME" \
  --alias "oce-$CLUSTER_NAME" \
  --kubeconfig "$GENERATED_DIR/kubeconfig"
chmod 600 "$GENERATED_DIR/kubeconfig"

printf 'Created EKS Auto Mode cluster %s in account %s.\n' "$CLUSTER_NAME" "$account_id"
