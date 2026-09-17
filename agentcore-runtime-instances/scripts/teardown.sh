#!/bin/bash
set -euo pipefail

# Teardown — remove all AgentCore and CDK resources
#
# API notes (verified September 2026): DeleteAgentRuntime takes
# --agent-runtime-id and DeleteCapacityProvider takes --capacity-provider-id,
# so both names are resolved to ids first. The runtime must be deleted before
# its capacity provider: a capacity provider with associated runtimes cannot
# be deleted.
#
# Deleting the capacity provider terminates its managed EC2 instances and
# deletes the sessions' persistent EBS volumes.

REGION="${AWS_REGION:-us-east-1}"
# Note: Name regex is ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no hyphens! Must match deploy.sh.
RUNTIME_NAME="${RUNTIME_NAME:-openclaw_agent}"
CAPACITY_PROVIDER_NAME="${CAPACITY_PROVIDER_NAME:-openclaw_capacity_provider}"

echo "============================================"
echo " Tearing down OpenClaw AgentCore Instances"
echo " Region: $REGION"
echo "============================================"
echo ""

echo "[1/3] Deleting agent runtime '$RUNTIME_NAME'..."
python3 - "$REGION" "$RUNTIME_NAME" <<'PYTHON'
import sys
import boto3

region, name = sys.argv[1], sys.argv[2]
client = boto3.client("bedrock-agentcore-control", region_name=region)
token = None
while True:
    page = client.list_agent_runtimes(**({"nextToken": token} if token else {}))
    for runtime in page.get("agentRuntimes", []):
        if runtime.get("agentRuntimeName") == name:
            client.delete_agent_runtime(agentRuntimeId=runtime["agentRuntimeId"])
            print(f"  Deleted runtime {runtime['agentRuntimeId']}")
            sys.exit(0)
    token = page.get("nextToken")
    if not token:
        print("  (not found or already deleted)")
        sys.exit(0)
PYTHON

echo "[2/3] Deleting capacity provider '$CAPACITY_PROVIDER_NAME'..."
python3 - "$REGION" "$CAPACITY_PROVIDER_NAME" <<'PYTHON'
import sys
import time
import boto3

region, name = sys.argv[1], sys.argv[2]
client = boto3.client("bedrock-agentcore-control", region_name=region)
token = None
target = None
while True:
    page = client.list_capacity_providers(**({"nextToken": token} if token else {}))
    for provider in page.get("capacityProviders", []):
        if provider.get("name") == name:
            target = provider["capacityProviderId"]
            break
    token = page.get("nextToken")
    if target or not token:
        break

if not target:
    print("  (not found or already deleted)")
    sys.exit(0)

# A runtime deleted a moment ago may still be associated; retry briefly.
for attempt in range(6):
    try:
        client.delete_capacity_provider(capacityProviderId=target)
        print(f"  Deleted capacity provider {target}")
        sys.exit(0)
    except client.exceptions.ResourceNotFoundException:
        print("  (already deleted)")
        sys.exit(0)
    except Exception as exc:  # ConflictException while runtimes detach
        if attempt == 5:
            sys.exit(f"  Could not delete capacity provider {target}: {exc}")
        print(f"  Waiting for runtimes to detach ({exc.__class__.__name__})...")
        time.sleep(20)
PYTHON

echo "[3/3] Destroying CDK stacks..."
cd "$(dirname "$0")/.."
cdk destroy --all --force

echo ""
echo "Done. Note: S3 bucket is retained (RemovalPolicy.RETAIN)."
echo "Delete manually if you want to remove all workspace data:"
echo "  aws s3 rb s3://<bucket-name> --force"
