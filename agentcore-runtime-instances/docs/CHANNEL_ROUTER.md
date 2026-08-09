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
| User sees no feedback during 60-90s cold start | Cold-start detection + immediate status message |
| Markdown characters break Telegram replies | Plain text (no parse_mode) |

## Cold-Start UX

The router tracks last successful invocation in DynamoDB. When `elapsed > idle_timeout`, it knows a cold start is likely:

| Channel | Cold-start feedback |
|---------|-------------------|
| Telegram | Sends "⏳ Waking up..." message, edits with real response when ready |
| Discord | Deferred interaction response (Discord shows "thinking..." natively) |
| Slack | Posts "⏳ Starting up..." in thread, updates with real response |

Warm instance: no status message, just typing indicator + fast response.

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

### Quick deploy (Telegram only)

```bash
./scripts/deploy-telegram-router.sh "BOT_TOKEN" "ALLOWED_USER_IDS"
```

### CDK (all channels)

```bash
cd agentcore-runtime-instances
cdk deploy OpenClaw-LambdaRouter
```

### Set idle timeout (15 min recommended)

```bash
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> \
  --lifecycle-configuration '{"idleRuntimeSessionTimeout": 900}' \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<ECR_URI>"}}' \
  --role-arn "<EXECUTION_ROLE_ARN>" \
  --capacity-provider-configuration '{"capacityProviderArn":"<CP_ARN>"}' \
  --region us-east-1
```

### Set Telegram webhook

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://<API_ID>.execute-api.us-east-1.amazonaws.com/webhook/telegram" \
  -d "secret_token=<SECRET>" \
  -d 'allowed_updates=["message","edited_message"]'
```

## Container Configuration

The container runs in **webhook-only mode** by default (no `CHANNEL_SECRETS_ARN` set). The Lambda router handles all channel I/O externally. The container only exposes the `/v1/responses` HTTP endpoint for AgentCore invocations.

If `CHANNEL_SECRETS_ARN` is set, the container falls back to legacy polling mode.

## IAM Permissions

Lambda role requires:
- `bedrock-agentcore:InvokeAgentRuntime` on the runtime ARN
- `lambda:InvokeFunction` on itself (async worker)
- `dynamodb:GetItem` + `dynamodb:PutItem` on the cold-start table

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Sorry, I'm having trouble" | AgentCore returned RuntimeClientError 400 | Instance gateway not ready; wait 60s, retry |
| No response at all | Webhook not set or API Gateway misconfigured | Check `getWebhookInfo`; verify route exists |
| 403 on webhook | Secret token mismatch | Verify `WEBHOOK_SECRET_TOKEN` matches setWebhook |
| Response takes 2+ min | Instance cold-starting from idle | Normal for first message after 15-min idle |
| DynamoDB error in logs | Table missing or IAM insufficient | Create table; check IAM policy |

## Cost

| Component | Cost |
|-----------|------|
| Lambda (webhook + worker) | ~$0/mo (free tier covers typical personal use) |
| API Gateway | ~$0/mo (first 1M requests free) |
| DynamoDB | ~$0/mo (single item, on-demand) |
| EC2 (c7g.large, only when active) | ~$0.068/hr |
| Bedrock (Claude Sonnet 4.6) | Per-token pricing |

**Idle cost: $0.** Only pay when actively conversing.
