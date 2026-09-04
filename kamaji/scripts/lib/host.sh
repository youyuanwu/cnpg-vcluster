#!/usr/bin/env bash

set -Eeuo pipefail
HOST_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${HOST_LIB_DIR}/common.sh"

record_original_inotify_values() {
  if [[ -f "${HOST_SYSCTL_STATE_FILE}" ]]; then
    grep -Eq '^MAX_USER_INSTANCES=[0-9]+$' "${HOST_SYSCTL_STATE_FILE}" \
      && grep -Eq '^MAX_USER_WATCHES=[0-9]+$' "${HOST_SYSCTL_STATE_FILE}" \
      || die "host inotify state record is malformed: ${HOST_SYSCTL_STATE_FILE}"
    return
  fi
  {
    printf 'MAX_USER_INSTANCES=%s\n' "$(read_inotify_value max_user_instances)"
    printf 'MAX_USER_WATCHES=%s\n' "$(read_inotify_value max_user_watches)"
  } | write_secret_file "${HOST_SYSCTL_STATE_FILE}"
}

raise_inotify_value() {
  local name="$1"
  local floor="$2"
  local current
  current="$(read_inotify_value "${name}")"
  if (( current < floor )); then
    apply_runtime_sysctl "${name}" "${floor}"
  fi
}

apply_runtime_sysctl() {
  local name="$1"
  local value="$2"
  if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    sudo sysctl -q -w "fs.inotify.${name}=${value}"
    return
  fi
  docker info >/dev/null 2>&1 \
    && docker image inspect "${KIND_NODE_IMAGE}" >/dev/null 2>&1 \
    || die "non-interactive sudo is unavailable and the pinned worker image is not local; run sudo sysctl -w fs.inotify.${name}=${value}"
  warn "non-interactive sudo is unavailable; applying the same runtime-only host sysctl through a privileged pinned worker container"
  docker run --rm --privileged --pid=host --network=host \
    --entrypoint sh "${KIND_NODE_IMAGE}" \
    -ec "sysctl -q -w fs.inotify.${name}=${value}"
}

prepare_host_inotify() {
  ensure_runtime_layout
  record_original_inotify_values
  raise_inotify_value max_user_instances "${MIN_INOTIFY_INSTANCES}"
  raise_inotify_value max_user_watches "${MIN_INOTIFY_WATCHES}"
  require_host_inotify_capacity
}

restore_recorded_inotify_values() {
  [[ -f "${HOST_SYSCTL_STATE_FILE}" ]] || return 0
  [[ "$(docker ps -aq --filter "$(owned_docker_filter)" \
      --filter 'label=kamaji.cnpg-vcluster.io/role=worker' | wc -l)" -eq 0 ]] \
    || die "host inotify values cannot be restored while nested workers exist"
  local original_instances original_watches
  original_instances="$(sed -n 's/^MAX_USER_INSTANCES=//p' "${HOST_SYSCTL_STATE_FILE}")"
  original_watches="$(sed -n 's/^MAX_USER_WATCHES=//p' "${HOST_SYSCTL_STATE_FILE}")"
  [[ "${original_instances}" =~ ^[0-9]+$ && "${original_watches}" =~ ^[0-9]+$ ]] \
    || die "host inotify state record is malformed: ${HOST_SYSCTL_STATE_FILE}"
  apply_runtime_sysctl max_user_instances "${original_instances}"
  apply_runtime_sysctl max_user_watches "${original_watches}"
  [[ "$(read_inotify_value max_user_instances)" == "${original_instances}" \
    && "$(read_inotify_value max_user_watches)" == "${original_watches}" ]] \
    || die "recorded host inotify values were not restored"
  rm -f "${HOST_SYSCTL_STATE_FILE}"
}
