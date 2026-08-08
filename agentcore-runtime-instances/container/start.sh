#!/bin/bash
set -e

# OpenClaw on AgentCore Runtime Instances — Entrypoint
#
# This script handles:
# - Workspace initialization (EBS-first, S3 restore fallback)
# - Background S3 backup sync (every 5 minutes)
# - Graceful shutdown with final S3 sync on SIGTERM
# - Starting the Python AgentCore wrapper (which starts OpenClaw gateway)

OPENCLAW_HOME="${OPENCLAW_HOME:-/home/agent/.openclaw}"
S3_BACKUP_BUCKET="${S3_BACKUP_BUCKET:-}"
S3_BACKUP_PREFIX="${S3_BACKUP_PREFIX:-workspace}"

# Auto-discover S3 backup bucket from SSM Parameter Store if not set
if [ -z "$S3_BACKUP_BUCKET" ]; then
    DISCOVERED=$(aws ssm get-parameter --name /openclaw/backup-bucket --query Parameter.Value --output text --region "${AWS_REGION:-us-east-1}" 2>/dev/null || echo "")
    if [ -n "$DISCOVERED" ]; then
        S3_BACKUP_BUCKET="$DISCOVERED"
        echo "[start.sh] S3 backup bucket discovered: $S3_BACKUP_BUCKET"
    fi
fi

echo "[start.sh] OpenClaw home: $OPENCLAW_HOME"

# --- Workspace initialization (EBS-first) ---
if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
    echo "[start.sh] Workspace exists on EBS — zero cold start."
else
    echo "[start.sh] Workspace not found on EBS."

    # Try restoring from S3 backup (session expired after 14 days — rare)
    RESTORED=false
    if [ -n "$S3_BACKUP_BUCKET" ]; then
        echo "[start.sh] Attempting restore from S3 backup..."
        mkdir -p "$OPENCLAW_HOME"
        if aws s3 sync "s3://$S3_BACKUP_BUCKET/$S3_BACKUP_PREFIX/" "$OPENCLAW_HOME/" --quiet 2>/dev/null; then
            if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
                echo "[start.sh] Restored workspace from S3 backup."
                RESTORED=true
            fi
        fi
    fi

    # If no S3 backup, initialize from defaults (first boot)
    if [ "$RESTORED" = "false" ]; then
        echo "[start.sh] First boot — initializing workspace from defaults..."
        mkdir -p "$OPENCLAW_HOME"
        cp -r /app/.openclaw-defaults/* "$OPENCLAW_HOME/"
        echo "[start.sh] Workspace initialized."
    fi
fi

mkdir -p "$OPENCLAW_HOME/workspace"

# --- Background S3 backup sync (non-blocking) ---
if [ -n "$S3_BACKUP_BUCKET" ]; then
    (
        while true; do
            sleep 300  # every 5 minutes
            aws s3 sync "$OPENCLAW_HOME/" "s3://$S3_BACKUP_BUCKET/$S3_BACKUP_PREFIX/" --quiet 2>/dev/null || true
        done
    ) &
    S3_SYNC_PID=$!
    echo "[start.sh] Background S3 sync started (PID: $S3_SYNC_PID)"
fi

# --- SIGTERM handler: final S3 sync before exit ---
cleanup() {
    echo "[start.sh] SIGTERM received. Running final sync..."
    if [ -n "$S3_BACKUP_BUCKET" ]; then
        aws s3 sync "$OPENCLAW_HOME/" "s3://$S3_BACKUP_BUCKET/$S3_BACKUP_PREFIX/" --quiet 2>/dev/null || true
        echo "[start.sh] Final S3 sync complete."
    fi
    # Kill background sync if running
    [ -n "$S3_SYNC_PID" ] && kill "$S3_SYNC_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Start Python AgentCore wrapper (handles gateway startup) ---
echo "[start.sh] Starting AgentCore wrapper..."
python main.py &
WRAPPER_PID=$!
wait $WRAPPER_PID || true
