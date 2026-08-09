← Back to [README](../README.md)

# Configuration

## Model

Set in `container/.openclaw/openclaw.json`:

```json
{
  "models": {
    "providers": {
      "amazon-bedrock": {
        "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com",
        "api": "bedrock-converse-stream",
        "auth": "aws-sdk",
        "models": [{ "id": "global.anthropic.claude-sonnet-4-6", "name": "Claude Sonnet 4.6" }]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": { "primary": "amazon-bedrock/global.anthropic.claude-sonnet-4-6" }
    }
  }
}
```

## Instance Type

Default is `c7g.large` (2 vCPU, 4GB RAM, Graviton ARM64). Change in `stacks/capacity_provider_stack.py`. Supported types include GPU instances (g4dn, g5, g6) for advanced workloads.

## Storage

**EBS root volume** (30GB gp3) provides the live workspace. Defined in the capacity provider configuration. Persists across session stop/resume — zero cold start.

**S3 bucket** (versioned, lifecycle rules) provides background backup. The container syncs workspace→S3 every 5 minutes. S3 restores are only used when the EBS workspace is empty (session expired after 14 days).

## Supported Regions

AgentCore Runtime Instances is available in the following AWS regions:

| Region Name | Region Code |
|-------------|-------------|
| US East (N. Virginia) | `us-east-1` |
| US East (Ohio) | `us-east-2` |
| US West (Oregon) | `us-west-2` |
| Asia Pacific (Mumbai) | `ap-south-1` |
| Asia Pacific (Singapore) | `ap-southeast-1` |
| Asia Pacific (Sydney) | `ap-southeast-2` |
| Asia Pacific (Tokyo) | `ap-northeast-1` |
| Europe (Frankfurt) | `eu-central-1` |
| Europe (Ireland) | `eu-west-1` |

Set your region via the `AWS_REGION` environment variable, the `region` context in `cdk.json`, or let it default to `us-east-1`.

## IAM Roles

The deployment creates three IAM roles:

### Execution Role (agent permissions)

This is what your agent code runs as. Defined in `stacks/runtime_stack.py`:

| Permission | Purpose |
|------------|--------|
| `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` | Call Claude/other models via Bedrock |
| `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` | Pull container image |
| `logs:CreateLogGroup`, `logs:PutLogEvents` | CloudWatch Logs |
| `cloudwatch:PutMetricData` | CloudWatch Metrics |
| `xray:PutTraceSegments` | X-Ray tracing |
| `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` | Workspace backup sync |
| `secretsmanager:GetSecretValue` | Channel tokens from Secrets Manager (`openclaw/*` prefix) |
| `ssm:GetParameter` | S3 backup bucket discovery via SSM Parameter Store (`/openclaw/*` prefix) |

The execution role is scoped to Bedrock, observability, and the backup bucket. The agent **cannot** create IAM roles, launch EC2, modify security groups, or access other AWS resources unless you explicitly add permissions.

### Infrastructure Role (AgentCore provisioning)

Assumed by AgentCore to manage EC2 instances in your account. Defined in `stacks/capacity_provider_stack.py`. Scope it with IAM conditions to restrict to specific VPCs/subnets.

### Instance Profile Role (host-level)

Attached to the EC2 instance. Used by AgentCore for system log collection. Also includes `AmazonSSMManagedInstanceCore` for optional SSM access.

### Extending permissions

To give your agent access to additional AWS services (e.g., DynamoDB, SQS, additional S3 buckets), add policies to the execution role in `stacks/runtime_stack.py`:

```python
self.execution_role.add_to_policy(
    iam.PolicyStatement(
        sid="DynamoDBAccess",
        actions=["dynamodb:GetItem", "dynamodb:PutItem"],
        resources=["arn:aws:dynamodb:us-east-1:*:table/my-table"],
    )
)
```

## Channel Tokens with Secrets Manager

For production deployments, store channel tokens in AWS Secrets Manager instead of passing them via the invoke API.

### Setup

```bash
# 1. Create a secret with your channel tokens
aws secretsmanager create-secret \
  --name openclaw/channels \
  --secret-string '{"telegram": "123456:ABC...", "discord": "MTIz..."}' \
  --region us-east-1

# 2. Set the environment variable in your capacity provider or container config
# Add to container env: CHANNEL_SECRETS_ARN=openclaw/channels
```

### How it works

On container boot, `main.py` checks for the `CHANNEL_SECRETS_ARN` environment variable. If set:
1. Fetches the secret value from Secrets Manager using `aws secretsmanager get-secret-value`
2. Parses the JSON (`{"telegram": "TOKEN", "discord": "TOKEN", ...}`)
3. Runs `patch_channels.py` to write channel config into `openclaw.json` (maps `telegram` → `{"botToken": "..."}` etc.)
4. Gateway starts with channels pre-configured

### Secret format

```json
{
  "telegram": "123456789:ABCdefGHIjklMNO",
  "discord": "MTIzNDU2Nzg5.GG3...",
  "slack_app_token": "xapp-1-...",
  "slack_bot_token": "xoxb-..."
}
```

Only include the channels you want to connect. The execution role is scoped to secrets with the `openclaw/*` prefix.

### Alternative: connect via invoke

For personal deployments, you can skip Secrets Manager and connect channels conversationally:

```bash
./scripts/connect-channel.sh telegram "YOUR_BOT_TOKEN"
```

Tokens are transmitted over TLS and stored on the encrypted EBS volume. The base64-encoded payload appears in CloudTrail logs. This is acceptable for single-user accounts but not recommended for shared environments.
