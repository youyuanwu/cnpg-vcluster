#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/platform.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenant.sh"

preflight() {
  [[ "${FORCE_PREFLIGHT_FAILURE:-0}" != 1 ]] \
    || die "forced preflight failure requested"

  require_command docker
  require_command curl
  require_command python3
  require_command sha256sum
  require_command kind
  require_command kubectl
  require_command helm
  require_command vcluster

  docker info >/dev/null
  [[ "$(docker info --format '{{.Driver}}')" == overlayfs ]] \
    || warn "Docker storage driver is not overlayfs"
  [[ -f /sys/fs/cgroup/cgroup.controllers ]] \
    || die "cgroup v2 is required for systemd worker containers"

  local cpus memory_bytes memory_gib free_kib free_gib
  cpus="$(docker info --format '{{.NCPU}}')"
  memory_bytes="$(docker info --format '{{.MemTotal}}')"
  memory_gib=$((memory_bytes / 1024 / 1024 / 1024))
  free_kib="$(df -Pk "$(docker info --format '{{.DockerRootDir}}')" | awk 'NR == 2 {print $4}')"
  free_gib=$((free_kib / 1024 / 1024))

  (( cpus >= MIN_DOCKER_CPUS )) \
    || die "Docker exposes ${cpus} CPUs; ${MIN_DOCKER_CPUS} required"
  (( memory_gib >= MIN_DOCKER_MEMORY_GIB )) \
    || die "Docker exposes ${memory_gib} GiB; ${MIN_DOCKER_MEMORY_GIB} GiB required"
  (( free_gib >= MIN_FREE_DISK_GIB )) \
    || die "Docker filesystem has ${free_gib} GiB free; ${MIN_FREE_DISK_GIB} GiB required"

  curl -sS --max-time 15 -o /dev/null https://admin.loft.sh/ \
    || die "vCluster Platform licensing endpoint is unreachable"
}

host_cluster_ready() {
  [[ -s "${HOST_KUBECONFIG}" ]] \
    && kubectl_host get --raw=/readyz >/dev/null 2>&1
}

ensure_host_cluster() {
  if host_cluster_ready; then
    log "kind control cluster ${KIND_CLUSTER_NAME} already ready"
    return
  fi

  if kind get clusters | grep -qx "${KIND_CLUSTER_NAME}"; then
    kind export kubeconfig --name "${KIND_CLUSTER_NAME}" --kubeconfig "${HOST_KUBECONFIG}"
  else
    log "creating kind control cluster ${KIND_CLUSTER_NAME}"
    kind create cluster \
      --name "${KIND_CLUSTER_NAME}" \
      --config "${REPO_ROOT}/config/kind.yaml" \
      --image "${KIND_NODE_IMAGE}" \
      --kubeconfig "${HOST_KUBECONFIG}" \
      --wait "${KIND_TIMEOUT}"
  fi
  chmod 0600 "${HOST_KUBECONFIG}"
  retry_for "${KIND_TIMEOUT}" "kind API" host_cluster_ready
  kubectl_host auth can-i create clusterrole | grep -qx yes \
    || die "kind kubeconfig does not have cluster-admin privileges"
}

main() {
  ensure_runtime_layout
  preflight
  rm -f "${RUNTIME_DIR}/blocker"
  ensure_host_cluster
  ensure_platform
  ensure_tenant_control_planes
  ensure_all_workers
  ensure_tenant_workers

  for tenant in ${TENANT_NAMES}; do
    platform_has_tenant "${tenant}" \
      || die "${tenant} is not linked to vCluster Platform"
  done

  if [[ "${SKIP_CNPG:-0}" == 1 ]]; then
    log "bootstrap complete; SKIP_CNPG=1 omitted database deployment"
    return
  fi

  log "private-node bootstrap complete; Phase 3 installs CloudNativePG"
}

main "$@"
