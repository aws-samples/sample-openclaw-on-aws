# Limitations

- The validation creates one embedded Agent. It does not test concurrent users
  or large-scale scheduling.
- PostgreSQL runs as one in-cluster Pod without TLS, backups, or failover.
- The OCE API and worker each run one replica.
- Bedrock authentication uses a manually rotated short-term bearer token. Its
  effective lifetime cannot exceed the source AWS credentials' remaining
  lifetime, and rotation requires an OCE Secret update plus a new Agent
  revision.
- The sample does not validate a Bedrock interface VPC endpoint.
- The sample does not install private Envoy workspace routing or browser TLS.
- Dedicated Codex is outside scope because the reviewed runtime can require a
  host-installed Localhost seccomp profile.
- EBS Availability Zone affinity constrains where a replacement Pod can run.
- The default Auto Mode NodePools remain available. OCE workloads use explicit
  selectors for the custom control and Agent NodePools.
- The sample does not automate the authenticated Agent chat, gateway Pod
  replacement, or NodeClaim replacement checks. These remain deliberate,
  operator-run validation steps.
- The validation images inherit critical Debian package findings with no fixed
  package published by the distribution at the time of testing. See
  [Security considerations](SECURITY.md).
