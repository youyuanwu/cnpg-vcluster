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

verify_spike_vip_is_free() {
  local claims
  if [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
    && management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    load_management_network
    claims="$(services_claiming_vip "${TENANT_A_VIP}")" \
      || die "spike.cleanup: unable to inspect borrowed VIP allocation"
    if final_tenant_exists tenant-a; then
      claims="$(grep -Fvx "${TENANT_A_NAMESPACE}/tenant-a" <<<"${claims}" || true)"
    fi
    [[ -z "${claims}" ]] \
      || die "spike.cleanup: borrowed VIP ${TENANT_A_VIP} remains claimed by ${claims//$'\n'/,}"
  elif [[ -x "${BIN_DIR}/kind" ]] \
    && "${BIN_DIR}/kind" get clusters 2>/dev/null | grep -Fxq "${KIND_CLUSTER_NAME}"; then
    die "spike.cleanup: management cluster exists but borrowed VIP release cannot be verified"
  fi
}

cleanup_spike_resources() {
  delete_spike_storage_smoke
  delete_spike_node
  remove_worker_join_material
  remove_spike_worker_and_volume
  if [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
    && management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    unpause_tenant_reconciliation spike
    delete_spike_tenant
  else
    rm -f "$(tenant_kubeconfig spike)"
  fi
  verify_spike_vip_is_free
  rm -rf "${SPIKE_RUNTIME_DIR}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  cleanup_spike_resources
  log "spike resources are absent"
fi
