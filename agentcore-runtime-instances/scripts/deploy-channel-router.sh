#!/bin/bash
# Deploy the Channel Router (Lambda + API Gateway) for one or more channels.
#
# Provisions a single Lambda function that handles Telegram, Discord, and/or
# Slack via the adapter pattern in lambda/router/adapters/. Each channel gets
# its own API Gateway route (POST /webhook/<channel>); you enable only the
# channels you provide credentials for.
#
# Prerequisites:
#   - AWS CLI configured with credentials for the target account
#   - AgentCore runtime already deployed (see scripts/deploy.sh)
#   - Bot credentials for at least one channel:
#       Telegram: bot token from @BotFather
#       Discord:  bot token, public key, application ID from the Developer Portal
#       Slack:    bot token (xoxb-...) and signing secret from the app config
#
# Usage:
#   ./scripts/deploy-channel-router.sh --runtime-arn <ARN> [channel flags...]
#
#   --runtime-arn <ARN>            AgentCore runtime ARN (required)
#   --session-id <ID>              Fixed runtimeSessionId (min 33 chars; default: generated)
#   --telegram-token <TOKEN>       Enable Telegram
#   --telegram-allowed-ids <IDS>   Comma-separated Telegram user IDs (required allowlist when Telegram enabled)
#   --discord-token <TOKEN>        Enable Discord (also requires --discord-public-key, --discord-app-id)
#   --discord-public-key <KEY>
#   --discord-app-id <ID>
#   --discord-allowed-ids <IDS>    Comma-separated Discord user IDs (required allowlist when Discord enabled)
#   --slack-token <TOKEN>          Enable Slack (also requires --slack-signing-secret)
#   --slack-signing-secret <SECRET>
#   --slack-allowed-ids <IDS>      Comma-separated Slack user IDs (required allowlist when Slack enabled)
#
# Example (Telegram only):
#   ./scripts/deploy-channel-router.sh \
#     --runtime-arn arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/my_agent-abc123 \
#     --telegram-token "123456:ABC-DEF..." --telegram-allowed-ids "111111111"
#
# Example (Telegram + Slack):
#   ./scripts/deploy-channel-router.sh \
#     --runtime-arn arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/my_agent-abc123 \
#     --telegram-token "123456:ABC-DEF..." \
#     --slack-token "xoxb-..." --slack-signing-secret "..."

set -euo pipefail

FUNCTION_NAME="OpenClaw-ChannelRouter"
ROLE_NAME="OpenClaw-ChannelRouter-Role"
API_NAME="OpenClaw-ChannelWebhook"
COLDSTART_TABLE="OpenClaw-RouterColdStart"
REGION="${AWS_REGION:-us-east-1}"

RUNTIME_ARN=""
SESSION_ID=""
TELEGRAM_BOT_TOKEN=""
TELEGRAM_ALLOWED_IDS=""
DISCORD_BOT_TOKEN=""
DISCORD_PUBLIC_KEY=""
DISCORD_APP_ID=""
DISCORD_ALLOWED_IDS=""
SLACK_BOT_TOKEN=""
SLACK_SIGNING_SECRET=""
SLACK_ALLOWED_IDS=""
RESERVED_CONCURRENCY="${RESERVED_CONCURRENCY:-8}"
THROTTLE_BURST_LIMIT="${THROTTLE_BURST_LIMIT:-10}"
THROTTLE_RATE_LIMIT="${THROTTLE_RATE_LIMIT:-5}"

while [ $# -gt 0 ]; do
    case "$1" in
        --runtime-arn) RUNTIME_ARN="$2"; shift 2 ;;
        --session-id) SESSION_ID="$2"; shift 2 ;;
        --telegram-token) TELEGRAM_BOT_TOKEN="$2"; shift 2 ;;
        --telegram-allowed-ids) TELEGRAM_ALLOWED_IDS="$2"; shift 2 ;;
        --discord-token) DISCORD_BOT_TOKEN="$2"; shift 2 ;;
        --discord-public-key) DISCORD_PUBLIC_KEY="$2"; shift 2 ;;
        --discord-app-id) DISCORD_APP_ID="$2"; shift 2 ;;
        --discord-allowed-ids) DISCORD_ALLOWED_IDS="$2"; shift 2 ;;
        --slack-token) SLACK_BOT_TOKEN="$2"; shift 2 ;;
        --slack-signing-secret) SLACK_SIGNING_SECRET="$2"; shift 2 ;;
        --slack-allowed-ids) SLACK_ALLOWED_IDS="$2"; shift 2 ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [ -z "$RUNTIME_ARN" ]; then
    echo "Error: --runtime-arn is required."
    echo "Run with --help for usage."
    exit 1
