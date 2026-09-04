#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

runtime_fingerprint() {
  if [[ -d "${RUNTIME_DIR}" ]]; then
    find "${RUNTIME_DIR}" -printf '%M %s %T@ %P\n' | sort | sha256sum
  else
    printf 'absent\n'
  fi
}

owned_docker_count() {
  docker ps -aq --filter "$(owned_docker_filter)" | wc -l
  docker volume ls -q --filter "$(owned_docker_filter)" | wc -l
  docker ps -aq --filter 'name=^kamaji-prerequisite-probe-' | wc -l
}

negative_fixture() {
  local instances="$1"
  local watches="$2"
  local expected="$3"
  local before_runtime before_docker output status
  before_runtime="$(runtime_fingerprint)"
  before_docker="$(owned_docker_count)"
  set +e
  output="$(
    env \
      KAMAJI_PREFLIGHT_INOTIFY_INSTANCES_FIXTURE="${instances}" \
      KAMAJI_PREFLIGHT_INOTIFY_WATCHES_FIXTURE="${watches}" \
      KAMAJI_PREFLIGHT_FORBID_MUTATION_FIXTURE=1 \
      "${SCRIPT_DIR}/preflight.sh" 2>&1
  )"
  status=$?
  set -e
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && grep -Fq "capacity.${expected}" <<<"${output}" \
    && [[ "$(runtime_fingerprint)" == "${before_runtime}" ]] \
    && [[ "$(owned_docker_count)" == "${before_docker}" ]]
}

negative_fixture "$((MIN_INOTIFY_INSTANCES - 1))" "${MIN_INOTIFY_WATCHES}" inotify-instances \
  || die "inotify instance fixture did not reject before mutation"
negative_fixture "${MIN_INOTIFY_INSTANCES}" "$((MIN_INOTIFY_WATCHES - 1))" inotify-watches \
  || die "inotify watch fixture did not reject before mutation"
log "negative inotify fixtures passed"
