#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in kubectl helm openssl git; do require_command "$command"; done
[[ -f "$GENERATED_DIR/state.env" ]] || die "run deploy-postgres-dev.sh first"
# shellcheck disable=SC1091
source "$GENERATED_DIR/state.env"
export POSTGRES_POD_IP
export KUBECONFIG="$GENERATED_DIR/kubeconfig"

[[ "$(git -C "$OCE_SOURCE_DIR" rev-parse HEAD)" = "$OCE_GIT_REF" ]] ||
  die "OCE_SOURCE_DIR is not at OCE_GIT_REF"

"$SAMPLE_DIR/scripts/render-oce-config.sh"
kubectl apply -f "$GENERATED_DIR/platform-network-policies.yaml"

if [[ ! -s "$GENERATED_DIR/occ-auth-secret" ]]; then
  printf '%s' "$(openssl rand -hex 32)" > "$GENERATED_DIR/occ-auth-secret"
fi
chmod 600 "$GENERATED_DIR/occ-auth-secret"

kubectl -n "$OCE_NAMESPACE" create secret generic occ-installation-startup \
  --from-file=installation.yaml="$GENERATED_DIR/installation.yaml" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$OCE_NAMESPACE" create secret generic occ-database \
  --from-file=application-url="$GENERATED_DIR/occ-application-url" \
  --from-file=migration-url="$GENERATED_DIR/occ-migration-url" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$OCE_NAMESPACE" create secret generic occ-auth \
  --from-file=secret="$GENERATED_DIR/occ-auth-secret" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f "$SAMPLE_DIR/manifests/bootstrap-pvc.yaml"
"$OCE_SOURCE_DIR/scripts/prepare-bootstrap-volume" \
  --kubeconfig "$GENERATED_DIR/kubeconfig" \
  --context "oce-$CLUSTER_NAME" \
  --namespace "$OCE_NAMESPACE" \
  --claim bootstrap-password \
  --image "$CONTROLLER_IMAGE" \
  --node-selector oce-role=control

helm upgrade --install "$OCE_RELEASE" \
  "$OCE_SOURCE_DIR/deploy/helm/openclaw-enterprise" \
  --kubeconfig "$GENERATED_DIR/kubeconfig" \
  --kube-context "oce-$CLUSTER_NAME" \
  --namespace "$OCE_NAMESPACE" \
  -f "$GENERATED_DIR/values.yaml" \
  --wait --timeout 10m

kubectl -n "$OCE_NAMESPACE" get pods,pvc -o wide
printf 'OCE is installed. Retrieve the bootstrap service key from the protected PVC before creating Agents.\n'
