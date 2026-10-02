#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in kubectl openssl python3; do require_command "$command"; done
export KUBECONFIG="$GENERATED_DIR/kubeconfig"
reader="bootstrap-key-reader-$(openssl rand -hex 4)"

cleanup() {
  kubectl -n "$OCE_NAMESPACE" delete pod "$reader" --ignore-not-found --wait=false >/dev/null
}
trap cleanup EXIT

kubectl -n "$OCE_NAMESPACE" create -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $reader
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  nodeSelector:
    oce-role: control
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: reader
      image: $CONTROLLER_IMAGE
      command: [node, -e, "setInterval(() => {}, 60000)"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      resources:
        requests: {cpu: 25m, memory: 32Mi}
        limits: {cpu: 100m, memory: 128Mi}
      volumeMounts:
        - name: bootstrap
          mountPath: /bootstrap
  volumes:
    - name: bootstrap
      persistentVolumeClaim:
        claimName: bootstrap-password
EOF

kubectl -n "$OCE_NAMESPACE" wait --for=condition=Ready "pod/$reader" --timeout=5m
kubectl -n "$OCE_NAMESPACE" exec "$reader" -- \
  node -e 'process.stdout.write(require("node:fs").readFileSync("/bootstrap/initial-admin-service-key.json"))' \
  > "$GENERATED_DIR/initial-admin-service-key.json"
chmod 600 "$GENERATED_DIR/initial-admin-service-key.json"

python3 - "$GENERATED_DIR/initial-admin-service-key.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    document = json.load(stream)
if not isinstance(document.get("data", {}).get("key"), str):
    raise SystemExit("service-key file does not contain data.key")
print("Bootstrap service key retrieved without printing its value.")
PY
