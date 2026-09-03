#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenants.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/addons.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/cnpg.sh"

health="${EXIT_SUCCESS}"

unhealthy() {
  printf 'unhealthy: %s\n' "$*" >&2
  health="${EXIT_ERROR}"
}

scope="${1:-all}"
case "${scope}" in
  all|management|spike|tenant-a|tenant-b) ;;
  *) die "diagnostic scope must be all, management, spike, tenant-a, or tenant-b" ;;
esac

printf '== tools ==\n'
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
docker ps -a --filter "label=io.x-k8s.kind.cluster=${KIND_CLUSTER_NAME}" --format '{{json .}}'
docker ps -a --filter "$(owned_docker_filter)" --format '{{json .}}'
docker volume ls --filter "$(owned_docker_filter)" --format '{{.Name}}'

printf '\n== host inotify ==\n'
printf 'max_user_instances=%s required=%s\n' \
  "$(read_inotify_value max_user_instances)" "${MIN_INOTIFY_INSTANCES}"
printf 'max_user_watches=%s required=%s\n' \
  "$(read_inotify_value max_user_watches)" "${MIN_INOTIFY_WATCHES}"
if [[ -f "${HOST_SYSCTL_STATE_FILE}" ]]; then
  printf 'original_values_recorded=yes mode=%s\n' "$(stat -c '%a' "${HOST_SYSCTL_STATE_FILE}")"
else
  printf 'original_values_recorded=no\n'
fi

printf '\n== runtime state (names and modes only) ==\n'
if [[ -d "${RUNTIME_DIR}" ]]; then
  find "${RUNTIME_DIR}" -printf '%M %P\n' | sort
else
  printf 'runtime directory absent\n'
fi

printf '\n== management ownership and network ==\n'
if [[ -f "${MANAGEMENT_OWNERSHIP_FILE}" ]]; then
  sed 's/^/  /' "${MANAGEMENT_OWNERSHIP_FILE}"
else
  printf 'ownership evidence absent\n'
fi
if [[ -f "${MANAGEMENT_NETWORK_FILE}" ]]; then
  sed 's/^/  /' "${MANAGEMENT_NETWORK_FILE}"
  load_management_network
else
  printf 'network assignment absent\n'
fi

printf '\n== blocker state ==\n'
if [[ -f "${BLOCKER_FILE}" ]]; then
  sed 's/^/  /' "${BLOCKER_FILE}"
else
  printf 'none\n'
fi

