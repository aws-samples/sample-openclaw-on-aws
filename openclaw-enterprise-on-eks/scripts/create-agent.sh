#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in kubectl jq python3; do require_command "$command"; done
export KUBECONFIG="$GENERATED_DIR/kubeconfig"
export OCC_URL="${OCC_URL:-http://127.0.0.1:3000}"
export OCC_SERVICE_KEY_FILE="$GENERATED_DIR/initial-admin-service-key.json"
export OCC_BIN="${OCC_BIN:-$OCE_SOURCE_DIR/bin/occ}"
bedrock_key_file="${BEDROCK_API_KEY_FILE:-}"
tenant_name="${TENANT_NAME:-default}"
agent_name="${AGENT_NAME:-eks-auto-mode-bedrock}"
verify_denied="${VERIFY_DENIED_BEFORE_BINDING:-0}"
state_file="${TENANT_STATE_FILE:-$GENERATED_DIR/agent-state.json}"
allow_existing_tenant="${ALLOW_EXISTING_TENANT:-0}"
expected_namespace_id="${EXPECTED_NAMESPACE_ID:-}"
[[ -x "$OCC_BIN" ]] || die "build the OCE CLI or set OCC_BIN"
[[ -s "$OCC_SERVICE_KEY_FILE" ]] || die "run retrieve-service-key.sh first"
[[ -n "$bedrock_key_file" ]] ||
  die "set BEDROCK_API_KEY_FILE to a mode-0600 key file"
