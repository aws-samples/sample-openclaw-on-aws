# Telegram Integration (Webhook + Lambda Router)

## Architecture

```
Telegram → Webhook → API Gateway → Lambda (webhook handler)
                                        ↓ (async invoke)
                                    Lambda (worker)
                                        ↓
                                    AgentCore invoke-agent-runtime
                                        ↓ (cold-starts instance if needed)
                                    EC2 Instance → OpenClaw Gateway → Bedrock
                                        ↓
                                    Lambda (worker) ← response
                                        ↓
                                    Telegram ← sendMessage
```

## Why This Architecture?

| Challenge | Solution |
|-----------|----------|
| API Gateway 30s timeout | Lambda returns 200 immediately, processes async |
| Telegram 60s webhook timeout | Same — async worker decouples timeout |
| AgentCore cold start (60-120s) | Worker has 5-min timeout, retries once |
| Instance idle shutdown | `invoke-agent-runtime` triggers cold start automatically |
| Markdown formatting errors | Plain text replies (no parse_mode) |

## Persistence Guarantees

1. **Webhook** — stored on Telegram's servers, survives indefinitely
2. **API Gateway** — serverless, always available, no cold start
3. **Lambda** — serverless, always available, ~500ms cold start
4. **AgentCore Runtime** — endpoint stays READY; instances wake on-demand
5. **No polling** — container doesn't poll Telegram; Lambda handles all I/O

## Cold Start Timeline

First message after idle timeout (24h default):

```
t=0s    Telegram sends webhook to API Gateway
t=0.5s  Lambda webhook handler returns 200, fires async worker
t=1s    Worker invokes AgentCore (triggers instance provisioning)
t=60s   EC2 instance boots, container starts
t=70s   OpenClaw gateway starts, waits for health check
t=80s   Gateway ready, processes the prompt
t=85s   Bedrock model responds
t=86s   Worker sends reply to Telegram
```

Subsequent messages (instance warm): ~5-15s response time.

## Setup

### Quick Deploy (script)

```bash
./scripts/deploy-telegram-router.sh "YOUR_BOT_TOKEN" "YOUR_TELEGRAM_USER_ID"
```

### Manual Deploy

1. Create the Lambda and API Gateway (see `stacks/lambda_router_stack.py`)
2. Set environment variables:
   - `AGENTCORE_RUNTIME_ARN` — `arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/<id>`
   - `SESSION_ID` — fixed session identifier for conversation continuity
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `WEBHOOK_SECRET_TOKEN` — random string for webhook validation
   - `ALLOWED_USER_IDS` — comma-separated Telegram user IDs (empty = allow all)
3. Set the Telegram webhook:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -d "url=<API_GATEWAY_URL>/webhook/telegram" \
     -d "secret_token=<WEBHOOK_SECRET>" \
     -d 'allowed_updates=["message","edited_message"]'
   ```

### Container Configuration

The container should **NOT** have `CHANNEL_SECRETS_ARN` set when using this architecture.
Without it, the container runs in "webhook-only mode" — no Telegram polling, no conflict.

If `CHANNEL_SECRETS_ARN` IS set, the container will also poll Telegram, which conflicts
with the webhook (Telegram only sends updates to one receiver).

## Troubleshooting

### "Sorry, I'm having trouble right now"
- Check Lambda CloudWatch logs: `/aws/lambda/OpenClaw-TelegramRouter`
- Common cause: `RuntimeClientError 400` = container gateway not ready yet
- Fix: wait for cold start to complete (60-90s), message again

### No response at all
- Check webhook: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
- Verify `pending_update_count` — if > 0, Lambda isn't being called
- Check API Gateway is deployed and route exists

### Instance never wakes
- Verify capacity provider status: `get-capacity-provider --capacity-provider-id <id>`
- Check runtime endpoint: `list-agent-runtime-endpoints --agent-runtime-id <id>`
- Both should show `READY`

## IAM Permissions

Lambda role needs:
```json
{
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeAgentRuntime",
      "Resource": "arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/<id>*"
    },
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:us-east-1:<account>:function:OpenClaw-TelegramRouter"
    }
  ]
}
```
