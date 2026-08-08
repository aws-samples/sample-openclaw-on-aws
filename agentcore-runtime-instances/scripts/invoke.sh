#!/bin/bash
set -euo pipefail

# Test invocation of OpenClaw on AgentCore Runtime Instances

REGION="${AWS_REGION:-us-east-1}"
RUNTIME_NAME="${RUNTIME_NAME:-openclaw-agent}"
SESSION_ID="${SESSION_ID:-openclaw-$(whoami)}"
PROMPT="${1:-Hello! What can you do?}"

echo "Invoking OpenClaw agent..."
echo "  Runtime: $RUNTIME_NAME"
echo "  Session: $SESSION_ID"
echo "  Prompt: $PROMPT"
echo ""

# Properly escape the prompt for JSON
PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'prompt': sys.argv[1]}))" "$PROMPT")

aws bedrock-agentcore-runtime invoke-agent-runtime \
    --agent-runtime-id "$RUNTIME_NAME" \
    --runtime-session-id "$SESSION_ID" \
    --payload "$PAYLOAD" \
    --region "$REGION"