fi

CHANNELS=()
[ -n "$TELEGRAM_BOT_TOKEN" ] && CHANNELS+=("telegram")
[ -n "$DISCORD_BOT_TOKEN" ] && CHANNELS+=("discord")
[ -n "$SLACK_BOT_TOKEN" ] && CHANNELS+=("slack")

if [ ${#CHANNELS[@]} -eq 0 ]; then
    echo "Error: provide credentials for at least one channel (--telegram-token, --discord-token, or --slack-token)."
    exit 1
fi

if [ -n "$DISCORD_BOT_TOKEN" ] && { [ -z "$DISCORD_PUBLIC_KEY" ] || [ -z "$DISCORD_APP_ID" ]; }; then
    echo "Error: Discord requires --discord-token, --discord-public-key, and --discord-app-id together."
    exit 1
fi

if [ -n "$SLACK_BOT_TOKEN" ] && [ -z "$SLACK_SIGNING_SECRET" ]; then
    echo "Error: Slack requires --slack-token and --slack-signing-secret together."
    exit 1
fi

# Each enabled channel needs an explicit allowlist. Fail closed: no allowlist
# means no one is authorized on that channel, not "everyone is" -- an empty
# allowlist is a misconfiguration to catch here, not a permissive default.
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -z "$TELEGRAM_ALLOWED_IDS" ]; then
    echo "Error: --telegram-allowed-ids is required when --telegram-token is set."
    exit 1
fi
if [ -n "$DISCORD_BOT_TOKEN" ] && [ -z "$DISCORD_ALLOWED_IDS" ]; then
    echo "Error: --discord-allowed-ids is required when --discord-token is set."
    exit 1
fi
if [ -n "$SLACK_BOT_TOKEN" ] && [ -z "$SLACK_ALLOWED_IDS" ]; then
    echo "Error: --slack-allowed-ids is required when --slack-token is set."
    exit 1
fi

if [ -z "$SESSION_ID" ]; then
    SESSION_ID="channel-router-session-$(date +%s)-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
if [ ${#SESSION_ID} -lt 33 ]; then
    echo "Error: --session-id must be at least 33 characters (AgentCore requirement)."
    exit 1
fi

WEBHOOK_SECRET="openclaw-webhook-$(date +%s)"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "Deploying to account: $ACCOUNT_ID in $REGION"
echo "Channels enabled: ${CHANNELS[*]}"

# --- Step 1: IAM role (idempotent) ---
echo "==> Creating IAM role..."
aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1 || {
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }' \
        --output text --query 'Role.Arn'
    sleep 10  # role propagation
}

aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name InvokeAgentCore \
    --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
            \"Effect\": \"Allow\",
            \"Action\": \"bedrock-agentcore:InvokeAgentRuntime\",
            \"Resource\": \"${RUNTIME_ARN}*\"
        }]
    }"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo "    Role: $ROLE_ARN"

# --- Step 2: DynamoDB cold-start table (idempotent) ---
echo "==> Ensuring cold-start tracking table..."
aws dynamodb describe-table --table-name "$COLDSTART_TABLE" >/dev/null 2>&1 || \
aws dynamodb create-table \
    --table-name "$COLDSTART_TABLE" \
    --attribute-definitions AttributeName=session_id,AttributeType=S \
    --key-schema AttributeName=session_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --query "TableDescription.TableStatus" --output text

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name DynamoDBColdStart \
    --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
            \"Effect\": \"Allow\",
            \"Action\": [\"dynamodb:GetItem\", \"dynamodb:PutItem\"],
            \"Resource\": \"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${COLDSTART_TABLE}\"
        }]
    }"

# --- Step 3: Package Lambda code (with dependencies) ---
echo "==> Packaging Lambda..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAMBDA_DIR="$SCRIPT_DIR/../lambda/router"
BUILD_DIR="/tmp/openclaw-channel-router-build"
ZIP_FILE="/tmp/openclaw-channel-router.zip"

rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR"
cp "$LAMBDA_DIR/index.py" "$LAMBDA_DIR/core.py" "$BUILD_DIR/"
cp -r "$LAMBDA_DIR/adapters" "$BUILD_DIR/"

# Lambda's built-in Python 3.12 runtime ships boto3 without the
# bedrock-agentcore service model, and doesn't ship pynacl at all (needed
# for Discord Ed25519 signature verification); bundle both.
pip install boto3 pynacl -t "$BUILD_DIR" --upgrade --quiet --no-cache-dir

