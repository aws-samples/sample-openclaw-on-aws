← Back to [README](../README.md)

# Cost Estimate

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| EC2 (c7g.large) | ~$53 max | **$0 when stopped** (auto-stop on idle) |
| EBS (30GB gp3) | ~$2.40 | Persists even when instance is stopped |
| S3 (backup bucket) | ~$0.02/GB | Workspace typically <1GB |
| S3 Gateway Endpoint | $0 | Free — used for backup sync |
| NAT Gateway | ~$32–35 | ~$0.045/hr fixed + ~$0.045/GB data processing — runs continuously, does **not** auto-stop with the instance |
| Bedrock (Claude Sonnet 4.6) | Usage-based | $3/$15 per 1M input/output tokens |
| Channel Router (Lambda + API Gateway + DynamoDB) | ~$0 | Free tier covers typical personal use; see [Channel Router](CHANNEL_ROUTER.md) |
| **Total (active use)** | **~$88/mo** | Plus token costs |
| **Total (mostly idle)** | **~$36/mo** | NAT Gateway keeps billing even when the agent is stopped; only EC2 scales to $0 |

**Cost optimization:** The agent automatically stops when idle (via `idleRuntimeSessionTimeout`). You only pay for EC2 while actively using it. When stopped, you only pay for EBS storage (~$2.40/mo), S3 backup (~pennies), and the NAT Gateway (~$32–35/mo — it has no idle state, unlike the EC2 instance).

**Why there's a NAT Gateway at all:** AgentCore's own networking layer handles internet access for other AgentCore Runtime compute types (microVMs), but Instances compute launches real EC2 into your VPC subnets, so those instances need an actual route to the internet for ECR, Bedrock, SSM, and ClawHub. S3 access itself stays free via the VPC Gateway Endpoint regardless. See [Configuration — Networking](CONFIGURATION.md#networking) for the full explanation, including why VPC interface endpoints reduce but can't eliminate the NAT Gateway need here (Telegram/Discord/Slack/ClawHub are non-AWS SaaS).
