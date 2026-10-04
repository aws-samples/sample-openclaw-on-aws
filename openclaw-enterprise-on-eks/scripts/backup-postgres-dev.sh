#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in date kubectl mktemp; do require_command "$command"; done

export KUBECONFIG="$GENERATED_DIR/kubeconfig"
backup_dir="$GENERATED_DIR/backups"
backup_s3_uri="${POSTGRES_BACKUP_S3_URI:-}"
backup_s3_owner="${POSTGRES_BACKUP_S3_EXPECTED_BUCKET_OWNER:-}"
backup_kms_key_id="${POSTGRES_BACKUP_KMS_KEY_ID:-}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
temporary_file="$(mktemp "$backup_dir/.postgres-backup.XXXXXX")"
suffix="${temporary_file##*.}"
backup_file="$backup_dir/openclaw-enterprise-$timestamp-$suffix.dump"

cleanup() {
  rm -f -- "$temporary_file"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

kubectl -n "$OCE_NAMESPACE" wait \
  --for=condition=Ready pod/postgres-0 --timeout=2m
kubectl -n "$OCE_NAMESPACE" exec postgres-0 -c postgres -- \
  pg_dump \
    --format=custom \
    --no-owner \
    --no-acl \
    --username=postgres \
    --dbname=openclaw_enterprise > "$temporary_file"

[[ -s "$temporary_file" ]] || die "PostgreSQL backup is empty"
kubectl -n "$OCE_NAMESPACE" exec -i postgres-0 -c postgres -- \
  pg_restore --list < "$temporary_file" >/dev/null

mv -n -- "$temporary_file" "$backup_file"
[[ ! -e "$temporary_file" ]] || die "backup destination already exists"
chmod 600 "$backup_file"

if [[ -n "$backup_s3_uri" ]]; then
  for command in aws jq wc; do require_command "$command"; done
  [[ "$backup_s3_uri" =~ ^s3://[^[:space:]]+/$ ]] ||
    die "POSTGRES_BACKUP_S3_URI must be an s3:// URI ending in /"
  [[ "$backup_s3_owner" =~ ^[0-9]{12}$ ]] ||
    die "POSTGRES_BACKUP_S3_EXPECTED_BUCKET_OWNER must be a 12-digit account ID"
  [[ -n "$backup_kms_key_id" ]] ||
    die "set POSTGRES_BACKUP_KMS_KEY_ID for S3 uploads"

  s3_path="${backup_s3_uri#s3://}"
  s3_bucket="${s3_path%%/*}"
  s3_prefix="${s3_path#*/}"
  object_key="$s3_prefix$(basename "$backup_file")"
  local_size="$(wc -c < "$backup_file" | tr -d '[:space:]')"

  aws s3api head-bucket \
    --bucket "$s3_bucket" \
    --expected-bucket-owner "$backup_s3_owner"
  aws s3api put-object \
    --bucket "$s3_bucket" \
    --key "$object_key" \
    --body "$backup_file" \
    --checksum-algorithm SHA256 \
    --server-side-encryption aws:kms \
    --ssekms-key-id "$backup_kms_key_id" \
    --expected-bucket-owner "$backup_s3_owner" \
    --if-none-match '*' >/dev/null
  uploaded_object="$(
    aws s3api head-object \
      --bucket "$s3_bucket" \
      --key "$object_key" \
      --expected-bucket-owner "$backup_s3_owner"
  )"
  jq -e \
    --argjson size "$local_size" \
    '.ContentLength == $size and
      .ServerSideEncryption == "aws:kms" and
      (.SSEKMSKeyId | type == "string" and length > 0)' \
    <<<"$uploaded_object" >/dev/null ||
    die "uploaded backup size or encryption check failed"
  printf 'Uploaded the encrypted backup to %s%s.\n' \
    "$backup_s3_uri" "$(basename "$backup_file")"
fi

printf 'Created PostgreSQL backup and checked its archive catalog: %s\n' \
  "$backup_file"
