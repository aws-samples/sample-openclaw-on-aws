# OpenClaw Enterprise on Amazon EKS Auto Mode

This experimental reference deployment runs the OpenClaw Enterprise (OCE)
control plane and one embedded OpenClaw Agent on Amazon EKS Auto Mode. Amazon
Bedrock supplies GPT 5.6 Sol through its OpenAI-compatible Responses API.

The sample was validated with Kubernetes 1.35, ARM64 Auto Mode nodes, encrypted
Amazon EBS volumes, an embedded Agent model turn, and recovery after an Auto
Mode NodeClaim replacement.

## Scope

Use this sample to evaluate:

- Separate Auto Mode NodePools for the OCE control plane and Agent runtimes.
- One OCE Agent with a persistent workspace. Repeat the pattern only after
  validating your tenancy and capacity design.
- Encrypted gp3 EBS storage with `WaitForFirstConsumer`.
- OCE NetworkPolicies plus the node-local DNS rule required by Auto Mode.
- Amazon Bedrock with `api: openai-responses`.
- Pod and node replacement with persistent gateway state.

The sample is not a production architecture or a multi-user scale test. The
validation environment uses a
single in-cluster PostgreSQL instance, one OCE API Pod, one worker Pod, a
manually rotated Bedrock bearer token, and local port-forwarding. Replace these
parts with your reviewed database, high-availability, private routing,
credential rotation, backup, and observability design.

Read [Security considerations](docs/SECURITY.md) before deployment. The
validation images currently inherit critical Debian package findings for which
the upstream distribution does not publish a fixed package. Use the sample only
in an isolated, time-boxed evaluation account with no customer or production
data.

Dedicated Codex runtimes are outside this sample. The reviewed OCE release can
require a host-installed Localhost seccomp profile for that mode, while Auto
Mode nodes are immutable. Use embedded Agents here, or validate a managed node
group with the required profile.

## Prerequisites

Install:

- AWS CLI v2
- `eksctl`
- `kubectl`
- Helm 3
- Docker Buildx
- `jq`
- Node.js 24, pnpm, and Go for the pinned OCE source

Confirm access to `global.openai.gpt-5.6-sol` in the target AWS Region. The AWS
identity also needs permission to create EKS, EC2, IAM, ECR, and EBS resources.

## Configure the sample

1. Clone this repository and the OCE repository.

   ```bash
   git clone https://github.com/aws-samples/sample-openclaw-on-aws.git
   git clone https://github.com/openclaw/openclaw-enterprise.git
   cd sample-openclaw-on-aws/openclaw-enterprise-on-eks
   ```

2. Check out the reviewed OCE revision.

   ```bash
   git -C ../openclaw-enterprise checkout \
     8c02880a0d64ae7576896cec22b136ac8ac5587e
   ```

3. Create the local configuration.

   ```bash
   cp sample.env .env
   ```

   Set the AWS Region, unique cluster name, your current public IPv4 `/32`, OCE
   source directory, and immutable private ECR image digests. Do not put
   credentials in `.env`.

## Deploy the EKS foundation

1. Create EKS Auto Mode.

   ```bash
   ./scripts/create-cluster.sh
   ```

2. Enable network policy before installing workloads, then create private
   control-plane and Agent NodePools.

   ```bash
   ./scripts/configure-auto-mode.sh
   ```

The script discovers the Auto Mode node role, private subnets, and EKS cluster
security group. It writes rendered files only to `.generated/`.

## Build and publish OCE

Build the pinned OCE controller and runtime images for `linux/arm64`. Push them
to private ECR repositories, then put their immutable digest references in
`.env`.

The controller uses the root OCE `Dockerfile` target `runtime`. The Agent image
uses `deploy/runtime/Dockerfile`. The runtime build needs more than 8 GiB of
Docker memory; use CodeBuild or another builder with enough memory if your local
build exits with code 137.

Follow the OCE image and installation procedures pinned to the tested commit:

