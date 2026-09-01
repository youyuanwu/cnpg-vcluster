#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${REPO_ROOT}/.runtime"
TOOLS_DIR="${REPO_ROOT}/.tools"
BIN_DIR="${TOOLS_DIR}/bin"
CACHE_DIR="${TOOLS_DIR}/cache"
HOST_KUBECONFIG="${RUNTIME_DIR}/kubeconfigs/host.yaml"
VCLUSTER_CONFIG="${RUNTIME_DIR}/vcluster-config.json"

# shellcheck disable=SC1091
source "${REPO_ROOT}/config/versions.env"
# shellcheck disable=SC1091
source "${REPO_ROOT}/config/settings.env"

export PATH="${BIN_DIR}:${PATH}"

mkdir -p -m 0700 "${RUNTIME_DIR}" "${CACHE_DIR}" "${BIN_DIR}"
chmod 0700 "${RUNTIME_DIR}" "${CACHE_DIR}" "${BIN_DIR}"

log() {
  printf '[cnpg-vcluster] %s\n' "$*"
}

warn() {
  printf '[cnpg-vcluster] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[cnpg-vcluster] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

tenant_namespace() {
  printf '%s-%s\n' "${TENANT_NAMESPACE_PREFIX}" "$1"
}

tenant_kubeconfig() {
  printf '%s/kubeconfigs/%s.yaml\n' "${RUNTIME_DIR}" "$1"
}

worker_name() {
  printf '%s-%s-worker-%s\n' "${LAB_PREFIX}" "$1" "$2"
}

ensure_runtime_layout() {
  mkdir -p -m 0700 \
    "${RUNTIME_DIR}/kubeconfigs" \
    "${RUNTIME_DIR}/credentials" \
    "${RUNTIME_DIR}/join" \
    "${RUNTIME_DIR}/logs" \
    "${RUNTIME_DIR}/workers"
  chmod 0700 \
    "${RUNTIME_DIR}/kubeconfigs" \
    "${RUNTIME_DIR}/credentials" \
    "${RUNTIME_DIR}/join" \
    "${RUNTIME_DIR}/logs" \
    "${RUNTIME_DIR}/workers"
}

host_context() {
  printf 'kind-%s\n' "${KIND_CLUSTER_NAME}"
}

kubectl_host() {
  KUBECONFIG="${HOST_KUBECONFIG}" kubectl --context "$(host_context)" "$@"
}

kubectl_tenant() {
  local tenant="$1"
  shift
  KUBECONFIG="$(tenant_kubeconfig "${tenant}")" kubectl "$@"
}

vcluster_cli() {
  vcluster --config "${VCLUSTER_CONFIG}" "$@"
}

record_blocker() {
  local code="$1"
  shift
  {
    printf 'code=%s\n' "${code}"
    printf 'message=%s\n' "$*"
  } >"${RUNTIME_DIR}/blocker"
  chmod 0600 "${RUNTIME_DIR}/blocker"
}

seconds_from_duration() {
  local value="$1"
  case "${value}" in
    *s) printf '%s\n' "${value%s}" ;;
    *m) printf '%s\n' "$(( ${value%m} * 60 ))" ;;
    *h) printf '%s\n' "$(( ${value%h} * 3600 ))" ;;
    *) die "unsupported duration: ${value}" ;;
  esac
}

retry_for() {
  local duration="$1"
  local description="$2"
  shift 2
  local deadline=$((SECONDS + $(seconds_from_duration "${duration}")))

  until "$@"; do
    if (( SECONDS >= deadline )); then
      die "timed out waiting for ${description} after ${duration}"
    fi
    sleep 5
  done
}

sha256_check() {
  local expected="$1"
  local file="$2"
  local actual
  actual="$(sha256sum "${file}" | awk '{print $1}')"
  [[ "${actual}" == "${expected}" ]] \
    || die "checksum mismatch for ${file}: expected ${expected}, got ${actual}"
}
