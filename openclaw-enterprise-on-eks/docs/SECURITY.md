# Security considerations

This sample is an experimental validation path, not a production baseline.
Deploy it only in an isolated, time-boxed AWS account or sandbox. Do not use
customer, confidential, or production data.

## Container findings

Amazon ECR enhanced scanning reported critical findings in Debian packages
inherited by the pinned controller and runtime images. At the time of the
validation, the distribution did not publish a fixed package version for these
findings:

- `CVE-2026-57433`, `CVE-2026-12087`, and `CVE-2026-13221` in Perl
- `CVE-2026-19445` in Python 3.11 in the runtime image

The same unresolved package findings were present in the current official
Node.js 24 Bookworm bases checked during the spike.

`build-and-publish-images.sh` waits for both ECR scans and fails closed unless
each scan completes with zero Critical and zero High findings. It writes
`.generated/images.env` only after both images pass and removes an older copy
before starting a build. The blocked images remain in ECR because scanning
occurs after the push.

Rebuild and rescan before each deployment. Do not waive a finding merely
because this document lists it. Remove the deployment when evaluation finishes.

## Evaluation controls

- Restrict the EKS public API endpoint to the operator's current IPv4 `/32`.
- Keep workload nodes in private subnets.
- Use immutable image digests and scan both images in Amazon ECR.
- Use a short-lived, narrowly scoped Bedrock API key.
- Keep service keys, model keys, rendered Secrets, and generated configuration
  out of Git.
- Apply the supplied NetworkPolicies before creating an Agent.
- Do not expose the OCC API or Agent gateway publicly.
- Confirm that the Agent Pod references the model credential through
  `secretKeyRef` and contains no literal token.
- Delete the cluster, retained EBS volumes, ECR images, and local generated
  secrets when testing finishes.

OCE manages tenant and gateway namespaces dynamically. The reviewed release
therefore grants its worker cluster-wide permission to get, list, create,
patch, and delete namespaces. This is not `cluster-admin`, but namespace
deletion has a large blast radius. Run OCE in a dedicated cluster or apply
additional admission controls after confirming they do not break reconciliation.

## Backup handling

`scripts/backup-postgres-dev.sh` stores permission-restricted plaintext
custom-format `pg_dump` archives in a mode-`0700` local directory and sets each
completed archive to mode `0600`. It uses unique `mktemp` filenames and selects
the `postgres` container explicitly for `pg_dump` and `pg_restore`. Treat these
files as sensitive data.

If `POSTGRES_BACKUP_S3_URI` names an existing S3 URI ending in `/`, the helper
requires the expected bucket-owner account ID and an AWS KMS key. It checks the
expected owner, refuses to overwrite an existing object, uploads with SSE-KMS,
and checks the stored size and encryption. It does not create the bucket or
replace bucket access controls, retention, versioning, or lifecycle policies.

The helper runs `pg_restore --list` to check that `pg_restore` can read the
archive catalog. This check is not a validated full restore. The helper does
not provide automatic failover or point-in-time recovery. Restrict `pods/exec`
access because it permits operators to read the database, and test restores in
an isolated environment.

For production, replace the in-cluster database and manual credential workflow
with reviewed high-availability, backup, private-routing, secret-rotation,
monitoring, and incident-response designs.
