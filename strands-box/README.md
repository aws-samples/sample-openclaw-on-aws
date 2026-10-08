# Strands Box: sandboxing a local OpenClaw gateway

[Strands Box](https://github.com/strands-agents/box) runs an agent process
inside an OS-enforced sandbox and routes everything else — shell commands,
Python, outbound HTTP, local MCP calls — through a separate trusted process
that checks a Dogwood policy (default-deny) before allowing it. This directory
configures Box to run an OpenClaw gateway under that model.

## Scope and limitation (read first)

Box is Preview software, macOS Apple Silicon only, no Linux support as of
2026-10. This repo's production deployment target
(`agentcore-runtime-instances`) runs Linux/arm64 EC2 under AgentCore Instances
compute — Box cannot run there today. This secures a **local OpenClaw gateway
on a dev Mac**. Revisit for the production container once Box ships Linux
support; the same policy model would apply directly to it at that point.

## What Box adds beyond OpenClaw's own hardening

OpenClaw already has in-process exec hardening (`tools.exec.security`,
`argPattern`, `strictInlineEval` — see `agentcore-runtime-instances/README.md`
Security section). Box adds a layer **outside** that process:

1. OS-enforced (Seatbelt) filesystem/network boundaries that don't depend on
   OpenClaw's own code being bug-free.
2. Credential injection at the network layer — the gateway process never
   holds the raw Bedrock/Discord secret.
3. Temporal/semantic Dogwood policy that can see event history across shell,
   Python, and network calls — e.g. deny egress for an hour after a secrets
   file is read. OpenClaw's own argv filtering can't express this; see
   `policy.dw`'s `no_egress_after_secret_read` rule.
4. An audit trail (OTLP JSON) written to a directory the agent can't reach,
   independent of OpenClaw's own logs.

## Open question to resolve before publishing

OpenClaw's `exec` tool shells out via the OS login shell (`sh -lc`) directly;
there's no documented config to redirect it through Box's own shell
interpreter. That means Box's seatbelt profile covers the gateway process's
own direct file/network access, but it's unverified whether every subprocess
OpenClaw's `exec` tool spawns (git, npm, node, python3...) gets individual
Dogwood decisions, or just inherits the gateway process's seatbelt grants
wholesale. Confirm this against a running box before claiming the two layers
compose — open an issue upstream if the shell layer needs explicit wiring.

## Files

- `box.toml` — what Box runs and its direct OS-enforced grants.
- `policy.dw` — Dogwood policy: Bedrock/Discord egress, workspace file
  access, the temporal post-secret-read egress block, and a sample shell
  denial mirroring OpenClaw's own `git config` restriction.

## Getting started

You need a Mac with Apple silicon, macOS 15+, Node.js (whatever OpenClaw
itself requires), OpenClaw installed globally (`npm install -g openclaw`),
and access to a Bedrock model.

**1. Download Box.**

```sh
curl -fsSL https://raw.githubusercontent.com/strands-agents/box/main/download.sh | sh
./box-core/box --version
```

**2. Create the directories this config expects.**

```sh
mkdir -p ~/strands-box/{state,state-openclaw,home,workspace}
```

**3. Fill in the placeholders in `box.toml`.**

```sh
sed -i '' "s|<HOME>|$HOME|g; s|<NODE_MODULES>|$(npm root -g)|g" box.toml
```

Set a real gateway token (don't commit one) and a short-term Bedrock API key —
see Box's own [getting-started guide](https://github.com/strands-agents/box/blob/main/docs/user/getting-started.md#step-5-get-a-bedrock-api-key)
for how to generate the key:

```sh
export OPENCLAW_GATEWAY_TOKEN="$(openssl rand -hex 32)"
export AWS_BEARER_TOKEN_BEDROCK="..."
```

**4. Run the box.**

```sh
./box-core/box run --config box.toml
```

Box prints what the gateway can touch at startup, then starts it. Connect to
it the same way you'd connect to any OpenClaw gateway bound to loopback (CLI,
Control UI, or a paired channel), using the token from step 3.

**5. Try to break it, then read the decision log.**

Have the agent read a file outside `workspace/`, or make a request to a host
that isn't Bedrock or Discord. Both should be refused. Then read what Box
recorded:

```sh
jq -r '.resourceLogs[]?.scopeLogs[].logRecords[]
  | (.attributes | map({(.key): .value}) | add) as $a
  | select($a["strands.box.policy.verdict"])
  | [$a["strands.box.policy.verdict"].stringValue, $a["strands.box.policy.action"].stringValue,
     ($a["file.path"].stringValue // $a["server.address"].stringValue
      // ($a["process.command_args"].arrayValue.values | map(.stringValue) | join(" ")))]
  | @tsv' state/private/telemetry/records.jsonl
```

**6. Verify the open question above** before relying on this: confirm whether
a `git`/`npm`/`node` command run through OpenClaw's `exec` tool shows up as
its own `shell:spawn`/`fs:*` decision in that log, or whether it's invisible
to Box (inheriting the gateway's own seatbelt grants). If invisible, the
temporal `no_egress_after_secret_read` rule and the shell denial rule in
`policy.dw` aren't actually covering `exec`-tool subprocesses yet — only the
gateway process's own direct activity.

**7. Iterate.** Tighten `box.toml`'s direct grants and `policy.dw`'s rules
down to exactly what your OpenClaw setup needs, the same way you would for
any other box — see Box's
[policy guide](https://github.com/strands-agents/box/blob/main/docs/user/policy.md)
for the full rule syntax.
