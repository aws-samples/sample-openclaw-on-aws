← Back to [README](../README.md)

# Channel Router (Lambda)

Multi-channel webhook-to-AgentCore bridge with idle-wake support and cold-start UX.

## Architecture

```
Channel (Telegram/Discord/Slack)
  │
  │ webhook
  ▼
API Gateway HTTP API
  │ POST /webhook/{channel}
  ▼
Lambda (webhook handler)          <-- returns 200 in <2s
  │ async self-invoke
  ▼
Lambda (worker)                   <-- 5-min timeout
  │ invoke-agent-runtime
  ▼
AgentCore Runtime                 <-- cold-starts instance if idle
  │
  ▼
EC2 Instance → OpenClaw Gateway → Bedrock Model
  │
  ▼
Lambda (worker) ← response
  │
  ▼
Channel ← reply (sendMessage / edit / postMessage)
```

## Why This Architecture

| Problem | Solution |
|---------|----------|
| Instance stops when idle — bot goes silent | Lambda invocation triggers cold start automatically |
| API Gateway 30s timeout | Async self-invoke decouples processing from webhook response |
| Telegram 60s webhook timeout | Same — worker runs independently |
| User sees no feedback during cold start | Cold-start detection + immediate status message |
| Markdown characters break Telegram replies | Plain text (no parse_mode) |

## Cold-Start UX

The router tracks last successful invocation in DynamoDB. When `elapsed > idle_timeout`, it knows a cold start is likely:

| Channel | Cold-start feedback |
|---------|-------------------|
| Telegram | Sends "⏳ Waking up..." message, edits with real response when ready |
| Discord | Deferred interaction response (Discord shows "thinking..." natively) |
| Slack | Posts "⏳ Starting up..." in thread, updates with real response |

Warm instance: no status message, just typing indicator + fast response.

## Cold-Start Behaviour

### Response parsing

`invoke_agent_runtime`'s response payload comes back under the **`"response"`**
key (a `botocore.response.StreamingBody`), not `"body"`:

```python
>>> resp.keys()
dict_keys(['ResponseMetadata', 'runtimeSessionId', 'contentType', 'statusCode', 'response'])
```

`core.py`'s `invoke_agent()` reads `resp["response"]`, calls `.read()`,
decodes, and parses the `data: {"result": ...}` SSE-style payload the
container emits. A wrong key here would silently return `None` on every
call, success or failure, since a KeyError-free `.get()` on the wrong key
produces no exception to catch.

### Warm vs cold latency

On a **warm** instance, `invoke_agent_runtime` returns on the first attempt.
Measured through the full webhook -> worker -> AgentCore -> Telegram path:
**~4 seconds** end-to-end.

On a **cold** instance, `invoke_agent_runtime` **fails fast** (does not
block/queue) while the EC2 instance is provisioning and the container is
booting. Measured cold-start recovery (SSM-confirmed cold instance, zero
prior activity, real webhook path, no retry needed): **91 seconds** from
webhook receipt to confirmed response delivered to Telegram. Contributing
factors:

- EC2 instance launch + container pull time
- OpenClaw gateway subprocess startup (workspace load, model config, plugin discovery)
- Occasional S3 restore path when the workspace isn't already on EBS

**Retry strategy:** the Lambda retries with a schedule covering ~255s total
(`core.py` `invoke_with_retry`: delays of 0, 15, 20, 25, 30, 35, 40, 45, 45s)
as headroom beyond the measured baseline for slower boots. Each retry is a
fresh `invoke_agent_runtime` call against the *same* `runtimeSessionId` —
AgentCore routes it to the same provisioning/booting instance rather than
starting a new one, so retries don't cause competing
cold starts.

### Why "LLM request failed" or RuntimeClientError 400 can appear briefly

AgentCore can route a request to the container as soon as its port is
listening, before the OpenClaw gateway inside has finished starting. The
container's `_invoke_gateway()` (main.py) waits for the gateway's health
endpoint and retries 5xx responses internally, but a request that arrives
during the narrow window between "container up" and "gateway ready" can still
surface as a `RuntimeClientError 400` back to the Lambda's `invoke_agent`
call. This is expected and handled by the outer Lambda-side retry loop, not a
bug — it only becomes a problem if the retry window doesn't cover the actual
cold-start time.

### Warm instance behaviour

When the instance is already running (message within the 15-min idle
timeout):
- DynamoDB shows recent success → no "⏳ Waking up" message shown, just typing indicator
- `invoke_agent_runtime` routes immediately to the running container
- Gateway already loaded → prompt processed and reply delivered in ~2-5s total
  (webhook handler + worker + AgentCore + Telegram send)

## Supported Channels