require_mode_0600 BEDROCK_API_KEY_FILE "$bedrock_key_file"
require_digest CONTROLLER_IMAGE "$CONTROLLER_IMAGE"
[[ "$tenant_name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
  die "TENANT_NAME must use lowercase letters, numbers, and hyphens"
[[ "$agent_name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
  die "AGENT_NAME must use lowercase letters, numbers, and hyphens"
[[ "$verify_denied" = 0 || "$verify_denied" = 1 ]] ||
  die "VERIFY_DENIED_BEFORE_BINDING must be 0 or 1"
[[ "$allow_existing_tenant" = 0 || "$allow_existing_tenant" = 1 ]] ||
  die "ALLOW_EXISTING_TENANT must be 0 or 1"
[[ "$state_file" = "$GENERATED_DIR/agent-state.json" ||
  "$state_file" = "$GENERATED_DIR/tenant-$tenant_name.json" ]] ||
  die "TENANT_STATE_FILE must use the generated default or tenant state path"

proxy_pod=occ-auto-client
port_forward_log="$GENERATED_DIR/occ-port-forward.log"
model_secret_file="$GENERATED_DIR/model-secret.json"

# shellcheck disable=SC2153
cleanup() {
  local status=$?
  rm -f -- "$model_secret_file" || true
  if [[ -n "${port_forward_pid:-}" ]]; then
    kill "$port_forward_pid" 2>/dev/null || true
    wait "$port_forward_pid" 2>/dev/null || true
  fi
  kubectl -n "$OCE_NAMESPACE" delete pod "$proxy_pod" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl -n "$OCE_NAMESPACE" delete networkpolicy occ-auto-client-egress \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  return "$status"
}
trap cleanup EXIT

kubectl -n "$OCE_NAMESPACE" delete pod "$proxy_pod" \
  --ignore-not-found --wait=true >/dev/null
kubectl -n "$OCE_NAMESPACE" apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: occ-auto-client-egress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: occ-auto-client
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: openclaw-enterprise
              app.kubernetes.io/component: api
      ports:
        - protocol: TCP
          port: 8080
---
apiVersion: v1
kind: Pod
metadata:
  name: $proxy_pod
  labels:
    app.kubernetes.io/name: occ-auto-client
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  nodeSelector:
    oce-role: control
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: proxy
      image: $CONTROLLER_IMAGE
      command: [node, -e]
      args:
        - |
          const http = require("node:http");
          const host = process.env.OPENCLAW_ENTERPRISE_API_SERVICE_HOST;
          const port = Number(process.env.OPENCLAW_ENTERPRISE_API_SERVICE_PORT || 8080);
          http.createServer((request, response) => {
            const headers = {...request.headers, host: host + ":" + port};
            const upstream = http.request({
              host, port, path: request.url, method: request.method, headers
            }, (upstreamResponse) => {
              response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
              upstreamResponse.pipe(response);
            });
            upstream.on("error", () => {
              response.writeHead(502, {"content-type": "text/plain"});
              response.end("OCC API unavailable");
            });
            request.pipe(upstream);
          }).listen(8080, "127.0.0.1");
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      resources:
        requests: {cpu: 25m, memory: 32Mi}
        limits: {cpu: 100m, memory: 128Mi}
EOF

kubectl -n "$OCE_NAMESPACE" wait \
  --for=condition=Ready "pod/$proxy_pod" --timeout=5m
kubectl -n "$OCE_NAMESPACE" port-forward \
  "pod/$proxy_pod" 3000:8080 >"$port_forward_log" 2>&1 &
port_forward_pid=$!
for _ in $(seq 1 30); do
  if python3 - <<'PY'
import socket

try:
    with socket.create_connection(("127.0.0.1", 3000), timeout=1):
        pass
except OSError:
    raise SystemExit(1)
PY
  then
    break
  fi
  kill -0 "$port_forward_pid" 2>/dev/null ||
    die "OCC port-forward exited; see $port_forward_log"
  sleep 1
done
python3 - <<'PY' || die "OCC port-forward did not become ready; see the generated log"
import socket

with socket.create_connection(("127.0.0.1", 3000), timeout=1):
    pass
PY

namespace_json="$("$OCC_BIN" namespace list --output json)"
namespace_id="$(TENANT_NAME="$tenant_name" python3 -c '
import json,sys
import os
data=json.load(sys.stdin)
if isinstance(data, list):
    items=data
elif isinstance(data, dict):
    items=data.get("items", data.get("data", []))
    if isinstance(items, dict):
        items=items.get("items", [])
else:
    items=[]
matches=[item for item in items if item.get("name")==os.environ["TENANT_NAME"]]
if len(matches)>1:
    sys.exit("Expected at most one matching Namespace")
if matches:
    print(matches[0].get("id") or matches[0].get("data",{}).get("id"))
' <<<"$namespace_json")"
if [[ -z "$namespace_id" ]]; then
  namespace_response="$("$OCC_BIN" namespace create "$tenant_name" --output json)"
  namespace_id="$(jq -er '.id // .data.id' <<<"$namespace_response")"
elif [[ "$tenant_name" != default ]]; then
  [[ "$allow_existing_tenant" = 1 ]] ||
    die "Namespace $tenant_name already exists; refusing implicit tenant reuse"
  [[ -n "$expected_namespace_id" ]] ||
    die "set EXPECTED_NAMESPACE_ID when ALLOW_EXISTING_TENANT=1"
  [[ "$namespace_id" = "$expected_namespace_id" ]] ||
    die "existing Namespace ID does not match EXPECTED_NAMESPACE_ID"
fi
export OCC_NAMESPACE="$namespace_id"

tenant_namespace=
for _ in $(seq 1 120); do
  tenant_namespace="$(kubectl get namespaces \
    -l "openclaw.dev/namespace=$namespace_id" -o json |
    jq -r 'if (.items|length)==1 then .items[0].metadata.name else "" end')"
  [[ -n "$tenant_namespace" ]] && break
  sleep 5
done
[[ -n "$tenant_namespace" ]] || die "tenant namespace was not created"

rolebinding() {
  kubectl -n "$1" create rolebinding "$2" \
    --clusterrole="$3" --serviceaccount="$OCE_NAMESPACE:$4" \
    --dry-run=client -o yaml | kubectl apply -f -
}
rolebinding "$tenant_namespace" openclaw-enterprise-worker \
  "$OCE_RELEASE-openclaw-tenant-worker" openclaw-enterprise-worker
rolebinding "$tenant_namespace" openclaw-enterprise-api-observer \
  "$OCE_RELEASE-openclaw-gateway-observer" openclaw-enterprise-api
rolebinding "$tenant_namespace" openclaw-enterprise-api-secrets \
  "$OCE_RELEASE-openclaw-tenant-api" openclaw-enterprise-api

gateway_namespace=
for _ in $(seq 1 120); do
  gateway_namespace="$(kubectl get namespaces \
    -l "openclaw.dev/gateway-namespace=$namespace_id" -o json |
    jq -r 'if (.items|length)==1 then .items[0].metadata.name else "" end')"
  [[ -n "$gateway_namespace" ]] && break
  sleep 5
done
[[ -n "$gateway_namespace" ]] || die "gateway namespace was not created"

rolebinding "$gateway_namespace" openclaw-enterprise-worker \
  "$OCE_RELEASE-openclaw-tenant-worker" openclaw-enterprise-worker
rolebinding "$gateway_namespace" openclaw-enterprise-api-secrets \
  "$OCE_RELEASE-openclaw-tenant-api" openclaw-enterprise-api
rolebinding "$gateway_namespace" openclaw-enterprise-api-configuration \
  "$OCE_RELEASE-openclaw-tenant-configuration" openclaw-enterprise-api
"$SAMPLE_DIR/scripts/apply-tenant-dns.sh" "$tenant_namespace" "$gateway_namespace"

for _ in $(seq 1 120); do
  namespace_status="$("$OCC_BIN" namespace get "$namespace_id" --output json 2>/dev/null || true)"
  if [[ "$(jq -r '.status // .data.status // ""' <<<"$namespace_status")" = ready ]]; then
    break
  fi
  sleep 5
done
[[ "$(jq -r '.status // .data.status // ""' <<<"$namespace_status")" = ready ]] ||
  die "OCE Namespace did not become ready"

tr -d '\n' < "$bedrock_key_file" |
  jq -Rs '{name:"bedrock-model-key", value:.}' \
  > "$model_secret_file"
secret_response="$("$OCC_BIN" secret create \
  --file "$model_secret_file" --output json)"
rm -f -- "$model_secret_file"
secret_id="$(jq -er '.data.id // .id // .ref.id' <<<"$secret_response")"

configuration_response="$("$OCC_BIN" configuration create \
  --file "$GENERATED_DIR/agent-configuration.json" --output json)"
configuration_id="$(jq -er '.id // .data.id' <<<"$configuration_response")"

jq -n \
  --arg name "$agent_name" \
  --arg configurationId "$configuration_id" \
  --arg namespaceId "$namespace_id" \
  --arg secretId "$secret_id" \
  '{
    name:$name,
    configurationId:$configurationId,
    executionMode:"embedded",
    harnessAuth:{
      method:"api_key",
      source:{kind:"secret", namespaceId:$namespaceId, id:$secretId}
    }
  }' > "$GENERATED_DIR/agent.json"
agent_response="$("$OCC_BIN" agent create \
  --file "$GENERATED_DIR/agent.json" --output json)"
agent_id="$(jq -er '.id // .data.id' <<<"$agent_response")"
service_principal_id="$(jq -er '.servicePrincipalId // .data.servicePrincipalId' <<<"$agent_response")"

"$OCC_BIN" agent runtime-credentials provision "$agent_id" --output json >/dev/null
if [[ "$verify_denied" = 1 ]]; then
  denied_output="$GENERATED_DIR/tenant-$tenant_name-denied-deploy.log"
  if "$OCC_BIN" agent deploy "$agent_id" --output json >"$denied_output" 2>&1; then
    die "Agent deployment succeeded before the exact Secret access binding"
  fi
  if ! grep -q 'HTTP 403' "$denied_output" ||
    ! grep -q 'FORBIDDEN' "$denied_output" ||
    ! grep -q "$service_principal_id" "$denied_output" ||
    ! grep -q "$secret_id" "$denied_output"; then
    cat "$denied_output" >&2
    die "pre-binding deployment did not return the expected exact-resource HTTP 403"
  fi
  rm -f -- "$denied_output"
  printf 'Verified HTTP 403 before granting Agent %s access to Secret %s.\n' \
    "$agent_id" "$secret_id"
fi

role_response="$("$OCC_BIN" iam role create \
  --file "$SAMPLE_DIR/oce/model-secret-role.json" --output json)"
role_id="$(jq -er '.id // .data.id' <<<"$role_response")"
jq -n \
  --arg subjectId "$service_principal_id" \
  --arg roleId "$role_id" \
  --arg secretId "$secret_id" \
  '{
    subjectKind:"identity",
    subjectId:$subjectId,
    roleId:$roleId,
    resourceKind:"secret",
    resourceId:$secretId
  }' > "$GENERATED_DIR/model-secret-binding.json"
