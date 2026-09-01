#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

SENTINEL="${LAB_PREFIX}-unrelated-sentinel"
RESULT_LOG="${TOOLS_DIR}/cache/e2e-last-result.log"

cleanup() {
  docker rm -f "${SENTINEL}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p -m 0700 "${TOOLS_DIR}/cache"
: >"${RESULT_LOG}"
chmod 0600 "${RESULT_LOG}"

docker rm -f "${SENTINEL}" >/dev/null 2>&1 || true
docker run -d --name "${SENTINEL}" "${WORKER_BASE_IMAGE}" sleep 3600 >/dev/null

"${SCRIPT_DIR}/destroy.sh"
docker inspect "${SENTINEL}" >/dev/null

set +e
"${SCRIPT_DIR}/status.sh" >>"${RESULT_LOG}" 2>&1
status_rc=$?
set -e
[[ "${status_rc}" -ne 0 ]] || die "status unexpectedly reported a clean environment as healthy"
[[ ! -e "${RUNTIME_DIR}" ]] || die "status mutated repository runtime state"

set +e
FORCE_PREFLIGHT_FAILURE=1 SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh" \
  >>"${RESULT_LOG}" 2>&1
preflight_rc=$?
set -e
[[ "${preflight_rc}" -ne 0 ]] || die "forced preflight failure unexpectedly succeeded"
[[ ! -e "${RUNTIME_DIR}" ]] || die "forced preflight failure created runtime state"
[[ -z "$(kind get clusters 2>/dev/null | grep "^${KIND_CLUSTER_NAME}$" || true)" ]] \
  || die "forced preflight failure created the kind cluster"

set +e
"${SCRIPT_DIR}/create.sh" >>"${RESULT_LOG}" 2>&1
create_rc=$?
set -e

if [[ "${create_rc}" -eq 0 ]]; then
  "${SCRIPT_DIR}/create.sh" >>"${RESULT_LOG}" 2>&1
  "${SCRIPT_DIR}/status.sh" >>"${RESULT_LOG}" 2>&1
  "${SCRIPT_DIR}/verify.sh" >>"${RESULT_LOG}" 2>&1
  printf 'result=passed\n' >>"${RESULT_LOG}"
else
  if [[ -s "${RUNTIME_DIR}/blocker" ]] \
    && grep -Eq '^code=(platform-free-tier-activation-required|platform-endpoint-unavailable)$' \
      "${RUNTIME_DIR}/blocker"; then
    "${SCRIPT_DIR}/status.sh" >>"${RESULT_LOG}" 2>&1 || true
    "${SCRIPT_DIR}/diagnose.sh" >>"${RESULT_LOG}" 2>&1 || true
    printf 'result=blocked\n' >>"${RESULT_LOG}"
    cat "${RUNTIME_DIR}/blocker" >>"${RESULT_LOG}"
  else
    "${SCRIPT_DIR}/diagnose.sh" >>"${RESULT_LOG}" 2>&1 || true
    die "environment creation failed without a recognized external blocker; see ${RESULT_LOG}"
  fi
fi

"${SCRIPT_DIR}/destroy.sh"
"${SCRIPT_DIR}/destroy.sh"
docker inspect "${SENTINEL}" >/dev/null
[[ -z "$(kind get clusters 2>/dev/null | grep "^${KIND_CLUSTER_NAME}$" || true)" ]]
[[ -z "$(docker ps -a --filter "label=cnpg-vcluster.lab=${LAB_PREFIX}" -q)" ]]
[[ ! -e "${RUNTIME_DIR}" ]]

if [[ "${create_rc}" -ne 0 ]]; then
  warn "end-to-end runtime is blocked by a documented external Platform prerequisite; see ${RESULT_LOG}"
  exit 2
fi

log "complete end-to-end lifecycle passed"
