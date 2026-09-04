#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LAB_ROOT="$(cd "${COMMON_DIR}/../.." && pwd -P)"
if [[ -z "${HOST_PATH+x}" ]]; then
  HOST_PATH="${PATH}"
fi
export HOST_PATH
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
HOST_SYSCTL_STATE_FILE="${RUNTIME_DIR}/host/inotify-original.env"
SPIKE_RUNTIME_DIR="${RUNTIME_DIR}/tenants/spike"
SPIKE_RENDERED_MANIFEST="${SPIKE_RUNTIME_DIR}/tenantcontrolplane.yaml"
SPIKE_WORKER_OWNERSHIP_FILE="${SPIKE_RUNTIME_DIR}/worker.env"
SPIKE_JOIN_FILE="${SPIKE_RUNTIME_DIR}/join.sh"
SPIKE_PREFLIGHT_EVIDENCE="${SPIKE_RUNTIME_DIR}/kubeadm-preflight.log"
SPIKE_PERSISTENCE_EVIDENCE="${SPIKE_RUNTIME_DIR}/persistence.env"
MANAGEMENT_OWNERSHIP_FILE="${RUNTIME_DIR}/management/ownership.env"
MANAGEMENT_NETWORK_FILE="${RUNTIME_DIR}/network/management.env"
METALLB_RENDERED_MANIFEST="${RUNTIME_DIR}/rendered/metallb-pool.yaml"
METALLB_PINNED_MANIFEST="${RUNTIME_DIR}/rendered/metallb-native-pinned.yaml"
CERT_MANAGER_RENDERED_MANIFEST="${RUNTIME_DIR}/rendered/cert-manager.yaml"
KAMAJI_PRE_HOOKS_MANIFEST="${RUNTIME_DIR}/rendered/kamaji-hooks-pre.yaml"
KAMAJI_POST_HOOKS_MANIFEST="${RUNTIME_DIR}/rendered/kamaji-hooks-post.yaml"
KAMAJI_CHART_DIR="${CHARTS_DIR}/kamaji"
KAMAJI_IMAGE_INVENTORY="${RENDERED_DIR}/kamaji-images.txt"
KAMAJI_RENDERED_MANIFEST="${RENDERED_DIR}/kamaji.yaml"

# shellcheck disable=SC1091
source "${LAB_ROOT}/config/versions.env"
# shellcheck disable=SC1091
source "${LAB_ROOT}/config/settings.env"

ETCD_INSPECTOR_NAME="${LAB_PREFIX}-etcd-inspector"
ETCD_INSPECTOR_MANIFEST="${RUNTIME_DIR}/rendered/etcd-inspector.json"

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
    "${RUNTIME_DIR}/host" \
    "${RUNTIME_DIR}/management" \
    "${RUNTIME_DIR}/network" \
    "${RUNTIME_DIR}/rendered" \
    "${RUNTIME_DIR}/tenants"
  find "${RUNTIME_DIR}" -type d -exec chmod 0700 {} +
}

read_inotify_value() {
  local name="$1"
  cat "/proc/sys/fs/inotify/${name}"
}

inotify_values_are_sufficient() {
  local instances watches
  instances="${KAMAJI_PREFLIGHT_INOTIFY_INSTANCES_FIXTURE:-$(read_inotify_value max_user_instances)}"
  watches="${KAMAJI_PREFLIGHT_INOTIFY_WATCHES_FIXTURE:-$(read_inotify_value max_user_watches)}"
  (( instances >= MIN_INOTIFY_INSTANCES && watches >= MIN_INOTIFY_WATCHES ))
}

require_host_inotify_capacity() {
  local instances watches
  instances="$(read_inotify_value max_user_instances)"
  watches="$(read_inotify_value max_user_watches)"
  (( instances >= MIN_INOTIFY_INSTANCES )) \
    || die "capacity.inotify-instances: requires ${MIN_INOTIFY_INSTANCES}, detected ${instances}; run just prepare-host"
  (( watches >= MIN_INOTIFY_WATCHES )) \
    || die "capacity.inotify-watches: requires ${MIN_INOTIFY_WATCHES}, detected ${watches}; run just prepare-host"
}

