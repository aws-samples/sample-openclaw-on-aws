#!/bin/bash
set -e

# OpenClaw on AgentCore Runtime Instances — Entrypoint
#
# Persistence: S3 backup/restore (container filesystem is ephemeral)
# - Small files (credentials, workspace, config): S3 sync
# - Large dirs (npm, agents): S3 tarball (compressed)
# - Excluded: logs/, telegram/ (ephemeral)
#
# Tenant isolation: the actual S3 restore/backup logic lives in main.py, not
# here. The AgentCore runtime session id is only available inside an HTTP
# request handler (via the X-Amzn-Bedrock-AgentCore-Runtime-Session-Id
# header), so it cannot be known at container boot time, before start.sh has
# run. Doing session-unaware restore/backup here against one static prefix
# (the old "workspace" prefix) would let a second tenant that cold-starts on
# this same container silently inherit the first tenant's files. So:
#   - start.sh's job is just: discover the S3 bucket, export it, launch
#     main.py, and handle SIGTERM by forwarding it to main.py so *it* can run
#     a final per-session sync with the session-scoped prefix it already
#     knows about.
#   - main.py performs the actual per-session S3 restore (on the first
#     request of a cold container) and periodic/final per-session backup,
#     using f"sessions/{sanitized_session_id}" as the S3 prefix instead of a
#     shared static prefix.
#
# Boot: discover bucket -> start main.py (which restores/starts gateway on
#       first request) -> main.py runs periodic per-session sync.
# Shutdown: SIGTERM -> forwarded to main.py -> final per-session sync -> exit.

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/agent/.openclaw}"
S3_BACKUP_BUCKET="${S3_BACKUP_BUCKET:-}"

# Auto-discover S3 backup bucket from SSM Parameter Store if not set
if [ -z "$S3_BACKUP_BUCKET" ]; then
    DISCOVERED=$(aws ssm get-parameter --name /openclaw/backup-bucket --query Parameter.Value --output text --region "${AWS_REGION:-us-east-1}" 2>/dev/null || echo "")
    if [ -n "$DISCOVERED" ]; then
        S3_BACKUP_BUCKET="$DISCOVERED"
        echo "[start.sh] S3 backup bucket discovered: $S3_BACKUP_BUCKET"
    fi
fi
export S3_BACKUP_BUCKET

echo "[start.sh] OpenClaw home: $OPENCLAW_HOME"
mkdir -p "$OPENCLAW_HOME/workspace"

# --- Fallback-only workspace init ---
# If nothing has ever restored a workspace on this EBS volume yet, seed it
# with defaults so `openclaw.json` exists even before the first request
# comes in. main.py still does the real (session-scoped) S3 restore-or-init
# at request time; this is just so the process tree has something sane if
# main.py's own default-init path is ever bypassed.
if [ ! -f "$OPENCLAW_HOME/openclaw.json" ] && [ -d /app/.openclaw-defaults ]; then
    echo "[start.sh] No workspace on EBS yet — main.py will restore or initialize it per-session on first request."
fi

# --- SIGTERM handler ---
# main.py installs its own SIGTERM/SIGINT handlers to run a final,
# session-scoped S3 backup using the prefix it derived at request time.
# start.sh just needs to forward the signal and wait.
cleanup() {
    echo "[start.sh] Signal received. Forwarding to main.py for final per-session sync..."
    [ -n "$WRAPPER_PID" ] && kill -TERM "$WRAPPER_PID" 2>/dev/null || true
    wait "$WRAPPER_PID" 2>/dev/null || true
    echo "[start.sh] main.py exited. Exiting."
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Start AgentCore wrapper ---
echo "[start.sh] Starting AgentCore wrapper..."
python main.py &
WRAPPER_PID=$!
wait $WRAPPER_PID || true
