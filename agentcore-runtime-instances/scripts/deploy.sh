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
# - CreateCapacityProvider requires launchParameters.operatingSystem and
#   instanceRequirements.allowedInstanceTypes; VPC keys are subnets/
#   securityGroups; rootVolume takes freeSpaceGiB. Get/DeleteCapacityProvider
#   take capacityProviderId, so the name must be resolved via List first.
#   (Verified against the API in September 2026.)
#
# Prerequisites:
# - AWS credentials configured
# - CDK bootstrapped (cdk bootstrap)
# - A container builder for the arm64 image build. Docker Desktop works; if it
#   is unavailable (for example an org sign-in requirement blocks builds), set
#   CDK_DOCKER=finch and start the Finch VM instead.

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
INSTANCE_PROFILE_ARN=$(get_output "$STACK_PREFIX-CapacityProvider" "InstanceProfileArn")
IMAGE_URI=$(get_output "$STACK_PREFIX-Runtime" "ContainerImageUri")
EXECUTION_ROLE_ARN=$(get_output "$STACK_PREFIX-Runtime" "ExecutionRoleArn")
BUCKET_NAME=$(get_output "$STACK_PREFIX-Storage" "BucketName")

echo "  Subnets: $SUBNET_IDS"
echo "  Security Group: $SECURITY_GROUP_ID"
echo "  Infrastructure Role: $INFRA_ROLE_ARN"
echo "  Instance Profile: $INSTANCE_PROFILE_ARN"
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
instance_profile_arn = "${INSTANCE_PROFILE_ARN}"
image_uri = "${IMAGE_URI}"
execution_role_arn = "${EXECUTION_ROLE_ARN}"
bucket_name = "${BUCKET_NAME}"

client = boto3.client("bedrock-agentcore-control", region_name=region)

# --- Create Capacity Provider ---
print("\n  Creating capacity provider...")

# Get/Delete take capacityProviderId, not name, so resolve name -> id via List.
def find_capacity_provider():
    paginator_token = None
    while True:
        kwargs = {"nextToken": paginator_token} if paginator_token else {}
        page = client.list_capacity_providers(**kwargs)
        for provider in page.get("capacityProviders", []):
            if provider.get("name") == capacity_provider_name:
                return client.get_capacity_provider(
                    capacityProviderId=provider["capacityProviderId"]
                )
        paginator_token = page.get("nextToken")
        if not paginator_token:
            return None

existing = find_capacity_provider()
if existing:
    cp_arn = existing.get("capacityProviderArn", "")
    cp_id = existing.get("capacityProviderId", "")
    print(f"  Capacity provider '{capacity_provider_name}' already exists "
          f"(status={existing.get('status', 'UNKNOWN')})")
else:
    resp = client.create_capacity_provider(
        name=capacity_provider_name,
        computeConfiguration={
            "ec2Configuration": {
                "launchTemplateSource": {
                    "launchParameters": {
                        # Required: the sample image is arm64 (Graviton).
                        "operatingSystem": "LINUX_ARM64",
                        "instanceRequirements": {
                            "allowedInstanceTypes": ["c7g.large"],
                        },
                        "instanceProfileArn": instance_profile_arn,
                    }
                },
                # Note: these keys are `subnets`/`securityGroups`, not
                # `subnetIds`/`securityGroupIds`.
                "vpcConfiguration": {
                    "subnets": subnet_ids,
                    "securityGroups": [security_group_id],
                },
                # Root volume takes guaranteed free space, not total size;
                # AgentCore adds OS overhead on top.
                "rootVolume": {
                    "volumeType": "gp3",
                    "freeSpaceGiB": 30,
                    "encrypted": True,
                },
                "lifecycleConfiguration": {
                    "idleInstanceTimeout": 14400,   # 4h
                    "maxLifetime": 1209600,         # 14 days
                },
            }
        },
        permissionsConfiguration={
            "capacityProviderOperatorRoleArn": infra_role_arn,
        },
    )
    cp_arn = resp.get("capacityProviderArn", "")
    cp_id = resp.get("capacityProviderId", "")
    print(f"  Capacity provider created: {cp_arn}")
    print("  Waiting for READY status...")

    for i in range(60):
        time.sleep(10)
        resp = client.get_capacity_provider(capacityProviderId=cp_id)
        status = resp.get("status", "UNKNOWN")
        cp_arn = resp.get("capacityProviderArn", cp_arn)
        if status == "READY":
            print("  ✅ Capacity provider is READY")
            break
        if "FAIL" in status:
            print(f"  ❌ Capacity provider {status}: "
                  f"{resp.get('statusReason', 'no reason given')}")
            sys.exit(1)
        print(f"    Status: {status} (waiting...)")
    else:
        print(f"  ⚠️  Capacity provider not READY after 10 min (status={status})")
        print("     It may still be provisioning. Check with:")
        print(f"     aws bedrock-agentcore-control get-capacity-provider "
              f"--capacity-provider-id {cp_id}")

