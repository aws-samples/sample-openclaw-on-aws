#!/bin/bash
set -e

# The AgentCore Instances host may start this script with an arbitrary working
# directory; main.py is referenced relatively below, so pin cwd to /app.
cd "$(dirname "$(readlink -f "$0")")"

# Fail loudly. When this script dies under `set -e`, the AgentCore host only
# reports "Container exited before becoming ready" -- the failing command is
# invisible. Print it, then pause briefly so the host's log drainer can attach
# and ship these lines to CloudWatch before the container is torn down.
_start_failed() {
    ec=$?
    echo "[start.sh] FATAL: command '$BASH_COMMAND' failed with exit code $ec"
    echo "[start.sh] diag: uid=$(id -u) gid=$(id -g) cwd=$(pwd) OPENCLAW_HOME=$OPENCLAW_HOME"
    echo "[start.sh] diag: stat OPENCLAW_HOME -> $(ls -ld "$OPENCLAW_HOME" 2>&1)"
    echo "[start.sh] diag: stat parent -> $(ls -ld "$(dirname "$OPENCLAW_HOME")" 2>&1)"
    echo "[start.sh] diag: mounts:"
    (grep -E " / | /home| /tmp | /app" /proc/mounts 2>&1 || true)
    echo "[start.sh] diag: write test root -> $(touch /.__wtest 2>&1 && echo ok || true)"
    echo "[start.sh] diag: write test home -> $(touch "$OPENCLAW_HOME/.__wtest" 2>&1 && echo ok || true)"
    sleep 20
    exit $ec
}
trap _start_failed ERR

# OpenClaw on AgentCore Runtime Instances — Entrypoint
#
# Privilege model: this script runs as root only long enough to prepare the
# EBS-backed OPENCLAW_HOME mount (chown to the non-root `agent` user), then
# drops privileges via `gosu` before starting main.py / the OpenClaw
# gateway. Everything the agent executes (including allowlist-approved exec
# tool commands) runs as `agent`, not root -- so an allowlist escape or
# exec-approval bypass no longer grants root on the host. See the Dockerfile
# for the `agent` user definition and the checkov:skip rationale.
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
RUN_USER="${OPENCLAW_RUN_USER:-agent}"

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

# --- Workspace ownership ---
# The container runs as the unprivileged `agent` user from the start (see
# Dockerfile: USER agent), so there is no privilege drop to perform here and
# no root-owned mount to fix up. OPENCLAW_HOME is created and chowned to
# `agent` at build time.
#
# Do NOT reintroduce a root-time `mkdir`/`chown` of OPENCLAW_HOME: AgentCore
# Runtime Instances starts the container on an id-mapped overlay mount where
# the container's uid 0 holds no DAC override over `agent`-owned paths, so
# that step fails with EPERM and the entrypoint dies before the agent can
# answer /ping. The host reports only "Container exited before becoming
# ready", which is why the ERR trap above prints the failing command.
#
# If the process is somehow still root (a modified image, or a local `docker
# run --user 0`), fix ownership and re-exec as `agent` so the gateway and
# every exec-tool command it runs stay unprivileged.
if [ "$(id -u)" = "0" ]; then
    echo "[start.sh] Running as root; repairing ownership and re-execing as '$RUN_USER'."
    mkdir -p "$OPENCLAW_HOME/workspace"
    chown -R "$RUN_USER":"$RUN_USER" "$OPENCLAW_HOME"
    exec gosu "$RUN_USER" env HOME=/home/agent "$0" "$@"
fi

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

# --- Start AgentCore wrapper (already running as non-root `agent`) ---
echo "[start.sh] Starting AgentCore wrapper as user '$(id -un)'..."
HOME=/home/agent python3 main.py &
WRAPPER_PID=$!
wait $WRAPPER_PID || true
