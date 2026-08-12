# Multi-Tenancy Considerations

Considerations for deploying OpenClaw on AgentCore Runtime Instances to multiple users.

> **Current scope:** This sample is validated end-to-end for **single-tenant**
> deployments. The Silo Model described below is the target multi-tenant
> architecture. Per-user AgentCore session routing (EC2/EBS isolation,
> conversation history) is implemented today via the
> [Channel Router](CHANNEL_ROUTER.md)'s deterministic per-user
> `runtimeSessionId` derivation. **Per-tenant S3 backup isolation is the one
> piece not yet implemented** — see the callout in "Isolation Model: Silo"
> below — pending either AgentCore platform support for exposing
> session identity inside the container, or a per-tenant-runtime deployment
> variant. Tracked as the next milestone for full multi-tenant support.

## Isolation Model: Silo

This sample implements a **Silo Model** — each user gets fully isolated, dedicated resources:

| Resource | Isolation | Managed By |
|----------|-----------|------------|
| EC2 instance | Per-user | AgentCore (auto-provision on first invoke) |
| EBS volume | Per-user | AgentCore (persists across stop/resume) |
| Workspace state | Per-user | OpenClaw (memory, history, config) |
| S3 backup | **Shared prefix (not yet per-user — see note below)** | Container sync logic |
| Capacity provider | Shared (infra template) | You (created once) |
| Agent runtime | Shared (container image) | You (created once) |
| Container image | Shared (ECR) | CDK |

> **S3 backup isolation gap:** `S3_BACKUP_PREFIX` is currently a single
> constant (`"workspace"`) set once at the shared AgentCore runtime level
> (`scripts/deploy.sh`'s `create_agent_runtime` call). AgentCore Runtime
> Instances doesn't expose the invoking `runtimeSessionId` to the running
> container as an environment variable, so the container has no way to
> derive a per-tenant prefix on its own today. **Don't deploy multi-tenant
> assuming S3 backups are already isolated per user** — until this lands,
> treat S3 backup as shared infrastructure state, not tenant-private data.
> The EC2 instance and EBS volume *are* already fully isolated per user via
> AgentCore's own session-to-instance mapping — this gap is specifically
> about the S3 background-backup path, not primary runtime isolation.

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

For teams using a single bot token (one Telegram bot, one Discord app) serving multiple users, the [Channel Router](CHANNEL_ROUTER.md) already implements per-user session routing directly — webhook → Lambda → `invoke-agent-runtime`, with the Lambda deriving each user's `runtimeSessionId` deterministically from `(channel, user_id)` (see `core.py`'s `derive_session_id()`). This is simpler than a DynamoDB user-table lookup and requires no separate provisioning step:

```
Users (Telegram / Slack / Discord)
    │
    ▼
┌──────────────────────────────┐
│  Router Lambda               │  ← detects channel + user identity
│  + API Gateway (webhooks)    │  ← derive_session_id(channel, user_id)
│  + DynamoDB (cold-start only)│  ← tracks warm/cold state, not user mapping
└──────────────┬───────────────┘
               │ InvokeAgentRuntime(runtimeSessionId=derived-per-user-id)
         ┌─────┼─────┐
         ▼     ▼     ▼
      User A  User B  User C   ← per-user AgentCore sessions (silo)
```

The Router Lambda:
1. Receives webhook from messaging platform
2. Extracts user identity from the parsed inbound event (already done per-channel in `lambda/router/adapters/`)\n3. Derives that user's `runtimeSessionId` deterministically (no lookup table needed) and calls `InvokeAgentRuntime` with it
4. Returns response to the messaging platform

No separate user onboarding/provisioning step is required — a new user's
first message automatically gets its own derived session id, and AgentCore
provisions the underlying EC2/EBS instance on that first `InvokeAgentRuntime`
call.

### Shared Context Across Users

In a team deployment, all agents can share organizational knowledge without breaking silo isolation:

| Shared Context | How | Isolation Impact |
|---------------|-----|-----------------|
| Company instructions (AGENTS.md) | Baked into container image | None — read-only, same for all |
| Shared skills | Pre-installed in container | None — read-only |
| Org knowledge base | Shared S3 read-only prefix | None — read-only mount |

Each user still gets their own private workspace — the shared context is read-only material baked into the container or applied at the routing layer.

> **Prompt injection is a known limitation, not mitigated by Guardrails
> today.** Nothing between the public webhook and the model currently
> delimits or filters attacker-controlled message text — it's passed
> straight through as `{"prompt": message_text}`. If you need Bedrock
> Guardrails or another content-filtering layer in front of the model, add
> it explicitly at the Router Lambda; it is not wired in by default in this
> sample. See the exec-allowlist posture in the main
> [README Security section](../README.md#security) for the primary
> mitigation this sample does ship: even a successful injection is
> constrained to an allowlisted set of coding tools, not arbitrary shell.

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
