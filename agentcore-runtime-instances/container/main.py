"""OpenClaw <-> AgentCore Runtime Wrapper (HTTP endpoint)

Bridges AgentCore Runtime protocol (:8080) to OpenClaw gateway's
OpenAI-compatible HTTP endpoint (/v1/responses).

Persistence model (webhook-only architecture):
  - Telegram webhook -> API Gateway -> Lambda -> invoke-agent-runtime -> this wrapper
  - Instance cold-starts on first invoke, stays warm for idleRuntimeSessionTimeout
  - No Telegram polling in container -- webhook-only via external Lambda router

Tenant isolation (S3 backup prefixing):
  - The AgentCore runtime session id is ONLY available per-HTTP-request (via
    the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header, surfaced through
    `BedrockAgentCoreContext.get_session_id()`). It is not available at
    container boot, so S3 restore/backup MUST happen at request time, keyed
    by the session id, rather than at boot time against a single static
    "workspace" prefix shared by every tenant that happens to land on this
    container. Sharing one static prefix across sessions is a cross-tenant
    data leak: a second tenant's cold start would silently restore the first
    tenant's files.
  - The session id is attacker-influenced input (it flows from the caller of
    invoke_agent_runtime), so it is strictly sanitized (`_sanitize_session_id`)
    before being used to build an S3 key prefix. Anything that doesn't match
    the allowlist falls back to a fixed safe prefix instead of being
    silently stripped and used anyway.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

app = BedrockAgentCoreApp()

OPENCLAW_HOME = os.environ.get("OPENCLAW_HOME", "/home/agent/.openclaw")
GATEWAY_PORT = int(os.environ.get("OPENCLAW_PORT", "18789"))
GATEWAY_HTTP_URL = f"http://127.0.0.1:{GATEWAY_PORT}/v1/responses"
GATEWAY_HEALTH_URL = f"http://127.0.0.1:{GATEWAY_PORT}/"

_gateway_ready = False
_gateway_process = None

# --- Session-scoped workspace state ---
# Set once, on the first request handled by this (cold) container, by
# _ensure_session_workspace(). The background sync thread and the SIGTERM
# handler both read this same module-level value so a periodic sync or a
# final shutdown sync always writes to the same per-session S3 prefix that
# was restored from, instead of racing back to a stale/default prefix.
_session_prefix = None
_workspace_ready = False
_init_lock = threading.Lock()
_sync_thread_started = False

# Strict allowlist for the (attacker-influenced) AgentCore session id before
# it is used to build an S3 key. Rejects "/", "..", null bytes, and anything
# else outside this character set -- refuse-and-fall-back, never
# strip-and-continue.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FALLBACK_SESSION_PREFIX = "unknown"


def _sanitize_session_id(raw_session_id: Optional[str]) -> str:
    """Validate an AgentCore session id for safe use as an S3 key segment.

    Returns the input unchanged if it matches the strict allowlist
    (``^[A-Za-z0-9_-]{1,128}$``). Otherwise returns a fixed, safe fallback
    segment -- callers must not strip/sanitize-in-place and continue with a
    mutated version of attacker-influenced input.
    """
    if not raw_session_id or not isinstance(raw_session_id, str):
        print("[main.py] WARNING: no AgentCore session id available; using fallback S3 prefix.")
        return _FALLBACK_SESSION_PREFIX
    if not _SESSION_ID_RE.match(raw_session_id):
        print(
            "[main.py] WARNING: AgentCore session id failed strict validation "
            f"(len={len(raw_session_id)}); using fallback S3 prefix instead of "
            "trusting attacker-influenced input."
        )
        return _FALLBACK_SESSION_PREFIX
    return raw_session_id


def _get_s3_bucket() -> Optional[str]:
    """Bucket name is discovered/exported by start.sh (SSM or env) at boot."""
    return os.environ.get("S3_BACKUP_BUCKET") or None


def _run_quiet(cmd):
    """Run a subprocess, swallowing failures the same way the old bash
    helpers did (`|| true`) -- S3 backup/restore is best-effort."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        print(f"[main.py] Command failed: {' '.join(cmd)}: {exc}")
        return None


