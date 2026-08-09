#!/bin/bash
set -e

# OpenClaw on AgentCore Runtime Instances — Entrypoint
#
# Persistence: S3 backup/restore (container filesystem is ephemeral)
# - Small files (credentials, workspace, config): S3 sync
# - Large dirs (npm, agents): S3 tarball (compressed)
# - Excluded: logs/, telegram/ (ephemeral)
#
# Boot: restore from S3 → start gateway → background sync
# Shutdown: SIGTERM → final sync → exit

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/agent/.openclaw}"
S3_BACKUP_BUCKET="${S3_BACKUP_BUCKET:-}"
S3_BACKUP_PREFIX="${S3_BACKUP_PREFIX:-workspace}"
SYNC_INTERVAL="${SYNC_INTERVAL:-300}"

# Auto-discover S3 backup bucket from SSM Parameter Store if not set
if [ -z "$S3_BACKUP_BUCKET" ]; then
    DISCOVERED=$(aws ssm get-parameter --name /openclaw/backup-bucket --query Parameter.Value --output text --region "${AWS_REGION:-us-east-1}" 2>/dev/null || echo "")
    if [ -n "$DISCOVERED" ]; then
        S3_BACKUP_BUCKET="$DISCOVERED"
        echo "[start.sh] S3 backup bucket discovered: $S3_BACKUP_BUCKET"
    fi
fi

echo "[start.sh] OpenClaw home: $OPENCLAW_HOME"

# --- S3 helper functions ---
s3_restore() {
    if [ -z "$S3_BACKUP_BUCKET" ]; then return 1; fi
    local PREFIX="s3://$S3_BACKUP_BUCKET/$S3_BACKUP_PREFIX"

    echo "[start.sh] Restoring from S3..."
    mkdir -p "$OPENCLAW_HOME/credentials" "$OPENCLAW_HOME/workspace"

    # Restore small files (fast, individual sync)
    aws s3 cp "$PREFIX/openclaw.json" "$OPENCLAW_HOME/openclaw.json" --quiet 2>/dev/null || true
    aws s3 sync "$PREFIX/credentials/" "$OPENCLAW_HOME/credentials/" --quiet 2>/dev/null || true
    aws s3 sync "$PREFIX/workspace/" "$OPENCLAW_HOME/workspace/" --quiet 2>/dev/null || true
    aws s3 sync "$PREFIX/identity/" "$OPENCLAW_HOME/identity/" --quiet 2>/dev/null || true

    # Restore tarballs (npm + agents)
    if aws s3 cp "$PREFIX/npm.tar.gz" /tmp/npm.tar.gz --quiet 2>/dev/null; then
        tar xzf /tmp/npm.tar.gz -C "$OPENCLAW_HOME/" 2>/dev/null || true
        rm -f /tmp/npm.tar.gz
        echo "[start.sh] Restored npm/ from tarball."
    fi
    if aws s3 cp "$PREFIX/agents.tar.gz" /tmp/agents.tar.gz --quiet 2>/dev/null; then
        tar xzf /tmp/agents.tar.gz -C "$OPENCLAW_HOME/" 2>/dev/null || true
        rm -f /tmp/agents.tar.gz
        echo "[start.sh] Restored agents/ from tarball."
    fi

    [ -f "$OPENCLAW_HOME/openclaw.json" ]
}

s3_backup() {
    if [ -z "$S3_BACKUP_BUCKET" ]; then return; fi
    local PREFIX="s3://$S3_BACKUP_BUCKET/$S3_BACKUP_PREFIX"

    # Sync small files
    aws s3 cp "$OPENCLAW_HOME/openclaw.json" "$PREFIX/openclaw.json" --quiet 2>/dev/null || true
    aws s3 sync "$OPENCLAW_HOME/credentials/" "$PREFIX/credentials/" --quiet 2>/dev/null || true
    aws s3 sync "$OPENCLAW_HOME/workspace/" "$PREFIX/workspace/" --quiet 2>/dev/null || true
    [ -d "$OPENCLAW_HOME/identity" ] && aws s3 sync "$OPENCLAW_HOME/identity/" "$PREFIX/identity/" --quiet 2>/dev/null || true

    # Tarball large dirs (only if they exist and have content)
    if [ -d "$OPENCLAW_HOME/npm" ] && [ "$(ls -A "$OPENCLAW_HOME/npm" 2>/dev/null)" ]; then
        tar czf /tmp/npm.tar.gz -C "$OPENCLAW_HOME" npm 2>/dev/null && \
            aws s3 cp /tmp/npm.tar.gz "$PREFIX/npm.tar.gz" --quiet 2>/dev/null || true
        rm -f /tmp/npm.tar.gz
    fi
    if [ -d "$OPENCLAW_HOME/agents" ] && [ "$(ls -A "$OPENCLAW_HOME/agents" 2>/dev/null)" ]; then
        tar czf /tmp/agents.tar.gz -C "$OPENCLAW_HOME" agents 2>/dev/null && \
            aws s3 cp /tmp/agents.tar.gz "$PREFIX/agents.tar.gz" --quiet 2>/dev/null || true
        rm -f /tmp/agents.tar.gz
    fi
}

# --- Workspace initialization ---
if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
    echo "[start.sh] Workspace exists — zero cold start."
else
    echo "[start.sh] Workspace not found."
    if s3_restore; then
        echo "[start.sh] Restored from S3 backup."
    else
        echo "[start.sh] First boot — initializing from defaults..."
        mkdir -p "$OPENCLAW_HOME"
        cp -r /app/.openclaw-defaults/* "$OPENCLAW_HOME/"
        echo "[start.sh] Workspace initialized."
    fi
fi

mkdir -p "$OPENCLAW_HOME/workspace"

# --- Background S3 sync ---
if [ -n "$S3_BACKUP_BUCKET" ]; then
    (
        while true; do
            sleep "$SYNC_INTERVAL"
            s3_backup
        done
    ) &
    S3_SYNC_PID=$!
    echo "[start.sh] Background S3 sync started (every ${SYNC_INTERVAL}s)"
fi

# --- SIGTERM handler ---
cleanup() {
    echo "[start.sh] SIGTERM received. Final sync..."
    s3_backup
    echo "[start.sh] Sync complete. Exiting."
    [ -n "$S3_SYNC_PID" ] && kill "$S3_SYNC_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Start AgentCore wrapper ---
echo "[start.sh] Starting AgentCore wrapper..."
python main.py &
WRAPPER_PID=$!
wait $WRAPPER_PID || true
