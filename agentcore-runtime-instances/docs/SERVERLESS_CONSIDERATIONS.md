# OpenClaw Serverless Considerations

> Observations from deploying OpenClaw on AgentCore Runtime (both microVMs and Instances).
> These are areas where OpenClaw's architecture assumes an always-on server, creating friction for serverless/managed compute environments.

## 1. HTTP Invocation Endpoint

**Status: ✅ Resolved (OpenClaw 2026.7+)**

OpenClaw ships with an OpenAI-compatible HTTP endpoint (`/v1/responses`) that can be enabled via config. This eliminates the WebSocket state machine and reduces the wrapper to a simple HTTP pass-through.

**Before (WebSocket):** ~243 lines, `websockets` dependency, 5-step protocol (connect → challenge → auth → agent → agent.wait)

**After (HTTP endpoint):** ~185 lines, stdlib only (`urllib`), single POST call with direct text response.

Enable with:
```json
"gateway": {
  "http": { "endpoints": { "responses": { "enabled": true } } }
}
```

**Remaining gap:** The Python wrapper still exists for the AgentCore SDK (`bedrock_agentcore` entrypoint). A fully native integration would eliminate Python entirely — OpenClaw could serve the AgentCore protocol (`:8080 /invocations + /ping`) directly.

## 2. Slow Gateway Startup (No Lazy Init)

**Problem:** OpenClaw gateway takes several seconds to boot — it eagerly loads plugins, skills, workspace files, and initializes all subsystems before serving requests.

**Impact:** AgentCore kills containers that don't respond to `/ping` (health check) within ~10 seconds. The wrapper works around this by serving `:8080` immediately while the gateway boots in the background, but this means the first invocation must wait/retry until the gateway is ready.

**Ideal:** A "serverless mode" or `--lazy-init` flag that:
- Binds the port immediately (health check passes)
- Defers workspace loading until the first request arrives
- Lazy-loads plugins/skills on first use rather than at boot