if not cp_arn:
    print("  ❌ Could not get capacity provider ARN. Aborting.")
    sys.exit(1)

print(f"  Capacity Provider ARN: {cp_arn}")

# --- Create Agent Runtime ---
print("\n[4/4] Creating agent runtime...")

# Get/Update/Delete take agentRuntimeId, not agentRuntimeName, so resolve the
# name through List first.
def find_agent_runtime():
    next_token = None
    while True:
        kwargs = {"nextToken": next_token} if next_token else {}
        page = client.list_agent_runtimes(**kwargs)
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == runtime_name:
                return runtime
        next_token = page.get("nextToken")
        if not next_token:
            return None

existing_runtime = find_agent_runtime()
if existing_runtime:
    runtime_id = existing_runtime["agentRuntimeId"]
    print(f"  Runtime '{runtime_name}' already exists "
          f"(status={existing_runtime.get('status', 'UNKNOWN')})")
    print("  Updating container image...")
    # Resend the full configuration: UpdateAgentRuntime replaces the runtime's
    # configuration rather than patching it, so omitting these would drop the
    # capacity provider association, lifecycle window and environment.
    client.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact={
            "containerConfiguration": {
                "containerUri": image_uri,
            }
        },
        roleArn=execution_role_arn,
        capacityProviderConfiguration={
            "capacityProviderArn": cp_arn,
        },
        lifecycleConfiguration={
            "idleRuntimeSessionTimeout": 14400,
            "maxLifetime": 1209600,
        },
        environmentVariables={
            "S3_BACKUP_BUCKET": bucket_name,
            "S3_BACKUP_PREFIX": "workspace",
            "SYNC_INTERVAL": "300",
            "AWS_REGION": region,
        },
    )
    print("  ✅ Runtime updated.")
else:
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
        lifecycleConfiguration={
            "idleRuntimeSessionTimeout": 14400,  # 4h rolling window (see docs/RUNTIME_BEHAVIOR.md)
            "maxLifetime": 1209600,  # 14 days
        },
        environmentVariables={
            "S3_BACKUP_BUCKET": bucket_name,
            "S3_BACKUP_PREFIX": "workspace",
            "SYNC_INTERVAL": "300",
            "AWS_REGION": region,
        },
    )
    runtime_id = resp["agentRuntimeId"]
    print(f"  Runtime created: {resp.get('agentRuntimeArn', runtime_id)}")
    print("  Waiting for READY status...")

    for i in range(60):
        time.sleep(10)
        resp = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = resp.get("status", "UNKNOWN")
        if status == "READY":
            print("  ✅ Runtime is READY")
            break
        if "FAIL" in status:
            print(f"  ❌ Runtime {status}: {resp.get('statusReason', '')}")
            sys.exit(1)
        print(f"    Status: {status} (waiting...)")
    else:
        print(f"  ⚠️  Runtime not READY after 10 min (status={status})")

runtime_arn = client.get_agent_runtime(agentRuntimeId=runtime_id).get("agentRuntimeArn", "")

print("\n============================================")
print(" Deployment complete!")
print("")
print(f" Runtime ARN: {runtime_arn}")
print("")
print(" Invoke your agent (session id must be at least 33 characters, and the")
print(" payload is base64-encoded):")
print("")
print(f'   SESSION_ID="my-openclaw-session-$(date +%s)"')
print("   PAYLOAD=$(echo -n '{\"prompt\":\"Hello!\"}' | base64)")
print("   aws bedrock-agentcore invoke-agent-runtime \\\\")
print(f'     --agent-runtime-arn "{runtime_arn}" \\\\')
print('     --runtime-session-id "$SESSION_ID" \\\\')
print('     --payload "$PAYLOAD" \\\\')
print(f"     --region {region} \\\\")
print("     --cli-read-timeout 300 /dev/stdout")
print("")
print(" The first invocation provisions the EC2 instance (~2-3 min).")
print(" Subsequent invocations reuse the running instance.")
print(" Session resume after stop: 0s cold start (EBS persists).")
print("============================================")
PYTHON_SCRIPT

echo ""
echo "Done."
