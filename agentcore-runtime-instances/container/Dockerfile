# syntax=docker/dockerfile:1
# OpenClaw on AgentCore Runtime Instances — Container Image
#
# ARM64 image containing:
# - Node.js 24 (OpenClaw runtime)
# - OpenClaw (npm global install)
# - Python 3.11 (Bedrock AgentCore SDK wrapper)
# - AWS CLI v2 (for S3 backup sync)
# - start.sh entrypoint
#
# The container exposes :8080 for AgentCore protocol (health + invocations)
# and runs OpenClaw gateway internally on :18789.
#
# Persistence model: EBS root volume (live workspace) + S3 bucket (background backup).
# OPENCLAW_HOME is on the EBS root volume — no external mount required.

FROM public.ecr.aws/docker/library/python:3.11-slim

# System dependencies
# gosu is used by start.sh to drop root privileges before running the
# gateway/agent process (see privilege-drop note near ENTRYPOINT below).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    unzip \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 (needed for S3 backup sync)
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscli.zip && \
    unzip -q /tmp/awscli.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscli.zip

# Install Node.js 24 (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install OpenClaw globally
RUN npm install -g openclaw

# Python dependencies for AgentCore SDK wrapper
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Application code
COPY main.py .
COPY patch_channels.py .
COPY start.sh .
RUN chmod +x start.sh

# Non-root runtime user. The container still starts as root (needed for the
# EBS-backed OPENCLAW_HOME mount prep AgentCore performs before start.sh's
# main work begins), but start.sh drops privileges to this user via `gosu`
# before launching main.py/the OpenClaw gateway -- see ENTRYPOINT note below.
RUN groupadd --gid 1000 agent && \
    useradd --uid 1000 --gid agent --home-dir /home/agent --create-home --shell /usr/sbin/nologin agent

# Create the workspace tree and hand the whole home directory to `agent`.
# This must be done at build time, not in the entrypoint: AgentCore Runtime
# Instances starts the container on an id-mapped overlay mount, where the
# container's uid 0 has no DAC override over files owned by another uid. A
# root-time `mkdir`/`chown` of /home/agent therefore fails with EPERM and
# kills the entrypoint. Mode 755 on /home/agent keeps it traversable.
RUN mkdir -p /home/agent/.openclaw/workspace && \
    chown -R agent:agent /home/agent && \
    chmod 755 /home/agent

# Default OpenClaw configuration (used if workspace is empty on first boot)
COPY .openclaw/ /app/.openclaw-defaults/

EXPOSE 8080

# Health check — AgentCore protocol ping endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8080/ping || exit 1

# Environment — workspace on EBS root volume (persists across stop/resume)
ENV OPENCLAW_HOME=/home/agent/.openclaw
ENV AGENTCORE_PORT=8080
ENV NODE_ENV=production
# CHANNEL_SECRETS_ARN is intentionally NOT set here. Default is webhook-only
# mode (Channel Router handles Telegram/Discord/Slack externally). Set this
# env var explicitly at the capacity-provider/runtime level only if you want
# the container to poll channels directly instead (legacy mode, incompatible
# with the Channel Router — see docs/CONFIGURATION.md).

# Run as the unprivileged `agent` user for the container's entire lifetime.
# Nothing here needs root: OPENCLAW_HOME is baked in owned by `agent` above,
# and the AgentCore Instances host mounts the session's persistent storage
# with ownership the container user can already write. Starting as root and
# dropping later via `gosu` does not work on this compute type anyway --
# the id-mapped overlay mount denies the container's uid 0 any override over
# `agent`-owned paths, so the root-time prep step fails with EPERM.
# Every exec-tool command the agent runs therefore executes as `agent`.
USER agent
# Absolute path: the AgentCore Runtime Instances host wraps the entrypoint in
# `/bin/sh -c "exec '<entrypoint>'"` without honoring the image WORKDIR, so a
# relative "./start.sh" exits immediately with "not found".
ENTRYPOINT ["/app/start.sh"]
