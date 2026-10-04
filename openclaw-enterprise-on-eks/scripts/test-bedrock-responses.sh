#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

require_command python3
key_file="${BEDROCK_API_KEY_FILE:-}"
[[ -n "$key_file" ]] ||
  die "set BEDROCK_API_KEY_FILE to a mode-0600 key file"
require_mode_0600 BEDROCK_API_KEY_FILE "$key_file"

AWS_REGION="$AWS_REGION" MODEL_ID="$MODEL_ID" BEDROCK_API_KEY_FILE="$key_file" \
  python3 "$SAMPLE_DIR/tests/bedrock_responses.py"
