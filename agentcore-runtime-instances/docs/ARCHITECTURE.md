← Back to [README](../README.md)

# Architecture

```
User (WhatsApp / Telegram / Discord / Slack)
       │
       │  (native channels — outbound connections from OpenClaw)
       ▼
┌─────────────────────────────────────────────────────────────────┐
│  AgentCore Runtime Instance (c7g.large, ARM64)                  │
│                                                                 │
│  ┌──────────────────────┐     ┌──────────────────────────────┐ │
│  │  AgentCore Wrapper   │     │  OpenClaw Gateway            │ │
│  │  (Python :8080)      │HTTP→│  (Node.js :18789)            │ │
│  │  /invocations + /ping│     │  tools/memory/MCP/skills     │ │
│  └──────────────────────┘     └──────────────────────────────┘ │
│           ↑                              ↑          ↑          │
│           │ AgentCore                    │ Bedrock  │ Channels │
│           │ Protocol                     │ (IAM)    │ (outbound)│
└───────────┼──────────────────────────────┼──────────┼──────────┘
            │                              │          │
     InvokeAgentRuntime             Amazon Bedrock   Telegram/
     (first invocation               (Claude 4.6)   Discord/
      provisions instance)                          WhatsApp
            │
            ▼
┌───────────────────────────────────────────────────────────────┐
│  EBS Root Volume (30GB gp3)                                   │
│  /home/agent/.openclaw/                                        │
│  ├── openclaw.json    ← config                                │
│  ├── workspace/       ← memory, agents, tools                 │
│  ├── sessions/        ← conversation history                  │
│  └── ...                                                      │
│                                                               │
│  ✅ Persists across session stop/resume                       │
│  ✅ Zero cold start on resume                                 │
│  ✅ Defined in capacity provider                              │
└───────────────────────────────────────────────────────────────┘
         │
         │  aws s3 sync (every 5 min, background, non-blocking)
         ▼
┌───────────────────────────────────────────────────────────────┐
│  S3 Bucket (versioned, lifecycle rules)                       │
│  ← backup / disaster recovery                                 │
│  ← restore only if EBS is empty                               │
│    (session expired after 14 days — rare)                     │
└───────────────────────────────────────────────────────────────┘
```

## Persistence Model: EBS-First, S3-Backup

| Scenario | What happens | Cold start |
|----------|-------------|-----------|
| **Session resume** (after idle stop) | EBS volume still attached, workspace intact | **0 seconds** |
| **New user** (first invocation) | Workspace initialized from container defaults | **0 seconds** |
| **Session expired** (after 14 days) | Restore from S3 backup | **~3 seconds** |

The EBS root volume (defined in the capacity provider) persists across session stop/resume cycles. This means:

- **Normal operation:** Agent stops when idle, resumes instantly on next message. Zero cold start.
- **Background S3 sync:** Every 5 minutes, workspace is synced to S3 (non-blocking). This is insurance, not the primary persistence mechanism.
- **Final sync on SIGTERM:** When AgentCore stops the session, a final sync ensures S3 is up-to-date.
- **S3 restore (rare):** Only triggered when the workspace directory is empty — which only happens if the 14-day session TTL expires and the EBS volume is reclaimed.

## How Channels Work (No Lambda Required)

Unlike the microVM approach which needs a Router Lambda to wake containers, Runtime Instances runs a persistent OpenClaw gateway that handles channels natively:

- **Telegram**: Long-polling (outbound connection, no webhook URL needed)
- **Discord**: WebSocket bot connection (outbound)
- **WhatsApp**: WhatsApp Web pairing (outbound)
- **Slack**: Socket Mode (outbound WebSocket, no public URL needed)

The gateway stays running for the session lifetime (up to 14 days). No inbound webhooks, no API Gateway, no public IP required.

## Why Runtime Instances?

| Feature | EC2 Standalone | AgentCore microVMs | **AgentCore Instances** |
|---------|---------------|-------------------|------------------------|
| Session duration | Unlimited (self-managed) | Up to 8 hours | **Up to 14 days** |
| Persistence | EBS (self-managed) | S3 sync (custom code) | **EBS root volume (managed)** |
| Cold start | None (always on) | Per-invocation | **0s on resume** (EBS persists) |
| Idle cost | Full EC2 cost 24/7 | $0 (serverless) | **$0 (auto-stop on idle)** |
| GPU support | Manual setup | ❌ | ✅ |
| Patching/lifecycle | You manage | AWS managed | **AWS managed** |
| Auto-stop on idle | Manual implementation | N/A (ephemeral) | **Built-in (idleInstanceTimeout)** |
| Auto-resume | N/A | N/A | **Automatic on next invocation** |
| Multi-agent | Manual | ❌ | ✅ (shared session) |
| Cost model | EC2 on-demand | Pay-per-use | EC2 in your account (Savings Plans apply) |

## Why Instances > Plain EC2

Running OpenClaw on a self-managed EC2 instance works, but AgentCore Instances adds significant operational advantages:

| Capability | Plain EC2 | AgentCore Instances |
|-----------|-----------|---------------------|
| **Auto-stop on idle** | DIY (cron + CloudWatch) | Built-in `idleInstanceTimeout` |
| **Auto-resume on message** | Not possible without proxy | Automatic via `InvokeAgentRuntime` |
| **Managed patching** | SSM Patch Manager (you configure) | AWS-managed, zero-downtime |
| **Built-in tracing** | Install X-Ray SDK manually | Native X-Ray integration |
| **Health monitoring** | DIY health checks | Built-in `/ping` + auto-restart |
| **Session lifecycle** | Manual state machine | Managed (PENDING→READY→STOPPED→READY) |
| **GPU support** | Manual driver install | Managed GPU instance types |
| **Zero cold start on resume** | ✅ (always on, paying 24/7) | ✅ (EBS persists, $0 when stopped) |

**Bottom line:** You get the persistence of EC2 with serverless economics (stop paying when idle) and managed operations (no patching, no health check scripting, no auto-scaling logic).

## Comparison with Other Deployment Options

| Feature | This (Instances) | [EC2 Standalone](../README.md) | [AgentCore microVMs](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore) |
|---------|-----------------|-------------------------------|--------------------------------------|
| Managed lifecycle | ✅ AWS patches/updates | ❌ You manage | ✅ AWS managed |
| Session persistence | EBS (permanent) + S3 backup | EBS (self-managed) | 8 hours + S3 sync |
| Native channels | ✅ (persistent gateway) | ✅ (persistent gateway) | ❌ (needs Lambda relay) |
| Cold start (resume) | **0 seconds** (EBS persists) | None (always on, paying) | Every invocation |
| GPU support | ✅ | Manual | ❌ |
| Cost when idle | **$0** (auto-stop) | ~$53/mo (EC2 running) | $0 (serverless) |
| Auto-stop on idle | ✅ Built-in | ❌ | N/A |
| Auto-resume | ✅ On next invocation | ❌ | N/A |
| Multi-user | Per-user sessions | Single user | Per-user microVM |
| Setup complexity | CDK + boto3 (medium) | CloudFormation (low) | CDK (high) |
