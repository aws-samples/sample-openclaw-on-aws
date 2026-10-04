# Tenant isolation

Treat one OCE Namespace as one tenant. Do not place mutually untrusted tenants
in the same OCE Namespace.

## Implemented shared-cluster isolation

For each OCE Namespace, the Kubernetes Compute Driver creates:

- A data-plane Kubernetes namespace for embedded Agent workloads.
- A separate managed gateway namespace.
- Restricted Pod Security labels.
- Default-deny NetworkPolicies plus explicit DNS and workload paths.
- A ResourceQuota and LimitRange from `oce/installation.yaml.tmpl`.
- One stable service principal per Agent.
- One exact-resource OCE IAM binding from that Agent identity to its model
  Secret.

Kubernetes RoleBindings let the OCE API and worker reconcile resources in the
generated namespaces. They do not grant OCE application access. OCE IAM grants
application access, and the model binding includes both `resourceKind:
"secret"` and the exact Secret ID.

Keep the two OCE AccessBinding paths separate:

| Access path | Subject | Exact resources | Created by |
|---|---|---|---|
| Agent runtime to model credential | Agent service principal, stored as an `identity` subject | One model Secret | `create-agent.sh` and `onboard-tenant.sh` |
| User to tenant resources | User account principal, stored as an `identity` subject | One Namespace and one Agent | The user-access procedure below |

The tenant onboarding scripts create only the first path. They do not create
accounts or user AccessBindings.

The OCE worker can create and delete managed namespaces cluster-wide. Run this
pattern in a dedicated cluster, or add reviewed admission controls that preserve
the required reconciliation operations.

## Onboard a second tenant

1. Create a separate short-term Bedrock API key for the tenant and store it in
   a mode-`0600` file.

   ```bash
   chmod 600 /secure/team-two-bedrock-api-key
   ```

2. Create the second OCE Namespace and Agent.

   ```bash
   TENANT_NAME=team-two \
   AGENT_NAME=team-two-agent \
   BEDROCK_API_KEY_FILE=/secure/team-two-bedrock-api-key \
     ./scripts/onboard-tenant.sh
   ```

The script:

1. Creates the exact OCE Namespace named by `TENANT_NAME` if it does not exist.
2. Waits for its generated data-plane and gateway Kubernetes namespaces.
3. Applies only the required RoleBindings and Auto Mode DNS policies.
4. Creates a Namespace-owned Configuration, model Secret, and embedded Agent.
5. Provisions the Agent's runtime credentials.
6. Attempts deployment before the model Secret grant and requires HTTP 403
   `FORBIDDEN`, including the expected Agent service-principal and Secret IDs.
7. Creates an `operate` Role and creates an AccessBinding from that Agent
   service principal to the exact Secret.
8. Reads the stored binding back and verifies every scope field.
9. Deploys the immutable Agent revision.

The script writes IDs to `.generated/tenant-team-two.json`. It aborts if the
pre-binding deployment succeeds or returns a different failure. Keep this
negative check in onboarding; a successful deployment alone cannot prove that
the grant is narrowly scoped.

For a non-default tenant, the script refuses to reuse an existing Namespace by
default. Reuse one only after verifying its identity, then set
`ALLOW_EXISTING_TENANT=1` and set `EXPECTED_NAMESPACE_ID` to that exact
Namespace ID. A missing or mismatched ID aborts onboarding. The `default`
Namespace remains the only implicit reuse path.

The state file must be `.generated/agent-state.json` or
`.generated/tenant-$TENANT_NAME.json`. The script rejects custom filenames and
paths outside `.generated/`.

This HTTP 403 comes from the failed `occ agent deploy` command. It proves that
the Agent service principal cannot use the model Secret before its exact
AccessBinding exists. It does not test a user's cross-tenant access, create a
tenant administrator credential, or prove isolation from the Installation-wide
bootstrap administrator, which can manage every tenant.

Inspect the exact binding that the script read back from OCE:

```bash
jq .binding .generated/tenant-team-two.json
```

## Grant a user access to the tenant

Agent-to-Secret authorization and user-to-tenant authorization are separate.
After the tenant Agent is ready, create the user account and bind its account
principal only to that tenant's Namespace and Agent.

1. Sign in as an Installation administrator. Set `OCC_URL`, `OCC_ORIGIN`, and
   `OCC_SESSION_COOKIE_JAR` to the console endpoint, expected browser origin,
   and administrator cookie jar.
