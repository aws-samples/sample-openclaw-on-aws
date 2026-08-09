"""Core AgentCore invocation logic — shared across all channel adapters.

Handles:
- AgentCore invoke-agent-runtime call
- Cold-start detection and tracking via DynamoDB
- Retry logic for transient failures during instance warm-up
"""

import base64
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
        # No tracking table — assume cold if we can't tell
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
            return True  # Never invoked before — definitely cold

        last_success = int(item["last_success_epoch"]["N"])
        elapsed = time.time() - last_success
        return elapsed > IDLE_TIMEOUT_SECONDS
    except Exception as e:
        logger.warning("Cold-start check failed: %s", e)
        return True  # Assume cold on error


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
    except Exception as e:
        logger.warning("Failed to record success: %s", e)


def invoke_agent(message_text: str, session_id: str = None) -> str | None:
    """Invoke the AgentCore Runtime and return the response text.

    Returns None on failure (caller should retry or send error message).
    """
    session_id = session_id or SESSION_ID
    payload = json.dumps({"prompt": message_text}).encode()
    payload_b64 = base64.b64encode(payload).decode()

    try:
        resp = _get_agentcore_client().invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=payload_b64,
        )
        body = resp.get("body")
        if body:
            result = ""
            for event in body:
                if "chunk" in event:
                    chunk_bytes = event["chunk"].get("bytes", b"")
                    result += chunk_bytes.decode("utf-8", errors="replace")
            # Parse response — handler returns data: {"result": "text"}
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


def invoke_with_retry(message_text: str, session_id: str = None) -> str:
    """Invoke AgentCore with retry logic for cold starts.

    Retries once after 10s if the first attempt fails (instance mid-boot).
    Returns the response text or an error message.
    """
    session_id = session_id or SESSION_ID

    response = invoke_agent(message_text, session_id)

    if response is None:
        # First attempt failed — instance might be mid-cold-start
        logger.info("First attempt failed, retrying after 10s...")
        time.sleep(10)
        response = invoke_agent(message_text, session_id)

    if response is None:
        return (
            "Sorry, I'm having trouble right now. "
            "The instance may be cold-starting. Please try again in ~60 seconds."
        )

    # Record successful invocation for cold-start tracking
    record_success(session_id)
    return response
