#!/bin/bash
set -euo pipefail

# Deploy OpenClaw on AgentCore Runtime Instances
#
# This script:
# 1. Deploys CDK stacks (networking, storage, capacity provider prereqs, ECR image)
# 2. Creates AgentCore capacity provider via boto3 (if not exists)
# 3. Creates AgentCore agent runtime via boto3 (if not exists)
#
# Note: We use a Python boto3 helper for AgentCore API calls because:
# - The AWS CLI may not have bedrock-agentcore commands yet
# - The API shape uses nested structures best expressed as Python dicts
# - capacityProviderConfiguration uses capacityProviderArn (not name)
# - filesystemConfigurations is NOT supported with Instances compute
#
# Prerequisites:
# - AWS credentials configured
# - CDK bootstrapped (cdk bootstrap)
# - Docker running (for container image build)

REGION="${AWS_REGION:-us-east-1}"
STACK_PREFIX="OpenClaw"
# Note: Name regex is ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no hyphens!
CAPACITY_PROVIDER_NAME="openclaw_capacity_provider"
RUNTIME_NAME="openclaw_agent"

echo "============================================"
echo " OpenClaw on AgentCore Runtime Instances"
echo " Region: $REGION"
echo "============================================"

# Step 1: Deploy CDK stacks
echo ""
echo "[1/4] Deploying CDK stacks..."
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null || true
cdk deploy --all --require-approval never

# Step 2: Fetch stack outputs
echo ""
echo "[2/4] Fetching stack outputs..."
get_output() {
    aws cloudformation describe-stacks \
        --stack-name "$1" \
        --query "Stacks[0].Outputs[?OutputKey==\`$2\`].OutputValue" \
        --output text --region "$REGION"
}

SUBNET_IDS=$(get_output "$STACK_PREFIX-CapacityProvider" "SubnetIds")
SECURITY_GROUP_ID=$(get_output "$STACK_PREFIX-CapacityProvider" "SecurityGroupId")
INFRA_ROLE_ARN=$(get_output "$STACK_PREFIX-CapacityProvider" "InfrastructureRoleArn")
IMAGE_URI=$(get_output "$STACK_PREFIX-Runtime" "ContainerImageUri")
EXECUTION_ROLE_ARN=$(get_output "$STACK_PREFIX-Runtime" "ExecutionRoleArn")
BUCKET_NAME=$(get_output "$STACK_PREFIX-Storage" "BucketName")

echo "  Subnets: $SUBNET_IDS"
echo "  Security Group: $SECURITY_GROUP_ID"
echo "  Infrastructure Role: $INFRA_ROLE_ARN"
echo "  Image: $IMAGE_URI"
echo "  Execution Role: $EXECUTION_ROLE_ARN"
echo "  Backup Bucket: $BUCKET_NAME"

# Step 3 & 4: Create capacity provider and runtime via boto3
echo ""
echo "[3/4] Creating AgentCore resources via boto3..."

python3 - <<PYTHON_SCRIPT
import boto3
import json
import time
import sys

region = "${REGION}"
capacity_provider_name = "${CAPACITY_PROVIDER_NAME}"
runtime_name = "${RUNTIME_NAME}"
subnet_ids = "${SUBNET_IDS}".split(",")
security_group_id = "${SECURITY_GROUP_ID}"
infra_role_arn = "${INFRA_ROLE_ARN}"
image_uri = "${IMAGE_URI}"
execution_role_arn = "${EXECUTION_ROLE_ARN}"
bucket_name = "${BUCKET_NAME}"

client = boto3.client("bedrock-agentcore-control", region_name=region)

# --- Create Capacity Provider ---
print("\n  Creating capacity provider...")
try:
    resp = client.get_capacity_provider(name=capacity_provider_name)
    status = resp.get("status", "UNKNOWN")
    cp_arn = resp.get("capacityProviderArn", "")
    print(f"  Capacity provider '{capacity_provider_name}' already exists (status={status})")
