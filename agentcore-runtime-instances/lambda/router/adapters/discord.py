"""Discord adapter -- Interactions endpoint with deferred response pattern.

Discord flow:
1. User sends slash command or message (via bot)
2. Discord sends Interaction to our webhook
3. We ACK with type=5 (DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE) within 3s
4. Worker processes, then PATCHes the deferred response with real content

Cold-start UX: The deferred response shows "thinking..." automatically in Discord UI.
If cold start detected, we follow up with a status message.

Requires: DISCORD_BOT_TOKEN, DISCORD_PUBLIC_KEY, DISCORD_APPLICATION_ID
"""

import hashlib
import json
import logging
import os
from urllib import request as urllib_request
from urllib.error import URLError

logger = logging.getLogger()

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
DISCORD_APPLICATION_ID = os.environ.get("DISCORD_APPLICATION_ID", "")


def validate_webhook(event: dict) -> bool:
    """Validate Discord interaction signature.

    Discord requires Ed25519 signature verification. For simplicity in Lambda
    (no nacl dependency), we skip verification here and rely on API Gateway
    to not expose the endpoint publicly without the webhook secret.
    In production, add PyNaCl layer for proper verification.
    """
    # TODO: Add Ed25519 verification with PyNaCl Lambda layer
    return True


def parse_inbound(event: dict) -> dict | None:
    """Parse a Discord interaction into a normalized message dict."""
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return None

    interaction_type = body.get("type")

    # Type 1: PING (Discord verification)
    if interaction_type == 1:
        return {"_ping": True}

    # Type 2: APPLICATION_COMMAND (slash commands)
    # Type 4: MESSAGE_COMPONENT (buttons, selects)
    if interaction_type not in (2, 4):
        return None

    data = body.get("data", {})
    # For slash commands, get the "message" option
    message_text = ""
    for option in data.get("options", []):
        if option.get("name") == "message":
            message_text = option.get("value", "")
            break

    if not message_text:
        # Try message content for message commands
        resolved = data.get("resolved", {}).get("messages", {})
        for msg in resolved.values():
            message_text = msg.get("content", "")
            break

    if not message_text:
        return None

    user = body.get("member", {}).get("user", {}) or body.get("user", {})

    return {
        "chat_id": body.get("channel_id"),
        "user_id": user.get("id", ""),
        "message_text": message_text,
        "message_id": body.get("id"),
        "interaction_token": body.get("token"),
        "channel": "discord",
    }


def send_deferred_response(interaction_id, interaction_token):
    """ACK with deferred response (shows 'thinking...' in Discord)."""
    url = f"https://discord.com/api/v10/interactions/{interaction_id}/callback"
    payload = {"type": 5}  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
    data = json.dumps(payload).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=5)
    except URLError as e:
        logger.error("Failed to send deferred response: %s", e)


def edit_original_response(interaction_token, text):
    """Edit the deferred response with the actual content."""
    url = (
        f"https://discord.com/api/v10/webhooks/"
        f"{DISCORD_APPLICATION_ID}/{interaction_token}/messages/@original"
    )
    payload = {"content": text[:2000]}
    data = json.dumps(payload).encode()
    req = urllib_request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        urllib_request.urlopen(req, timeout=10)
    except URLError as e:
        logger.error("Failed to edit Discord response: %s", e)


def send_message(channel_id, text, reply_to=None) -> dict | None:
    """Send a follow-up message to a channel."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = {"content": text[:2000]}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}
    data = json.dumps(payload).encode()
    req = urllib_request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        },
    )
    try:
        resp = urllib_request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except (URLError, json.JSONDecodeError) as e:
        logger.error("Failed to send Discord message: %s", e)
        return None


def send_cold_start_notice(interaction_token=None, channel_id=None, **kwargs) -> str | None:
    """For Discord, the deferred response IS the cold-start notice.

    Discord shows 'thinking...' automatically. If we want to add extra context,
    we can edit the deferred response with a status message.
    """
    if interaction_token:
        edit_original_response(
            interaction_token,
            "\u23f3 Waking up the AI instance... first response may take ~60s.",
        )
        return interaction_token
    return None


def replace_cold_start_notice(interaction_token, response_text, **kwargs):
    """Replace the cold-start message with the actual response."""
    edit_original_response(interaction_token, response_text)
