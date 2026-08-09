# Multi-Tenancy Considerations

Considerations for deploying OpenClaw on AgentCore Runtime Instances to multiple users.

## Isolation Model: Silo

This sample implements a **Silo Model** — each user gets fully isolated, dedicated resources:

| Resource | Isolation | Managed By |
|----------|-----------|------------|
| EC2 instance | Per-user | AgentCore (auto-provision on first invoke) |
| EBS volume | Per-user | AgentCore (persists across stop/resume) |
| Workspace state | Per-user | OpenClaw (memory, history, config) |
| S3 backup | Per-user prefix | Container sync logic |
| Capacity provider | Shared (infra template) | You (created once) |
| Agent runtime | Shared (container image) | You (created once) |
| Container image | Shared (ECR) | CDK |

AgentCore enforces the isolation boundary — each `runtimeSessionId` maps to a separate EC2 instance with its own EBS volume. No tenant can access another tenant's compute or storage.

### Why Silo

- **AWS-enforced isolation** — separate EC2, separate EBS, no application-level trust required
- **No noisy neighbor** — each user's agent runs on dedicated compute
- **Independent lifecycle** — each user's instance stops/resumes independently (pay only when active)
- **Simple mental model** — one user = one session = one instance

## Deploying for Multiple Users

The same silo, repeated per user. No code changes needed — just different `runtimeSessionId` values.

### What's shared (created once)

```
┌─────────────────────────────────────────────┐
│  Shared infrastructure (deploy once)        │
│                                             │
│  • Capacity provider (instance type, VPC)   │
│  • Agent runtime (container image, IAM)     │
│  • S3 backup bucket                         │
│  • VPC / networking                         │
└─────────────────────────────────────────────┘
```

### What's per-user (created automatically)

```
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│  User A          │ │  User B          │ │  User C          │
│  Session: usr-a  │ │  Session: usr-b  │ │  Session: usr-c  │
│  EC2: dedicated  │ │  EC2: dedicated  │ │  EC2: dedicated  │
│  EBS: dedicated  │ │  EBS: dedicated  │ │  EBS: dedicated  │
│  S3: prefix/a/   │ │  S3: prefix/b/   │ │  S3: prefix/c/   │
└──────────────────┘ └──────────────────┘ └──────────────────┘
```

AgentCore provisions the per-user instance automatically on first `InvokeAgentRuntime` call with a new `runtimeSessionId`. No manual provisioning required.

### Adding a Router for Centralized Channel Ingestion

For teams using a single bot token (one Telegram bot, one Discord app) serving multiple users, add a Router Lambda. This is the same pattern used by the single-user [Channel Router](CHANNEL_ROUTER.md) — webhook → Lambda → `invoke-agent-runtime` — extended with a DynamoDB lookup to resolve *which* user's session to route to:

```
Users (Telegram / Slack / Discord)
    │
    ▼
┌──────────────────────────────┐
│  Router Lambda               │  ← resolves user identity
│  + API Gateway (webhooks)    │  ← maps user → runtimeSessionId
│  + DynamoDB (user table)     │  ← stores session mappings
└──────────────┬───────────────┘
               │ InvokeAgentRuntime(runtimeSessionId=user-specific)
         ┌─────┼─────┐
         ▼     ▼     ▼
      User A  User B  User C   ← per-user AgentCore sessions (silo)
```

The Router Lambda:
1. Receives webhook from messaging platform
2. Resolves user identity (channel + userId → DynamoDB lookup)
3. Calls `InvokeAgentRuntime` with the user's `runtimeSessionId`
4. Returns response to the messaging platform

### User Provisioning

```python
# First-time user onboarding
def provision_user(user_id, channel_type, channel_id):
    dynamodb.put_item(
        TableName="openclaw-users",
        Item={
            "userId": user_id,
            "channelType": channel_type,
            "channelId": channel_id,
            "sessionId": f"session-{user_id}",
            "createdAt": datetime.utcnow().isoformat()
        }
    )
    # No need to pre-create the AgentCore session.
    # It's provisioned automatically on first InvokeAgentRuntime call.
```

### Shared Context Across Users

In a team deployment, all agents can share organizational knowledge without breaking silo isolation:

| Shared Context | How | Isolation Impact |
|---------------|-----|-----------------|
| Company instructions (AGENTS.md) | Baked into container image | None — read-only, same for all |
| Shared skills | Pre-installed in container | None — read-only |
| Guardrails | Bedrock Guardrails at Router Lambda | None — applied before routing |
| Org knowledge base | Shared S3 read-only prefix | None — read-only mount |

Each user still gets their own private workspace — the shared context is read-only material baked into the container or applied at the routing layer.

## Cost Model (Silo)

Per-user costs with auto-stop on idle:

| State | Cost per user |
|-------|--------------|
| Active (responding to messages) | ~$0.07/hr (c7g.large) |
| Idle (EBS persists, EC2 stopped) | ~$0.08/day |
| Dormant (no activity for 14 days) | ~$0.02/day (S3 backup only) |

**Example: 50 users, each active 2 hours/day:**
- EC2: 50 × 2hr × $0.07 = ~$7/day
- EBS: 50 × $2.40/mo = ~$120/mo
- Router Lambda: ~$1/mo
- **Total: ~$330/mo** (vs $2,650/mo if always-on)

## Pool Model (Reference)

AgentCore supports multiple agents per session (shared EC2 instance). In theory, multiple users could share one instance with separate OpenClaw processes. However:

- **OpenClaw is single-tenant by design** — one gateway per user
- Multiple gateway processes on one host requires custom orchestration
- No filesystem isolation between co-located users
- Resource contention between users

**Not recommended for OpenClaw.** Use the Silo Model.

For teams wanting pool-model economics with OpenClaw, consider [AgentCore microVMs](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore) which provides per-user Firecracker containers with serverless pricing.

## Security

See [AgentCore Runtime Instances Security Model](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html) for:
- Session routing and multi-tenant isolation
- Session-to-user binding best practices
- Per-tenant vs shared IAM principals

Key point from the docs: *"AgentCore Runtime authorizes invocations against the agent runtime resource ARN, not against individual sessions."* Your Router Lambda must enforce session-to-user binding — never accept `runtimeSessionId` from untrusted client input.

## Related Projects

- **[Multi-tenant EKS Platform](https://github.com/aws-samples/sample-openclaw-multi-tenant-platform)** (⭐38) — EKS + CDK + KEDA scale-to-zero + ArgoCD + Pod Identity, namespace-per-user isolation
- **[Multi-tenancy on EKS + Kata](https://github.com/aws-samples/sample-multi-tenancy-openclaw-on-eks)** (⭐30) — Go orchestrator + Kata Containers VM isolation + Karpenter autoscaling + Redis routing + S3 state sync
- **[OpenClaw Pool on Firecracker](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker)** (⭐23) — EC2 Firecracker KVM microVMs + multi-AZ failover + web management console + Prometheus/Grafana
- **[EC2 Enterprise — Tenant Router](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock/blob/main/README_ENTERPRISE.md)** — H2 proxy + per-tenant Firecracker microVMs + Bedrock Guardrails
- **[AgentCore Session Security](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html)** — Session routing and multi-tenant isolation docs
- **[AWS SaaS Lens: Silo, Pool, and Bridge Models](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-pool-and-bridge-models.html)**