binding_response="$("$OCC_BIN" iam access-binding create \
  --file "$GENERATED_DIR/model-secret-binding.json" --output json)"
binding_id="$(jq -er '.id // .data.id' <<<"$binding_response")"
stored_binding="$("$OCC_BIN" iam access-binding get "$binding_id" --output json)"
jq -e \
  --arg namespaceId "$namespace_id" \
  --arg subjectId "$service_principal_id" \
  --arg roleId "$role_id" \
  --arg secretId "$secret_id" \
  '
    (.namespaceId // .data.namespaceId) == $namespaceId and
    (.subjectKind // .data.subjectKind) == "identity" and
    (.subjectId // .data.subjectId) == $subjectId and
    (.roleId // .data.roleId) == $roleId and
    (.resourceKind // .data.resourceKind) == "secret" and
    (.resourceId // .data.resourceId) == $secretId
  ' <<<"$stored_binding" >/dev/null ||
  die "stored access binding does not match the exact Agent and Secret"

revision_response="$("$OCC_BIN" agent deploy "$agent_id" --output json)"
revision_id="$(jq -er '.id // .data.id' <<<"$revision_response")"

jq -n \
  --arg tenantName "$tenant_name" \
  --arg agentName "$agent_name" \
  --arg namespaceId "$namespace_id" \
  --arg tenantNamespace "$tenant_namespace" \
  --arg gatewayNamespace "$gateway_namespace" \
  --arg configurationId "$configuration_id" \
  --arg secretId "$secret_id" \
  --arg agentId "$agent_id" \
  --arg servicePrincipalId "$service_principal_id" \
  --arg revisionId "$revision_id" \
  --arg roleId "$role_id" \
  --arg bindingId "$binding_id" \
  --argjson verifiedDeniedBeforeBinding "$verify_denied" \
  '{
    tenantName:$tenantName,
    agentName:$agentName,
    namespaceId:$namespaceId,
    tenantNamespace:$tenantNamespace,
    gatewayNamespace:$gatewayNamespace,
    configurationId:$configurationId,
    secretId:$secretId,
    agentId:$agentId,
    servicePrincipalId:$servicePrincipalId,
    revisionId:$revisionId,
    roleId:$roleId,
    bindingId:$bindingId,
    binding:{
      id:$bindingId,
      namespaceId:$namespaceId,
      subjectKind:"identity",
      subjectId:$servicePrincipalId,
      roleId:$roleId,
      resourceKind:"secret",
      resourceId:$secretId
    },
    verifiedDeniedBeforeBinding:($verifiedDeniedBeforeBinding == 1)
  }' > "$state_file"
chmod 600 "$state_file"

printf 'Tenant %s Agent %s deployed as revision %s. State: %s\n' \
  "$tenant_name" "$agent_id" "$revision_id" "$state_file"
