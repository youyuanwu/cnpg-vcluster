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

other_final_tenant() {
  case "$1" in
    tenant-a) printf 'tenant-b\n' ;;
    tenant-b) printf 'tenant-a\n' ;;
    *) die "destroy-tenant expects tenant-a or tenant-b" ;;
  esac
}

tenant_management_secrets() {
  local tenant="$1"
  local namespace
  namespace="$(tenant_namespace "${tenant}")"
  management_kubectl get namespace "${namespace}" >/dev/null 2>&1 || return 0
  management_kubectl -n "${namespace}" get secrets -o json 2>/dev/null \
    | TENANT_NAME="${tenant}" python3 -c '
import json, os, sys
for item in json.load(sys.stdin).get("items", []):
    owners=item.get("metadata", {}).get("ownerReferences", [])
    if any(owner.get("kind") == "TenantControlPlane"
           and owner.get("name") == os.environ["TENANT_NAME"] for owner in owners):
        print(item["metadata"]["name"])
'
}

ensure_tenant_kubeconfig_if_possible() {
  local tenant="$1"
  final_tenant_exists "${tenant}" || return 0
  if [[ ! -f "$(tenant_kubeconfig "${tenant}")" ]] \
    || ! tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
    export_tenant_kubeconfig "${tenant}"
  fi
}

