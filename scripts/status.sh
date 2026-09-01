#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/platform.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/cnpg.sh"

healthy=0

fail_status() {
  printf 'UNHEALTHY: %s\n' "$*" >&2
  healthy=1
}

printf 'Pinned versions: kind=%s host-k8s=%s tenant-k8s=%s vcluster=%s platform=%s cnpg=%s postgres=%s\n' \
  "${KIND_VERSION}" "${KIND_NODE_IMAGE%%@*}" "${KUBERNETES_VERSION}" \
  "${VCLUSTER_VERSION}" "${PLATFORM_VERSION}" "${CNPG_VERSION}" \
  "${POSTGRES_IMAGE%%@*}"

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
  kubectl_tenant "${tenant}" -n kube-system get pods \
    -o wide | grep -E 'NAME|coredns|flannel|kube-proxy|local-path' || true
  kubectl_tenant "${tenant}" get storageclass

  if kubectl_tenant "${tenant}" get crd clusters.postgresql.cnpg.io >/dev/null 2>&1; then
    kubectl_tenant "${tenant}" -n cnpg-system get deployment cnpg-controller-manager
    kubectl_tenant "${tenant}" -n database get cluster,pods,pvc
    cnpg_cluster_ready "${tenant}" \
      || fail_status "${tenant} PostgreSQL cluster is not fully ready"
  else
    fail_status "${tenant} does not have the CloudNativePG CRDs"
  fi
done

exit "${healthy}"
