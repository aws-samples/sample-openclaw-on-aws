"""Telegram Router Lambda — Async webhook-to-AgentCore bridge.

Architecture (handles cold starts gracefully):
1. Webhook handler: validates request, returns 200 immediately, async-invokes worker
2. Worker: invokes AgentCore (up to 5 min cold start), sends reply directly to Telegram

This avoids API Gateway's 30s timeout and Telegram's 60s webhook timeout
while supporting AgentCore cold starts that can take 2-3 minutes.

Setup:
  - API Gateway HTTP API with POST /webhook/telegram route
  - Telegram webhook pointed at the API Gateway URL
  - Lambda env vars: AGENTCORE_RUNTIME_ARN, SESSION_ID, TELEGRAM_BOT_TOKEN,
    WEBHOOK_SECRET_TOKEN, ALLOWED_USER_IDS
  - IAM: bedrock-agentcore:InvokeAgentRuntime + lambda:InvokeFunction (self)
"""

import base64
import json
import logging
import os
from urllib import request as urllib_request
from urllib.error import URLError

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
SESSION_ID = os.environ["SESSION_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "").split(",")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")

# Clients
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=AWS_REGION,
    config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 0}),
)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)


def send_telegram(chat_id, text, reply_to_message_id=None):
    """Send a message to Telegram (plain text, no parse_mode to avoid formatting errors)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    data = json.dumps(payload).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
        logger.info("Telegram send ok: chat=%s msg=%s", chat_id, reply_to_message_id)
    except URLError as e:
        logger.error("Failed to send Telegram message: %s", e)


def send_typing(chat_id):
    """Send typing indicator."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"
    payload = {"chat_id": chat_id, "action": "typing"}
    data = json.dumps(payload).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=5)
    except URLError:
        pass


def invoke_agent(message_text):
    """Invoke the AgentCore Runtime and return the response text.

    Returns None on failure (caller should retry or send error).
    """
    payload = json.dumps({"prompt": message_text}).encode()
    payload_b64 = base64.b64encode(payload).decode()

    try:
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=SESSION_ID,
            payload=payload_b64,
        )
        body = resp.get("body")
        if body:
            result = ""
            for event in body:
                if "chunk" in event:
                    chunk_bytes = event["chunk"].get("bytes", b"")
                    result += chunk_bytes.decode("utf-8", errors="replace")
            # Parse response - handler returns data: {"result": "text"}
            try:
                text = result.split("data: ", 1)[-1] if "data: " in result else result
                data = json.loads(text)
                return data.get("result", result)
            except (json.JSONDecodeError, IndexError):
                return result if result else None
        return None
    except Exception as e:
        logger.error("AgentCore invocation failed: %s", e, exc_info=True)
        return None


def handle_worker(event):
    """Worker phase: invoke AgentCore and reply to Telegram.

    Retries once on failure to handle the case where the instance is
    mid-cold-start and the gateway isn't ready yet.
    """
    chat_id = event["chat_id"]
    message_text = event["message_text"]
    message_id = event.get("message_id")

    send_typing(chat_id)

    response_text = invoke_agent(message_text)

    if response_text is None:
        # First attempt failed — instance might be mid-cold-start.
        # Wait and retry once.
        logger.info("First attempt failed, retrying after 10s...")
        import time
        time.sleep(10)
        send_typing(chat_id)
        response_text = invoke_agent(message_text)

    if response_text is None:
        response_text = (
            "Sorry, I'm having trouble right now. "
            "The instance may be cold-starting. Please try again in ~60 seconds."
        )

    send_telegram(chat_id, response_text, reply_to_message_id=message_id)
    return {"statusCode": 200, "body": "Worker complete"}


def handle_webhook(event):
    """Webhook phase: validate, return 200 fast, async-invoke worker."""
    # Validate webhook secret token
    if WEBHOOK_SECRET_TOKEN:
        headers = event.get("headers", {})
        secret = headers.get("x-telegram-bot-api-secret-token", "")
        if secret != WEBHOOK_SECRET_TOKEN:
            logger.warning("Invalid webhook secret token")
            return {"statusCode": 403, "body": "Forbidden"}

    # Parse the Telegram update
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "Invalid JSON"}

    message = body.get("message") or body.get("edited_message")
    if not message:
        return {"statusCode": 200, "body": "OK"}

    # Extract message details
    chat_id = message["chat"]["id"]
    user_id = str(message["from"]["id"])
    message_text = message.get("text", "")
    message_id = message.get("message_id")

    # Access control
    if ALLOWED_USER_IDS and ALLOWED_USER_IDS != [""] and user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user: %s", user_id)
        send_telegram(chat_id, "Access denied.", reply_to_message_id=message_id)
        return {"statusCode": 200, "body": "OK"}

    if not message_text:
        return {"statusCode": 200, "body": "OK"}

    if message_text.startswith("/start"):
        send_telegram(
            chat_id,
            "Hello! I'm your AI assistant powered by OpenClaw on AWS. "
            "Send me a message and I'll respond."
        )
        return {"statusCode": 200, "body": "OK"}

    logger.info("Processing message from user %s: %s", user_id, message_text[:100])

    # Send typing indicator immediately
    send_typing(chat_id)

    # Async invoke self as worker (decouples from API Gateway timeout)
    worker_payload = {
        "_worker": True,
        "chat_id": chat_id,
        "message_text": message_text,
        "message_id": message_id,
    }

    lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="Event",  # Async — returns immediately
        Payload=json.dumps(worker_payload).encode(),
    )

    # Return 200 immediately — Telegram won't retry
    return {"statusCode": 200, "body": "OK"}


def handler(event, context):
    """Lambda entry point — routes to webhook or worker handler."""
    if event.get("_worker"):
        return handle_worker(event)
    return handle_webhook(event)
