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
[[ -x "$OCC_BIN" ]] || die "build the OCE CLI or set OCC_BIN"
[[ -s "$OCC_SERVICE_KEY_FILE" ]] || die "run retrieve-service-key.sh first"
[[ -n "$bedrock_key_file" && -s "$bedrock_key_file" ]] ||
  die "set BEDROCK_API_KEY_FILE to a mode-0600 key file"

port_forward_log="$GENERATED_DIR/occ-port-forward.log"
# shellcheck disable=SC2153
kubectl -n "$OCE_NAMESPACE" port-forward \
  service/openclaw-enterprise-api 3000:8080 >"$port_forward_log" 2>&1 &
port_forward_pid=$!
trap 'kill "$port_forward_pid" 2>/dev/null || true' EXIT
sleep 3

namespace_json="$("$OCC_BIN" namespace list --output json)"
namespace_id="$(python3 -c '
import json,sys
data=json.load(sys.stdin)
items=data.get("items", data.get("data", data if isinstance(data,list) else []))
matches=[item for item in items if item.get("name")=="default"]
print(matches[0].get("id") or matches[0].get("data",{}).get("id")) if len(matches)==1 else sys.exit("Expected one default Namespace")
' <<<"$namespace_json")"
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
  > "$GENERATED_DIR/model-secret.json"
secret_response="$("$OCC_BIN" secret create \
  --file "$GENERATED_DIR/model-secret.json" --output json)"
rm -f "$GENERATED_DIR/model-secret.json"
secret_id="$(jq -er '.data.id // .id // .ref.id' <<<"$secret_response")"

configuration_response="$("$OCC_BIN" configuration create \
  --file "$GENERATED_DIR/agent-configuration.json" --output json)"
configuration_id="$(jq -er '.id // .data.id' <<<"$configuration_response")"

jq -n \
  --arg configurationId "$configuration_id" \
  --arg namespaceId "$namespace_id" \
  --arg secretId "$secret_id" \
  '{
    name:"eks-auto-mode-bedrock",
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
"$OCC_BIN" iam access-binding create \
  --file "$GENERATED_DIR/model-secret-binding.json" --output json >/dev/null

"$OCC_BIN" agent runtime-credentials provision "$agent_id" --output json >/dev/null
revision_response="$("$OCC_BIN" agent deploy "$agent_id" --output json)"
revision_id="$(jq -er '.id // .data.id' <<<"$revision_response")"

jq -n \
  --arg namespaceId "$namespace_id" \
  --arg tenantNamespace "$tenant_namespace" \
  --arg gatewayNamespace "$gateway_namespace" \
  --arg configurationId "$configuration_id" \
  --arg secretId "$secret_id" \
  --arg agentId "$agent_id" \
  --arg revisionId "$revision_id" \
  '{
    namespaceId:$namespaceId,
    tenantNamespace:$tenantNamespace,
    gatewayNamespace:$gatewayNamespace,
    configurationId:$configurationId,
    secretId:$secretId,
    agentId:$agentId,
    revisionId:$revisionId
  }' > "$GENERATED_DIR/agent-state.json"
chmod 600 "$GENERATED_DIR/agent-state.json"

printf 'Agent %s deployed as revision %s.\n' "$agent_id" "$revision_id"
