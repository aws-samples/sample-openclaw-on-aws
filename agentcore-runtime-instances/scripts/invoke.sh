#!/bin/bash
set -euo pipefail

# Test invocation of OpenClaw on AgentCore Runtime Instances
#
# API notes (verified September 2026):
# - The data-plane service is `bedrock-agentcore` (not `bedrock-agentcore-runtime`)
# - InvokeAgentRuntime takes --agent-runtime-arn, not a runtime name or id
# - --runtime-session-id must be at least 33 characters
# - --payload must be base64-encoded, and an output file argument is required
#
# Usage:
#   ./scripts/invoke.sh "Your prompt here"
#   RUNTIME_NAME=openclaw_agent SESSION_ID=... ./scripts/invoke.sh "Hi"

REGION="${AWS_REGION:-us-east-1}"
RUNTIME_NAME="${RUNTIME_NAME:-openclaw_agent}"
PROMPT="${1:-Hello! What can you do?}"

# Resolve the runtime name to its ARN (Get/Invoke work by id/ARN, not name).
resolve_runtime_arn() {
    python3 -c '
import sys
import boto3

region, name = sys.argv[1], sys.argv[2]
client = boto3.client("bedrock-agentcore-control", region_name=region)
token = None
while True:
    page = client.list_agent_runtimes(**({"nextToken": token} if token else {}))
    for runtime in page.get("agentRuntimes", []):
        if runtime.get("agentRuntimeName") == name:
            print(runtime["agentRuntimeArn"])
            sys.exit(0)
    token = page.get("nextToken")
    if not token:
        sys.exit("No agent runtime named %r in %s" % (name, region))
' "$1" "$2"
}

if [ -z "${RUNTIME_ARN:-}" ]; then
    RUNTIME_ARN=$(resolve_runtime_arn "$REGION" "$RUNTIME_NAME")
fi

# A session id shorter than 33 characters is rejected by the API. Keep the same
# id across calls to reuse (and resume) the same EC2 instance and its EBS volume.
SESSION_ID="${SESSION_ID:-openclaw-$(whoami)-$(date +%s)-000000000000}"

echo "Invoking OpenClaw agent..."
echo "  Runtime: $RUNTIME_NAME"
echo "  ARN:     $RUNTIME_ARN"
echo "  Session: $SESSION_ID"
echo "  Prompt:  $PROMPT"
echo ""
echo "The first call on a new session provisions an EC2 instance (~90s-3min)."
echo ""

PAYLOAD=$(python3 -c "
import base64, json, sys
print(base64.b64encode(json.dumps({'prompt': sys.argv[1]}).encode()).decode())
" "$PROMPT")

aws bedrock-agentcore invoke-agent-runtime \
    --agent-runtime-arn "$RUNTIME_ARN" \
    --runtime-session-id "$SESSION_ID" \
    --payload "$PAYLOAD" \
    --region "$REGION" \
    --cli-read-timeout 300 \
    /dev/stdout
