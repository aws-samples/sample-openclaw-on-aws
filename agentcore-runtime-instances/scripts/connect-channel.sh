#!/bin/bash
set -euo pipefail

# Connect a messaging channel to your OpenClaw agent on AgentCore Runtime Instances.
#
# Usage (direct token — fine for personal deployments):
#   ./scripts/connect-channel.sh telegram "YOUR_BOT_TOKEN"
#   ./scripts/connect-channel.sh discord "YOUR_BOT_TOKEN"
#   ./scripts/connect-channel.sh slack "xapp-TOKEN" "xoxb-TOKEN"
#
# Usage (Secrets Manager — recommended for production):
#   ./scripts/connect-channel.sh --secret openclaw/channels
#
#   Secret value should be JSON:
#   {"telegram": "TOKEN", "discord": "TOKEN", "slack_app_token": "...", "slack_bot_token": "..."}
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - Agent runtime deployed (run ./scripts/deploy.sh first)

REGION="${AWS_REGION:-us-east-1}"
RUNTIME_ARN="${RUNTIME_ARN:-}"
SESSION_ID="${SESSION_ID:-openclaw-channel-setup-$(date +%s)}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
    echo "Usage: $0 <channel> <token> [slack-bot-token]"
    echo "       $0 --secret <secret-name-or-arn>"
    echo ""
    echo "Channels (direct token):"
    echo "  telegram    Bot token from @BotFather"
    echo "  discord     Bot token from Developer Portal"
    echo "  slack       App token (xapp-...) + Bot token (xoxb-...)"
    echo ""
    echo "Secrets Manager (production):"
    echo "  --secret    Fetch tokens from AWS Secrets Manager"
    echo "              Secret value: {\"telegram\": \"TOKEN\", \"discord\": \"TOKEN\"}"
    echo ""
    echo "Environment variables:"
    echo "  RUNTIME_ARN   Agent runtime ARN (required)"
    echo "  AWS_REGION    AWS region (default: us-east-1)"
    echo "  SESSION_ID    Session ID (auto-generated if not set)"
    echo ""
    echo "Examples:"
    echo "  $0 telegram 123456789:ABCdefGHI..."
    echo "  $0 --secret openclaw/channels"
    exit 1
}

# Check for --secret mode
if [ "${1:-}" = "--secret" ]; then
    SECRET_ID="${2:-}"
    if [ -z "$SECRET_ID" ]; then
        echo -e "${RED}Error: --secret requires a secret name or ARN.${NC}"
        usage
    fi

    if [ -z "$RUNTIME_ARN" ]; then
        echo -e "${RED}Error: RUNTIME_ARN not set.${NC}"
        echo "  export RUNTIME_ARN=\"arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT>:runtime/<ID>\""
        exit 1
    fi

    echo -e "${YELLOW}Fetching channel tokens from Secrets Manager...${NC}"
    echo "  Secret: $SECRET_ID"
    echo ""

    # Fetch the secret
    SECRET_JSON=$(aws secretsmanager get-secret-value \
        --secret-id "$SECRET_ID" \
        --query 'SecretString' \
        --output text \
        --region "$REGION" 2>&1)

    if [ $? -ne 0 ]; then
        echo -e "${RED}Error fetching secret: $SECRET_JSON${NC}"
        exit 1
    fi

    # Build prompt with tokens from the secret
    PROMPT="Configure my messaging channels from these tokens (write the config and restart the gateway): $SECRET_JSON"

    echo -e "${YELLOW}Invoking agent to configure channels...${NC}"
    PAYLOAD=$(echo -n "{\"prompt\":\"${PROMPT}\"}" | base64 -w0 2>/dev/null || echo -n "{\"prompt\":\"${PROMPT}\"}" | base64)

    aws bedrock-agentcore invoke-agent-runtime \
        --agent-runtime-arn "$RUNTIME_ARN" \
        --runtime-session-id "$SESSION_ID" \
        --payload "$PAYLOAD" \
        --region "$REGION" \
        --cli-read-timeout 300 \
        /dev/stdout

    echo ""
    echo -e "${GREEN}Done!${NC}"
    exit 0
fi

# Direct token mode
CHANNEL="${1:-}"
TOKEN="${2:-}"
SLACK_BOT_TOKEN="${3:-}"

if [ -z "$CHANNEL" ] || [ -z "$TOKEN" ]; then
    usage
fi

if [ -z "$RUNTIME_ARN" ]; then
    echo -e "${RED}Error: RUNTIME_ARN not set.${NC}"
    echo "Set it from deploy.sh output:"
    echo "  export RUNTIME_ARN=\"arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT>:runtime/<ID>\""
    exit 1
fi

# Build the prompt based on channel type
case "$CHANNEL" in
    telegram)
        PROMPT="Connect me to Telegram. My bot token is ${TOKEN}"
        ;;
    discord)
        PROMPT="Connect me to Discord. My bot token is ${TOKEN}"
        ;;
    slack)
        if [ -z "$SLACK_BOT_TOKEN" ]; then
            echo -e "${RED}Error: Slack requires both app token and bot token.${NC}"
            echo "Usage: $0 slack <app-token> <bot-token>"
            exit 1
        fi
        PROMPT="Connect me to Slack. App token: ${TOKEN}, Bot token: ${SLACK_BOT_TOKEN}"
        ;;
    *)
        echo -e "${RED}Error: Unknown channel '${CHANNEL}'.${NC}"
        echo "Supported: telegram, discord, slack"
        exit 1
        ;;
esac

echo -e "${YELLOW}Connecting ${CHANNEL}...${NC}"
echo "  Runtime: $RUNTIME_ARN"
echo "  Session: $SESSION_ID"
echo "  Region:  $REGION"
echo ""

# Encode payload
PAYLOAD=$(echo -n "{\"prompt\":\"${PROMPT}\"}" | base64 -w0 2>/dev/null || echo -n "{\"prompt\":\"${PROMPT}\"}" | base64)

# Invoke the agent
echo -e "${YELLOW}Invoking agent (first call may take 2-3 min to provision)...${NC}"
echo ""

aws bedrock-agentcore invoke-agent-runtime \
    --agent-runtime-arn "$RUNTIME_ARN" \
    --runtime-session-id "$SESSION_ID" \
    --payload "$PAYLOAD" \
    --region "$REGION" \
    --cli-read-timeout 300 \
    /dev/stdout

echo ""
echo -e "${GREEN}Done!${NC} If the agent confirmed the connection, try messaging your bot."
echo ""
echo "To send follow-up messages to this session:"
echo "  export RUNTIME_ARN=\"$RUNTIME_ARN\""
echo "  export SESSION_ID=\"$SESSION_ID\""
echo "  ./scripts/invoke.sh \"Your message here\""