2. Set `OCC_BIN` to the reviewed OCE CLI if it is not already set.

   ```bash
   set -a
   source .env
   set +a
   OCC_BIN="${OCC_BIN:-$OCE_SOURCE_DIR/bin/occ}"
   ```

3. Create the account and capture its Principal ID.

   ```bash
   umask 077
   account_file="$(mktemp "${TMPDIR:-/tmp}/oce-account.XXXXXX.json")"
   trap 'rm -f -- "$account_file" user-role.json user-binding.json' EXIT
   user_password="$(openssl rand -base64 24)"
   jq -n \
     --arg email '<user@example.com>' \
     --arg password "$user_password" \
     '{email:$email,password:$password}' > "$account_file"

   principal_id="$(
     curl --fail-with-body --silent --show-error \
       --cookie "$OCC_SESSION_COOKIE_JAR" \
       -H "Origin: $OCC_ORIGIN" \
       -H 'Content-Type: application/json' \
       --data-binary "@$account_file" \
       "$OCC_URL/api/auth/accounts" |
       jq -er '.data.principalId'
   )"
   ```

4. Read the tenant Namespace and Agent IDs from its state file, then create a
   least-privilege Role.

   ```bash
   state=.generated/tenant-team-two.json
   namespace_id="$(jq -er .namespaceId "$state")"
   agent_id="$(jq -er .agentId "$state")"

   jq -n '{
     name:"Use assigned tenant Agent",
     permissions:[
       {action:"read",resourceKind:"namespace"},
       {action:"read",resourceKind:"agent"},
       {action:"administer",resourceKind:"agent"}
     ]
   }' > user-role.json
   role_id="$(
     "$OCC_BIN" iam role create --file user-role.json --output json |
       jq -er '.id // .data.id'
   )"
   ```

5. Create separate AccessBindings from the user account principal to the exact
   Namespace and Agent.

   ```bash
   for target in "namespace:$namespace_id" "agent:$agent_id"; do
     jq -n \
       --arg principalId "$principal_id" \
       --arg roleId "$role_id" \
       --arg resourceKind "${target%%:*}" \
       --arg resourceId "${target#*:}" \
       '{
         subjectKind:"identity",
         subjectId:$principalId,
         roleId:$roleId,
         resourceKind:$resourceKind,
         resourceId:$resourceId
       }' > user-binding.json
     "$OCC_BIN" iam access-binding create \
       --file user-binding.json --output json >/dev/null
   done
   ```

   These user AccessBindings do not grant the Agent service principal access to
   its model Secret. The tenant onboarding script creates that binding
   separately.

6. Deliver the password in `user_password` through an approved secure channel,
   then run `unset user_password`. The exit trap removes the temporary local
   request, Role, and binding files.
7. Sign in as the user. Verify that the assigned Agent opens and that the other
   tenant's Agent returns HTTP `403`.
8. If the account is temporary, remove its AccessBindings and Role after
   validation, then delete the isolated evaluation environment to remove the
   password-only account. Deleting local request files, passwords, or cookies
   does not delete the OCE account.

For external sign-in, provision the OCE account before the user's first sign-in,
then apply the same exact-resource bindings. Follow the upstream
[external sign-in](https://github.com/openclaw/openclaw-enterprise/blob/main/docs/reference/authentication/external-sign-in.md)
and [IAM](https://github.com/openclaw/openclaw-enterprise/blob/main/docs/guides/topics/iam.md)
guides for the OCE release you deploy.

## Choose a stronger isolation boundary

Use the shared installation only when the tenants accept a shared Kubernetes
and OCE administrative boundary. The Installation bootstrap service key is an
administrator credential across all OCE Namespaces.

| Boundary | Separation | Trade-off |
|---|---|---|
| OCE Namespace per tenant | Namespaces, NetworkPolicies, quotas, Agent identities, and exact Secrets | Shares the cluster, OCE control plane, database, node IAM role, and platform administrators |
| OCE installation per tenant | Separate OCE control plane, database schema or database, bootstrap identity, and Kubernetes namespaces | More operational overhead; still shares the EKS cluster and cluster administrators |
| EKS cluster and AWS account per tenant | Separate AWS, Kubernetes, IAM, quota, logging, and billing boundaries | Highest cost and operational overhead; strongest boundary in this sample family |

Use separate model identities per tenant for revocation and audit. Reusing one
Bedrock key across tenants preserves OCE Secret isolation but does not provide
provider-side tenant separation or cost attribution.

The upstream OCE API also supports adopting an operator-prepared Kubernetes
namespace. This sample does not automate that path because admission labels,
ownership markers, foreign NetworkPolicies, and exact RoleBindings require a
separate review.
