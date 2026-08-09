"""OpenClaw <-> AgentCore Runtime Wrapper (HTTP endpoint)

Bridges AgentCore Runtime protocol (:8080) to OpenClaw gateway's
OpenAI-compatible HTTP endpoint (/v1/responses).

Flow:
  1. On startup: initialize workspace, start OpenClaw gateway as subprocess
  2. AgentCore -> POST /invocations -> this wrapper -> POST /v1/responses -> response
  3. AgentCore -> GET /ping -> health status

Persistence model (webhook-only architecture):
  - Telegram webhook -> API Gateway -> Lambda -> invoke-agent-runtime -> this wrapper
  - Instance cold-starts on first invoke, stays warm for idleRuntimeSessionTimeout
  - Gateway health check blocks handler until ready (prevents 400 during cold-start)
  - No Telegram polling in container -- webhook-only via external Lambda router
"""

import json
import os
import shutil
import subprocess
import time
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from bedrock_agentcore import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

OPENCLAW_HOME = os.environ.get("OPENCLAW_HOME", "/home/agent/.openclaw")
GATEWAY_PORT = int(os.environ.get("OPENCLAW_PORT", "18789"))
GATEWAY_HTTP_URL = f"http://127.0.0.1:{GATEWAY_PORT}/v1/responses"
GATEWAY_HEALTH_URL = f"http://127.0.0.1:{GATEWAY_PORT}/"

_gateway_ready = False
_gateway_process = None


