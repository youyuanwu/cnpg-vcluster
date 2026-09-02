#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LAB_ROOT="$(cd "${COMMON_DIR}/../.." && pwd -P)"
TOOLS_DIR="${LAB_ROOT}/.tools"
BIN_DIR="${TOOLS_DIR}/bin"
CACHE_DIR="${TOOLS_DIR}/cache"
INPUTS_DIR="${TOOLS_DIR}/inputs"
SOURCE_DIR="${TOOLS_DIR}/source"
CHARTS_DIR="${TOOLS_DIR}/charts"
RENDERED_DIR="${TOOLS_DIR}/rendered"
TOOLS_TMP_DIR="${TOOLS_DIR}/tmp"
RUNTIME_DIR="${LAB_ROOT}/.runtime"
MANAGEMENT_KUBECONFIG="${RUNTIME_DIR}/kubeconfigs/management.yaml"
BLOCKER_FILE="${RUNTIME_DIR}/blocker"
SPIKE_RESULT_FILE="${RUNTIME_DIR}/spike-result.env"
FINAL_RESULT_FILE="${RUNTIME_DIR}/final-result.env"
SPIKE_RUNTIME_DIR="${RUNTIME_DIR}/tenants/spike"
SPIKE_RENDERED_MANIFEST="${SPIKE_RUNTIME_DIR}/tenantcontrolplane.yaml"
SPIKE_WORKER_OWNERSHIP_FILE="${SPIKE_RUNTIME_DIR}/worker.env"
SPIKE_JOIN_FILE="${SPIKE_RUNTIME_DIR}/join.sh"
SPIKE_PREFLIGHT_EVIDENCE="${SPIKE_RUNTIME_DIR}/kubeadm-preflight.log"
SPIKE_PERSISTENCE_EVIDENCE="${SPIKE_RUNTIME_DIR}/persistence.env"
MANAGEMENT_OWNERSHIP_FILE="${RUNTIME_DIR}/management/ownership.env"
MANAGEMENT_NETWORK_FILE="${RUNTIME_DIR}/network/management.env"
METALLB_RENDERED_MANIFEST="${RUNTIME_DIR}/rendered/metallb-pool.yaml"
KAMAJI_PRE_HOOKS_MANIFEST="${RUNTIME_DIR}/rendered/kamaji-hooks-pre.yaml"
KAMAJI_POST_HOOKS_MANIFEST="${RUNTIME_DIR}/rendered/kamaji-hooks-post.yaml"
KAMAJI_CHART_DIR="${CHARTS_DIR}/kamaji"
KAMAJI_IMAGE_INVENTORY="${RENDERED_DIR}/kamaji-images.txt"
KAMAJI_RENDERED_MANIFEST="${RENDERED_DIR}/kamaji.yaml"

# shellcheck disable=SC1091
source "${LAB_ROOT}/config/versions.env"
# shellcheck disable=SC1091
source "${LAB_ROOT}/config/settings.env"

export PATH="${BIN_DIR}:${PATH}"
export TMPDIR="${TOOLS_TMP_DIR}"

log() {
  printf '[kamaji-lab] %s\n' "$*"
}

warn() {
  printf '[kamaji-lab] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[kamaji-lab] ERROR: %s\n' "$*" >&2
  exit "${EXIT_ERROR}"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

seconds_from_duration() {
  local value="$1"
  case "${value}" in
    *s) printf '%s\n' "${value%s}" ;;
    *m) printf '%s\n' "$(( ${value%m} * 60 ))" ;;
    *h) printf '%s\n' "$(( ${value%h} * 3600 ))" ;;
    *) die "unsupported finite duration: ${value}" ;;
  esac
}

wait_for() {
  local duration="$1"
  local description="$2"
  shift 2
  local deadline=$((SECONDS + $(seconds_from_duration "${duration}")))

  until "$@"; do
    if (( SECONDS >= deadline )); then
      warn "timed out waiting for ${description} after ${duration}"
      return 1
    fi
    sleep "${WAIT_POLL_INTERVAL}"
  done
}

retry_for() {
  local duration="$1"
  local description="$2"
  shift 2
  wait_for "${duration}" "${description}" "$@" \
    || die "timed out waiting for ${description} after ${duration}"
}