write_secret_file() {
  local destination="$1"
  mkdir -p -m 0700 "$(dirname "${destination}")"
  cat >"${destination}"
  chmod 0600 "${destination}"
}

require_exact_just() {
  local executable actual
  executable="$(resolve_host_just || true)"
  [[ -n "${executable}" ]] \
    || die "host-installed just ${JUST_VERSION} is required; executables below ${BIN_DIR} cannot satisfy the prerequisite"
  actual="$("${executable}" --version 2>/dev/null || true)"
  [[ "${actual}" == "just ${JUST_VERSION}" ]] \
    || die "host-installed just ${JUST_VERSION} is required; found ${actual:-unavailable} at ${executable}"
}

canonical_existing_path() {
  readlink -f -- "$1" 2>/dev/null
}

canonical_missing_path() {
  readlink -m -- "$1" 2>/dev/null
}

resolve_host_just() {
  local executable canonical_executable canonical_bin
  executable="$(PATH="${HOST_PATH}" command -v just 2>/dev/null || true)"
  [[ -n "${executable}" ]] || return 1
  canonical_executable="$(canonical_existing_path "${executable}")" || return 1
  canonical_bin="$(canonical_missing_path "${BIN_DIR}")" || return 1
  case "${canonical_executable}" in
    "${canonical_bin}"|"${canonical_bin}/"*) return 1 ;;
  esac
  printf '%s\n' "${canonical_executable}"
}

record_value() {
  local file="$1"
  local key="$2"
  awk -v key="${key}" '
    index($0, key "=") == 1 {
      count++
      value=substr($0, length(key) + 2)
    }
    END {
      if (count != 1 || value == "") exit 1
      print value
    }
  ' "${file}" 2>/dev/null
}

compatibility_blocker_code_is_allowed() {
  [[ " ${COMPATIBILITY_BLOCKER_CODES} " == *" $1 "* ]]
}

blocked_result_records_are_current_consistent() {
  local result revision prerequisite code evidence cleanup
  local blocker_owner blocker_code blocker_prerequisite blocker_message
  [[ -f "${FINAL_RESULT_FILE}" && -f "${BLOCKER_FILE}" ]] || return 1
  result="$(record_value "${FINAL_RESULT_FILE}" result)" || return 1
  revision="$(record_value "${FINAL_RESULT_FILE}" compatibility_revision)" \
    || return 1
  prerequisite="$(record_value "${FINAL_RESULT_FILE}" first_failing_prerequisite)" \
    || return 1
  code="$(record_value "${FINAL_RESULT_FILE}" blocker_code)" || return 1
  evidence="$(record_value "${FINAL_RESULT_FILE}" blocker_evidence)" || return 1
  cleanup="$(record_value "${FINAL_RESULT_FILE}" cleanup)" || return 1
  blocker_owner="$(record_value "${BLOCKER_FILE}" owner)" || return 1
  blocker_code="$(record_value "${BLOCKER_FILE}" code)" || return 1
  blocker_prerequisite="$(record_value "${BLOCKER_FILE}" prerequisite)" \
    || return 1
  blocker_message="$(record_value "${BLOCKER_FILE}" message)" || return 1
  [[ "${result}" == blocked \
    && "${revision}" == "${COMPATIBILITY_REVISION}" \
    && "${prerequisite}" != none \
    && "${evidence}" != none \
    && "${cleanup}" == proved \
    && "${blocker_owner}" == final \
    && "${blocker_code}" == "${code}" \
    && "${blocker_prerequisite}" == "${prerequisite}" \
    && "${blocker_message}" == "${evidence}" ]] \
    && compatibility_blocker_code_is_allowed "${code}" \
    && grep -Fxq 'final_tenants=absent' "${FINAL_RESULT_FILE}" \
    && grep -Fxq 'final_workers=absent' "${FINAL_RESULT_FILE}" \
    && grep -Fxq 'final_volumes=absent' "${FINAL_RESULT_FILE}" \
    && grep -Fxq 'final_runtime=absent' "${FINAL_RESULT_FILE}"
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
