# Operations

Use these procedures for the first deployment and routine validation. Run every
command from `openclaw-enterprise-on-eks/` unless stated otherwise.

## Prerequisites

Install Git, the AWS CLI v2, `eksctl`, `kubectl`, Helm 3, Docker Buildx,
`jq`, Python 3, OpenSSL, Node.js 24, pnpm, and Go.

Use an AWS identity that can create EKS, EC2, IAM, ECR, EBS, and AWS Budgets
resources. Confirm model access for `global.openai.gpt-5.6-sol` in the target
Region.

The runtime image build needs more than 8 GiB of Docker memory. Use CodeBuild or
another ARM64-capable builder with enough memory if a local build exits with
code 137.

## Prepare the source and configuration

1. Clone this repository and OCE beside each other.

   ```bash
   git clone https://github.com/aws-samples/sample-openclaw-on-aws.git
   git clone https://github.com/openclaw/openclaw-enterprise.git
   cd sample-openclaw-on-aws/openclaw-enterprise-on-eks
   ```

2. Check out and build the reviewed OCE revision.

   ```bash
   git -C ../../openclaw-enterprise checkout \
     8c02880a0d64ae7576896cec22b136ac8ac5587e
   pnpm --dir ../../openclaw-enterprise install --frozen-lockfile
   pnpm --dir ../../openclaw-enterprise cli:build
   ```

3. Create the local configuration.

   ```bash
   cp sample.env .env
   ```

4. Set `AWS_REGION`, `CLUSTER_NAME`, `PUBLIC_ACCESS_CIDR`, and
   `OCE_SOURCE_DIR` in `.env`.

   Set `PUBLIC_ACCESS_CIDR` to the operator's current public IPv4 `/32`. Do not
   put credentials in `.env`.

   Keep `OCE_IMAGE_PLATFORM=linux/arm64`. The image build rejects other
   platforms. Change `ECR_CONTROLLER_REPOSITORY` and
   `ECR_RUNTIME_REPOSITORY` only when the default private ECR repository names
   do not fit the target account.

## Build and publish the images

1. Authenticate Docker and publish both reviewed images to private ECR.

   ```bash
   ./scripts/build-and-publish-images.sh
   ```

   The script requires a clean OCE checkout at `OCE_GIT_REF`, creates missing
   ECR repositories with immutable tags and scan-on-push, builds for
   `linux/arm64`, and pushes both images. It waits for both scans, requires a
   successful scan status with zero Critical and zero High findings, then writes
   digest references to `.generated/images.env`. A scan timeout, scan error,
   unexpected status, or matching finding exits non-zero before new references
   are written.

   The script removes any existing `.generated/images.env` at startup, so any
   later failure leaves no deployable reference file. Blocked images remain in
   ECR because the scan runs after the push.

   To replace the pinned Node.js base image, set `NODE_BASE_IMAGE` to another
   immutable `@sha256` reference. The script rejects mutable image references.

2. Copy `CONTROLLER_IMAGE` and `RUNTIME_IMAGE` from
   `.generated/images.env` into `.env`.

3. Inspect the pushed image details and scan status.

   ```bash
   set -a
   source .env
   source .generated/images.env
   set +a
   aws ecr describe-images \
     --region "$AWS_REGION" \
     --repository-name "${ECR_CONTROLLER_REPOSITORY:-openclaw-enterprise/controller}" \
     --image-ids "imageDigest=${CONTROLLER_IMAGE##*@}"
   aws ecr describe-images \
     --region "$AWS_REGION" \
     --repository-name "${ECR_RUNTIME_REPOSITORY:-openclaw-enterprise/runtime}" \
     --image-ids "imageDigest=${RUNTIME_IMAGE##*@}"
   ```

   Configure Amazon Inspector enhanced scanning separately if your evaluation
   requires continuous rescanning. Do not deploy images with unreviewed
   findings.

## Deploy the first tenant

1. Create the EKS Auto Mode cluster.

   ```bash
   ./scripts/create-cluster.sh
   ```

2. Enable network policy and create the private NodeClass, NodePools, and
   StorageClasses.

   ```bash
   ./scripts/configure-auto-mode.sh
   ```

3. Deploy the disposable PostgreSQL instance and OCE.

   ```bash
   ./scripts/deploy-postgres-dev.sh
   ./scripts/install-oce.sh
   ```

4. Retrieve the bootstrap service key without printing it.

   ```bash
   ./scripts/retrieve-service-key.sh
   ```

5. Put a short-term Bedrock API key in a private file and restrict its mode.

   ```bash
   chmod 600 /secure/bedrock-api-key
   ```

