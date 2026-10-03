#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

tenant_name="${TENANT_NAME:-}"
agent_name="${AGENT_NAME:-}"

[[ -n "$tenant_name" ]] || die "set TENANT_NAME for the second tenant"
[[ "$tenant_name" != default ]] || die "use create-agent.sh for the default tenant"
[[ -n "$agent_name" ]] || die "set AGENT_NAME for the second tenant"
[[ "$tenant_name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
  die "TENANT_NAME must use lowercase letters, numbers, and hyphens"
[[ "$agent_name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
  die "AGENT_NAME must use lowercase letters, numbers, and hyphens"

state_file="$GENERATED_DIR/tenant-$tenant_name.json"
TENANT_NAME="$tenant_name" \
AGENT_NAME="$agent_name" \
TENANT_STATE_FILE="$state_file" \
VERIFY_DENIED_BEFORE_BINDING=1 \
  "$SAMPLE_DIR/scripts/create-agent.sh"
