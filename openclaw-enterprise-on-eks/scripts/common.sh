#!/usr/bin/env bash
set -euo pipefail

SAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$SAMPLE_DIR/.env}"
GENERATED_DIR="$SAMPLE_DIR/.generated"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

load_config() {
  [[ -f "$ENV_FILE" ]] || die "copy sample.env to .env and configure it"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  umask 077
  mkdir -p "$GENERATED_DIR"
  chmod 700 "$GENERATED_DIR"
}

require_digest() {
  [[ "$2" =~ @sha256:[a-fA-F0-9]{64}$ ]] || die "$1 must use an immutable sha256 digest"
}