def _s3_restore(prefix: str) -> bool:
    """Restore workspace state from s3://<bucket>/<prefix>/ (session-scoped)."""
    bucket = _get_s3_bucket()
    if not bucket:
        return False
    base = f"s3://{bucket}/{prefix}"
    print(f"[main.py] Restoring from {base} ...")
    os.makedirs(os.path.join(OPENCLAW_HOME, "credentials"), exist_ok=True)
    os.makedirs(os.path.join(OPENCLAW_HOME, "workspace"), exist_ok=True)

    config_path = os.path.join(OPENCLAW_HOME, "openclaw.json")
    _run_quiet(["aws", "s3", "cp", f"{base}/openclaw.json", config_path, "--quiet"])
    _run_quiet(["aws", "s3", "sync", f"{base}/credentials/",
                os.path.join(OPENCLAW_HOME, "credentials") + "/", "--quiet"])
    _run_quiet(["aws", "s3", "sync", f"{base}/workspace/",
                os.path.join(OPENCLAW_HOME, "workspace") + "/", "--quiet"])
    _run_quiet(["aws", "s3", "sync", f"{base}/identity/",
                os.path.join(OPENCLAW_HOME, "identity") + "/", "--quiet"])

    npm_tar = "/tmp/npm.tar.gz"
    result = _run_quiet(["aws", "s3", "cp", f"{base}/npm.tar.gz", npm_tar, "--quiet"])
    if result is not None and result.returncode == 0 and os.path.exists(npm_tar):
        _run_quiet(["tar", "xzf", npm_tar, "-C", OPENCLAW_HOME])
        os.remove(npm_tar)
        print("[main.py] Restored npm/ from tarball.")

    agents_tar = "/tmp/agents.tar.gz"
    result = _run_quiet(["aws", "s3", "cp", f"{base}/agents.tar.gz", agents_tar, "--quiet"])
    if result is not None and result.returncode == 0 and os.path.exists(agents_tar):
        _run_quiet(["tar", "xzf", agents_tar, "-C", OPENCLAW_HOME])
        os.remove(agents_tar)
        print("[main.py] Restored agents/ from tarball.")

    return os.path.exists(config_path)


def _s3_backup(prefix: Optional[str]) -> None:
    """Sync workspace state to s3://<bucket>/<prefix>/ (session-scoped)."""
    if not prefix:
        return
    bucket = _get_s3_bucket()
    if not bucket:
        return
    base = f"s3://{bucket}/{prefix}"

    config_path = os.path.join(OPENCLAW_HOME, "openclaw.json")
    if os.path.exists(config_path):
        _run_quiet(["aws", "s3", "cp", config_path, f"{base}/openclaw.json", "--quiet"])
    _run_quiet(["aws", "s3", "sync", os.path.join(OPENCLAW_HOME, "credentials") + "/",
                f"{base}/credentials/", "--quiet"])
    _run_quiet(["aws", "s3", "sync", os.path.join(OPENCLAW_HOME, "workspace") + "/",
                f"{base}/workspace/", "--quiet"])
    identity_dir = os.path.join(OPENCLAW_HOME, "identity")
    if os.path.isdir(identity_dir):
        _run_quiet(["aws", "s3", "sync", identity_dir + "/", f"{base}/identity/", "--quiet"])

    npm_dir = os.path.join(OPENCLAW_HOME, "npm")
    if os.path.isdir(npm_dir) and os.listdir(npm_dir):
        npm_tar = "/tmp/npm.tar.gz"
        tar_result = subprocess.run(["tar", "czf", npm_tar, "-C", OPENCLAW_HOME, "npm"],
                                     capture_output=True)
        if tar_result.returncode == 0:
            _run_quiet(["aws", "s3", "cp", npm_tar, f"{base}/npm.tar.gz", "--quiet"])
        if os.path.exists(npm_tar):
            os.remove(npm_tar)

    agents_dir = os.path.join(OPENCLAW_HOME, "agents")
    if os.path.isdir(agents_dir) and os.listdir(agents_dir):
        agents_tar = "/tmp/agents.tar.gz"
        tar_result = subprocess.run(["tar", "czf", agents_tar, "-C", OPENCLAW_HOME, "agents"],
                                     capture_output=True)
        if tar_result.returncode == 0:
            _run_quiet(["aws", "s3", "cp", agents_tar, f"{base}/agents.tar.gz", "--quiet"])
        if os.path.exists(agents_tar):
            os.remove(agents_tar)


def _start_background_sync():
    """Periodic S3 backup, keyed to the per-session prefix established by
    the first request. Started only after that prefix is known."""
    global _sync_thread_started
    if _sync_thread_started:
        return
    if not _get_s3_bucket():
        return
    interval = int(os.environ.get("SYNC_INTERVAL", "300"))

    def _loop():
        while True:
            time.sleep(interval)
            try:
                _s3_backup(_session_prefix)
            except Exception as exc:
                print(f"[main.py] Background S3 sync failed: {exc}")

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    _sync_thread_started = True
    print(f"[main.py] Background S3 sync started (every {interval}s, prefix={_session_prefix})")


def _handle_shutdown_signal(signum, frame):
    print(f"[main.py] Signal {signum} received -- final S3 backup for prefix={_session_prefix}...")
    try:
        _s3_backup(_session_prefix)
    except Exception as exc:
        print(f"[main.py] Final S3 backup failed: {exc}")
    print("[main.py] Sync complete. Exiting.")
    os._exit(0)


signal.signal(signal.SIGTERM, _handle_shutdown_signal)
signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _initialize_workspace():
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