(cd "$BUILD_DIR" && zip -r "$ZIP_FILE" . -x "*.dist-info/*" "*.pyc" "__pycache__/*" "*.egg-info/*" >/dev/null)
echo "    Package size: $(du -h "$ZIP_FILE" | cut -f1)"

# --- Step 4: Environment variables ---
# Built via `jq -n --arg` rather than interpolating shell variables into a
# Python source string. `--arg` treats every value as an opaque string and
# escapes it correctly for JSON output regardless of its contents (quotes,
# backslashes, etc.) -- there is no code-generation step for an attacker-
# controlled credential value to break out of.
ENV_JSON=$(jq -n \
    --arg runtime_arn "$RUNTIME_ARN" \
    --arg session_id "$SESSION_ID" \
    --arg coldstart_table "$COLDSTART_TABLE" \
    --arg telegram_token "$TELEGRAM_BOT_TOKEN" \
    --arg webhook_secret "$WEBHOOK_SECRET" \
    --arg telegram_allowed_ids "$TELEGRAM_ALLOWED_IDS" \
    --arg discord_token "$DISCORD_BOT_TOKEN" \
    --arg discord_public_key "$DISCORD_PUBLIC_KEY" \
    --arg discord_app_id "$DISCORD_APP_ID" \
    --arg discord_allowed_ids "$DISCORD_ALLOWED_IDS" \
    --arg slack_token "$SLACK_BOT_TOKEN" \
    --arg slack_signing_secret "$SLACK_SIGNING_SECRET" \
    --arg slack_allowed_ids "$SLACK_ALLOWED_IDS" \
    '{
        Variables: {
            AGENTCORE_RUNTIME_ARN: $runtime_arn,
            SESSION_ID: $session_id,
            COLDSTART_TABLE: $coldstart_table,
            IDLE_TIMEOUT_SECONDS: "900",
            TELEGRAM_BOT_TOKEN: $telegram_token,
            WEBHOOK_SECRET_TOKEN: $webhook_secret,
            ALLOWED_USER_IDS: $telegram_allowed_ids,
            DISCORD_BOT_TOKEN: $discord_token,
            DISCORD_PUBLIC_KEY: $discord_public_key,
            DISCORD_APPLICATION_ID: $discord_app_id,
            DISCORD_ALLOWED_USER_IDS: $discord_allowed_ids,
            SLACK_BOT_TOKEN: $slack_token,
            SLACK_SIGNING_SECRET: $slack_signing_secret,
            SLACK_ALLOWED_USER_IDS: $slack_allowed_ids
        }
    }')

# --- Step 5: Create or update Lambda ---
echo "==> Deploying Lambda function..."
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" >/dev/null 2>&1; then
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file "fileb://$ZIP_FILE" \
        --region "$REGION" \
        --query 'LastUpdateStatus' --output text
    sleep 5
    aws lambda update-function-configuration \
        --function-name "$FUNCTION_NAME" \
        --timeout 300 \
        --memory-size 256 \
        --environment "$ENV_JSON" \
        --region "$REGION" \
        --query 'LastUpdateStatus' --output text
else
    sleep 5
    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.12 \
        --handler index.handler \
        --role "$ROLE_ARN" \
        --zip-file "fileb://$ZIP_FILE" \
        --timeout 300 \
        --memory-size 256 \
        --environment "$ENV_JSON" \
        --region "$REGION" \
        --query 'FunctionArn' --output text
fi

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name SelfInvoke \
    --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
            \"Effect\": \"Allow\",
            \"Action\": \"lambda:InvokeFunction\",
            \"Resource\": \"arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}\"
        }]
    }"

# Reserved concurrency bounds worst-case cost from an unauthenticated/
# abusive request volume -- each concurrent execution can hold a c7g.large
# provisioning cycle open for up to the ~255s retry window, so an unbounded
# concurrency ceiling multiplies directly into EC2/Bedrock/Lambda spend.
echo "==> Setting reserved concurrency ($RESERVED_CONCURRENCY)..."
aws lambda put-function-concurrency \
    --function-name "$FUNCTION_NAME" \
    --reserved-concurrent-executions "$RESERVED_CONCURRENCY" \
    --region "$REGION" >/dev/null

FUNCTION_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

# --- Step 6: API Gateway HTTP API + one route per enabled channel ---
echo "==> Setting up API Gateway..."
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" \
    --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text 2>/dev/null)

