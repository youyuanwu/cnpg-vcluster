#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/management.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenants.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/addons.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/cnpg.sh"

tenant="${1:-}"
case "${tenant}" in
  tenant-a|tenant-b) ;;
  *) die "usage: repair-tenant.sh tenant-a|tenant-b" ;;
esac

require_exact_just
"${SCRIPT_DIR}/preflight.sh"
require_host_inotify_capacity
ensure_owned_management_access
load_management_network
validate_final_tenant_ownership "${tenant}"
require_management_datastore_inspection "${tenant}"

marker_before=""
if [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
  && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
  relation="$(cnpg_run_sql "${tenant}" \
    "SELECT COALESCE(to_regclass('public.kamaji_verification')::text, 'absent');" \
    || true)"
  if [[ "$(tail -1 <<<"${relation}")" == kamaji_verification ]]; then
    cnpg_verify_marker "${tenant}" \
      || die "${tenant}.repair: existing verification marker is unhealthy before repair"
    marker_before="$(cnpg_database_marker "${tenant}")"
  fi
fi

unpause_tenant_reconciliation "${tenant}"
reconcile_final_tenant "${tenant}"
configure_tenant_kube_proxy_conntrack "${tenant}"
reconcile_tenant_workers "${tenant}" \
  || die "${tenant}.repair: worker reconciliation failed: ${FINAL_WORKER_FAILURE_EVIDENCE:-no evidence}"
install_final_tenant_addons "${tenant}" \
  || die "${tenant}.repair: add-on reconciliation did not converge"
install_cnpg_for_tenant "${tenant}"
validate_exact_tenant_node_set "${tenant}" >/dev/null
validate_final_worker_request_capacity "${tenant}"
tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
  || die "${tenant}.repair: pause/remediation state was not restored"
if [[ -n "${marker_before}" ]]; then
  cnpg_verify_marker "${tenant}" \
    || die "${tenant}.repair: existing database marker was not preserved"
fi
log "repaired owned ${tenant} control plane, workers, add-ons, and database without replacing healthy identities"
