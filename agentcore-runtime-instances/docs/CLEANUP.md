← Back to [README](../README.md)

# Cleanup

Three levels of cleanup depending on what you need:

## Quick: Stop sessions (keep infrastructure)

Stop your active AgentCore session to halt EC2 billing while preserving state:

```bash
# Stop a running session (EBS data persists — zero cost while stopped)
aws bedrock-agentcore stop-runtime-session \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/<RUNTIME_ID>" \
  --runtime-session-id "my-session" \
  --region us-east-1
```

## Medium: Delete runtime and capacity provider (keep data)

```bash
# Delete the agent runtime
python3 -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='us-east-1')
client.delete_agent_runtime(agentRuntimeName='openclaw_agent')
print('Runtime deleted')
"

# Delete the capacity provider
python3 -c "
import boto3
client = boto3.client('bedrock-agentcore-control', region_name='us-east-1')
client.delete_capacity_provider(name='openclaw_capacity_provider')
print('Capacity provider deleted')
"

# Destroy CDK stacks (VPC, IAM, ECR)
# Does NOT delete the workspace S3 bucket (RemovalPolicy.RETAIN)
cdk destroy --all
```

## Full: Remove everything including data

```bash
# 1. Delete AgentCore resources (see Medium above)

# 2. Destroy CDK stacks
cdk destroy --all

# 3. Delete the retained workspace bucket
#    (⚠️  This permanently deletes all agent backup data)
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name OpenClaw-Storage \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text 2>/dev/null || echo "already-deleted")

if [ "$BUCKET" != "already-deleted" ]; then
  # Must delete all versions (bucket is versioned)
  aws s3api list-object-versions --bucket "$BUCKET" \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({'Objects':d.get('Objects',[]),'Quiet':True}))" | \
    aws s3api delete-objects --bucket "$BUCKET" --delete file:///dev/stdin

  aws s3api list-object-versions --bucket "$BUCKET" \
    --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({'Objects':d.get('Objects',[]),'Quiet':True}))" | \
    aws s3api delete-objects --bucket "$BUCKET" --delete file:///dev/stdin

  aws s3 rb "s3://$BUCKET"
fi
```

## What gets billed while deployed

| Resource | Billing | How to stop |
|----------|---------|-------------|
| EC2 instance (c7g.large) | ~$0.07/hr while session is running | Auto-stops on idle; or manual stop |
| EBS volume (30GB gp3) | ~$0.08/day | Deleted when session expires (14 days) |
| S3 bucket | ~$0.023/GB/month | Negligible for small workspaces |
| ECR images | ~$0.10/GB/month | Removed on `cdk destroy` |

**Cost tip:** The agent instance auto-stops on idle — no manual intervention needed. When stopped, you only pay for EBS (~$2.40/mo) and S3 (pennies). No NAT Gateway costs.
