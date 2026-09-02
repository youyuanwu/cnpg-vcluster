#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"

delete_tenant_workloads() {
  local tenant="$1"
  local kubeconfig
  kubeconfig="$(tenant_kubeconfig "${tenant}")"
  if [[ ! -s "${kubeconfig}" ]]; then
    return 0
  fi

  KUBECONFIG="${kubeconfig}" kubectl delete \
    -f "${REPO_ROOT}/manifests/cnpg/cluster-${tenant}.yaml" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  KUBECONFIG="${kubeconfig}" kubectl delete \
    -f "${REPO_ROOT}/manifests/cnpg/operator.yaml" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}

reset_and_remove_workers() {
  local tenant index name
  for tenant in ${TENANT_NAMES}; do
    for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
      name="$(worker_name "${tenant}" "${index}")"
      if docker container inspect "${name}" >/dev/null 2>&1; then
        if ! worker_container_owned "${tenant}" "${name}"; then
          warn "refusing to remove same-named unowned container: ${name}"
          continue
        fi
        docker exec "${name}" test -x /root/vcluster-join.sh >/dev/null 2>&1 \
          && docker exec "${name}" /root/vcluster-join.sh --reset-only \
            >"${RUNTIME_DIR}/logs/${name}-reset.log" 2>&1 || true
        docker rm -f "${name}" >/dev/null
      fi
      if docker volume inspect "${name}-var-lib" >/dev/null 2>&1; then
        if worker_volume_owned "${tenant}" "${name}-var-lib"; then
          docker volume rm "${name}-var-lib" >/dev/null 2>&1 || true
        else
          warn "refusing to remove same-named unowned volume: ${name}-var-lib"
        fi
      fi
    done
  done

  local owned
  while IFS= read -r owned; do
    [[ -n "${owned}" ]] || continue
    docker rm -f "${owned}" >/dev/null
  done < <(docker ps -aq \
    --filter "label=cnpg-vcluster.lab=${LAB_PREFIX}" \
    --filter "label=cnpg-vcluster.role=private-worker")
  while IFS= read -r owned; do
    [[ -n "${owned}" ]] || continue
    docker volume rm "${owned}" >/dev/null 2>&1 || true
  done < <(docker volume ls -q --filter "label=cnpg-vcluster.lab=${LAB_PREFIX}")
}

delete_tenant_control_plane() {
  local tenant="$1"
  local namespace
  namespace="$(tenant_namespace "${tenant}")"
  if [[ -s "${HOST_KUBECONFIG}" ]]; then
    KUBECONFIG="${HOST_KUBECONFIG}" \
      vcluster_cli delete "${tenant}" \
        --driver helm \
        --context "$(host_context)" \
        --namespace "${namespace}" \
        --delete-namespace \
        --ignore-not-found >/dev/null 2>&1 || true
    kubectl_host delete namespace "${namespace}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
}

main() {
  ensure_runtime_layout
  local tenant
  for tenant in ${TENANT_NAMES}; do
    delete_tenant_workloads "${tenant}"
  done
  reset_and_remove_workers
  for tenant in ${TENANT_NAMES}; do
    delete_tenant_control_plane "${tenant}"
  done

  if [[ -s "${HOST_KUBECONFIG}" ]]; then
    KUBECONFIG="${HOST_KUBECONFIG}" helm uninstall loft \
      --namespace "${PLATFORM_NAMESPACE}" >/dev/null 2>&1 || true
    kubectl_host delete namespace "${PLATFORM_NAMESPACE}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi

  kind delete cluster --name "${KIND_CLUSTER_NAME}" >/dev/null 2>&1 || true
  rm -rf "${RUNTIME_DIR}"
  log "removed ${LAB_PREFIX} tenants, workers, Platform, kind cluster, and runtime state"
}

main "$@"