6. Create the first Agent in the OCE `default` Namespace.

   ```bash
   BEDROCK_API_KEY_FILE=/secure/bedrock-api-key \
     ./scripts/create-agent.sh
   ```

   The script records generated resource IDs in
   `.generated/agent-state.json`. It stores the model key in an OCE Secret,
   creates an AccessBinding from the Agent's service principal to that exact
   Secret, and never prints the key. It does not create a user account or grant
   a user access to the Namespace or Agent.

   `create-agent.sh` defaults `TENANT_NAME` to `default`, `AGENT_NAME` to
   `eks-auto-mode-bedrock`, and `TENANT_STATE_FILE` to
   `.generated/agent-state.json`. Use `onboard-tenant.sh` for another tenant;
   it sets the state-file path and enables the required pre-binding denial
   check.

   `TENANT_STATE_FILE` accepts only `.generated/agent-state.json` or
   `.generated/tenant-$TENANT_NAME.json`. The script rejects other filenames
   and paths outside `.generated/`.

When a Bedrock key expires, update the existing OCE Secret and deploy a new
Agent revision. Restarting a Pod does not refresh the credential snapshot in an
existing revision.

## Validate the deployment

1. Run the non-destructive cluster and control-plane checks.

   ```bash
   export KUBECONFIG="$PWD/.generated/kubeconfig"
   ./scripts/validate.sh
   ```

2. Verify the Bedrock Responses API directly with a fresh nonce.

   ```bash
   BEDROCK_API_KEY_FILE=/secure/bedrock-api-key \
     ./scripts/test-bedrock-responses.sh
   ```

3. Inspect the exact Agent-service-principal-to-Secret AccessBinding that the
   script read back from OCE.

   ```bash
   jq .binding .generated/agent-state.json
   ```

4. Verify gateway Pod replacement without deleting the PVC.

   ```bash
   state=.generated/agent-state.json
   tenant_namespace="$(jq -r .tenantNamespace "$state")"
   agent_id="$(jq -r .agentId "$state")"
   gateway_pod="$(
     kubectl -n "$tenant_namespace" get pods \
       -l "openclaw.dev/agent=$agent_id,openclaw.dev/workload-role=gateway" \
       -o json | jq -er '.items | select(length == 1) | .[0].metadata.name'
   )"
   pvc_before="$(
     kubectl -n "$tenant_namespace" get pod "$gateway_pod" \
       -o json | jq -er '.spec.volumes[] |
       select(.name == "openclaw-gateway-state") |
       .persistentVolumeClaim.claimName'
   )"
   kubectl -n "$tenant_namespace" delete pod "$gateway_pod"
   kubectl -n "$tenant_namespace" wait \
     --for=condition=Ready \
     -l "openclaw.dev/agent=$agent_id,openclaw.dev/workload-role=gateway" \
     pod --timeout=10m
   kubectl -n "$tenant_namespace" get pvc "$pvc_before"
   ```

5. Verify Auto Mode node replacement only in an isolated evaluation cluster.

   ```bash
   replacement_pod="$(
     kubectl -n "$tenant_namespace" get pods \
       -l "openclaw.dev/agent=$agent_id,openclaw.dev/workload-role=gateway" \
       -o json | jq -er '.items | select(length == 1) | .[0].metadata.name'
   )"
   node_name="$(
     kubectl -n "$tenant_namespace" get pod "$replacement_pod" \
       -o jsonpath='{.spec.nodeName}'
   )"
   kubectl get pods -A --field-selector "spec.nodeName=$node_name" -o wide
   nodeclaim="$(
     kubectl get node "$node_name" -o json |
       jq -er '.metadata.ownerReferences[] | select(.kind == "NodeClaim") | .name'
   )"
   kubectl delete nodeclaim "$nodeclaim"
   kubectl -n "$tenant_namespace" wait \
     --for=condition=Ready \
     -l "openclaw.dev/agent=$agent_id,openclaw.dev/workload-role=gateway" \
     pod --timeout=15m
   kubectl -n "$tenant_namespace" get pvc "$pvc_before"
   ```

   Before deletion, confirm the node has no unrelated non-DaemonSet workloads.
   Run another authenticated Agent model turn after either replacement. A Ready
   Pod and an attached PVC do not prove model access.

## Clean up

1. Delete the EKS cluster.

   ```bash
   ./scripts/cleanup.sh
   ```

2. Review and delete retained EBS volumes, ECR images, snapshots, budget
   resources, and local files under `.generated/`.

The cleanup script reports retained EBS volumes but does not delete them.
