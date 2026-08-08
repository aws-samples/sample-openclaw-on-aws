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

Channels use **outbound long-polling or WebSocket connections** from inside the container. They only work while the gateway process is running.

| State | Channel behavior | Messages from users |
|-------|-----------------|---------------------|
| **Running** | Connected, responding in real-time | Delivered immediately |
| **Stopped (idle)** | Disconnected, no polling | Queued by Telegram/Slack (~24h); Discord may drop |
| **Resumed** | Auto-reconnects, queued messages delivered | Delivered on reconnect (may be stale) |
| **Expired (14-day)** | Reconnects after S3 restore | Delivered after restore (if within queue TTL) |

### Key implication

**When the instance is stopped, the bot does not respond.** Telegram and Slack queue messages server-side (typically 24 hours). Discord may not reliably deliver missed messages.

The instance only wakes on an explicit `InvokeAgentRuntime` API call — incoming Telegram messages cannot trigger a wake.

### Recommendations

- Set `idleRuntimeSessionTimeout` high enough for your use case (default: 900s / 15 min)
- For always-responsive bots, set timeout to 28800 (8 hours) or the `maxLifetime` value
- Accept that the bot sleeps during idle periods — message when you need it, it resumes in ~30s

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
    "idleRuntimeSessionTimeout": 900,   // seconds before auto-stop (default: 15 min)
    "maxLifetime": 28800                // max session duration (default: 8 hours, max: 14 days)
  }
}
```

These are set on the agent runtime via `create-agent-runtime` or `update-agent-runtime`.

### Recommended values for messaging bots

| Use case | idleRuntimeSessionTimeout | maxLifetime |
|----------|--------------------------|-------------|
| Personal bot (occasional use) | 3600 (1 hour) | 28800 (8 hours) |
| Active bot (frequent messaging) | 14400 (4 hours) | 86400 (24 hours) |
| Always-on (within session) | 86400 (24 hours) | 1209600 (14 days) |
