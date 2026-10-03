# OpenClaw Enterprise on Amazon EKS Auto Mode

This experimental sample runs the OpenClaw Enterprise (OCE) control plane and
embedded OpenClaw Agents on Amazon EKS Auto Mode. Amazon Bedrock supplies the
model through its OpenAI-compatible Responses API.

The sample was validated with Kubernetes 1.35, ARM64 Auto Mode nodes, encrypted
Amazon EBS volumes, an embedded Agent model turn, and recovery after an Auto
Mode NodeClaim replacement.

## Scope

Use this sample to evaluate:

- Separate Auto Mode NodePools for the OCE control plane and Agent runtimes.
- One OCE Namespace and Agent per tenant.
- Exact-resource OCE IAM bindings between each Agent identity and model Secret.
- Encrypted gp3 EBS storage with `WaitForFirstConsumer`.
- OCE NetworkPolicies plus the node-local DNS rule required by Auto Mode.
- Pod and node replacement with persistent gateway state.

The sample is not a production architecture or a scale test. It uses a
single-Pod PostgreSQL database, one OCE API replica, one worker replica,
manually rotated Bedrock bearer tokens, and local port-forwarding. Replace these
parts with reviewed high-availability, private-routing, credential-rotation,
backup, and observability designs.

Read [Security considerations](docs/SECURITY.md) before deployment. The
validation previously found critical Debian package findings for which the
upstream distribution did not publish a fixed package at validation time. The
image publisher now blocks any build with Critical or High findings. Use the
sample only in an isolated, time-boxed evaluation account with no customer or
production data.

Dedicated Codex runtimes are outside this sample. The reviewed OCE release can
require a host-installed Localhost seccomp profile for that mode, while Auto
Mode nodes are immutable.

## Quick start

1. Follow [Operations](docs/OPERATIONS.md) to build and publish images, deploy
   the cluster and OCE, and create the first Agent.
2. Follow [Tenant isolation](docs/TENANT_ISOLATION.md) to onboard a second
   tenant, verify HTTP 403 before its Agent-to-Secret binding is granted, and
   configure separate user-to-Namespace and user-to-Agent AccessBindings.
3. Review [Capacity, persistence, and cost controls](docs/CAPACITY_AND_COST.md)
   before changing NodePools, replicas, storage, quotas, or budgets.
4. Run the validation commands in
   [Operations](docs/OPERATIONS.md#validate-the-deployment).

## Documentation

- [Operations](docs/OPERATIONS.md) - images, first deployment, validation, and cleanup.
- [Tenant isolation](docs/TENANT_ISOLATION.md) - tenancy choices, exact-resource bindings, and second-tenant onboarding.
- [Capacity, persistence, and cost controls](docs/CAPACITY_AND_COST.md) - Auto Mode behaviour, EBS Availability Zone constraints, quotas, and budgets.
- [Architecture](docs/ARCHITECTURE.md) - component and responsibility boundaries.
- [Security considerations](docs/SECURITY.md) - current security controls and gaps.
- [Limitations](docs/LIMITATIONS.md) - unsupported and unvalidated behaviour.
