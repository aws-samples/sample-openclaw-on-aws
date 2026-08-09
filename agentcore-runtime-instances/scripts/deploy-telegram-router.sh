#!/bin/bash
# Deploy the Telegram Lambda Router
#
# Prerequisites:
#   - AWS CLI configured with account 575108946562
#   - Telegram bot token from @BotFather
#   - AgentCore runtime already deployed
#
# Usage:
#   ./scripts/deploy-telegram-router.sh <telegram-bot-token> [allowed-user-ids]

set -euo pipefail

# Configuration
FUNCTION_NAME="OpenClaw-TelegramRouter"
ROLE_NAME="OpenClaw-TelegramRouter-Role"
API_NAME="OpenClaw-TelegramWebhook"
RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:575108946562:runtime/openclaw_agent-85IiOx42wo"
SESSION_ID="telegram-user-1120488445-default-session"
REGION="us-east-1"

TELEGRAM_BOT_TOKEN="${1:-}"
ALLOWED_USER_IDS="${2:-}"
WEBHOOK_SECRET="openclaw-webhook-$(date +%s)"

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "Usage: $0 <telegram-bot-token> [allowed-user-ids]"
    echo ""
    echo "  telegram-bot-token   Token from @BotFather"
    echo "  allowed-user-ids     Comma-separated Telegram user IDs (optional, empty = allow all)"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "Deploying to account: $ACCOUNT_ID in $REGION"

# Step 1: Create IAM Role (idempotent)
echo "==> Creating IAM role..."
aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1 || \
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

aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

# InvokeAgentRuntime permission
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

# Step 2: Package Lambda code
echo "==> Packaging Lambda..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAMBDA_DIR="$SCRIPT_DIR/../lambda/router"
ZIP_FILE="/tmp/openclaw-telegram-router.zip"
(cd "$LAMBDA_DIR" && zip -j "$ZIP_FILE" index.py)

# Step 3: Create or Update Lambda
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
        --environment "{
            \"Variables\": {
                \"AGENTCORE_RUNTIME_ARN\": \"$RUNTIME_ARN\",
                \"SESSION_ID\": \"$SESSION_ID\",
                \"TELEGRAM_BOT_TOKEN\": \"$TELEGRAM_BOT_TOKEN\",
                \"WEBHOOK_SECRET_TOKEN\": \"$WEBHOOK_SECRET\",
                \"ALLOWED_USER_IDS\": \"$ALLOWED_USER_IDS\"
            }
        }" \
        --region "$REGION" \
        --query 'LastUpdateStatus' --output text
else
    # Wait for role propagation
    sleep 10
    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.12 \
        --handler index.handler \
        --role "$ROLE_ARN" \
        --zip-file "fileb://$ZIP_FILE" \
        --timeout 300 \
        --memory-size 256 \
        --environment "{
            \"Variables\": {
                \"AGENTCORE_RUNTIME_ARN\": \"$RUNTIME_ARN\",
                \"SESSION_ID\": \"$SESSION_ID\",
                \"TELEGRAM_BOT_TOKEN\": \"$TELEGRAM_BOT_TOKEN\",
                \"WEBHOOK_SECRET_TOKEN\": \"$WEBHOOK_SECRET\",
                \"ALLOWED_USER_IDS\": \"$ALLOWED_USER_IDS\"
            }
        }" \
        --region "$REGION" \
        --query 'FunctionArn' --output text
fi

# Self-invoke permission
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

FUNCTION_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

# Step 4: Create API Gateway HTTP API
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

    # Create integration
    INTEGRATION_ID=$(aws apigatewayv2 create-integration \
        --api-id "$API_ID" \
        --integration-type AWS_PROXY \
        --integration-uri "$FUNCTION_ARN" \
        --integration-method POST \
        --payload-format-version "2.0" \
        --region "$REGION" \
        --query 'IntegrationId' --output text)

    # Create route
    aws apigatewayv2 create-route \
        --api-id "$API_ID" \
        --route-key "POST /webhook/telegram" \
        --target "integrations/$INTEGRATION_ID" \
        --region "$REGION" >/dev/null

    # Create default stage with auto-deploy
    aws apigatewayv2 create-stage \
        --api-id "$API_ID" \
        --stage-name '$default' \
        --auto-deploy \
        --region "$REGION" >/dev/null

    # Add Lambda invoke permission for API Gateway
    aws lambda add-permission \
        --function-name "$FUNCTION_NAME" \
        --statement-id "apigateway-invoke" \
        --action "lambda:InvokeFunction" \
        --principal "apigateway.amazonaws.com" \
        --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
        --region "$REGION" >/dev/null 2>&1 || true
fi

API_ENDPOINT="https://${API_ID}.execute-api.${REGION}.amazonaws.com"
WEBHOOK_URL="${API_ENDPOINT}/webhook/telegram"
echo "    API Gateway: $WEBHOOK_URL"

# Step 5: Set Telegram webhook
echo "==> Setting Telegram webhook..."
RESULT=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
    -d "url=${WEBHOOK_URL}" \
    -d "secret_token=${WEBHOOK_SECRET}" \
    -d 'allowed_updates=["message","edited_message"]')
echo "    $RESULT"

# Step 6: Verify
echo ""
echo "=== Deployment Complete ==="
echo ""
echo "  Function:     $FUNCTION_NAME"
echo "  API Gateway:  $WEBHOOK_URL"
echo "  Webhook secret: $WEBHOOK_SECRET"
echo "  Session ID:   $SESSION_ID"
echo ""
echo "Test by sending a message to your Telegram bot."
echo "First message after idle may take 60-90s (instance cold start)."
echo ""
echo "To verify webhook:"
echo "  curl -s https://api.telegram.org/bot\${TOKEN}/getWebhookInfo | python3 -m json.tool"
