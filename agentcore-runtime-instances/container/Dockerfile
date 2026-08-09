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
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    unzip \
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

# Create workspace directory structure (used as defaults on first boot)
RUN mkdir -p /home/agent/.openclaw/workspace

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

# Run as root — AgentCore manages the security boundary at the instance/session level.
# The EBS volume mounted by AgentCore is owned by root.
# checkov:skip=CKV_DOCKER_3: AgentCore requires root for EBS volume access
ENTRYPOINT ["./start.sh"]
