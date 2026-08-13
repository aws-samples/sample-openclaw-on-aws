# OpenClaw on AgentCore Runtime Instances

[![Open in Kiro](https://img.shields.io/badge/Open_in-Kiro-blue?style=flat-square&logo=amazon-aws)](https://kiro.dev/open?repo=aws-samples/sample-openclaw-on-aws&path=agentcore-runtime-instances)
![License: MIT-0](https://img.shields.io/badge/License-MIT--0-yellow.svg?style=flat-square)
![AWS CDK](https://img.shields.io/badge/AWS_CDK-v2-orange?style=flat-square&logo=amazon-aws)
![AgentCore Runtime](https://img.shields.io/badge/AgentCore-Runtime_Instances-blue?style=flat-square&logo=amazon-aws)
![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python)
![Node.js 24](https://img.shields.io/badge/Node.js-24-green?style=flat-square&logo=node.js)
![Claude Sonnet 4.6](https://img.shields.io/badge/Model-Claude_Sonnet_4.6-purple?style=flat-square)
![Status: Active](https://img.shields.io/badge/Status-Active-brightgreen?style=flat-square)

> **Deploy your own AI assistant that auto-stops when idle ($0 cost) and instantly resumes when you message it — no infrastructure management required.**

Deploy [OpenClaw](https://openclaw.ai) as a persistent AI assistant on [Amazon Bedrock AgentCore Runtime Instances](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html) — AWS-managed EC2 infrastructure with 14-day sessions, EBS persistence, automatic idle shutdown, and native messaging channel support.

## Quick Start

```bash
git clone https://github.com/aws-samples/sample-openclaw-on-aws.git
cd sample-openclaw-on-aws/agentcore-runtime-instances
./scripts/deploy.sh
```

That's it (after CDK bootstrap and Python deps — see Detailed setup below). The deploy script creates the stacks, container image, capacity provider, and runtime.

Once deployed, invoke your agent:

```bash
# Runtime ARN is printed by deploy.sh
RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/<RUNTIME_ID>"
SESSION_ID="my-openclaw-session-$(date +%s)"  # must be ≥33 chars

PAYLOAD=$(echo -n '{"prompt":"Hello! What can you do?"}' | base64 -w0)

aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$RUNTIME_ARN" \
  --runtime-session-id "$SESSION_ID" \
  --payload "$PAYLOAD" \
  --region us-east-1 \
  --cli-read-timeout 300 \
  /dev/stdout
```

The first invocation provisions the EC2 instance (~90s–3 min depending on image cache state). Subsequent invocations on the same session resume instantly.

<details>
<summary><strong>Detailed setup (manual steps)</strong></summary>

```bash
# 1. Install CDK dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure model/region (optional — defaults work)
vim container/.openclaw/openclaw.json

# 3. Bootstrap CDK (first time only)
npx aws-cdk bootstrap aws://<ACCOUNT_ID>/us-east-1

# 4. Deploy CDK stacks (VPC, S3, IAM, ECR image)
npx aws-cdk deploy --all --require-approval never

# 5. Create AgentCore resources (capacity provider + runtime)
# The deploy.sh script handles this via boto3, or run manually:
python3 scripts/deploy.sh
# deploy.sh is a bash script; invoke directly as ./scripts/deploy.sh
```

</details>

## Connecting Channels

Deploy the Lambda router to connect Telegram, Discord, or Slack — it wakes the instance on demand and works across instance idle-stop-wake cycles:

```bash
# Telegram
./scripts/deploy-channel-router.sh \
  --runtime-arn arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/<RUNTIME_ID> \
  --telegram-token "YOUR_BOT_TOKEN" --telegram-allowed-ids "YOUR_TELEGRAM_USER_ID"
```

This deploys a Lambda + API Gateway that receives webhooks, wakes the instance on-demand via `invoke-agent-runtime`, and replies directly. The bot responds even after hours of inactivity — first message after idle takes 60-235s (cold start), subsequent messages a few seconds.

**Supported channels:** Telegram, Discord, Slack — pass `--discord-token`/`--slack-token` (with their required companion flags) to enable them in the same deployment. See [Channel Router docs](docs/CHANNEL_ROUTER.md) for full multi-channel setup.

```
User message → Webhook → Lambda → invoke-agent-runtime (wakes instance) → reply
```

The agent patches its gateway config, restarts the channel, and confirms when live. Once connected, message the bot directly on that platform — no further API calls needed.

### Pairing Mode

When a channel is first connected, OpenClaw enters **pairing mode**. The first message sent to the bot will display a **pairing code** (e.g. `PAIR-XXXX`). To approve the pairing, invoke the runtime with the code:

```bash
PAYLOAD=$(echo -n '{"prompt":"approve pairing PAIR-XXXX"}' | base64 -w0)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$RUNTIME_ARN" \
  --runtime-session-id "$SESSION_ID" \
  --payload "$PAYLOAD" \
  --region us-east-1 \
  --cli-read-timeout 300 \
  /dev/stdout
```

Once approved, the channel is fully active and subsequent messages flow directly without further pairing.

> **Security note:** Bot tokens are transmitted via HTTPS (TLS) and stored on the encrypted EBS volume. For production/multi-tenant deployments, consider storing tokens in AWS Secrets Manager. See [Configuration — IAM Roles](./docs/CONFIGURATION.md#iam-roles).

## Prerequisites

- AWS account with Bedrock model access (Claude Sonnet 4.6)
- [AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html) installed
- Python 3.11+
- Docker (for container image build)
- AWS credentials configured
- boto3 with bedrock-agentcore-control support (>=1.35, needed for `create_capacity_provider`/`create_agent_runtime`; older boto3 lacks the service model)
- `jq` (used by `deploy-channel-router.sh` to build Lambda environment JSON safely — avoids interpolating credential values into a script body)

## Project Structure

```
.
├── app.py                      # CDK app entrypoint
├── cdk.json                    # CDK configuration
├── requirements.txt            # Python dependencies (CDK)
├── stacks/                     # CDK stacks (networking, storage, capacity, runtime, router)
│   └── lambda_router_stack.py  # Lambda + API Gateway + DynamoDB for channel router
├── container/
│   ├── Dockerfile              # ARM64: Node.js 24 + OpenClaw + Python + AWS CLI
│   ├── start.sh                # Entrypoint: gateway + S3 backup sync
│   ├── main.py                 # @app.entrypoint → OpenClaw HTTP endpoint bridge
│   └── .openclaw/              # Default config + workspace
├── lambda/router/              # Multi-channel webhook → AgentCore bridge
│   ├── index.py                # Handler: channel detection, webhook/worker routing
│   ├── core.py                 # AgentCore invocation + cold-start tracking (DynamoDB)
│   └── adapters/               # Channel-specific parsing + sending
│       ├── telegram.py         # Webhook validation, send/edit messages, cold-start UX
│       ├── discord.py          # Interactions endpoint, deferred responses
│       └── slack.py            # Events API, chat.postMessage/update
├── docs/                       # Architecture, configuration, cost, runtime, channel router
└── scripts/                    # deploy.sh, deploy-channel-router.sh, connect-channel.sh
```

## Security

- **Network**: VPC-only networking, no public IP on instances
- **IAM**: Least-privilege execution role (Bedrock + ECR + Logs + S3 + Secrets Manager)
- **Encryption**: S3 bucket encrypted at rest (SSE-S3 or KMS), EBS encrypted
- **Gateway auth**: Loopback-only binding (`--bind loopback`) — gateway only listens on 127.0.0.1, unreachable from outside the container
- **Exec posture**: `tools.exec.security` is `"allowlist"` (not `"full"`) with `ask: "off"`. The bot stays fully autonomous — no approval prompt blocks an async Telegram/Discord/Slack reply — but it can only run an explicit allowlist of coding tools, and the risky interpreters/package managers in that allowlist (`git`, `npm`, `node`, `python3`, `pip`/`pip3`) are further restricted by an `argPattern` on each entry: `git` is limited to a safe subcommand set (no `-c`/`-C`/`--upload-pack`/`--receive-pack`/`--exec`/`--git-dir`/`--work-tree`, and no `git config`), `npm` to `install`/`ci`/`test` (no `exec`/arbitrary `run`), `node`/`python3` deny their inline eval flags (`-e`/`--eval`/`-p`, `-c`/`--command`), and `pip`/`pip3` are limited to `install`/`uninstall`/`list`/`show`/`freeze`/`download`. Plain file tools (`cat`/`ls`/`grep`/`find`/`mkdir`/`cp`/`mv`) and `pytest` remain unrestricted since they have no code-eval surface. `strictInlineEval` is also enabled so injected text can't smuggle a shell command through an already-allowlisted interpreter even outside the argPattern coverage. This limits what a successful prompt injection (see [Multi-Tenancy — known limitations](./docs/MULTI_TENANCY_CONSIDERATIONS.md)) can actually do, without giving up the unattended-bot UX. Residual risk: `npm install`/`pip install` can still execute attacker-controlled install-time scripts for a malicious package name — inherent to those package managers, not closeable via argv filtering.
- **Non-root execution**: the container starts as root only long enough to prepare the EBS-backed `OPENCLAW_HOME` mount (`chown` to a non-root `agent` user); `start.sh` then drops privileges via `gosu` before launching `main.py`/the OpenClaw gateway. Every exec-tool command the agent runs — including anything that slips past the allowlist hardening above — executes as `agent`, not root, so an allowlist escape no longer implies a root compromise of the instance.
- **Channel tokens**: With the Lambda router (recommended), tokens are Lambda environment variables set once at deploy time — never written to the instance's workspace. For the legacy direct-polling mode only, tokens land on encrypted EBS; if using that mode, prefer [Secrets Manager](./docs/CONFIGURATION.md#channel-tokens-with-secrets-manager) (`openclaw/*` prefix) over passing tokens at invoke time.
- **Channel webhook auth**: Every channel adapter fails **closed** — an unconfigured or unverifiable channel (missing secret, missing/invalid Discord public key, bad signature) rejects the request rather than defaulting to allow. Discord webhooks use real Ed25519 signature verification (`PyNaCl`) against `DISCORD_PUBLIC_KEY`; Telegram/Slack verify their respective secret/signing-secret headers. Each channel also enforces a required user-id allowlist (`ALLOWED_USER_IDS` / `DISCORD_ALLOWED_USER_IDS` / `SLACK_ALLOWED_USER_IDS`) — see [Channel Router](./docs/CHANNEL_ROUTER.md).
- **Cost controls**: Lambda reserved concurrency + API Gateway throttling bound worst-case spend from an unauthenticated request burst, and a per-user cooldown drops rapid repeat messages before they trigger another full invoke-with-retry cycle. See [Channel Router — Rate Limiting](./docs/CHANNEL_ROUTER.md).
- **Per-user isolation**: The Lambda router derives a distinct AgentCore `runtimeSessionId` per (channel, user) pair, so different users get their own dedicated EC2/EBS instance and conversation history instead of sharing one session.
- **Instance management**: AWS-managed EC2 — no direct access needed for normal operation (use AgentCore APIs); optionally use SSM Session Manager for shell access if you need to inspect the instance directly (see [Architecture](./docs/ARCHITECTURE.md))

This sample follows the [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/). Review and harden configurations before deploying to production.

## Learn More

- **[Architecture](./docs/ARCHITECTURE.md)** — System design, persistence model, comparisons vs EC2/microVMs
- **[Runtime Behavior](./docs/RUNTIME_BEHAVIOR.md)** — Idle timeout, channel reconnection, cron/heartbeat, pairing persistence
- **[Configuration](./docs/CONFIGURATION.md)** — Model, instance type, storage, supported regions, IAM roles
- **[Cost Estimate](./docs/COST.md)** — Monthly cost breakdown and optimization tips
- **[Cleanup](./docs/CLEANUP.md)** — Stop, delete, or fully remove all resources
- **[Skills](./docs/SKILLS.md)** — Pre-installed and recommended AWS skills
- **[Channel Router](./docs/CHANNEL_ROUTER.md)** — Telegram/Discord/Slack via Lambda, idle-wake, cold-start behavior
- **[Serverless Considerations](./docs/SERVERLESS_CONSIDERATIONS.md)** — Known friction points and workarounds
- **[Multi-Tenancy](./docs/MULTI_TENANCY_CONSIDERATIONS.md)** — Patterns for 10-1000+ users

## License

MIT-0 — See [LICENSE](../LICENSE)
