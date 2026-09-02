#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenants.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"

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
docker ps -a --filter "label=io.x-k8s.kind.cluster=${KIND_CLUSTER_NAME}" --format '{{json .}}'
docker ps -a --filter "$(owned_docker_filter)" --format '{{json .}}'
docker volume ls --filter "$(owned_docker_filter)" --format '{{.Name}}'

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
    printf '\n== %s management identity ==\n' "${tenant}"
    management_kubectl -n "$(tenant_namespace "${tenant}")" get \
      tenantcontrolplanes,deployments,pods,services,secrets,pvc -o wide \
      2>/dev/null || true
    printf '\n== %s Docker resources ==\n' "${tenant}"
    docker ps -a --filter "$(owned_docker_filter)" \
      --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}" \
      --format '{{json .}}'
    docker volume ls --filter "$(owned_docker_filter)" \
      --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}" \
      --format '{{.Name}}'
    printf '\n== %s API ==\n' "${tenant}"
    if [[ -f "$(tenant_kubeconfig "${tenant}")" ]]; then
      tenant_kubectl "${tenant}" get nodes -o wide 2>/dev/null || true
      tenant_kubectl "${tenant}" get pods,services,pvc --all-namespaces \
        -o wide 2>/dev/null || true
      tenant_kubectl "${tenant}" get storageclass,pv -o wide 2>/dev/null || true
      printf 'kube-proxy conntrack.maxPerCore='
      tenant_kubectl "${tenant}" -n kube-system get configmap kube-proxy \
        -o jsonpath='{.data.config\.conf}' 2>/dev/null \
        | sed -n 's/^  maxPerCore: //p' || true
      printf '\n'
      tenant_kubectl "${tenant}" get events --all-namespaces \
        --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -30 || true
    else
      printf '%s kubeconfig absent; no tenant API query attempted\n' "${tenant}"
    fi
  fi
done

printf '\n== final result evidence ==\n'
if [[ -f "${FINAL_RESULT_FILE}" ]]; then
  sed 's/^/  /' "${FINAL_RESULT_FILE}"
else
  printf 'none\n'
fi
