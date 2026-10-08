# Strands Box PoC: sandboxing a local OpenClaw gateway

## Scope and limitation (read first)

[Strands Box](https://github.com/strands-agents/box) is Preview software,
macOS Apple Silicon only, no Linux support as of 2026-10. This repo's
production deployment target (`agentcore-runtime-instances`) runs Linux/arm64
EC2 under AgentCore Instances compute -- Box cannot run there today. This PoC
secures a **local OpenClaw gateway on a dev Mac**, not the AWS deployment.
Revisit once Box ships Linux support; at that point the same policy model
could apply directly to the production container.

## What Box adds beyond OpenClaw's own hardening

OpenClaw already has in-process exec hardening (`tools.exec.security`,
`argPattern`, `strictInlineEval` -- see `agentcore-runtime-instances/README.md`
Security section). Box adds a layer **outside** that process:

1. OS-enforced (Seatbelt) filesystem/network boundaries that don't depend on
   OpenClaw's own code being bug-free.
2. Credential injection at the network layer -- the gateway process never
   holds the raw Bedrock/Discord secret.
3. Temporal/semantic Dogwood policy that can see event history across shell,
   Python, and network calls -- e.g. deny egress for an hour after a secrets
   file is read. OpenClaw's own argv filtering can't express this; see
   `policy.dw`'s `no_egress_after_secret_read` rule.
4. An audit trail (OTLP JSON) written to a directory the agent can't reach,
   independent of OpenClaw's own logs.

## Open question this PoC needs to answer

OpenClaw's `exec` tool shells out via the OS login shell (`sh -lc`) directly;
there's no documented config to redirect it through Box's own shell
interpreter. That means Box's seatbelt profile covers the gateway process's
own direct file/network access, but it's unverified whether every subprocess
OpenClaw's `exec` tool spawns (git, npm, node, python3...) gets individual
Dogwood decisions, or just inherits the gateway process's seatbelt grants
wholesale. Confirm this against a running box before claiming the two layers
compose -- open an issue upstream if the shell layer needs explicit wiring.

## Files

- `box.toml` -- what Box runs and its direct OS-enforced grants.
- `policy.dw` -- Dogwood policy: Bedrock/Discord egress, workspace file
  access, the temporal post-secret-read egress block, and a sample shell
  denial mirroring OpenClaw's own `git config` restriction.

## Running it (macOS Apple Silicon only)

```sh
curl -fsSL https://raw.githubusercontent.com/strands-agents/box/main/download.sh | sh
sed -i '' "s|<HOME>|$HOME|g; s|<NODE_MODULES>|$(npm root -g)|g" box.toml
export AWS_BEARER_TOKEN_BEDROCK="..."   # short-term key, see Box's getting-started guide
export <GATEWAY_TOKEN>="..."            # pick a real token, don't commit one
./box-core/box run --config box.toml
```

Not yet run end-to-end -- authored against Box's documented `box.toml`/
`policy.dw` schema and the `getting-started.md`/`policy.md` examples in the
Box repo, on a Linux host with no Apple Silicon Mac available to execute it.
Needs a real run on macOS before calling this verified.
