"""Slack adapter -- Events API with chat.postMessage response.

Slack flow:
1. User mentions bot or DMs it
2. Slack sends event to our webhook (Events API)
3. We return 200 immediately (Slack requires <3s response)
4. Worker processes, then posts reply via chat.postMessage

Cold-start UX: Post an ephemeral "warming up" message, then follow up with real response.

Requires: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
"""

import hashlib
import hmac
import json
import logging
import os
import time
from urllib import request as urllib_request
from urllib.error import URLError

logger = logging.getLogger()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")


def validate_webhook(event: dict) -> bool:
    """Validate Slack request signature."""
    if not SLACK_SIGNING_SECRET:
        return True

    headers = event.get("headers", {})
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")

    if not timestamp or not signature:
        return False

    # Reject requests older than 5 minutes
    if abs(time.time() - int(timestamp)) > 300:
        return False

    body = event.get("body", "")
    sig_basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.HMAC(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature)


def parse_inbound(event: dict) -> dict | None:
    """Parse a Slack Events API event into a normalized message dict."""
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return None

    # URL verification challenge
    if body.get("type") == "url_verification":
        return {"_challenge": body.get("challenge")}

    # Event callback
    if body.get("type") != "event_callback":
        return None

    slack_event = body.get("event", {})
    event_type = slack_event.get("type")

    # Only handle messages (not bot messages)
    if event_type != "message" or slack_event.get("bot_id"):
        return None

    # Skip message subtypes (edits, deletes, etc)
    if slack_event.get("subtype"):
        return None

    return {
        "chat_id": slack_event.get("channel"),
        "user_id": slack_event.get("user", ""),
        "message_text": slack_event.get("text", ""),
        "message_id": slack_event.get("ts"),
        "thread_ts": slack_event.get("thread_ts"),
        "channel": "slack",
    }


def send_message(channel, text, thread_ts=None, reply_to=None) -> dict | None:
    """Post a message to Slack."""
    url = "https://slack.com/api/chat.postMessage"
    payload = {
        "channel": channel,
        "text": text[:4000],
    }
    if thread_ts or reply_to:
        payload["thread_ts"] = thread_ts or reply_to

    data = json.dumps(payload).encode()
    req = urllib_request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
    )
    try:
        resp = urllib_request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
        if result.get("ok"):
            return result
        logger.error("Slack API error: %s", result.get("error"))
        return None
    except (URLError, json.JSONDecodeError) as e:
        logger.error("Failed to send Slack message: %s", e)
        return None


def update_message(channel, ts, text):
    """Update an existing Slack message."""
    url = "https://slack.com/api/chat.update"
    payload = {"channel": channel, "ts": ts, "text": text[:4000]}
    data = json.dumps(payload).encode()
    req = urllib_request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
    )
    try:
        urllib_request.urlopen(req, timeout=10)
    except URLError as e:
        logger.error("Failed to update Slack message: %s", e)


def send_cold_start_notice(channel, thread_ts=None, **kwargs) -> str | None:
    """Send a cold-start notice. Returns the message ts to update later."""
    result = send_message(
        channel,
        "\u23f3 Waking up the AI instance... first response may take ~60s.",
        thread_ts=thread_ts,
    )
    if result:
        return result.get("ts")
    return None


def replace_cold_start_notice(channel, notice_ts, response_text, **kwargs):
    """Replace the cold-start notice with the actual response."""
    update_message(channel, notice_ts, response_text)