sha256_check() {
  local expected="$1"
  local file="$2"
  local actual
  actual="$(sha256sum "${file}" | awk '{print $1}')"
  [[ "${actual}" == "${expected}" ]] \
    || die "checksum mismatch for ${file}: expected ${expected}, got ${actual}"
}

sha256_matches() {
  local expected="$1"
  local file="$2"
  [[ -f "${file}" ]] \
    && [[ "$(sha256sum "${file}" | awk '{print $1}')" == "${expected}" ]]
}

ensure_tools_layout() {
  mkdir -p -m 0700 \
    "${TOOLS_DIR}" "${BIN_DIR}" "${CACHE_DIR}" "${INPUTS_DIR}" \
    "${SOURCE_DIR}" "${CHARTS_DIR}" "${RENDERED_DIR}" "${TOOLS_TMP_DIR}"
  find "${TOOLS_DIR}" -type d -exec chmod 0700 {} +
}

ensure_runtime_layout() {
  mkdir -p -m 0700 \
    "${RUNTIME_DIR}" \
    "${RUNTIME_DIR}/kubeconfigs" \
    "${RUNTIME_DIR}/logs" \
    "${RUNTIME_DIR}/management" \
    "${RUNTIME_DIR}/network" \
    "${RUNTIME_DIR}/rendered" \
    "${RUNTIME_DIR}/tenants"
  find "${RUNTIME_DIR}" -type d -exec chmod 0700 {} +
}

write_secret_file() {
  local destination="$1"
  mkdir -p -m 0700 "$(dirname "${destination}")"
  cat >"${destination}"
  chmod 0600 "${destination}"
}

require_exact_just() {
  require_command just
  local actual
  actual="$(just --version 2>/dev/null || true)"
  [[ "${actual}" == "just ${JUST_VERSION}" ]] \
    || die "just ${JUST_VERSION} is required; found ${actual:-unavailable}"
}

management_context() {
  printf 'kind-%s\n' "${KIND_CLUSTER_NAME}"
}

tenant_kubeconfig() {
  printf '%s/tenants/%s/admin.conf\n' "${RUNTIME_DIR}" "$1"
}

tenant_runtime_dir() {
  printf '%s/tenants/%s\n' "${RUNTIME_DIR}" "$1"
}

tenant_rendered_manifest() {
  printf '%s/tenantcontrolplane.yaml\n' "$(tenant_runtime_dir "$1")"
}

tenant_kube_proxy_evidence() {
  printf '%s/kube-proxy.env\n' "$(tenant_runtime_dir "$1")"
}

tenant_addon_dir() {
  printf '%s/addons\n' "$(tenant_runtime_dir "$1")"
}

management_kubectl() {
  KUBECONFIG="${MANAGEMENT_KUBECONFIG}" kubectl \
    --context "$(management_context)" \
    --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" "$@"
}

management_helm() {
  KUBECONFIG="${MANAGEMENT_KUBECONFIG}" helm \
    --kube-context "$(management_context)" "$@"
}

tenant_kubectl() {
  local tenant="$1"
  shift
  KUBECONFIG="$(tenant_kubeconfig "${tenant}")" kubectl \
    --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" "$@"
}

record_blocker() {
  local code="$1"
  shift
  ensure_runtime_layout
  {
    printf 'code=%s\n' "${code}"
    printf 'message=%s\n' "$*"
  } | write_secret_file "${BLOCKER_FILE}"
}

record_spike_blocker() {
  local code="$1"
  shift
  case " ${COMPATIBILITY_BLOCKER_CODES} " in
    *" ${code} "*) ;;
    *) die "unrecognized compatibility blocker code: ${code}" ;;
  esac
  ensure_runtime_layout
  {
    printf 'owner=spike\n'
    printf 'code=%s\n' "${code}"
    printf 'message=%s\n' "$*"
  } | write_secret_file "${BLOCKER_FILE}"
  warn "compatibility blocker ${code}: $*"
}

clear_owned_spike_blocker() {
  if [[ -f "${BLOCKER_FILE}" ]] \
    && grep -Fxq 'owner=spike' "${BLOCKER_FILE}"; then
    rm -f "${BLOCKER_FILE}"
  fi
}

clear_owned_spike_evidence() {
  clear_owned_spike_blocker
  rm -f "${SPIKE_RESULT_FILE}"
}

owned_docker_filter() {
  printf 'label=%s=%s\n' "${OWNERSHIP_LABEL}" "${LAB_PREFIX}"
}