if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
    API_ID=$(aws apigatewayv2 create-api \
        --name "$API_NAME" \
        --protocol-type HTTP \
        --region "$REGION" \
        --query 'ApiId' --output text)
    echo "    Created API: $API_ID"

    aws apigatewayv2 create-stage \
        --api-id "$API_ID" \
        --stage-name '$default' \
        --auto-deploy \
        --region "$REGION" >/dev/null

    aws lambda add-permission \
        --function-name "$FUNCTION_NAME" \
        --statement-id "apigateway-invoke" \
        --action "lambda:InvokeFunction" \
        --principal "apigateway.amazonaws.com" \
        --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
        --region "$REGION" >/dev/null 2>&1 || true
fi

INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" \
    --query "Items[?IntegrationUri=='$FUNCTION_ARN'].IntegrationId | [0]" --output text 2>/dev/null)

if [ "$INTEGRATION_ID" = "None" ] || [ -z "$INTEGRATION_ID" ]; then
    INTEGRATION_ID=$(aws apigatewayv2 create-integration \
        --api-id "$API_ID" \
        --integration-type AWS_PROXY \
        --integration-uri "$FUNCTION_ARN" \
        --integration-method POST \
        --payload-format-version "2.0" \
        --region "$REGION" \
        --query 'IntegrationId' --output text)
fi

API_ENDPOINT="https://${API_ID}.execute-api.${REGION}.amazonaws.com"

for CHANNEL in "${CHANNELS[@]}"; do
    ROUTE_KEY="POST /webhook/${CHANNEL}"
    EXISTING=$(aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" \
        --query "Items[?RouteKey=='$ROUTE_KEY'].RouteId | [0]" --output text 2>/dev/null)
    if [ "$EXISTING" = "None" ] || [ -z "$EXISTING" ]; then
        aws apigatewayv2 create-route \
            --api-id "$API_ID" \
            --route-key "$ROUTE_KEY" \
            --target "integrations/$INTEGRATION_ID" \
            --region "$REGION" >/dev/null
        echo "    Route added: $ROUTE_KEY"
    fi
done

# Throttle the default stage to bound worst-case cost from an
# unauthenticated/abusive request volume (defense in depth alongside
# channel auth + reserved concurrency above).
echo "==> Setting API Gateway throttling (burst=$THROTTLE_BURST_LIMIT, rate=$THROTTLE_RATE_LIMIT/s)..."
aws apigatewayv2 update-stage \
    --api-id "$API_ID" \
    --stage-name '$default' \
    --default-route-settings "ThrottlingBurstLimit=${THROTTLE_BURST_LIMIT},ThrottlingRateLimit=${THROTTLE_RATE_LIMIT}" \
    --region "$REGION" >/dev/null

# --- Step 7: Register webhooks / interaction endpoints per channel ---
echo "==> Registering channel webhooks..."

if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
    WEBHOOK_URL="${API_ENDPOINT}/webhook/telegram"
    RESULT=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
        -d "url=${WEBHOOK_URL}" \
        -d "secret_token=${WEBHOOK_SECRET}" \
        -d 'allowed_updates=["message","edited_message"]')
    echo "    Telegram: $WEBHOOK_URL -> $RESULT"
fi

if [ -n "$DISCORD_BOT_TOKEN" ]; then
    echo "    Discord:  ${API_ENDPOINT}/webhook/discord"
    echo "              Set this as the 'Interactions Endpoint URL' in the"
    echo "              Discord Developer Portal (Application > General Information)."
fi

if [ -n "$SLACK_BOT_TOKEN" ]; then
    echo "    Slack:    ${API_ENDPOINT}/webhook/slack"
    echo "              Set this as the Event Subscriptions Request URL in the"
    echo "              Slack app config (Features > Event Subscriptions)."
fi

# --- Step 8: Summary ---
echo ""
echo "=== Deployment Complete ==="
echo ""
echo "  Function:       $FUNCTION_NAME"
echo "  API Gateway:     $API_ENDPOINT"
echo "  Session ID:      $SESSION_ID"
echo "  Channels:        ${CHANNELS[*]}"
[ -n "$TELEGRAM_BOT_TOKEN" ] && echo "  Webhook secret:  $WEBHOOK_SECRET"
echo ""
echo "First message after idle may take 60-235s (instance cold start)."
echo ""
echo "To verify Telegram webhook:"
echo "  curl -s https://api.telegram.org/bot\${TOKEN}/getWebhookInfo | python3 -m json.tool"
