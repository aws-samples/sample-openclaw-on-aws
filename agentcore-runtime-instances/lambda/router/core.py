"""Core AgentCore invocation logic — shared across all channel adapters.

Handles:
- AgentCore invoke-agent-runtime call
- Cold-start detection and tracking via DynamoDB
- Retry logic for transient failures during instance warm-up
"""

import json
import logging
import os
import time

import boto3
from botocore.config import Config

logger = logging.getLogger()

# Configuration
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
SESSION_ID = os.environ.get("SESSION_ID", "default-session")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "900"))
COLDSTART_TABLE = os.environ.get("COLDSTART_TABLE", "")

# Clients (lazy init)
_agentcore_client = None
_dynamodb_client = None


def _get_agentcore_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client(
            "bedrock-agentcore",
            region_name=AWS_REGION,
            config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 0}),
        )
    return _agentcore_client


def _get_dynamodb_client():
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb", region_name=AWS_REGION)
    return _dynamodb_client


def is_likely_cold_start(session_id: str = None) -> bool:
    """Check if the instance is likely cold (idle timeout exceeded).

    Uses DynamoDB to track last successful invocation time.
    Returns True if the instance is probably cold and will need to boot.
    """
    if not COLDSTART_TABLE:
        return True

    session_id = session_id or SESSION_ID
    try:
        resp = _get_dynamodb_client().get_item(
            TableName=COLDSTART_TABLE,
            Key={"session_id": {"S": session_id}},
            ProjectionExpression="last_success_epoch",
        )
        item = resp.get("Item")
        if not item:
            return True

        last_success = int(item["last_success_epoch"]["N"])
        elapsed = time.time() - last_success
        return elapsed > IDLE_TIMEOUT_SECONDS
    except Exception as exc:
        logger.warning("Cold-start check failed: %s", exc)
        return True


def record_success(session_id: str = None):
    """Record a successful invocation timestamp."""
    if not COLDSTART_TABLE:
        return

    session_id = session_id or SESSION_ID
    try:
        _get_dynamodb_client().put_item(
            TableName=COLDSTART_TABLE,
            Item={
                "session_id": {"S": session_id},
                "last_success_epoch": {"N": str(int(time.time()))},
            },
        )
    except Exception as exc:
        logger.warning("Failed to record success: %s", exc)


def invoke_agent(message_text: str, session_id: str = None) -> str | None:
    """Invoke the AgentCore Runtime and return the response text.

    Returns None on failure (caller should retry or send error message).
    """
    session_id = session_id or SESSION_ID
    payload = json.dumps({"prompt": message_text}).encode()

    try:
        resp = _get_agentcore_client().invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=payload,
            contentType="application/json",
        )
    except Exception as exc:
        logger.error("AgentCore invocation failed: %s", exc, exc_info=True)
        return None

    # ROOT CAUSE (confirmed via direct boto3 repro, 2026-08-09):
    # invoke_agent_runtime's streamed payload comes back under the
    # "response" key (a botocore.response.StreamingBody), NOT "body".
    # resp.keys() == ['ResponseMetadata', 'runtimeSessionId', 'contentType',
    # 'statusCode', 'response']. Every previous version of this function
    # checked resp.get("body"), which is always None, so invoke_agent
    # silently returned None on EVERY call -- success or failure -- even
    # when the container had already replied correctly within seconds.
    # The retry loop then kept firing for the full ~260s window regardless
    # of whether the underlying invocation actually succeeded, which is why
    # cold-start "reliability" looked random: it depended entirely on
    # whether the LAST retry happened to land after the instance was warm,
    # not on whether any individual attempt succeeded.
    stream = resp.get("response")
    if stream is None:
        logger.error("invoke_agent_runtime response missing 'response' stream: %s", list(resp.keys()))
        return None

    raw = stream.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        return None

    # Handler emits Server-Sent-Events style: 'data: {"result": "text"}\n\n'
    try:
        text = raw.split("data: ", 1)[-1] if "data: " in raw else raw
        data = json.loads(text)
        return data.get("result", raw)
    except (json.JSONDecodeError, IndexError):
        return raw


def invoke_with_retry(message_text: str, session_id: str = None) -> str:
    """Invoke AgentCore with retry logic for cold starts.

    A single invoke_agent_runtime call that lands on a warm instance returns
    correctly on the FIRST attempt (measured: ~4s round trip through the full
    webhook -> worker -> AgentCore -> Telegram path). Retries matter only for
    genuine cold starts, where AgentCore fails fast
    (RuntimeClientError/ResourceNotFoundException) while the instance is
    still provisioning/booting -- it does not block/queue.

    Measured cold-start recovery (SSM-confirmed cold instance, real webhook
    path, zero prior activity): 91s from webhook receipt to confirmed
    response, succeeding without needing a retry. The schedule below
    provides headroom beyond that measured baseline for slower boots.
    """
    session_id = session_id or SESSION_ID

    delays = [0, 15, 20, 25, 30, 35, 40, 45, 45]  # ~255s total coverage

    for attempt, delay in enumerate(delays, start=1):
        if delay > 0:
            time.sleep(delay)

        response = invoke_agent(message_text, session_id)
        if response is not None:
            record_success(session_id)
            return response

        logger.info("Attempt %d/%d returned no response, will retry.", attempt, len(delays))

    return (
        "Sorry, the instance failed to respond after multiple attempts. "
        "It may still be starting up. Please try again in a moment."
    )
