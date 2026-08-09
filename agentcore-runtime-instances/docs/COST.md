← Back to [README](../README.md)

# Cost Estimate

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| EC2 (c7g.large) | ~$53 max | **$0 when stopped** (auto-stop on idle) |
| EBS (30GB gp3) | ~$2.40 | Persists even when instance is stopped |
| S3 (backup bucket) | ~$0.02/GB | Workspace typically <1GB |
| S3 Gateway Endpoint | $0 | Free — used for backup sync |
| Bedrock (Claude Sonnet 4.6) | Usage-based | $3/$15 per 1M input/output tokens |
| Channel Router (Lambda + API Gateway + DynamoDB) | ~$0 | Free tier covers typical personal use; see [Channel Router](CHANNEL_ROUTER.md) |
| **Total (active use)** | **~$55/mo** | Plus token costs |
| **Total (mostly idle)** | **~$3/mo** | Agent auto-stops, only S3 + EBS costs |

**Cost optimization:** The agent automatically stops when idle (via `idleInstanceTimeout`). You only pay for EC2 while actively using it. When stopped, you only pay for EBS storage (~$2.40/mo) and S3 backup (~pennies). No NAT Gateway — S3 access uses a free VPC Gateway Endpoint and AgentCore manages container internet access.
