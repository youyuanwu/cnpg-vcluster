#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

scope="${1:-all}"
case "${scope}" in
  all|management|spike|tenant-a|tenant-b) ;;
  *) die "diagnostic scope must be all, management, spike, tenant-a, or tenant-b" ;;
esac

printf '== tool files ==\n'
if [[ -d "${TOOLS_DIR}" ]]; then
  find "${TOOLS_DIR}" -maxdepth 2 -printf '%M %P\n' | sort
else
  printf 'tools directory absent\n'
fi

printf '\n== Docker runtime ==\n'
if ! docker info >/dev/null 2>&1; then
  die "Docker Engine is unavailable"
fi
docker info --format 'server={{.ServerVersion}} root={{.DockerRootDir}} cgroup={{.CgroupVersion}} cpus={{.NCPU}} memory_bytes={{.MemTotal}}'
docker ps -a --filter "$(owned_docker_filter)" \
  --format '{{json .}}'
docker volume ls --filter "$(owned_docker_filter)" --format '{{.Name}}'
docker network ls --format '{{.Name}}\t{{.Driver}}\t{{.Scope}}'

printf '\n== runtime state (names and modes only) ==\n'
if [[ -d "${RUNTIME_DIR}" ]]; then
  find "${RUNTIME_DIR}" -printf '%M %P\n' | sort
else
  printf 'runtime directory absent\n'
fi

printf '\n== blocker state ==\n'
if [[ -f "${BLOCKER_FILE}" ]]; then
  sed 's/^/  /' "${BLOCKER_FILE}"
else
  printf 'none\n'
fi

printf '\n== owned management resources ==\n'
if [[ -f "${MANAGEMENT_KUBECONFIG}" ]]; then
  management_kubectl get nodes,pods,services,pvc --all-namespaces -o wide || true
  management_kubectl get \
    tenantcontrolplanes.kamaji.clastix.io,datastores.kamaji.clastix.io \
    --all-namespaces -o wide 2>/dev/null || true
  management_kubectl get events --all-namespaces \
    --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -50 || true
else
  printf 'management kubeconfig absent; no management API query attempted\n'
fi

if [[ "${scope}" == tenant-* ]]; then
  printf '\n== %s API ==\n' "${scope}"
  if [[ -f "$(tenant_kubeconfig "${scope}")" ]]; then
    tenant_kubectl "${scope}" get nodes,pods,services,pvc --all-namespaces -o wide || true
  else
    printf '%s kubeconfig absent; no tenant API query attempted\n' "${scope}"
  fi
fi
