#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
load_config

for command in aws docker git jq; do require_command "$command"; done

controller_repository="${ECR_CONTROLLER_REPOSITORY:-openclaw-enterprise/controller}"
runtime_repository="${ECR_RUNTIME_REPOSITORY:-openclaw-enterprise/runtime}"
image_platform="${OCE_IMAGE_PLATFORM:-linux/arm64}"
node_base_image="${NODE_BASE_IMAGE:-docker.io/library/node:24-bookworm@sha256:934240a162082fd8b8a2f90cd5114446443f1eba1c5378f6687167ca405e6584}"
metadata_dir="$GENERATED_DIR/image-build"
repository_error="$metadata_dir/ecr-error.log"
images_env="$GENERATED_DIR/images.env"

rm -f -- "$images_env"

git -C "$OCE_SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
  die "OCE_SOURCE_DIR must be a Git checkout"
resolved_ref="$(git -C "$OCE_SOURCE_DIR" rev-parse "$OCE_GIT_REF^{commit}")"
current_ref="$(git -C "$OCE_SOURCE_DIR" rev-parse HEAD)"
[[ "$current_ref" = "$resolved_ref" ]] ||
  die "OCE_SOURCE_DIR must be checked out at OCE_GIT_REF"
[[ -z "$(git -C "$OCE_SOURCE_DIR" status --porcelain)" ]] ||
  die "OCE_SOURCE_DIR must be clean before building images"
[[ "$image_platform" = linux/arm64 ]] ||
  die "OCE_IMAGE_PLATFORM must be linux/arm64 for this sample"
[[ "$node_base_image" =~ @sha256:[a-f0-9]{64}$ ]] ||
  die "NODE_BASE_IMAGE must use an immutable sha256 digest"

docker buildx inspect >/dev/null
account_id="$(aws sts get-caller-identity --query Account --output text)"
[[ "$account_id" =~ ^[0-9]{12}$ ]] || die "could not resolve the AWS account"
registry="$account_id.dkr.ecr.$AWS_REGION.amazonaws.com"

mkdir -p "$metadata_dir"
chmod 700 "$metadata_dir"

ensure_repository() {
  local repository="$1" repository_json
  if repository_json="$(aws ecr describe-repositories \
    --region "$AWS_REGION" \
    --repository-names "$repository" 2>"$repository_error")"; then
    :
  elif grep -q RepositoryNotFoundException "$repository_error"; then
    repository_json="$(aws ecr create-repository \
      --region "$AWS_REGION" \
      --repository-name "$repository" \
      --image-tag-mutability IMMUTABLE \
      --image-scanning-configuration scanOnPush=true \
      --encryption-configuration encryptionType=AES256)"
  else
    cat "$repository_error" >&2
    die "could not inspect ECR repository $repository"
  fi
  jq -e '
    (.repositories[0] // .repository).imageTagMutability == "IMMUTABLE" and
    (.repositories[0] // .repository).imageScanningConfiguration.scanOnPush == true
  ' <<<"$repository_json" >/dev/null ||
    die "ECR repository $repository must use immutable tags and scan-on-push"
}

ensure_repository "$controller_repository"
ensure_repository "$runtime_repository"
rm -f -- "$repository_error"

aws ecr get-login-password --region "$AWS_REGION" |
  docker login --username AWS --password-stdin "$registry"

controller_tag="$registry/$controller_repository:$resolved_ref"
runtime_tag="$registry/$runtime_repository:$resolved_ref"

tag_error="$metadata_dir/tag-error.log"
for repository in "$controller_repository" "$runtime_repository"; do
  if aws ecr describe-images \
    --region "$AWS_REGION" \
    --repository-name "$repository" \
    --image-ids "imageTag=$resolved_ref" >/dev/null 2>"$tag_error"; then
    die "immutable image tag already exists in $repository: $resolved_ref"
  elif ! grep -q ImageNotFoundException "$tag_error"; then
    cat "$tag_error" >&2
    die "could not inspect image tag in $repository"
  fi
done
rm -f -- "$tag_error"

docker buildx build \
  --push \
  --platform "$image_platform" \
  --target runtime \
  --metadata-file "$metadata_dir/controller.json" \
  --build-arg "NODE_BASE_IMAGE=$node_base_image" \
  --build-arg "OCC_BUILD_REVISION=$resolved_ref" \
  --label "org.opencontainers.image.revision=$resolved_ref" \
  --tag "$controller_tag" \
  "$OCE_SOURCE_DIR"

docker buildx build \
  --push \
  --platform "$image_platform" \
  --metadata-file "$metadata_dir/runtime.json" \
  --build-arg "NODE_BASE_IMAGE=$node_base_image" \
  --build-arg "OCC_BUILD_REVISION=$resolved_ref" \
  --file "$OCE_SOURCE_DIR/deploy/runtime/Dockerfile" \
  --tag "$runtime_tag" \
  "$OCE_SOURCE_DIR"

controller_digest="$(
  jq -er '."containerimage.digest" | select(test("^sha256:[a-f0-9]{64}$"))' \
    "$metadata_dir/controller.json"
)"
runtime_digest="$(
  jq -er '."containerimage.digest" | select(test("^sha256:[a-f0-9]{64}$"))' \
    "$metadata_dir/runtime.json"
)"

require_clean_scan() {
  local repository="$1" digest="$2" findings status critical high

  aws ecr wait image-scan-complete \
    --region "$AWS_REGION" \
    --repository-name "$repository" \
    --image-id "imageDigest=$digest" ||
    die "image scan did not complete for $repository@$digest"

  findings="$(
    aws ecr describe-image-scan-findings \
      --region "$AWS_REGION" \
      --repository-name "$repository" \
      --image-id "imageDigest=$digest"
  )"
  status="$(jq -er '.imageScanStatus.status' <<<"$findings")"
  [[ "$status" = COMPLETE || "$status" = ACTIVE ]] ||
    die "image scan status is $status for $repository@$digest"
  critical="$(jq -er '.imageScanFindings.findingSeverityCounts.CRITICAL // 0' <<<"$findings")"
  high="$(jq -er '.imageScanFindings.findingSeverityCounts.HIGH // 0' <<<"$findings")"
  [[ "$critical" = 0 && "$high" = 0 ]] ||
    die "image scan blocked $repository@$digest: $critical critical, $high high"
}

require_clean_scan "$controller_repository" "$controller_digest"
require_clean_scan "$runtime_repository" "$runtime_digest"

controller_image="$registry/$controller_repository@$controller_digest"
runtime_image="$registry/$runtime_repository@$runtime_digest"
cat > "$images_env" <<EOF
CONTROLLER_IMAGE=$controller_image
RUNTIME_IMAGE=$runtime_image
EOF
chmod 600 "$images_env"

printf 'Published immutable images.\n'
printf 'Controller: %s\nRuntime: %s\n' "$controller_image" "$runtime_image"
printf 'Copy both values from %s into .env before deployment.\n' \
  "$images_env"
