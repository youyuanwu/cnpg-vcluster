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

cleanup_spike_resources() {
  delete_spike_storage_smoke
  delete_spike_node
  remove_worker_join_material
  remove_spike_worker_and_volume
  if [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
    && management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    delete_spike_tenant
  else
    rm -f "$(tenant_kubeconfig spike)"
  fi
  rm -rf "${SPIKE_RUNTIME_DIR}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  cleanup_spike_resources
  log "spike resources are absent"
fi