printf '\n== management Kubernetes ==\n'
if [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
  && management_kubectl get --raw=/readyz >/dev/null 2>&1; then
  management_kubectl get nodes -o wide
  management_kubectl -n cert-manager get deployments,pods,endpoints -o wide 2>/dev/null || true
  management_kubectl -n metallb-system get deployments,daemonsets,pods,endpoints,ipaddresspools,l2advertisements -o wide 2>/dev/null || true
  management_kubectl -n "${MANAGEMENT_NAMESPACE}" get deployments,statefulsets,pods,services,endpoints,pvc,certificates,issuers -o wide 2>/dev/null || true
  management_kubectl get datastores.kamaji.clastix.io -o wide 2>/dev/null || true
  management_kubectl get tenantcontrolplanes.kamaji.clastix.io --all-namespaces -o wide 2>/dev/null || true
  management_kubectl get mutatingwebhookconfigurations,validatingwebhookconfigurations \
    -l app.kubernetes.io/name=kamaji -o wide 2>/dev/null || true
  management_kubectl get events --all-namespaces \
    --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -50 || true
else
  printf 'management kubeconfig absent or API unreachable; no management API query attempted\n'
  printf 'Kamaji controller and datastore unavailable\n'
fi

if [[ "${scope}" == spike || "${scope}" == all ]]; then
  printf '\n== spike management resources ==\n'
  management_kubectl -n "${SPIKE_NAMESPACE}" get \
    tenantcontrolplanes,deployments,pods,services,secrets,pvc -o wide 2>/dev/null || true
  printf '\n== spike Docker resources ==\n'
  docker ps -a --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/tenant=spike' --format '{{json .}}'
  docker volume ls --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/tenant=spike' --format '{{.Name}}'
  printf '\n== spike result evidence ==\n'
  if [[ -f "${SPIKE_RESULT_FILE}" ]]; then
    sed 's/^/  /' "${SPIKE_RESULT_FILE}"
  else
    printf 'none\n'
  fi
  printf '\n== spike tenant API ==\n'
  if [[ -f "$(tenant_kubeconfig spike)" ]]; then
    tenant_kubectl spike get nodes -o wide 2>/dev/null || true
    tenant_kubectl spike get pods,services,pvc --all-namespaces -o wide 2>/dev/null || true
    tenant_kubectl spike get storageclass,pv -o wide 2>/dev/null || true
  else
    printf 'spike kubeconfig absent; no tenant API query attempted\n'
  fi
fi

for tenant in ${TENANT_NAMES}; do
  if [[ "${scope}" == all || "${scope}" == "${tenant}" ]]; then
    tenant_exists=false
    final_tenant_exists "${tenant}" && tenant_exists=true
    printf '\n== %s management identity ==\n' "${tenant}"
    management_kubectl -n "$(tenant_namespace "${tenant}")" get \
      tenantcontrolplanes,deployments,pods,services,secrets,pvc -o wide \
      2>/dev/null || true
    if [[ "${tenant_exists}" == true ]]; then
      printf 'TCP paused=%s remediation=%s\n' \
        "$(tenant_reconciliation_pause_value "${tenant}")" \
        "$(tenant_reconciliation_remediation_revision "${tenant}")"
      tenant_reconciliation_is_paused "${tenant}" \
        || unhealthy "${tenant} Kamaji reconciliation is not intentionally paused"
      [[ "$(tenant_reconciliation_remediation_revision "${tenant}")" \
        == "${COMPATIBILITY_REVISION}" ]] \
        || unhealthy "${tenant} kube-proxy remediation revision is absent or stale"
      final_tenant_tcp_ready "${tenant}" \
        || unhealthy "${tenant} TenantControlPlane is not Ready"
    else
      printf 'TCP absent\n'
      if [[ -f "${FINAL_RESULT_FILE}" ]] \
        && grep -Fxq 'result=pass' "${FINAL_RESULT_FILE}"; then
        unhealthy "${tenant} TenantControlPlane is absent despite passing final result"
      fi
    fi
    printf '\n== %s Docker resources ==\n' "${tenant}"
    docker ps -a --filter "$(owned_docker_filter)" \
      --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}" \
      --format '{{json .}}'
    docker volume ls --filter "$(owned_docker_filter)" \
      --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}" \
      --format '{{.Name}}'
    printf '\n== %s API ==\n' "${tenant}"
    if [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
      && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
      tenant_kubectl "${tenant}" get nodes -o wide 2>/dev/null || true
      tenant_kubectl "${tenant}" get pods,services,pvc --all-namespaces \
        -o wide 2>/dev/null || true
      tenant_kubectl "${tenant}" get storageclass,pv -o wide 2>/dev/null || true
      printf '\n== %s workers, add-ons, and storage ==\n' "${tenant}"
      printf 'kube-proxy conntrack.maxPerCore='
      tenant_kubectl "${tenant}" -n kube-system get configmap kube-proxy \
        -o jsonpath='{.data.config\.conf}' 2>/dev/null \
        | sed -n 's/^  maxPerCore: //p' || true
      printf '\n'
      for component in \
        "deployment kube-system coredns CoreDNS" \
        "daemonset kube-system kube-proxy kube-proxy" \
        "daemonset kube-system konnectivity-agent Konnectivity" \
        "daemonset kube-system calico-node Calico-node" \
        "deployment kube-system calico-kube-controllers Calico-controllers" \
        "deployment local-path-storage local-path-provisioner local-path"; do
        read -r kind component_namespace component_name component_label <<<"${component}"
        if [[ "${kind}" == deployment ]]; then
          desired="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
            get deployment "${component_name}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
          ready="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
            get deployment "${component_name}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
          component_ok=false
          final_tenant_deployment_ready "${tenant}" "${component_namespace}" "${component_name}" \
            && component_ok=true
        else
          desired="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
            get daemonset "${component_name}" -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || true)"
          ready="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
            get daemonset "${component_name}" -o jsonpath='{.status.numberReady}' 2>/dev/null || true)"
          component_ok=false
          final_tenant_daemonset_ready "${tenant}" "${component_namespace}" "${component_name}" \
            && component_ok=true
        fi
        printf '%s=%s/%s Ready\n' "${component_label}" "${ready:-0}" "${desired:-0}"
        [[ "${component_ok}" == true ]] \
          || unhealthy "${tenant} ${component_label} replicas are not Ready"
      done
      tenant_workers_ready "${tenant}" \
        || unhealthy "${tenant} workers are not all Ready"
      tenant_kube_proxy_conntrack_is_zero "${tenant}" \
        || unhealthy "${tenant} kube-proxy conntrack.maxPerCore is not ${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}"
      final_tenant_local_path_ready "${tenant}" \
        || unhealthy "${tenant} local-path storage is not ready or uniquely default"
      [[ "$(tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true)" == Bound ]] \
        || unhealthy "${tenant} smoke PVC is not Bound"
      printf '\n== %s CloudNativePG and PostgreSQL ==\n' "${tenant}"
      tenant_kubectl "${tenant}" get crd \
        -o name 2>/dev/null | grep 'postgresql.cnpg.io' || true
      tenant_kubectl "${tenant}" get \
        mutatingwebhookconfigurations,validatingwebhookconfigurations \
        2>/dev/null | grep cnpg || true
      tenant_kubectl "${tenant}" get clusterroles,clusterrolebindings \
        2>/dev/null | grep cnpg || true
      tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" get \
        deployments,pods,services,endpoints,serviceaccounts -o wide 2>/dev/null || true
      tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get \
        clusters.postgresql.cnpg.io,pods,services,endpoints,pvc -o wide \
        2>/dev/null || true
      tenant_kubectl "${tenant}" get pv -o wide 2>/dev/null || true
      tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" get events \
        --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -30 || true
      tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get events \
        --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -50 || true
      cnpg_operator_ready "${tenant}" \
        || unhealthy "${tenant} CloudNativePG CRDs, webhooks, RBAC, or operator are not ready"
      cnpg_tenant_ready "${tenant}" \
        || unhealthy "${tenant} PostgreSQL cluster, instances, placements, or storage are not ready"
      tenant_kubectl "${tenant}" get events --all-namespaces \
        --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -30 || true
    else
      printf '%s kubeconfig absent; no tenant API query attempted\n' "${tenant}"
      printf 'TenantControlPlane endpoint, workers, add-ons, storage, CloudNativePG operator, and PostgreSQL cluster are absent or unavailable\n'
      [[ "${tenant_exists}" == false ]] \
        || unhealthy "${tenant} API is unreachable"
    fi
  fi
done

printf '\n== final result evidence ==\n'
if [[ -f "${FINAL_RESULT_FILE}" ]]; then
  sed 's/^/  /' "${FINAL_RESULT_FILE}"
else
  printf 'none\n'
fi

exit "${health}"
