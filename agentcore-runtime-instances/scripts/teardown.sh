#!/bin/bash
set -euo pipefail

# Teardown — remove all AgentCore and CDK resources

REGION="${AWS_REGION:-us-east-1}"
RUNTIME_NAME="${RUNTIME_NAME:-openclaw-agent}"
CAPACITY_PROVIDER_NAME="${CAPACITY_PROVIDER_NAME:-openclaw-capacity-provider}"

echo "============================================"
echo " Tearing down OpenClaw AgentCore Instances"
echo "============================================"
echo ""

# Step 1: Delete runtime (stops all sessions, deletes session storage)
echo "[1/3] Deleting agent runtime '$RUNTIME_NAME'..."
aws bedrock-agentcore-control delete-agent-runtime \
    --agent-runtime-name "$RUNTIME_NAME" \
    --region "$REGION" 2>/dev/null || echo "  (not found or already deleted)"

# Step 2: Delete capacity provider (terminates instances, deletes persistent volumes)
echo "[2/3] Deleting capacity provider '$CAPACITY_PROVIDER_NAME'..."
aws bedrock-agentcore-control delete-capacity-provider \
    --name "$CAPACITY_PROVIDER_NAME" \
    --region "$REGION" 2>/dev/null || echo "  (not found or already deleted)"

# Step 3: Destroy CDK stacks
echo "[3/3] Destroying CDK stacks..."
cd "$(dirname "$0")/.."
cdk destroy --all --force

echo ""
echo "Done. Note: S3 bucket is retained (RemovalPolicy.RETAIN)."
echo "Delete manually if you want to remove all workspace data:"
echo "  aws s3 rb s3://<bucket-name> --force"
