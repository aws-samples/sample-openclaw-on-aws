# 🦞 OpenClaw on AWS

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-yellow.svg?style=flat-square)](LICENSE)
[![AWS](https://img.shields.io/badge/AWS-Bedrock_|_AgentCore-orange?style=flat-square&logo=amazon-aws)](https://aws.amazon.com/bedrock/agentcore/)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-AI_Assistant-blue?style=flat-square)](https://openclaw.ai)
[![ClawHub AWS Skills](https://img.shields.io/badge/ClawHub-AWS_Skills-purple?style=flat-square)](https://clawhub.ai/skills?q=aws)

Deploy [OpenClaw](https://openclaw.ai) on AWS — an AI agent that codes, researches, analyzes, creates, and automates. Powered by Amazon Bedrock with access to frontier models from Anthropic, OpenAI, and leading open-weight models. Multiple deployment options from serverless to persistent.

## Deployment Options

| Approach | Description |
|----------|----------|
| **[AgentCore](./agentcore-runtime-instances/)** | Personal assistant, persistent state, auto-stop when idle |
| **[EC2](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock)** | Full control, always-on |
| **[EKS](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock/tree/main/eks)** | Kubernetes-native deployment for existing clusters |


See also: [Community Samples](#community-samples) for multi-tenant and experimental deployments.


## Getting Started

Choose a deployment approach from the [table above](#deployment-options) based on your needs, then follow its README.

### → [AgentCore](./agentcore-runtime-instances/) (New)

The fastest path to a running assistant. AWS-managed EC2 that auto-stops when idle ($0 cost) and resumes instantly on next message. Powered by [Runtime Instances](https://aws.amazon.com/blogs/aws/runtime-instances-persistent-compute-for-production-ai-agents-on-amazon-bedrock-agentcore/) — persistent compute for production AI agents on Amazon Bedrock AgentCore.

```bash
cd agentcore-runtime-instances
./scripts/deploy.sh
```

## What is OpenClaw?

[OpenClaw](https://github.com/openclaw/openclaw) is an open-source AI assistant that connects to your messaging apps (WhatsApp, Telegram, Discord, Slack), manages email, browses the web, runs commands, and schedules tasks — running on your own infrastructure.

## Extend with ClawHub Skills

[ClawHub](https://clawhub.ai/skills?q=aws) is the public skill and plugin registry for OpenClaw — 3,200+ skills you can install with a single command.

**Recommended skills for AWS deployments:**

Install the [Agent Toolkit for AWS](https://github.com/aws/agent-toolkit-for-aws) — the official AWS skill collection covering CloudFormation, Lambda, DynamoDB, S3, CloudWatch, and 30+ services:

```bash
# Install the official AWS skill collection
npx skills add aws/agent-toolkit-for-aws/skills/core-skills

# Or browse AWS skills on ClawHub
openclaw skills search "aws"
```

| Skill | Source | Purpose |
|-------|--------|---------|
| **agent-toolkit** | [aws/agent-toolkit-for-aws](https://github.com/aws/agent-toolkit-for-aws) | S3, Lambda, CloudWatch, DynamoDB |
| **amazon-bedrock** | Agent Toolkit specialized | Model inference — access to Anthropic, OpenAI, and open-weight models |
| **s3-share** | [Pre-bundled](./agentcore-runtime-instances/container/.openclaw/workspace/skills/s3-files/) | Upload files to S3, generate shareable download links |
| **aws-agent-payments** | [ClawHub](https://clawhub.ai/skills?q=aws) | x402 crypto payment flows |

> **🔒 Skill Security** — Every ClawHub skill is scanned by [ClawScan](https://openclaw.ai/blog/openclaw-nvidia-skill-security) for hidden instructions, risky code paths, and agentic threats (powered by NVIDIA SkillSpector), and ships with a verified Skill Card documenting provenance and scan results.

## Enterprise & Team Deployments

For deploying OpenClaw across teams (10-1000+ users), these reference architectures and community guides cover multi-tenant patterns:

| Pattern | Approach | Best For |
|---------|----------|----------|
| **[AgentCore — Multi-Tenant](./agentcore-runtime-instances/docs/MULTI_TENANCY_CONSIDERATIONS.md)** | Per-user sessions, shared capacity provider, per-user S3 prefixes | 10-100 users, variable usage, auto-stop on idle |
| **[EC2 Enterprise — Tenant Router](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock/tree/main/enterprise)** | H2 proxy, tenant router, per-tenant Firecracker microVMs, Bedrock Guardrails | 100-1000+ users, consistent load, compliance |

## Community Samples

| Project | Description |
|---------|----------|
| **[LowKey](https://github.com/inceptionstack/lowkey)** | Give your 🦞 an AWS account — vibe to production |
| **[Multi-tenant EKS Platform](https://github.com/aws-samples/sample-openclaw-multi-tenant-platform)** | EKS + CDK + KEDA scale-to-zero + ArgoCD + Pod Identity |
| **[Multi-tenancy on EKS + Kata](https://github.com/aws-samples/sample-multi-tenancy-openclaw-on-eks)** | Go orchestrator + Kata Containers VM isolation + Karpenter + Redis routing |
| **[Firecracker microVMs](https://github.com/aws-samples/sample-multi-tenant-openclaw-on-firecracker)** | EC2 Firecracker KVM + multi-AZ failover + web console + Prometheus |
| **[EKS + Graviton Lab](https://github.com/aws-samples/graviton-developer-workshop/tree/main/openclaw-eks-lab-guide)** | EKS on Graviton with gVisor sandboxing (workshop lab) |
| **[AgentCore microVMs](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore)** | Multi-tenant serverless — per-user Firecracker containers (Experimental) |

## Contributing

PRs welcome for new deployment patterns. Add a subfolder with its own README following the existing structure. See [agentcore-runtime-instances/](./agentcore-runtime-instances/) as a reference.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for reporting security issues.

This project follows the [AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/). AWS manages security **of** the cloud (infrastructure, hardware, networking, managed services). You are responsible for security **in** the cloud, including:

- **IAM policies** — least-privilege access for deployed resources
- **Network configuration** — security groups, NACLs, VPC design
- **Data encryption** — at rest and in transit
- **Application secrets** — bot tokens, API keys, credentials
- **Monitoring & incident response** — CloudWatch, CloudTrail, access logs

This sample is provided for demonstration purposes and may not implement all security best practices required for production workloads. Review and harden configurations before deploying to production.

## License

MIT-0 — See [LICENSE](./LICENSE)