**Related:**
- [openclaw/openclaw#65444](https://github.com/openclaw/openclaw/issues/65444) — Lazy-load channel plugins (deferred `connect()` vs immediate `init()`)
- [openclaw/openclaw#67040](https://github.com/openclaw/openclaw/issues/67040) — Persist plugin discovery cache, defer plugin loading
- [openclaw/openclaw#48380](https://github.com/openclaw/openclaw/issues/48380) — Gateway startup regression with bundled plugins

## 3. Incomplete Graceful Shutdown

**Problem:** OpenClaw does handle SIGTERM (logs `[gateway] signal SIGTERM received`), but it doesn't guarantee in-flight agent turns complete before exit. There are known issues with shutdown timeouts leaving lock files and child processes not being terminated.

**Impact:** With EBS persistence (Instances compute), this is less critical since the volume persists. But for the S3 backup sync, we need to ensure the final sync completes before exit. The `start.sh` entrypoint handles this with a SIGTERM trap that runs `aws s3 sync` before allowing the process to exit.

**Related:**
- [openclaw/openclaw#32961](https://github.com/openclaw/openclaw/issues/32961) — RFC: Drain phase (stop accepting → wait for in-flight → exit)
- [openclaw/openclaw#57052](https://github.com/openclaw/openclaw/issues/57052) — Shutdown timeout leaves lock file
- [openclaw/openclaw#18420](https://github.com/openclaw/openclaw/issues/18420) — Child process not terminated on SIGTERM

## 4. Cron/Scheduler Assumes Always-On

**Problem:** OpenClaw's built-in cron scheduler fires based on an always-running process timer. When the instance auto-stops on idle, the process isn't running and scheduled jobs miss their fire times.

**Mitigation:** OpenClaw already catches up missed cron jobs on gateway restart ([#73644](https://github.com/openclaw/openclaw/issues/73644), [#3733](https://github.com/openclaw/openclaw/issues/3733)). So when the instance resumes, overdue jobs fire immediately. This largely solves the problem for Instances.

**Remaining gap:** Some jobs are time-sensitive and stale after missing their window (e.g., "remind me at 9am" firing at 2pm is unhelpful). There's no per-job `maxSkip` or `catchUp: false` option yet.

**Workarounds for time-sensitive jobs:**
- **Accept the gap** — For personal assistants, catching up reminders late is better than not at all
- **Keep instance alive** — Set `idleInstanceTimeout` high enough that periodic cron activity prevents auto-stop

## 5. Channel Reconnection on Resume

**Problem:** When the instance auto-stops and resumes, outbound channel connections must re-establish. Behavior varies by channel:

| Channel | Reconnection behavior | Impact |
|---------|----------------------|--------|
| Telegram (long-poll) | ✅ Auto-reconnects with backoff | Messages queued server-side, delivered on resume |
| Discord (WebSocket) | ⚠️ Known reconnection bugs | May enter infinite reconnect loop; manual restart needed |
| WhatsApp Web | ⚠️ May require re-pairing | Session can expire during long idle periods |
| Slack (Socket Mode) | ✅ Auto-reconnects | Buffered messages delivered on resume |

**Impact:** Discord is the primary concern — multiple open issues report failed auto-reconnection after connection drops.

**Workaround:** For Instances, prefer Telegram or Slack (both handle resume well). Discord may need a gateway restart on resume if the reconnect fails.

**Related:**
- [openclaw/openclaw#30514](https://github.com/openclaw/openclaw/issues/30514) — Discord doesn't auto-reconnect after WebSocket error
- [openclaw/openclaw#13688](https://github.com/openclaw/openclaw/issues/13688) — Discord 1005/1006 disconnects with failing resume logic
- [openclaw/openclaw#11836](https://github.com/openclaw/openclaw/issues/11836) — Discord infinite reconnection loop

## 6. `OPENCLAW_HOME` Path Assumptions

**Problem:** OpenClaw defaults to `~/.openclaw/` and while it supports `--home` or `OPENCLAW_HOME`, some internal paths (plugin cache, skill downloads, npm packages) may not fully respect the override.

**Impact:** On AgentCore Instances, the workspace is at `/home/agent/.openclaw/` (on EBS root volume). If any subsystem writes to a different path, that data may not be included in S3 backup sync.

**Ideal:** All internal paths should derive exclusively from `OPENCLAW_HOME` with zero fallback to `~/.openclaw/`.

## 7. No Workspace Size Awareness

**Problem:** EBS root volume has a fixed size (30GB); S3 backup costs scale with size. OpenClaw doesn't track or constrain its own workspace disk usage.

**Impact:** Conversation history, memory files, and tool artifacts grow unbounded. On long-running sessions, the workspace can fill the EBS volume causing write failures.

**Ideal:** 
- `openclaw gateway --max-workspace-size 500MB` with automatic pruning of old sessions/history
- Or at minimum, a health check that surfaces disk usage warnings

---

## 8. Persistence: EBS Works, S3 Files Doesn't (Yet) for Instances

**Status (Aug 2026):** `filesystemConfigurations` (S3 Files, EFS, sessionStorage) is NOT supported with `capacityProviderConfiguration` (Instances compute type). The API returns:

> "sessionStorage, EFS, and S3 Files storage types are not supported with capacityProviderConfiguration"

**This is likely a day-one limitation** that will be resolved. The Instances compute type launched with EBS root volume persistence as the primary storage model. S3 Files support for Instances may come later.

**Current workaround:** EBS root volume provides zero-cold-start persistence for normal operation. S3 sync (via `aws s3 sync` in the container entrypoint) provides backup for the rare case where a session expires after 14 days.

**When S3 Files becomes available for Instances:**
- The S3 backup sync in `start.sh` would become unnecessary
- Workspace would persist indefinitely (beyond 14-day session TTL)
- No container-level sync code needed — AgentCore handles the mount

---

## Database Scaling & Persistence

OpenClaw stores state as files on disk with a [SQLite index](https://docs.openclaw.ai/concepts/memory-builtin) for search. The default architecture:
- **Markdown files** — source of truth (MEMORY.md, daily notes)
- **SQLite** — per-agent search index with FTS5 (BM25) + vector embeddings ([docs](https://docs.openclaw.ai/concepts/memory-builtin))
- **JSON files** — session/conversation history

This works for single-user persistent compute but creates friction at scale.

### Three persistence layers

| Layer | Default | Plugin slot? | Database option? |
|-------|---------|-------------|------------------|
| Memory (MEMORY.md, daily notes) | Markdown + [SQLite index](https://docs.openclaw.ai/concepts/memory-builtin) | ✅ `plugins.slots.memory` ([docs](https://docs.openclaw.ai/concepts/memory)) | Community plugins exist (not official) |
| Session history (conversations) | JSON files | ❌ | ❌ |
| Workspace (skills, config, tools) | Filesystem | ❌ | ❌ |

### What works today

The memory slot is replaceable — community projects like [PostClaw](https://github.com/ChristopherLittle51/PostClaw) demonstrate PostgreSQL + pgvector backends. These are unofficial and not production-validated.

Sessions and workspace remain file-based with no plugin abstraction. Our EBS + S3 backup pattern handles this for Instances.

### Related Issues

- [openclaw/openclaw#15093](https://github.com/openclaw/openclaw/issues/15093) — Native PostgreSQL + pgvector memory backend (closed)
- [openclaw/openclaw#32966](https://github.com/openclaw/openclaw/issues/32966) — Pluggable `memory_search` backend interface (open)

### What a full database backend would unlock

If OpenClaw added `plugins.slots.sessions` and `plugins.slots.storage`:
- Truly stateless containers (no EBS, no S3 sync)
- Instant multi-region failover
- Shared session history across agents
- Query-able conversation logs (analytics, compliance)

On AWS this maps to Aurora Serverless v2 (scales to zero, pgvector, IAM auth).

---

## Priority Ranking

| Consideration | Severity | Workaround | Effort |
|-----|----------|---------------------|--------|
| 1. HTTP endpoint | ✅ Resolved | HTTP `/v1/responses` endpoint | Low |
| 2. Slow startup | High | Wrapper serves health check first | Low |
| 3. Incomplete graceful shutdown | Medium | SIGTERM trap in start.sh | Low |
| 4. Cron catch-up timing | Low | Catch-up exists; accept stale jobs | Low |
| 5. Channel reconnection | Medium | Use Telegram/Slack (Discord has issues) | Low |
| 6. OPENCLAW_HOME paths | Low | Testing confirms it works for core | Low |
| 7. No size awareness | Low | Manual cleanup / S3 lifecycle rules | Low |
| 8. S3 Files not supported | Low | EBS + S3 sync (works great) | N/A (AWS) |
| 9. Database scaling | Low | Memory plugin slot exists; EBS/S3 for rest | Medium |

**Consideration #1 (HTTP endpoint) would eliminate the need for the Python wrapper entirely**, reducing container size by ~200MB, removing the startup race, and making the deployment a single Node.js process.
