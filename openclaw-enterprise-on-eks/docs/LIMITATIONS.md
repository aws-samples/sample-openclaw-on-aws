# Limitations

- The first-deployment validation creates one embedded Agent. Second-tenant
  onboarding verifies the pre-binding Secret denial, but the sample does not
  load test concurrent users or large-scale scheduling.
- PostgreSQL runs as one in-cluster Pod without TLS. The operator-run
  `scripts/backup-postgres-dev.sh` helper creates a permission-restricted
  plaintext custom-format `pg_dump`, checks its archive catalog with
  `pg_restore --list`, and can upload it to an existing S3 URI with an
  expected-owner check, SSE-KMS, no-overwrite condition, and post-upload size
  and encryption checks. It does not provide automatic failover, point-in-time
  recovery, or a validated full restore.
- The OCE API and worker each run one replica.
- The sample configures no HorizontalPodAutoscaler or PodDisruptionBudget.
  Auto Mode provisions and replaces nodes but does not scale application
  replicas.
- Bedrock authentication uses a manually rotated short-term bearer token. Its
  effective lifetime cannot exceed the source AWS credentials' remaining
  lifetime, and rotation requires an OCE Secret update plus a new Agent
  revision.
- The sample does not validate a Bedrock interface VPC endpoint.
- The sample does not install private Envoy workspace routing or browser TLS.
- Dedicated Codex is outside scope because the reviewed runtime can require a
  host-installed Localhost seccomp profile.
- EBS Availability Zone affinity constrains where a replacement Pod can run.
- The PostgreSQL and gateway EBS volumes are single-AZ and are not backups.
- The default Auto Mode NodePools remain available. OCE workloads use explicit
  selectors for the custom control and Agent NodePools.
- The sample does not automate the authenticated Agent chat, gateway Pod
  replacement, or NodeClaim replacement checks. The documented commands remain
  deliberate, operator-run validation steps.
- The AWS Budget helper creates delayed account-wide notifications. It does not
  enforce a hard spend stop or attribute Bedrock cost to an OCE Namespace.
- The validation previously found critical Debian package findings with no
  fixed package published by the distribution at the time of testing. The
  image publisher blocks a current build if any Critical or High findings
  remain. See [Security considerations](SECURITY.md).
