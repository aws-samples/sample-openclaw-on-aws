"""Multi-channel Router Lambda -- Async webhook-to-AgentCore bridge.

Supports: Telegram, Discord, Slack (extensible to WhatsApp, etc.)

Architecture:
1. Webhook handler: validates, parses channel-specific format, returns fast
2. Async worker: invokes AgentCore, sends response via channel-specific API
3. Cold-start UX: detects idle instance, shows status to user, updates with response

Routing by API Gateway path:
  POST /webhook/telegram  -> telegram adapter
  POST /webhook/discord   -> discord adapter
  POST /webhook/slack     -> slack adapter
"""

import json
import logging
import os
import sys

import boto3

# Add current dir to path for local imports
sys.path.insert(0, os.path.dirname(__file__))

from core import derive_session_id, is_likely_cold_start, is_rate_limited, invoke_with_retry
from adapters import telegram as tg_adapter
from adapters import discord as dc_adapter
from adapters import slack as sl_adapter

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

lambda_client = boto3.client("lambda", region_name=AWS_REGION)

# Channel adapter registry
ADAPTERS = {
    "telegram": tg_adapter,
    "discord": dc_adapter,
    "slack": sl_adapter,
}


def detect_channel(event: dict) -> str:
    """Detect which channel sent the webhook based on path or headers."""
    # API Gateway HTTP API puts the path in requestContext
    path = event.get("rawPath", "") or event.get("path", "")

    if "/telegram" in path:
        return "telegram"
    if "/discord" in path:
        return "discord"
    if "/slack" in path:
        return "slack"

    # Fallback: check headers for channel-specific markers
    headers = event.get("headers", {})
    if "x-telegram-bot-api-secret-token" in headers:
        return "telegram"
    if "x-slack-signature" in headers:
        return "slack"

    # Default to telegram for backward compat
    return "telegram"


def handle_webhook(event: dict) -> dict:
    """Phase 1: Validate, parse, send cold-start notice, async-invoke worker."""
    channel = detect_channel(event)
    adapter = ADAPTERS.get(channel)

    if not adapter:
        return {"statusCode": 400, "body": f"Unknown channel: {channel}"}

    # Validate webhook authenticity
    if not adapter.validate_webhook(event):
        logger.warning("Invalid webhook for channel: %s", channel)
        return {"statusCode": 403, "body": "Forbidden"}

    # Parse the inbound message
    parsed = adapter.parse_inbound(event)

    if parsed is None:
        return {"statusCode": 200, "body": "OK"}

    # Handle special cases (Discord PING, Slack challenge)
    if parsed.get("_ping"):
        return {"statusCode": 200, "body": json.dumps({"type": 1}),
                "headers": {"Content-Type": "application/json"}}
    if "_challenge" in parsed:
        return {"statusCode": 200, "body": json.dumps({"challenge": parsed["_challenge"]}),
                "headers": {"Content-Type": "application/json"}}

    logger.info("Inbound %s from user %s: %s",
                channel, parsed.get("user_id"), parsed.get("message_text", "")[:100])

    # Cheap per-user cooldown -- blunts cost-amplification abuse before we
    # spend an async self-invoke + full invoke-with-retry cycle on a burst
    # of repeated messages from the same user.
    if is_rate_limited(channel, parsed.get("user_id", "")):
        logger.info("Rate-limited %s user %s, dropping request.", channel, parsed.get("user_id"))
        return {"statusCode": 200, "body": "OK"}

    # Send typing / cold-start notice
    session_id = derive_session_id(channel, parsed.get("user_id", ""))
    cold = is_likely_cold_start(session_id)
    notice_id = None

    if channel == "telegram":
        if cold:
            notice_id = adapter.send_cold_start_notice(
                parsed["chat_id"], reply_to=parsed.get("message_id"))
        else:
            adapter.send_typing(parsed["chat_id"])
    elif channel == "discord" and parsed.get("interaction_token"):
        adapter.send_deferred_response(parsed["message_id"], parsed["interaction_token"])
        if cold:
            notice_id = adapter.send_cold_start_notice(
                interaction_token=parsed["interaction_token"])
    elif channel == "slack":
        if cold:
            notice_id = adapter.send_cold_start_notice(
                parsed["chat_id"], thread_ts=parsed.get("thread_ts"))

    # Async invoke worker
    worker_payload = {
        "_worker": True,
        "channel": channel,
        "parsed": parsed,
        "notice_id": notice_id,
        "is_cold": cold,
        "session_id": session_id,
    }

    lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(worker_payload).encode(),
    )

    return {"statusCode": 200, "body": "OK"}


def handle_worker(event: dict) -> dict:
    """Phase 2: Invoke AgentCore and send response via channel adapter."""
    channel = event["channel"]
    parsed = event["parsed"]
    notice_id = event.get("notice_id")
    is_cold = event.get("is_cold", False)

    adapter = ADAPTERS[channel]
    message_text = parsed["message_text"]
    session_id = event.get("session_id") or derive_session_id(channel, parsed.get("user_id", ""))

    # Keep sending typing indicators during processing (for non-cold Telegram)
    if channel == "telegram" and not is_cold:
        adapter.send_typing(parsed["chat_id"])

    # Invoke AgentCore -- routed to this user's own dedicated session/instance.
    response_text = invoke_with_retry(message_text, session_id=session_id)

    # Send response via channel-specific method
    if channel == "telegram":
        if notice_id:
            # Replace cold-start notice with real response
            adapter.replace_cold_start_notice(parsed["chat_id"], notice_id, response_text)
        else:
            adapter.send_message(parsed["chat_id"], response_text, reply_to=parsed.get("message_id"))

    elif channel == "discord":
        interaction_token = parsed.get("interaction_token")
        if interaction_token:
            adapter.replace_cold_start_notice(interaction_token, response_text)
        else:
            adapter.send_message(parsed["chat_id"], response_text)

    elif channel == "slack":
        if notice_id:
            adapter.replace_cold_start_notice(parsed["chat_id"], notice_id, response_text)
        else:
            adapter.send_message(
                parsed["chat_id"], response_text,
                thread_ts=parsed.get("thread_ts") or parsed.get("message_id"))

    return {"statusCode": 200, "body": "Worker complete"}


def handler(event, context):
    """Lambda entry point -- routes to webhook or worker handler."""
    if event.get("_worker"):
        return handle_worker(event)
    return handle_webhook(event)
