"""Telegram adapter -- webhook parsing, message sending, cold-start UX.

Inbound: Telegram webhook update (message or edited_message)
Outbound: sendMessage, editMessageText, sendChatAction
Cold-start UX: sends "Waking up..." then edits with real response
"""

import json
import logging
import os
from urllib import request as urllib_request
from urllib.error import URLError

logger = logging.getLogger()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET_TOKEN = os.environ.get("WEBHOOK_SECRET_TOKEN", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "").split(",")


def validate_webhook(event: dict) -> bool:
    """Validate the Telegram webhook secret token."""
    if not WEBHOOK_SECRET_TOKEN:
        return True
    headers = event.get("headers", {})
    secret = headers.get("x-telegram-bot-api-secret-token", "")
    return secret == WEBHOOK_SECRET_TOKEN


def parse_inbound(event: dict) -> dict | None:
    """Parse a Telegram webhook event into a normalized message dict.

    Returns None if the event should be ignored.
    Returns dict with: chat_id, user_id, message_text, message_id
    """
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return None

    message = body.get("message") or body.get("edited_message")
    if not message:
        return None

    chat_id = message["chat"]["id"]
    user_id = str(message["from"]["id"])
    message_text = message.get("text", "")
    message_id = message.get("message_id")

    # Access control
    if ALLOWED_USER_IDS and ALLOWED_USER_IDS != [""] and user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user: %s", user_id)
        send_message(chat_id, "Access denied.", reply_to=message_id)
        return None

    if not message_text:
        return None

    if message_text.startswith("/start"):
        send_message(
            chat_id,
            "Hello! I'm your AI assistant powered by OpenClaw on AWS. "
            "Send me a message and I'll respond.",
        )
        return None

    return {
        "chat_id": chat_id,
        "user_id": user_id,
        "message_text": message_text,
        "message_id": message_id,
        "channel": "telegram",
    }


def send_message(chat_id, text, reply_to=None) -> dict | None:
    """Send a message. Returns the Telegram result object or None.

    Falls back to sending without reply_to if the first attempt fails
    (e.g., original message too old or deleted).
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4096]}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    data = json.dumps(payload).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib_request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
        return result.get("result")
    except URLError as e:
        # If reply_to caused the failure, retry without it
        if reply_to:
            logger.warning("Send with reply_to failed (%s), retrying without...", e)
            payload.pop("reply_to_message_id", None)
            payload.pop("allow_sending_without_reply", None)
            data = json.dumps(payload).encode()
            req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
            try:
                resp = urllib_request.urlopen(req, timeout=10)
                result = json.loads(resp.read().decode())
                return result.get("result")
            except (URLError, json.JSONDecodeError) as e2:
                logger.error("Failed to send Telegram message (retry): %s", e2)
                return None
        logger.error("Failed to send Telegram message: %s", e)
        return None
    except json.JSONDecodeError as e:
        logger.error("Invalid response from Telegram: %s", e)
        return None


def edit_message(chat_id, message_id, text):
    """Edit an existing message (replace cold-start notice with real response)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096]}
    data = json.dumps(payload).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
    except URLError as e:
        logger.error("Failed to edit Telegram message: %s", e)


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


def send_cold_start_notice(chat_id, reply_to=None) -> int | None:
    """Send a cold-start notice. Returns the message_id to edit later."""
    result = send_message(
        chat_id,
        "\u23f3 Waking up the AI instance... first response may take ~90s.",
        reply_to=reply_to,
    )
    if result:
        return result.get("message_id")
    return None


def replace_cold_start_notice(chat_id, notice_message_id, response_text):
    """Replace the cold-start notice with the actual response."""
    edit_message(chat_id, notice_message_id, response_text)
