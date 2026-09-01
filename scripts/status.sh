#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/platform.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/cnpg.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenant.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"

healthy=0

fail_status() {
  printf 'UNHEALTHY: %s\n' "$*" >&2
  healthy=1
}

printf 'Pinned versions: kind=%s host-k8s=%s tenant-k8s=%s vcluster=%s platform=%s cnpg=%s postgres=%s\n' \
  "${KIND_VERSION}" "${KIND_NODE_IMAGE%%@*}" "${KUBERNETES_VERSION}" \
  "${VCLUSTER_VERSION}" "${PLATFORM_VERSION}" "${CNPG_VERSION}" \
  "${POSTGRES_IMAGE%%@*}"

printf '\n== Docker private workers ==\n'
docker ps -a --filter "label=cnpg-vcluster.lab=${LAB_PREFIX}" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Networks}}'
for tenant in ${TENANT_NAMES}; do
  for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
    name="$(worker_name "${tenant}" "${index}")"
    if ! docker container inspect "${name}" >/dev/null 2>&1; then
      fail_status "${name} is absent"
      continue
    fi
    worker_container_owned "${tenant}" "${name}" \
      || fail_status "${name} ownership labels do not match"
    if ! worker_prerequisites_ready "${name}"; then
      fail_status "${name} systemd/cgroup/iptables/swap prerequisites are unhealthy"
      continue
    fi
    worker_services_ready "${name}" \
      || fail_status "${name} containerd/kubelet/vcluster-vpn services are unhealthy"
    printf '%s: ' "${name}"
    docker exec "${name}" sh -c \
      'containerd --version 2>/dev/null; kubelet --version 2>/dev/null; vcluster version 2>/dev/null; systemctl show -p ExecStart --value vcluster-vpn 2>/dev/null' \
      | tr '\n' ';'
    printf '\n'
  done
done

if [[ -s "${RUNTIME_DIR}/blocker" ]]; then
  printf 'Bootstrap blocker:\n'
  sed 's/^/  /' "${RUNTIME_DIR}/blocker"
  healthy=1
fi

if [[ -s "${HOST_KUBECONFIG}" ]] && kubectl_host get --raw=/readyz >/dev/null 2>&1; then
  printf '\n== Central kind cluster ==\n'
  kubectl_host get nodes
else
  fail_status "central kind API is unavailable"
fi

if platform_ready; then
  printf '\n== vCluster Platform ==\n'
  kubectl_host -n "${PLATFORM_NAMESPACE}" get deployment loft
  kubectl_host -n "${PLATFORM_NAMESPACE}" get pods \
    -o custom-columns='NAME:.metadata.name,IMAGES:.spec.containers[*].image'
  platform_url >/dev/null && printf 'URL: %s\n' "$(platform_url)"
else
  fail_status "vCluster Platform is unavailable"
fi

for tenant in ${TENANT_NAMES}; do
  printf '\n== %s ==\n' "${tenant}"
  if [[ ! -s "$(tenant_kubeconfig "${tenant}")" ]] \
    || ! kubectl_tenant "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
    fail_status "${tenant} API is unavailable"
    continue
  fi

  kubectl_tenant "${tenant}" get nodes \
    -L cnpg-vcluster.io/tenant
  tenant_expected_nodes_ready "${tenant}" \
    || fail_status "${tenant} does not have exactly ${WORKERS_PER_TENANT} Ready labeled workers"
  platform_has_tenant "${tenant}" \
    || fail_status "${tenant} is not linked to vCluster Platform"
  kubectl_tenant "${tenant}" -n kube-system get pods \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,IMAGES:.spec.containers[*].image' \
    | grep -E 'NAME|coredns|flannel|kube-proxy|local-path' || true
  tenant_addons_ready "${tenant}" \
    || fail_status "${tenant} networking, DNS, or storage add-ons are unhealthy"
  kubectl_tenant "${tenant}" get storageclass
  kubectl_tenant "${tenant}" get services,pvc -A
  kubectl_tenant "${tenant}" get pv

  if kubectl_tenant "${tenant}" get crd clusters.postgresql.cnpg.io >/dev/null 2>&1; then
    kubectl_tenant "${tenant}" -n cnpg-system get deployment cnpg-controller-manager
    kubectl_tenant "${tenant}" -n database get cluster,pods,pvc
    kubectl_tenant "${tenant}" -n database get pods \
      -o custom-columns='NAME:.metadata.name,NODE:.spec.nodeName,IMAGES:.spec.containers[*].image'
    cnpg_cluster_ready "${tenant}" \
      || fail_status "${tenant} PostgreSQL cluster is not fully ready"
  else
    fail_status "${tenant} does not have the CloudNativePG CRDs"
  fi
done

exit "${healthy}"