delete_tenant_test_resources() {
  local tenant="$1"
  [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
    && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1 \
    || return 0
  tenant_kubectl "${tenant}" -n default delete pod \
    "${TENANT_NETWORK_SMOKE_POD}" "${TENANT_SMOKE_POD}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" -n default delete pvc "${TENANT_SMOKE_PVC}" \
    --ignore-not-found --wait=true --timeout="${TENANT_STORAGE_TIMEOUT}" \
    >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" -n kube-system delete \
    role/kubeadm:bootstrap-config-reader \
    rolebinding/kubeadm:bootstrap-config-reader \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}

verify_removed_tenant_absent() {
  local tenant="$1"
  local require_datastore_proof="${2:-true}"
  local namespace schema user secret claims
  namespace="$(tenant_namespace "${tenant}")"
  schema="$(tenant_schema "${tenant}")"
  user="$(tenant_datastore_user "${tenant}")"
  ! final_tenant_exists "${tenant}" \
    || die "${tenant}.cleanup: TenantControlPlane remains"
  management_namespace_absent "${namespace}" \
    || die "${tenant}.cleanup: management namespace remains or could not be inspected"
  if [[ "${require_datastore_proof}" == true ]]; then
    management_datastore_available \
      || die "${tenant}.cleanup: shared datastore is unavailable during cleanup proof"
    require_probe_state absent "${tenant}.cleanup: DataStore/default status.usedBy" \
      datastore_used_by_tenant_state "${tenant}"
    require_probe_state absent "${tenant}.cleanup: exact datastore schema" \
      etcd_prefix_state "${schema}"
    require_probe_state absent "${tenant}.cleanup: exact datastore user" \
      etcd_user_state "${user}"
    require_probe_state absent "${tenant}.cleanup: exact datastore role" \
      etcd_role_state "${schema}"
  fi
  while IFS= read -r secret; do
    [[ -z "${secret}" ]] \
      || die "${tenant}.cleanup: tenant-owned Secret ${secret} remains"
  done < <(tenant_management_secrets "${tenant}")
  [[ -z "$(docker ps -aq --filter "$(owned_docker_filter)" \
    --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}")" ]] \
    || die "${tenant}.cleanup: owned worker container remains"
  [[ -z "$(docker volume ls -q --filter "$(owned_docker_filter)" \
    --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}")" ]] \
    || die "${tenant}.cleanup: owned worker volume remains"
  [[ ! -e "$(tenant_runtime_dir "${tenant}")" ]] \
    || die "${tenant}.cleanup: runtime subtree remains"
  claims="$(services_claiming_vip "$(tenant_vip "${tenant}")")"
  [[ -z "${claims}" ]] \
    || die "${tenant}.cleanup: API VIP remains claimed by ${claims//$'\n'/,}"
}

verify_surviving_tenant_health() {
  local tenant="$1"
  local namespace kubeconfig_secret credential_secret ordinal
  namespace="$(tenant_namespace "${tenant}")"
  ensure_tenant_kubeconfig_if_possible "${tenant}"
  final_tenant_tcp_ready "${tenant}" \
    && tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
    || die "${tenant}.survivor: TenantControlPlane pause/remediation health failed"
  [[ -f "$(tenant_kubeconfig "${tenant}")" \
    && "$(stat -c '%a' "$(tenant_kubeconfig "${tenant}")")" == 600 ]] \
    && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1 \
    || die "${tenant}.survivor: API identity is absent or unreachable"
  kubeconfig_secret="$(management_kubectl -n "${namespace}" \
    get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.kubeconfig.admin.secretName}')"
  credential_secret="$(management_kubectl -n "${namespace}" \
    get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.storage.config.secretName}')"
  [[ -n "${kubeconfig_secret}" && -n "${credential_secret}" ]] \
    && management_kubectl -n "${namespace}" get secret \
      "${kubeconfig_secret}" "${credential_secret}" >/dev/null \
    || die "${tenant}.survivor: kubeconfig or datastore credentials are absent"
  datastore_used_by_tenant "${tenant}" \
    && etcd_prefix_exists "$(tenant_schema "${tenant}")" \
    && etcd_user_exists "$(tenant_datastore_user "${tenant}")" \
    && etcd_role_exists "$(tenant_schema "${tenant}")" \
    || die "${tenant}.survivor: datastore identity is incomplete"
  tenant_workers_ready "${tenant}" \
    && cnpg_tenant_ready "${tenant}" \
    && cnpg_verify_marker_if_present "${tenant}" \
    || die "${tenant}.survivor: workers, PostgreSQL, or marker are unhealthy"
  for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
    final_worker_current "${tenant}" "${ordinal}" \
      || die "${tenant}.survivor: worker ${ordinal} container identity drifted"
  done
}

destroy_one_tenant() {
  local tenant="$1"
  local verify_survivor="${2:-true}"
  local cleanup_mode="${3:-targeted}"
  local survivor survivor_present=false
  survivor="$(other_final_tenant "${tenant}")"

  if ! ensure_owned_management_access; then
    remove_tenant_workers "${tenant}"
    rm -rf "$(tenant_runtime_dir "${tenant}")"
    return 0
  fi
  [[ -f "${MANAGEMENT_NETWORK_FILE}" ]] \
    || die "management network assignment is absent"
  load_management_network
  if [[ "${cleanup_mode}" == targeted ]]; then
    require_management_datastore_inspection "${tenant}"
  fi
  ensure_tenant_kubeconfig_if_possible "${tenant}"
  if final_tenant_exists "${survivor}"; then
    survivor_present=true
    ensure_tenant_kubeconfig_if_possible "${survivor}"
  fi

  delete_cnpg_for_tenant "${tenant}"
  delete_tenant_test_resources "${tenant}"
  remove_tenant_workers "${tenant}"
  delete_final_tenant_control_plane "${tenant}" \
    "$([[ "${cleanup_mode}" == targeted ]] && printf true || printf false)"
  verify_removed_tenant_absent "${tenant}" \
    "$([[ "${cleanup_mode}" == targeted ]] && printf true || printf false)"
  if [[ "${verify_survivor}" == true && "${survivor_present}" == true ]]; then
    verify_surviving_tenant_health "${survivor}"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  require_exact_just
  [[ "$#" -eq 1 ]] || die "usage: destroy-tenant.sh tenant-a|tenant-b"
  destroy_one_tenant "$1" true
  log "removed exactly $1 and verified the surviving tenant"
fi