- [Build and publish production images](https://github.com/openclaw/openclaw-enterprise/blob/8c02880a0d64ae7576896cec22b136ac8ac5587e/docs/guides/deploy/production-installation.md#build-and-publish-production-images)
- [Deploy on Amazon EKS](https://github.com/openclaw/openclaw-enterprise/blob/8c02880a0d64ae7576896cec22b136ac8ac5587e/docs/guides/deploy/eks.md)

## Install OCE

Use the files in `oce/` as the Auto Mode overlays for the OCE production
installation guide. This sample uses the following tested differences:

1. Set the OCE namespace to `openclaw-system`.
2. Deploy a disposable PostgreSQL 16 instance on the `oce-control` NodePool for
   evaluation, with separate `occ_app` and `occ_migrator` roles, then install
   OCE.

   ```bash
   ./scripts/deploy-postgres-dev.sh
   ./scripts/install-oce.sh
   ```

3. Use `.generated/values.yaml` and `.generated/installation.yaml` with the OCE
   bootstrap and Helm commands.
4. Apply an egress rule for the Kubernetes service IP and the node-local DNS
   address derived from the cluster service CIDR. For the default
   `172.20.0.0/16` service CIDR, this address is `172.20.0.10/32`. Auto Mode
   uses the node-local DNS address, so OCE's default `kube-dns` Pod selector is
   not sufficient.
5. Retrieve the initial service key from the protected bootstrap PVC without
   printing it. Keep the local file mode at `0600`.

The templates select `oce-role=control` for OCE API, worker, and gateway Pods,
and `oce-role=agents` for embedded Agent workloads. The NodePools are untainted
because the reviewed OCE configuration supports `nodeSelector` but not
tolerations.

## Configure Amazon Bedrock

`oce/agent-configuration.json.tmpl` configures:

```json
{
  "baseUrl": "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
  "api": "openai-responses",
  "models": [
    {
      "id": "global.openai.gpt-5.6-sol",
      "name": "global.openai.gpt-5.6-sol"
    }
  ]
}
```

Create a short-term Bedrock API key from a narrowly scoped AWS identity. Store
it through an OCE Secret and bind only the Agent service identity that needs it.
Do not put the key in configuration JSON, Helm values, shell history, or Git.

Retrieve the bootstrap service key, create an embedded Agent, bind its Bedrock
credential, provision its runtime credentials, and deploy an immutable revision:

```bash
./scripts/retrieve-service-key.sh
BEDROCK_API_KEY_FILE=/secure/bedrock-api-key ./scripts/create-agent.sh
```

The script writes only resource IDs to `.generated/agent-state.json`. It does
not print or copy the Bedrock key.

After OCE creates the generated tenant and gateway namespaces, apply the Auto
Mode DNS rule to both:

```bash
./scripts/apply-tenant-dns.sh <tenant-namespace> <gateway-namespace>
```

## Validate

Run the control-plane smoke checks:

```bash
./scripts/validate.sh
BEDROCK_API_KEY_FILE=/secure/bedrock-api-key \
  ./scripts/test-bedrock-responses.sh
```

Then verify:

1. A request without gateway credentials returns HTTP 401.
2. A direct Bedrock `/openai/v1/responses` call returns a fresh nonce.
3. An authenticated OCE Agent chat returns another fresh nonce.
4. The Agent Pod uses a `secretKeyRef`, not a literal token.
5. Deleting the gateway Pod creates a new Pod with the same PVC and EBS volume.
6. Deleting its NodeClaim moves the workload to a new Auto Mode node with the
   same PVC, then the Agent completes another model turn.

NodeClaim deletion is disruptive. Confirm the selected node has no unrelated
non-DaemonSet workloads before running that test.

`validate.sh` deliberately stops at non-destructive cluster and control-plane
checks. The authenticated Agent turn and the Pod and NodeClaim replacement
checks require a temporary port-forward and explicit operator confirmation.

## Clean up

Delete the cluster when you finish:

```bash
./scripts/cleanup.sh
```

The script reports retained EBS volumes for manual review. Also remove ECR
images, Secrets Manager secrets, snapshots, and local files under `.generated/`
that you no longer need.