def _initialize_workspace():
    """Initialize workspace from defaults if not present on EBS."""
    config_path = os.path.join(OPENCLAW_HOME, "openclaw.json")
    if os.path.exists(config_path):
        print("[main.py] Workspace exists on EBS -- zero cold start.")
        return

    print("[main.py] Workspace not found. Initializing from defaults...")
    os.makedirs(OPENCLAW_HOME, exist_ok=True)
    defaults_dir = "/app/.openclaw-defaults"
    if os.path.isdir(defaults_dir):
        for item in os.listdir(defaults_dir):
            src = os.path.join(defaults_dir, item)
            dst = os.path.join(OPENCLAW_HOME, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    os.makedirs(os.path.join(OPENCLAW_HOME, "workspace"), exist_ok=True)
    print("[main.py] Workspace initialized.")


def _load_channel_secrets():
    """Load channel tokens from Secrets Manager if CHANNEL_SECRETS_ARN is set.

    NOTE: In webhook-only mode (Lambda router handles Telegram I/O), this is
    not needed. Only use when the container should do its own channel polling.
    """
    secret_arn = os.environ.get("CHANNEL_SECRETS_ARN", "")
    if not secret_arn:
        return

    print("[main.py] Fetching channel tokens from Secrets Manager...")
    try:
        result = subprocess.run(
            ["aws", "secretsmanager", "get-secret-value",
             "--secret-id", secret_arn,
             "--query", "SecretString",
             "--output", "text"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            proc = subprocess.run(
                ["python3", "/app/patch_channels.py",
                 os.path.join(OPENCLAW_HOME, "openclaw.json")],
                input=result.stdout.strip(),
                capture_output=True, text=True
            )
            print(proc.stdout)
        else:
            print(f"[main.py] WARNING: Could not fetch secret: {result.stderr[:200]}")
    except Exception as e:
        print(f"[main.py] WARNING: Secrets Manager error: {e}")


def _start_gateway():
    """Start OpenClaw gateway as a background subprocess."""
    global _gateway_process

    gw_home = os.path.dirname(OPENCLAW_HOME) if os.path.basename(
        OPENCLAW_HOME.rstrip("/")) == ".openclaw" else OPENCLAW_HOME

    gw_log = open("/tmp/openclaw-gateway.log", "w")  # nosec B108

    print(f"[main.py] Starting OpenClaw gateway on :{GATEWAY_PORT}...")
    print(f"[main.py] Gateway OPENCLAW_HOME={gw_home}")
    _gateway_process = subprocess.Popen(
        ["openclaw", "gateway", "run",
         "--port", str(GATEWAY_PORT),
         "--allow-unconfigured",
         "--auth", "none",
         "--bind", "loopback"],
        env={**os.environ, "OPENCLAW_HOME": gw_home},
        stdout=gw_log,
        stderr=subprocess.STDOUT,
    )
    print(f"[main.py] Gateway PID: {_gateway_process.pid}")


def _wait_for_gateway(timeout: float = 180.0) -> bool:
    """Wait for the gateway to be fully ready to process LLM requests.

    Two-phase check:
    1. Wait for health endpoint (/) to respond — gateway process is up
    2. Wait for responses endpoint to accept requests — models loaded

    The gateway binds its port early but may not be ready to process LLM
    requests until plugins, models, and workspace are fully initialized.
    Phase 2 prevents returning 400/"LLM request failed" during cold start.
    """
    global _gateway_ready
    if _gateway_ready:
        return True

    start = time.time()
    attempt = 0

    # Phase 1: Wait for HTTP port to respond
    while time.time() - start < timeout:
        try:
            urlopen(GATEWAY_HEALTH_URL, timeout=3)
            elapsed = time.time() - start
            print(f"[main.py] Gateway HTTP up after {elapsed:.1f}s ({attempt} polls)")
            break
        except (URLError, OSError):
            attempt += 1
            time.sleep(1.0)
    else:
        print(f"[main.py] ERROR: Gateway HTTP did not respond within {timeout}s")
        return False

    # Phase 2: Verify the responses endpoint actually works (model loaded)
    # Send a minimal prompt and check we don't get a 500/503
    verify_payload = json.dumps({
        "model": "openclaw",
        "input": "ping",
        "user": "healthcheck",
    }).encode()
    verify_headers = {"Content-Type": "application/json"}

    phase2_start = time.time()
    phase2_timeout = min(60.0, timeout - (time.time() - start))

    while time.time() - phase2_start < phase2_timeout:
        try:
            req = Request(GATEWAY_HTTP_URL, data=verify_payload,
                         headers=verify_headers, method="POST")
            with urlopen(req, timeout=30) as resp:
                elapsed = time.time() - start
                print(f"[main.py] Gateway fully ready after {elapsed:.1f}s (LLM verified)")
                _gateway_ready = True
                return True
        except HTTPError as e:
            if e.code >= 500:
                # Gateway up but not ready (model loading, etc)
                time.sleep(3)
                continue
            # 4xx means gateway is processing (maybe bad request format, but it's alive)
            elapsed = time.time() - start
            print(f"[main.py] Gateway ready after {elapsed:.1f}s (got {e.code}, treating as ready)")
            _gateway_ready = True
            return True
        except (URLError, OSError):
            time.sleep(3)
            continue

    # If phase 2 times out, assume it's as ready as it'll get
    print("[main.py] WARNING: Gateway phase-2 check timed out, proceeding anyway")
    _gateway_ready = True
    return True


def _invoke_gateway(prompt: str, session_key: Optional[str] = None) -> str:
    """Send a prompt to the gateway via the OpenAI-compatible HTTP endpoint.

    Includes retry logic for transient failures during cold-start warmup.
    The gateway may be "ready" (health endpoint responds) but still loading
    model configs or completing first-request initialization, which can cause
    initial 500/502 errors on the very first request.
    """
    if not _wait_for_gateway():
        return "Error: OpenClaw gateway did not start within timeout."

    session_key = session_key or "agentcore-default"

    payload = json.dumps({
        "model": "openclaw",
        "input": prompt,
        "user": session_key,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "x-openclaw-session-key": session_key,
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        req = Request(GATEWAY_HTTP_URL, data=payload, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=290) as resp:
                data = json.loads(resp.read().decode())
                # Extract text from OpenResponses format
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for content in item.get("content", []):
                            if content.get("type") == "output_text":
                                return content.get("text", "")
                # Fallback: return serialized output if no output_text found
                return json.dumps(data.get("output", data))
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:500]
            except Exception:
                pass
            if attempt < max_retries and e.code >= 500:
                print(f"[main.py] Gateway returned {e.code}, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(3)
                continue
            return f"Error: Gateway returned HTTP {e.code}: {body or e.reason}"
        except URLError as e:
            if attempt < max_retries:
                print(f"[main.py] Gateway request failed: {e.reason}, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(3)
                continue
            return f"Error: Gateway request failed: {e.reason}"
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON response from gateway: {e}"
        except Exception as e:
            return f"Error: {e}"
    return "Error: All retry attempts exhausted."


# --- Startup sequence ---
_initialize_workspace()
# In webhook-only mode (recommended), CHANNEL_SECRETS_ARN is not set.
# The Lambda router handles all Telegram I/O externally.
# Only load channel secrets for legacy polling mode.
if os.environ.get("CHANNEL_SECRETS_ARN"):
    _load_channel_secrets()
else:
    print("[main.py] Webhook-only mode -- no channel polling configured.")
_start_gateway()


@app.entrypoint
async def handler(request: dict):
    """AgentCore invocation handler."""
    prompt = request.get("prompt") or request.get("message")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")

    if not prompt.strip():
        yield {"result": "Error: prompt must not be empty"}
        return

    session_key = request.get("session_id") or request.get("session_key") or "agentcore-default"

    result = _invoke_gateway(prompt, session_key=session_key)
    yield {"result": result}


if __name__ == "__main__":
    app.run(port=int(os.environ.get("AGENTCORE_PORT", "8080")))