### Telegram
- **Inbound:** Webhook (setWebhook API)
- **Outbound:** sendMessage, editMessageText
- **Auth:** Secret token header validation
- **Config:** `TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET_TOKEN`, `ALLOWED_USER_IDS`

### Discord
- **Inbound:** Interactions endpoint (slash commands)
- **Outbound:** Deferred response + PATCH original
- **Auth:** Ed25519 signature (TODO: add PyNaCl layer)
- **Config:** `DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_APPLICATION_ID`

### Slack
- **Inbound:** Events API (app_mention, message)
- **Outbound:** chat.postMessage, chat.update
- **Auth:** HMAC-SHA256 request signing
- **Config:** `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`

## Deployment

### Shell script (recommended for single-account personal use)

```bash
# Telegram
./scripts/deploy-channel-router.sh \
  --runtime-arn <RUNTIME_ARN> \
  --telegram-token "<BOT_TOKEN>" --telegram-allowed-ids "<USER_ID>"

# Multiple channels in one deployment
./scripts/deploy-channel-router.sh \
  --runtime-arn <RUNTIME_ARN> \
  --telegram-token "<BOT_TOKEN>" \
  --slack-token "<xoxb-...>" --slack-signing-secret "<SECRET>" \
  --discord-token "<BOT_TOKEN>" --discord-public-key "<KEY>" --discord-app-id "<APP_ID>"
```

The script provisions the IAM role, DynamoDB cold-start table, Lambda
function (with bundled boto3), one API Gateway route per enabled channel,
and registers the Telegram webhook automatically. Discord and Slack print
the endpoint URL to paste into their respective developer consoles (see
"Supported Channels" below for where).

Run `./scripts/deploy-channel-router.sh --help` for the full flag list.

### CDK (for infrastructure-as-code deployments)

```bash
cd agentcore-runtime-instances
cdk deploy OpenClaw-LambdaRouter
```

`stacks/lambda_router_stack.py` accepts the same channel credentials as
constructor parameters and provisions identical resources.

### Idle timeout

The router relies on the AgentCore runtime's `idleRuntimeSessionTimeout` to
control cost vs cold-start frequency. See [Runtime Behavior](RUNTIME_BEHAVIOR.md#idle-timeout-and-what-keeps-the-session-alive) for configuration and recommended values.

## Container Configuration

The container runs in **webhook-only mode** by default (no `CHANNEL_SECRETS_ARN` set). The Lambda router handles all channel I/O externally. The container only exposes the `/v1/responses` HTTP endpoint for AgentCore invocations.

If `CHANNEL_SECRETS_ARN` is set, the container falls back to legacy polling mode.

## Cold-Start Detection (DynamoDB)

The router uses a single-item DynamoDB table (`OpenClaw-RouterColdStart`) to track when the last successful AgentCore invocation occurred.

**How it works:**
1. On each successful response, the worker writes `{session_id, last_success_epoch}` to DynamoDB
2. On each incoming webhook, the handler reads the item and checks: `now - last_success > idle_timeout?`
3. If yes → instance is likely cold → send immediate status message to user
4. If no → instance is warm → just show typing indicator

**Table schema:**
- Partition key: `session_id` (String)
- Attributes: `last_success_epoch` (Number, Unix timestamp)
- Billing: on-demand (PAY_PER_REQUEST) — effectively free for personal use

Without the table, the router assumes every request might be a cold start (still works, just shows the status message unnecessarily).

## IAM Permissions

Lambda role requires:
- `bedrock-agentcore:InvokeAgentRuntime` on the runtime ARN
- `lambda:InvokeFunction` on itself (async worker)
- `dynamodb:GetItem` + `dynamodb:PutItem` on the cold-start table

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Sorry, the instance failed to respond" | All retry attempts (~255s) exhausted — cold start took longer than the window | Message again; instance is now warm from the attempt |
| No response at all | Webhook not set, or something is polling `getUpdates` and clearing it | Check `getWebhookInfo`; ensure container has no `channels.telegram` in its persisted config (see below) |
| 403 on webhook | Secret token mismatch | Verify `WEBHOOK_SECRET_TOKEN` matches setWebhook |
| First response after idle takes ~90s | Instance cold-starting (expected, not a bug) | Normal for first message after 15-min idle; see Cold-Start Behaviour above |
| DynamoDB error in logs | Table missing or IAM insufficient | Create table; check IAM policy |
| Webhook silently disappears | Container's own Telegram channel config got restored/re-synced from EBS/S3 and started polling | Strip `channels` from persisted `openclaw.json`; container's `_strip_channels_for_webhook_mode()` (main.py) does this on every boot when `CHANNEL_SECRETS_ARN` is unset |
