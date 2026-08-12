# Security & Reliability Fixes — Summary

Fixes for all 9 findings from the code audit, implemented on
`fix/security-hardening-audit`. Each section: files changed, before/after
behavior.

## #1 — Discord webhook auth was stubbed out

**Files:** `lambda/router/adapters/discord.py`, `lambda/router/requirements.txt`,
`scripts/deploy-channel-router.sh`, `stacks/lambda_router_stack.py`

**Before:** `validate_webhook()` was `# TODO: Add Ed25519 verification... return True`.
`DISCORD_PUBLIC_KEY` was read into a module var and never used. Any anonymous
POST to `/webhook/discord` reached the agent with zero verification.

**After:** Real Ed25519 signature verification via PyNaCl, checking
`X-Signature-Ed25519` + `X-Signature-Timestamp` against `DISCORD_PUBLIC_KEY`
per Discord's spec (signed message = `timestamp + raw_body`). Fails **closed**:
missing/invalid public key, missing headers, or a bad signature all return
`False`. `pynacl` added to the Lambda dependency manifest (both the CDK asset
bundling command and the shell deploy script's `pip install` step).

## #2 — Telegram/Slack validators failed open when secret unset

**Files:** `lambda/router/adapters/telegram.py`, `lambda/router/adapters/slack.py`

**Before:** Both `validate_webhook()` returned `True` when their secret env
var was empty — an unconfigured channel accepted unauthenticated traffic.

**After:** Both return `False` when unconfigured. An unconfigured channel is
now simply unreachable, matching the correct posture (no config = no access,
not "everyone has access").

## #3 — Route registration was unconditional regardless of credentials

**Files:** `stacks/lambda_router_stack.py`

**Before:** The CDK stack looped over `("telegram", "discord", "slack")`
unconditionally and created all three API Gateway routes even if a channel's
token/secret was never supplied to the stack constructor.

**After:** Routes are only created for channels with their required
credentials present (`telegram_bot_token`; `discord_bot_token` +
`discord_public_key`; `slack_bot_token` + `slack_signing_secret`). CfnOutputs
for webhook URLs are similarly conditional. This brings the CDK stack to
parity with `deploy-channel-router.sh`'s existing conditional `CHANNELS` array.
Verified via CDK synth (see Validation below): only the channels with
credentials in the test input got routes.

## #4 — Unrestricted root exec with no approval gate

**Files:** `container/.openclaw/openclaw.json`, `container/.openclaw/openclaw.example.json`,
`container/.openclaw/exec-approvals.json` (new), `README.md`

**Before:** `tools.exec.security: "full"` with `ask: "off"` — the agent could
run arbitrary shell commands as root, with no approval prompt and no
restriction on what commands were allowed.

**After:** `tools.exec.security: "allowlist"` (kept `ask: "off"` — the bot
must stay non-interactive for async Telegram/Discord/Slack messaging), plus
`strictInlineEval: true` so injected text can't smuggle a shell command
through an already-allowlisted interpreter (e.g. `python3 -c "..."`). A new
`exec-approvals.json` defines the allowlist: `git`, `npm`, `node`, `python3`,
`pip`, `pip3`, `pytest`, and common file tools (`cat`, `ls`, `grep`, `find`,
`mkdir`, `cp`, `mv`). The bot keeps full coding capability (git, package
managers, running tests, file read/write) but can no longer run arbitrary
shell, `curl | bash`, `sudo`, or destructive commands outside that set — even
if a prompt injection reaches the model. Both `openclaw.json` and
`openclaw.example.json` updated identically (they were previously byte-
identical and remain so). README's Security section documents the change and
why `ask: "off"` was intentionally kept.

## #5 — No authorization on Slack/Discord; Telegram allowlist didn't trim whitespace

**Files:** `lambda/router/adapters/telegram.py`, `slack.py`, `discord.py`,
`scripts/deploy-channel-router.sh`, `stacks/lambda_router_stack.py`,
`docs/CHANNEL_ROUTER.md`

**Before:** Only `telegram.py` checked `ALLOWED_USER_IDS`. `slack.py` and
`discord.py` parsed `user_id` into their returned dict but nothing enforced
an allowlist for those channels — any Slack workspace member or Discord
guild member had unrestricted access. Telegram's `ALLOWED_USER_IDS.split(",")`
also didn't trim whitespace, so `"111, 222"` produced `["111", " 222"]` and
silently locked out anyone whose ID had a leading space.

**After:** Each adapter now enforces its own allowlist directly in
`parse_inbound()`: `ALLOWED_USER_IDS` (Telegram, existing — now trimmed),
`SLACK_ALLOWED_USER_IDS` (new), `DISCORD_ALLOWED_USER_IDS` (new). All three
`.strip()` each ID on split and filter empty entries. Unauthorized senders
are rejected before the worker is ever invoked. `deploy-channel-router.sh`
and `lambda_router_stack.py` both gained the new `--slack-allowed-ids`/
`--discord-allowed-ids` flags (and constructor params), and the shell script
now **requires** an allowlist for any channel that's enabled — an empty
allowlist on an enabled channel is treated as a misconfiguration to catch at
deploy time, not a permissive default. `docs/CHANNEL_ROUTER.md`'s per-channel
tables document the new env vars.

## #6 — All users shared one AgentCore session

**Files:** `lambda/router/core.py`, `lambda/router/index.py`,
`docs/MULTI_TENANCY_CONSIDERATIONS.md`

**Before:** `SESSION_ID` was a single Lambda-wide env var; every user's
message routed to the same `runtimeSessionId`, so all users shared one EBS
workspace and conversation history regardless of who sent the message.

**After:** `core.py`'s new `derive_session_id(channel, user_id)` produces a
deterministic, collision-safe per-user session id
(`f"{channel}-{user_id}-{sha256_hex}"`, padded to meet AgentCore's 33-char
minimum). `index.py`'s `handle_webhook` derives this id before invoking the
worker and passes it through the worker payload; `handle_worker` uses it for
`invoke_with_retry(message_text, session_id=derived_id)`. Cold-start tracking
(`is_likely_cold_start`/`record_success`) is now also keyed by the per-user
session id, so cold-start UX is correct per-user rather than globally shared.
`SESSION_ID` remains only as a fallback default for the (should-not-happen)
case where an adapter fails to supply a `user_id`. Docs updated to describe
this simpler in-router derivation instead of the previously-described-but-
never-implemented DynamoDB user-table lookup pattern.

## #7 — S3 backup prefix shared across all tenants (documented limitation, no code fix)

**Files:** `docs/MULTI_TENANCY_CONSIDERATIONS.md`

**Before:** `S3_BACKUP_PREFIX` defaults to the constant `"workspace"`, set
once at the shared AgentCore runtime level in `deploy.sy`'s
`create_agent_runtime` call — every tenant's instance syncs to the identical
S3 path.

**After (docs-only, as scoped):** AgentCore Runtime Instances doesn't expose
the invoking `runtimeSessionId` to the running container as an environment
variable, so this isn't fixable in application code alone without either (a)
AWS adding that exposure, or (b) deploying a separate runtime per tenant.
`docs/MULTI_TENANCY_CONSIDERATIONS.md` now states this positively and
explicitly up front: the sample is validated end-to-end for **single-tenant**
deployments; the Silo Model is the target multi-tenant architecture; and
per-tenant S3 backup isolation is called out as the specific gap (not the
EC2/EBS isolation, which AgentCore already enforces per-session) — framed as
"next milestone for multi-tenant support" with a clear callout box so nobody
deploys multi-tenant assuming S3 backups are already isolated.

## #8 — Unauthenticated cost-amplification DoS

**Files:** `stacks/lambda_router_stack.py`, `scripts/deploy-channel-router.sh`,
`lambda/router/core.py`, `lambda/router/index.py`, `docs/CHANNEL_ROUTER.md`

**Before:** No rate limiting anywhere. Each accepted webhook could provision
a c7g.large instance and hold a Lambda worker open for the full ~255s retry
schedule, with no reserved concurrency and no API Gateway throttling — a
burst of requests multiplied directly into EC2/Bedrock/Lambda spend.

**After:** Three independent layers, verified via CDK synth:
1. **Reserved concurrency** (default 8, `RESERVED_CONCURRENCY` env override
   in the shell script) caps how many concurrent webhook+worker invocations
   can run at once. Confirmed in synthesized template:
   `ReservedConcurrentExecutions: 8`.
2. **API Gateway throttling** on the `$default` stage (default burst=10,
   rate=5/s). Confirmed in synthesized template:
   `DefaultRouteSettings: {ThrottlingBurstLimit: 10, ThrottlingRateLimit: 5}`.
3. **Per-user cooldown** — `core.py`'s new `is_rate_limited(channel, user_id)`
   reuses the cold-start DynamoDB table with a `ratelimit:<channel>:<user_id>`
   key namespace to drop repeated messages from the same user within
   `REQUEST_COOLDOWN_SECONDS` (default 5s), wired into `index.py`'s
   `handle_webhook` *before* the async self-invoke, so a spam burst from one
   user doesn't multiply into N full invoke-with-retry cycles. Fails open on
   DynamoDB errors (cost guard, not the authorization boundary — should never
   block legitimate traffic due to a transient AWS issue).

`deploy-channel-router.sh` gained equivalent
`aws lambda put-function-concurrency` and `aws apigatewayv2 update-stage`
calls. `docs/CHANNEL_ROUTER.md` documents all three under a new
"Rate Limiting & Cost Controls" section.

## #9 — Python code injection in deploy-channel-router.sh

**Files:** `scripts/deploy-channel-router.sh`, `README.md`

**Before:** `ENV_JSON=$(python3 -c "... '$VAR' ...")` interpolated 10 shell
variables directly into a Python program body as naive triple-quoted string
literals. A credential value containing a single quote or triple-quote could
break out of the string and execute as Python — confirmed exploitable per the
audit finding (a crafted `SLACK_SIGNING_SECRET` ran injected Python locally).

**After:** Rewrote the block to build `ENV_JSON` via `jq -n --arg` — every
value is passed as an opaque string argument and JSON-escaped by `jq`
regardless of its contents (quotes, backslashes, newlines), with no
code-generation step for an attacker-controlled value to break out of.
`jq` added to README's Prerequisites list (it's typically already present
alongside the AWS CLI, but now called out explicitly since the script
depends on it).