except client.exceptions.ResourceNotFoundException:
    resp = client.create_capacity_provider(
        name=capacity_provider_name,
        computeConfiguration={
            "ec2Configuration": {
                "launchTemplateSource": {
                    "launchParameters": {
                        "instanceTypes": ["c7g.large"],
                    }
                },
                "vpcConfiguration": {
                    "subnetIds": subnet_ids,
                    "securityGroupIds": [security_group_id],
                },
                "rootVolume": {
                    "volumeType": "gp3",
                    "sizeInGb": 30,
                },
            }
        },
        permissionsConfiguration={
            "capacityProviderOperatorRoleArn": infra_role_arn,
        },
    )
    cp_arn = resp.get("capacityProviderArn", "")
    print(f"  Capacity provider created: {cp_arn}")
    print("  Waiting for READY status...")

    for i in range(60):
        time.sleep(10)
        resp = client.get_capacity_provider(name=capacity_provider_name)
        status = resp.get("status", "UNKNOWN")
        cp_arn = resp.get("capacityProviderArn", cp_arn)
        if status == "READY":
            print(f"  ✅ Capacity provider is READY")
            break
        print(f"    Status: {status} (waiting...)")
    else:
        print(f"  ⚠️  Capacity provider not READY after 10 min (status={status})")
        print("     It may still be provisioning. Check with:")
        print(f"     aws bedrock-agentcore-control get-capacity-provider --name {capacity_provider_name}")

# Get capacity provider ARN if we don't have it yet
if not cp_arn:
    resp = client.get_capacity_provider(name=capacity_provider_name)
    cp_arn = resp.get("capacityProviderArn", "")

if not cp_arn:
    print("  ❌ Could not get capacity provider ARN. Aborting.")
    sys.exit(1)

print(f"  Capacity Provider ARN: {cp_arn}")

# --- Create Agent Runtime ---
print("\n[4/4] Creating agent runtime...")
try:
    resp = client.get_agent_runtime(agentRuntimeName=runtime_name)
    status = resp.get("status", "UNKNOWN")
    print(f"  Runtime '{runtime_name}' already exists (status={status})")
    print("  Updating container image...")
    client.update_agent_runtime(
        agentRuntimeName=runtime_name,
        agentRuntimeArtifact={
            "containerConfiguration": {
                "containerUri": image_uri,
            }
        },
    )
    print("  ✅ Runtime updated.")
except client.exceptions.ResourceNotFoundException:
    # NOTE: Do NOT pass filesystemConfigurations with Instances compute!
    # "sessionStorage, EFS, and S3 Files storage types are not supported
    #  with capacityProviderConfiguration"
    resp = client.create_agent_runtime(
        agentRuntimeName=runtime_name,
        agentRuntimeArtifact={
            "containerConfiguration": {
                "containerUri": image_uri,
            }
        },
        roleArn=execution_role_arn,
        capacityProviderConfiguration={
            "capacityProviderArn": cp_arn,
        },
        environmentVariables={
            "S3_BACKUP_BUCKET": bucket_name,
            "S3_BACKUP_PREFIX": "workspace",
            "SYNC_INTERVAL": "300",
        },
    )
    print(f"  Runtime created.")
    print("  Waiting for READY status...")

    for i in range(60):
        time.sleep(10)
        resp = client.get_agent_runtime(agentRuntimeName=runtime_name)
        status = resp.get("status", "UNKNOWN")
        if status == "READY":
            print(f"  ✅ Runtime is READY")
            break
        print(f"    Status: {status} (waiting...)")
    else:
        print(f"  ⚠️  Runtime not READY after 10 min (status={status})")

print("\n============================================")
print(" Deployment complete!")
print("")
print(" Invoke your agent:")
print(f"   aws bedrock-agentcore-runtime invoke-agent-runtime \\\\")
print(f"     --agent-runtime-id {runtime_name} \\\\")
print(f'     --runtime-session-id "my-session" \\\\')
print(f"     --payload '{{\"prompt\": \"Hello!\"}}' \\\\")
print(f"     --region {region}")
print("")
print(" The first invocation provisions the EC2 instance (~2-3 min).")
print(" Subsequent invocations reuse the running instance.")
print(" Session resume after stop: 0s cold start (EBS persists).")
print("============================================")
PYTHON_SCRIPT

echo ""
echo "Done."
