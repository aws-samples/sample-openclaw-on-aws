#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in kubectl openssl python3; do require_command "$command"; done
export KUBECONFIG="$GENERATED_DIR/kubeconfig"
kubectl create namespace "$OCE_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

postgres_password="$(openssl rand -hex 24)"
app_password="$(openssl rand -hex 24)"
migrator_password="$(openssl rand -hex 24)"

cat > "$GENERATED_DIR/postgres-init.sql" <<SQL
CREATE ROLE occ_migrator LOGIN PASSWORD '$migrator_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE occ_app LOGIN PASSWORD '$app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT CREATE ON DATABASE openclaw_enterprise TO occ_migrator;
CREATE SCHEMA occ AUTHORIZATION occ_migrator;
CREATE SCHEMA drizzle AUTHORIZATION occ_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
printf '%s' "$postgres_password" > "$GENERATED_DIR/postgres-password"

kubectl -n "$OCE_NAMESPACE" create secret generic postgres-bootstrap \
  --from-file=password="$GENERATED_DIR/postgres-password" \
  --from-file=init.sql="$GENERATED_DIR/postgres-init.sql" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$SAMPLE_DIR/manifests/postgres-dev/"
kubectl -n "$OCE_NAMESPACE" rollout status statefulset/postgres --timeout=10m

python3 - "$app_password" "$migrator_password" "$GENERATED_DIR" <<'PY'
from pathlib import Path
from urllib.parse import quote
import sys

app, migrator, output = sys.argv[1], sys.argv[2], Path(sys.argv[3])
host = "postgres.openclaw-system.svc.cluster.local"
database = "openclaw_enterprise"
(output / "occ-application-url").write_text(
    f"postgresql://occ_app:{quote(app, safe='')}@{host}:5432/{database}"
)
(output / "occ-migration-url").write_text(
    f"postgresql://occ_migrator:{quote(migrator, safe='')}@{host}:5432/{database}"
)
PY
chmod 600 "$GENERATED_DIR"/postgres-* "$GENERATED_DIR"/occ-*-url

postgres_ip="$(kubectl -n "$OCE_NAMESPACE" get pod postgres-0 \
  -o jsonpath='{.status.podIP}')"
printf 'POSTGRES_POD_IP=%s\n' "$postgres_ip" > "$GENERATED_DIR/state.env"
chmod 600 "$GENERATED_DIR/state.env"
printf 'PostgreSQL is ready at Pod IP %s.\n' "$postgres_ip"
