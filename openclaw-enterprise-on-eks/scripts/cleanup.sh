#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in eksctl aws; do require_command "$command"; done

eksctl delete cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" --wait

# shellcheck disable=SC2016
remaining_volumes="$(aws ec2 describe-volumes --region "$AWS_REGION" \
  --filters "Name=tag:eks:eks-cluster-name,Values=$CLUSTER_NAME" \
  --query 'Volumes[?State!=`deleted`].VolumeId' --output text)"
if [[ -n "$remaining_volumes" ]]; then
  printf 'Review retained EBS volumes: %s\n' "$remaining_volumes" >&2
fi
