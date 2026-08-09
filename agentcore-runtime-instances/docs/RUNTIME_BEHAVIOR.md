← Back to [README](../README.md)

# Runtime Behavior

Understanding how OpenClaw behaves on AgentCore Runtime Instances across different lifecycle states.

## Instance Lifecycle States

```
┌──────────┐     invoke     ┌──────────┐     idle timeout     ┌──────────┐
│  STOPPED │ ─────────────→ │ RUNNING  │ ──────────────────→  │  STOPPED │
└──────────┘   (~2-3 min)   └──────────┘    (configurable)    └──────────┘
                                  │                                  │
                                  │  14-day TTL expires              │
                                  ▼                                  │
                            ┌──────────┐                             │
                            │ EXPIRED  │  ← EBS deleted              │
                            └──────────┘    S3 restore on next boot  │
                                                                     │
                              same session ID ───────────────────────┘
                              (EBS re-attached, zero cold start)
```

## Messaging Channels (Telegram, Discord, Slack)

### Architecture: Lambda Router (Recommended)

The **Lambda Router** pattern provides persistent channel delivery across idle-wake cycles. Channels are handled externally via webhooks — the container does NOT poll channels directly.

```
User message → Channel webhook → Lambda → invoke-agent-runtime (wakes instance)
                                              ← response
                                    Lambda → Channel API (reply to user)
```

| State | Channel behavior | Messages from users |
|-------|-----------------|---------------------|
| **Running** | Responding in real-time (5-15s) | Delivered immediately via Lambda |
| **Stopped (idle)** | Lambda still receives webhooks | Delivered after cold start (~90s) |
| **Expired (14-day)** | Lambda wakes new instance, S3 restore | Delivered after restore + boot |

### Key behavior

**The bot always responds**, regardless of instance state. The Lambda router is serverless and always available. It triggers `invoke-agent-runtime` which cold-starts the instance automatically.

Cold-start UX: user sees "⏳ Waking up..." immediately, then the real response replaces it.

### Idle-Wake Cycle

```
t=0      User sends message
t=0.5s   Lambda receives webhook, returns 200 to channel
t=1s     Lambda worker calls invoke-agent-runtime
t=1s     AgentCore provisions new instance (if stopped)
t=60s    Instance boots, container starts, gateway initializes
t=70-80s Gateway ready, processes prompt, calls Bedrock
t=80-90s Response sent to user via channel API
```

Subsequent messages while instance is warm: 5-15s.

### Configuration

- `idleRuntimeSessionTimeout: 14400` (4 hours) — rolling window: resets on every `invoke-agent-runtime` call, not fixed from first activity. Instance stays warm as long as a message arrives at least once within the window; a gap longer than the timeout triggers a cold start on the next message.
- Container runs in **webhook-only mode** (no `CHANNEL_SECRETS_ARN`)
- Lambda handles all channel I/O externally

See [Channel Router docs](CHANNEL_ROUTER.md) for full setup.

### Legacy: Direct Polling (Not Recommended)

If `CHANNEL_SECRETS_ARN` is set, the container polls channels directly. This breaks when the instance stops — the bot goes silent with no way to wake it from an incoming message. Use only if you keep the instance always-on.

## Idle Timeout and What Keeps the Session Alive

The `idleRuntimeSessionTimeout` (set in the agent runtime's `lifecycleConfiguration`) determines how long the instance runs without activity before AgentCore stops it.

**What counts as activity (resets the idle timer):**
- `InvokeAgentRuntime` API calls
- Messages received via connected channels (Telegram, Discord, etc.)

**What does NOT count as activity:**
- Heartbeat runs (internal agent turns)
- Cron job executions
- Background S3 sync
- Gateway health checks

This means: if your only interaction is via Telegram and you stop messaging, the instance will auto-stop after the timeout — even if heartbeat and cron jobs are running internally.

## Cron Jobs and Scheduled Tasks

Cron jobs are stored in SQLite on the EBS volume. They persist across stop/resume.

| State | Cron behavior |
|-------|--------------|
| **Running** | Jobs fire on schedule |
| **Stopped** | Jobs cannot fire (no process running) |
| **Resumed** | Overdue jobs are **rescheduled** (not replayed immediately) |
| **Expired + restored** | Jobs persist in SQLite, resume firing after restore |

### Important behaviors on resume

- Overdue isolated agent-turn jobs are rescheduled (not replayed all at once)
- This prevents a stampede of model calls during the channel-connect startup window
- Time-sensitive jobs that missed their window will fire late — there's no "discard if stale" option yet

## Heartbeat

Heartbeat runs periodic agent turns in the main session. It's designed for background checks (email, calendar, notifications).

| State | Heartbeat behavior |
|-------|-------------------|
| **Running** | Fires at configured cadence (default: 30 min) |
| **Stopped** | Does not fire |
| **Resumed** | Resumes with normal cadence |

**Heartbeat does NOT prevent idle timeout.** Heartbeat metadata updates don't count as user interaction for idle expiry purposes. Only real user messages or `InvokeAgentRuntime` calls keep the session alive.

## Pairing and Credentials

Pairing state is stored on the EBS volume:
```
$OPENCLAW_HOME/credentials/telegram-pairing.json
$OPENCLAW_HOME/credentials/telegram-default-allowFrom.json
```

| State | Pairing behavior |
|-------|-----------------|
| **Stopped → Resumed** | ✅ Persists (EBS intact) |
| **Expired + S3 backup** | ✅ Restored from S3 |
| **Expired, no S3 backup** | ❌ Lost — must re-pair |

### Ensuring pairing persists across 14-day expiry

Configure `S3_BACKUP_BUCKET` environment variable so the workspace (including credentials) is synced to S3. Without it, a session expiry after 14 days loses all state.

## Data Persistence Summary

| Data | Location | Stop/Resume | 14-day expiry (with S3) | 14-day expiry (no S3) |
|------|----------|-------------|------------------------|----------------------|
| Config (openclaw.json) | EBS | ✅ | ✅ Restored | ❌ Defaults only |
| Channel tokens | EBS (from Secrets Manager) | ✅ | ✅ Re-fetched on boot | ✅ Re-fetched on boot |
| Pairing state | EBS | ✅ | ✅ Restored | ❌ Must re-pair |
| Cron jobs | EBS (SQLite) | ✅ | ✅ Restored | ❌ Lost |
| Conversation history | EBS | ✅ | ✅ Restored | ❌ Lost |
| Workspace/memory | EBS | ✅ | ✅ Restored | ❌ Lost |

## Configuration Reference

```json
{
  "lifecycleConfiguration": {
    "idleRuntimeSessionTimeout": 14400,  // seconds before auto-stop (default: 4 hours, rolling window)
    "maxLifetime": 28800                // max session duration (default: 8 hours, max: 14 days)
  }
}
```

These are set on the agent runtime via `create-agent-runtime` or `update-agent-runtime`.

### Recommended values for messaging bots

| Use case | idleRuntimeSessionTimeout | maxLifetime | Notes |
|----------|--------------------------|-------------|-------|
| Lambda router (recommended) | 14400 (4 hours) | 1209600 (14 days) | Rolling window — resets per message; ~90s wake after a 4h+ gap |
| Frequent messaging | 3600 (1 hour) | 86400 (24 hours) | Fewer cold starts |
| Always-on (no cold starts) | 86400 (24 hours) | 1209600 (14 days) | Higher cost, instant responses |