def _strip_channels_for_webhook_mode():
    """Remove any channel config from openclaw.json when running webhook-only.

    Safety net: restored EBS/S3 state can contain live channel tokens that
    make the gateway poll Telegram/Discord/Slack directly, which clears the
    external Lambda router webhook. Webhook-only mode is signalled by the
    absence of CHANNEL_SECRETS_ARN; strip channels on every boot in that case.
    """
    if os.environ.get("CHANNEL_SECRETS_ARN"):
        return
    config_path = os.path.join(OPENCLAW_HOME, "openclaw.json")
    if not os.path.exists(config_path):
        return
    read_error = None
    config = None
    fh = open(config_path)
    try:
        config = json.load(fh)
    except json.JSONDecodeError as exc:
        read_error = exc
    finally:
        fh.close()
    if read_error is not None:
        print("[main.py] WARNING: Could not read config to strip channels: " + str(read_error))
        return
    if config.pop("channels", None) is not None:
        fh = open(config_path, "w")
        json.dump(config, fh, indent=2)
        fh.close()
        print("[main.py] Webhook-only mode: stripped stale channels config.")
    else:
        print("[main.py] Webhook-only mode: no channel config present (clean).")


def _load_channel_secrets():
    secret_arn = os.environ.get("CHANNEL_SECRETS_ARN", "")
    if not secret_arn:
        return
    print("[main.py] Fetching channel tokens from Secrets Manager...")
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
        print("[main.py] WARNING: Could not fetch secret: " + result.stderr[:200])


def _start_gateway():
    global _gateway_process
    gw_home = os.path.dirname(OPENCLAW_HOME) if os.path.basename(
        OPENCLAW_HOME.rstrip("/")) == ".openclaw" else OPENCLAW_HOME
    gw_log = open("/tmp/openclaw-gateway.log", "w")
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


def _ensure_session_workspace(raw_session_id: Optional[str]):
    """Request-time, session-scoped workspace init (replaces boot-time,
    session-unaware S3 restore). Runs once per (cold) container, on the
    first request it handles, because the AgentCore session id is only
    available inside a request context -- see module docstring."""
    global _workspace_ready, _session_prefix
    if _workspace_ready:
        return
    with _init_lock:
        if _workspace_ready:
            return
        sanitized = _sanitize_session_id(raw_session_id)
        _session_prefix = f"sessions/{sanitized}"
        print(f"[main.py] Session-scoped S3 prefix: {_session_prefix}")

        config_path = os.path.join(OPENCLAW_HOME, "openclaw.json")
        if os.path.exists(config_path):
            print("[main.py] Workspace already present on EBS -- zero cold start (no S3 restore needed).")
        elif _s3_restore(_session_prefix):
            print("[main.py] Restored workspace from S3 (session-scoped).")
        else:
            _initialize_workspace()

        _strip_channels_for_webhook_mode()
        if os.environ.get("CHANNEL_SECRETS_ARN"):
            _load_channel_secrets()
        else:
            print("[main.py] Webhook-only mode -- no channel polling configured.")

        _start_gateway()
        _start_background_sync()
        _workspace_ready = True


def _wait_for_gateway(timeout=180.0):
    global _gateway_ready
    if _gateway_ready:
        return True
    start = time.time()
    attempt = 0
    while time.time() - start < timeout:
        try:
            urlopen(GATEWAY_HEALTH_URL, timeout=3)
            elapsed = time.time() - start
            print(f"[main.py] Gateway ready after {elapsed:.1f}s ({attempt} polls)")
            _gateway_ready = True
            return True
        except (URLError, OSError):
            attempt += 1
            time.sleep(1.0)
    print(f"[main.py] ERROR: Gateway did not become ready within {timeout}s")
    return False


def _invoke_gateway(prompt, session_key=None):
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
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for content in item.get("content", []):
                            if content.get("type") == "output_text":
                                return content.get("text", "")
                return json.dumps(data.get("output", data))
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode()[:500]
            except Exception:
                pass
            if attempt < max_retries and exc.code >= 500:
                print(f"[main.py] Gateway returned {exc.code}, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(3)
                continue
            return f"Error: Gateway returned HTTP {exc.code}: " + (body or str(exc.reason))
        except URLError as exc:
            if attempt < max_retries:
                print(f"[main.py] Gateway request failed: {exc.reason}, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(3)
                continue
            return f"Error: Gateway request failed: {exc.reason}"
        except json.JSONDecodeError as exc:
            return f"Error: Invalid JSON response from gateway: {exc}"
        except Exception as exc:
            return f"Error: {exc}"
    return "Error: All retry attempts exhausted."


@app.entrypoint
async def handler(request: dict):
    session_id = None
    try:
        session_id = BedrockAgentCoreContext.get_session_id()
    except Exception as exc:
        print(f"[main.py] WARNING: could not read AgentCore session id from context: {exc}")
    _ensure_session_workspace(session_id)

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