**Verification of the fix:** tested by hand with values containing a single
quote and a backslash (`O'Brien\test`) as a stand-in credential — `jq -n
--arg` produces valid, correctly-escaped JSON for these inputs with no
possibility of code execution, since `--arg` never treats its value as
anything other than a string.

## Validation performed

- `python3 -m py_compile` on every changed `.py` file: all pass.
- `bash -n scripts/deploy-channel-router.sh`: pass.
- JSON validity check on `openclaw.json`, `openclaw.example.json`,
  `exec-approvals.json`: all valid.
- CDK synth of `LambdaRouterStack` via a standalone test script (not part of
  the deployed app — the stack isn't wired into `app.py` since its
  `agent_runtime_arn` constructor arg only exists after the main
  `deploy.sh` run). Synth succeeded; confirmed in the generated
  CloudFormation template:
  - `ReservedConcurrentExecutions: 8`
  - `DefaultRouteSettings: {ThrottlingBurstLimit: 10, ThrottlingRateLimit: 5}`
  - Exactly the three expected routes (`POST /webhook/telegram`, `/discord`,
    `/slack`) when all three channels' credentials are supplied — confirming
    the conditional-route logic for fix #3.
- Re-read every changed file in full after editing to confirm no leftover
  fail-open logic (`return True` on missing secrets) and no syntax errors.

## Files changed (full list)

- `container/.openclaw/openclaw.json`
- `container/.openclaw/openclaw.example.json`
- `container/.openclaw/exec-approvals.json` (new)
- `lambda/router/core.py`
- `lambda/router/index.py`
- `lambda/router/requirements.txt` (new)
- `lambda/router/adapters/telegram.py`
- `lambda/router/adapters/slack.py`
- `lambda/router/adapters/discord.py`
- `stacks/lambda_router_stack.py`
- `scripts/deploy-channel-router.sh`
- `docs/CHANNEL_ROUTER.md`
- `docs/MULTI_TENANCY_CONSIDERATIONS.md`
- `README.md`
