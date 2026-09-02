#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

health="${EXIT_SUCCESS}"

section() {
  printf '\n== %s ==\n' "$1"
}

unhealthy() {
  printf 'unhealthy: %s\n' "$*"
  health="${EXIT_ERROR}"
}

section "tools"
if command -v just >/dev/null 2>&1; then
  printf 'just: %s (required: %s)\n' "$(just --version 2>/dev/null || printf unknown)" "${JUST_VERSION}"
  [[ "$(just --version 2>/dev/null || true)" == "just ${JUST_VERSION}" ]] \
    || unhealthy "just version mismatch"
else
  unhealthy "just not found"
fi
for tool in kind kubectl helm; do
  if [[ -x "${BIN_DIR}/${tool}" ]]; then
    case "${tool}" in
      kind) printf 'kind: %s\n' "$("${BIN_DIR}/kind" version 2>/dev/null || printf unknown)" ;;
      kubectl) printf 'kubectl: %s\n' "$("${BIN_DIR}/kubectl" version --client=true 2>/dev/null | head -1 || printf unknown)" ;;
      helm) printf 'helm: %s\n' "$("${BIN_DIR}/helm" version --short 2>/dev/null || printf unknown)" ;;
    esac
  else
    unhealthy "lab-local ${tool} is missing"
  fi
done

section "Docker"
if docker info >/dev/null 2>&1; then
  docker info --format 'server={{.ServerVersion}} os={{.OSType}} cgroup={{.CgroupVersion}} cpus={{.NCPU}} memory_bytes={{.MemTotal}}'
  printf 'owned containers:\n'
  docker ps -a --filter "$(owned_docker_filter)" \
    --format '  {{.Names}}\t{{.Status}}\t{{.Image}}'
  printf 'owned volumes:\n'
  docker volume ls --filter "$(owned_docker_filter)" --format '  {{.Name}}'
else
  unhealthy "Docker Engine is unavailable"
fi

section "runtime state"
if [[ -d "${RUNTIME_DIR}" ]]; then
  printf 'runtime directory: present\n'
  find "${RUNTIME_DIR}" -mindepth 1 -maxdepth 3 -printf '  %M %P\n' | sort
else
  printf 'runtime directory: absent\n'
fi

section "blocker state"
if [[ -f "${BLOCKER_FILE}" ]]; then
  sed 's/^/  /' "${BLOCKER_FILE}"
else
  printf '  none\n'
fi

section "management resources"
if [[ -f "${MANAGEMENT_KUBECONFIG}" ]]; then
  if management_kubectl get namespace >/dev/null 2>&1; then
    printf 'management API: reachable\n'
    management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
      --all-namespaces 2>/dev/null || true
  else
    unhealthy "management kubeconfig exists but API is unreachable"
  fi
else
  unhealthy "management kubeconfig is absent; management resources do not exist yet"
fi

exit "${health}"
