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
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/destroy-spike.sh"

final_result=error
final_blocker_code=""
final_blocker_prerequisite=""
final_blocker_message=""
final_cleanup_proved=false
final_tenant_a_endpoint=""
final_tenant_b_endpoint=""
final_tenant_a_ca_sha256=""
final_tenant_b_ca_sha256=""
existing_final_tenants=""
final_cnpg_state=installed

clear_owned_compatibility_blocker() {
  if [[ -f "${BLOCKER_FILE}" ]] \
    && grep -Eq '^owner=(spike|final)$' "${BLOCKER_FILE}"; then
    rm -f "${BLOCKER_FILE}"
  fi
}

record_final_blocker() {
  ensure_runtime_layout
  {
    printf 'owner=final\n'
    printf 'code=%s\n' "${final_blocker_code}"
    printf 'prerequisite=%s\n' "${final_blocker_prerequisite}"
    printf 'message=%s\n' "${final_blocker_message}"
  } | write_secret_file "${BLOCKER_FILE}"
}

write_final_result() {
  ensure_runtime_layout
  {
    printf 'result=%s\n' "${final_result}"
    printf 'compatibility_revision=%s\n' "${COMPATIBILITY_REVISION}"
    printf 'first_failing_prerequisite=%s\n' \
      "${final_blocker_prerequisite:-none}"
    printf 'blocker_code=%s\n' "${final_blocker_code:-none}"
    printf 'blocker_evidence=%s\n' "${final_blocker_message:-none}"
    printf 'cnpg=%s\n' "${final_cnpg_state}"
    printf 'tenant_a_schema=%s\n' "${TENANT_A_SCHEMA}"
    printf 'tenant_b_schema=%s\n' "${TENANT_B_SCHEMA}"
    printf 'expected_workers=6\n'
    printf 'tenant_a_endpoint=%s\n' "${final_tenant_a_endpoint:-not-observed}"
    printf 'tenant_b_endpoint=%s\n' "${final_tenant_b_endpoint:-not-observed}"
    printf 'tenant_a_ca_sha256=%s\n' "${final_tenant_a_ca_sha256:-not-observed}"
    printf 'tenant_b_ca_sha256=%s\n' "${final_tenant_b_ca_sha256:-not-observed}"
    if [[ "${final_cleanup_proved}" == true ]]; then
      printf 'cleanup=proved\n'
      printf 'final_tenants=absent\n'
      printf 'final_workers=absent\n'
      printf 'final_volumes=absent\n'
      printf 'final_runtime=absent\n'
    elif [[ "${final_result}" == pass || "${final_result}" == partial ]]; then
      printf 'cleanup=not-required\n'
    elif [[ "${final_result}" == error ]]; then
      printf 'cleanup=not-attempted\n'
    else
      printf 'cleanup=failed\n'
    fi
  } | write_secret_file "${FINAL_RESULT_FILE}"
}

cleanup_final_topology() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    final_tenant_existed_at_start "${tenant}" && continue
    delete_final_tenant_smoke "${tenant}"
    remove_tenant_workers "${tenant}"
  done
  for tenant in ${TENANT_NAMES}; do
    final_tenant_existed_at_start "${tenant}" && continue
    delete_final_tenant_control_plane "${tenant}" true
  done
  for tenant in ${TENANT_NAMES}; do
    final_tenant_existed_at_start "${tenant}" && continue
    verify_final_tenant_cleanup_absent "${tenant}"
  done
}

finish_final_create() {
  local status=$?
  local oom_evidence=""
  trap - EXIT
  if [[ "${status}" -eq "${EXIT_BLOCKED}" ]]; then
    set +e
    if (cleanup_final_topology); then
      final_cleanup_proved=true
      final_result=blocked
      record_final_blocker
    else
      final_result=cleanup-failed
      final_blocker_prerequisite=cleanup
      final_blocker_message="final compatibility cleanup did not remove every owned tenant resource"
      status="${EXIT_ERROR}"
    fi
    write_final_result
    set -e
  elif [[ "${status}" -eq "${EXIT_SUCCESS}" ]]; then
    if [[ "${final_cnpg_state}" == installed ]]; then
      final_result=pass
    else
      final_result=partial
    fi
    clear_owned_compatibility_blocker
    write_final_result
  else
    final_result=error
    final_blocker_prerequisite="${final_blocker_prerequisite:-create}"
    oom_evidence="$(all_tenant_control_plane_oom_evidence || true)"
    if [[ -n "${oom_evidence}" ]]; then
      final_blocker_message="tenant control-plane OOMKilled: ${oom_evidence//$'\n'/; }"
      warn "${final_blocker_message}"
    else
      final_blocker_message="${final_blocker_message:-final reconciliation failed; inspect diagnostics}"
    fi
    write_final_result
  fi
  exit "${status}"
}

blocked_final() {
  final_blocker_code="$1"
  final_blocker_prerequisite="$2"
  shift 2
  final_blocker_message="$*"
  [[ " ${COMPATIBILITY_BLOCKER_CODES} " == *" ${final_blocker_code} "* \
    && -n "${final_blocker_message}" ]] \
    || die "final.compatibility: refusing unclassified exit 2"
  exit "${EXIT_BLOCKED}"
}

classify_kube_proxy_failure() {
  local tenant="$1"
  if final_tenant_kube_proxy_procfs_blocked "${tenant}"; then
    final_blocker_code=cni-konnectivity
    final_blocker_message="${tenant} kube-proxy has the recognized read-only nf_conntrack_max compatibility failure"
    return 0
  fi
  return 1
}

classify_addon_failure() {
  classify_kube_proxy_failure "$1"
}

classify_worker_failure() {
  [[ "${FINAL_WORKER_FAILURE_RECOGNIZED}" == true \
    && " ${COMPATIBILITY_BLOCKER_CODES} " == *" ${FINAL_WORKER_FAILURE_CODE} "* \
    && -n "${FINAL_WORKER_FAILURE_EVIDENCE}" ]] \
    || return 1
  final_blocker_code="${FINAL_WORKER_FAILURE_CODE}"
  final_blocker_message="${FINAL_WORKER_FAILURE_EVIDENCE}"
}

classify_topology_failure() {
  return 1
}

classify_capacity_failure() {
  return 1
}

handle_classified_failure() {
  local classifier="$1"
  local tenant="$2"
  local prerequisite="$3"
  local ordinary_message="$4"
  if "${classifier}" "${tenant}"; then
    if [[ "${tenant}" != all ]] && final_tenant_existed_at_start "${tenant}"; then
      die "${ordinary_message}; recognized evidence was retained but exit 2 cleanup is forbidden for a pre-existing tenant"
    fi
    blocked_final "${final_blocker_code}" "${prerequisite}" \
      "${final_blocker_message}"
  fi
  die "${ordinary_message}"
}

compatibility_result_is_current_pass() {
  [[ -f "${SPIKE_RESULT_FILE}" ]] \
    && grep -Fxq 'result=pass' "${SPIKE_RESULT_FILE}" \
    && grep -Fxq "compatibility_revision=${COMPATIBILITY_REVISION}" \
      "${SPIKE_RESULT_FILE}" \
    && grep -Fxq 'cleanup=proved' "${SPIKE_RESULT_FILE}"
}

any_final_tenant_exists() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    final_tenant_exists "${tenant}" && return 0
  done
  return 1
}

capture_existing_final_tenant_state() {
  local tenant
  if [[ ! -f "${MANAGEMENT_KUBECONFIG}" ]] \
    || ! management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    return
  fi
  if ! any_final_tenant_exists; then
    return
  fi
  [[ -f "${MANAGEMENT_NETWORK_FILE}" ]] \
    || die "final.repeat-create: management network state is absent"
  load_management_network
  for tenant in ${TENANT_NAMES}; do
    if final_tenant_exists "${tenant}"; then
      if [[ ! -f "$(tenant_kubeconfig "${tenant}")" ]] \
        || ! tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
        export_tenant_kubeconfig "${tenant}"
      fi
      [[ "$(stat -c '%a' "$(tenant_kubeconfig "${tenant}")")" == 600 ]] \
        || die "final.repeat-create: ${tenant} kubeconfig is not mode 0600"
      final_tenant_tcp_ready "${tenant}" \
        || die "final.repeat-create: ${tenant} control plane is not Ready"
      tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
        || die "final.repeat-create: ${tenant} must already be paused with remediation ${COMPATIBILITY_REVISION} and conntrack.maxPerCore=${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}"
      existing_final_tenants+=" ${tenant} "
    fi
  done
}

final_tenant_existed_at_start() {
  [[ "${existing_final_tenants}" == *" $1 "* ]]
}

ensure_current_compatibility_decision() {
  local status code evidence
  if compatibility_result_is_current_pass; then
    log "reusing compatibility decision ${COMPATIBILITY_REVISION}"
    return
  fi
  if any_final_tenant_exists; then
    die "final.compatibility: final tenants exist without a current passing compatibility decision"
  fi
  set +e
  "${SCRIPT_DIR}/create-spike.sh"
  status=$?
  set -e
  if [[ "${status}" -eq "${EXIT_BLOCKED}" ]]; then
    code="$(sed -n 's/^code=//p' "${BLOCKER_FILE}" 2>/dev/null || true)"
    evidence="$(sed -n 's/^message=//p' "${BLOCKER_FILE}" 2>/dev/null || true)"
    blocked_final "${code:-worker-substrate}" compatibility-decision \
      "${evidence:-worker compatibility decision remained blocked}"
  fi
  [[ "${status}" -eq "${EXIT_SUCCESS}" ]] \
    || die "final.compatibility: compatibility decision failed with exit ${status}"
  compatibility_result_is_current_pass \
    || die "final.compatibility: passing decision lacks current cleanup evidence"
}

validate_exact_final_topology() {
  local tcp_json worker_count volume_count tenant other name
  tcp_json="$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
    --all-namespaces -o json)"
  TCP_JSON="${tcp_json}" \
  TENANT_A_NAMESPACE="${TENANT_A_NAMESPACE}" \
  TENANT_B_NAMESPACE="${TENANT_B_NAMESPACE}" \
  TENANT_A_SCHEMA="${TENANT_A_SCHEMA}" \
  TENANT_B_SCHEMA="${TENANT_B_SCHEMA}" \
  python3 -c '
import json, os
items=json.loads(os.environ["TCP_JSON"]).get("items",[])
expected={
 ("tenant-a",os.environ["TENANT_A_NAMESPACE"],os.environ["TENANT_A_SCHEMA"]),
 ("tenant-b",os.environ["TENANT_B_NAMESPACE"],os.environ["TENANT_B_SCHEMA"]),
}
actual={(i["metadata"]["name"],i["metadata"]["namespace"],i["spec"]["dataStoreSchema"])
        for i in items}
assert actual == expected
'
  worker_count="$(docker ps -aq --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker' | wc -l)"
  volume_count="$(docker volume ls -q --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker-var-lib' | wc -l)"
  [[ "${worker_count}" -eq 6 && "${volume_count}" -eq 6 ]] \
    || die "final.topology: expected six owned workers and six owned volumes"
  validate_disjoint_worker_sets

  [[ "$(management_kubectl get nodes --no-headers | wc -l)" -eq 1 ]] \
    || die "final.isolation: management API contains tenant workers"
  for tenant in ${TENANT_NAMES}; do
    other=tenant-a
    [[ "${tenant}" == tenant-a ]] && other=tenant-b
    for name in $(tenant_kubectl "${tenant}" get nodes -o name | sed 's#node/##'); do
      ! tenant_kubectl "${other}" get node "${name}" >/dev/null 2>&1 \
        || die "final.isolation: ${name} appears in both tenants"
    done
    [[ "$(tenant_kubectl "${other}" get pvc --all-namespaces \
      -l "kamaji.cnpg-vcluster.io/tenant=${tenant}" --no-headers \
      2>/dev/null | wc -l)" -eq 0 ]] \
      || die "final.isolation: ${tenant} storage appears in ${other}"
    tenant_kube_proxy_conntrack_is_zero "${tenant}" \
      || die "final.kube-proxy: ${tenant} conntrack remediation is absent"
    [[ "$(tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
      -o jsonpath='{.status.phase}')" == Bound ]] \
      || die "final.storage: ${tenant} smoke PVC is not Bound"
  done
  verify_tenant_identity_separation
  verify_no_spike_residuals
}

require_exact_just
if [[ "${KAMAJI_TEST_SKIP_PREFLIGHT:-0}" != 1 ]]; then
  "${SCRIPT_DIR}/preflight.sh"
fi
require_host_inotify_capacity
clear_owned_compatibility_blocker
trap finish_final_create EXIT
capture_existing_final_tenant_state
reconcile_management_plane
load_management_network
cleanup_spike_resources
verify_no_spike_residuals
ensure_current_compatibility_decision
cleanup_spike_resources
verify_no_spike_residuals
verify_initial_final_identities_free

for tenant in ${TENANT_NAMES}; do
  log "reconciling ${tenant} control plane"
  reconcile_final_tenant "${tenant}"
  if final_tenant_existed_at_start "${tenant}"; then
    tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
      || die "final.repeat-create: ${tenant} pause or kube-proxy remediation changed during reconciliation"
  else
    if ! (configure_tenant_kube_proxy_conntrack "${tenant}"); then
      handle_classified_failure classify_kube_proxy_failure "${tenant}" \
        "${tenant}-kube-proxy-remediation" \
        "${tenant}.kube-proxy: remediation failed without recognized compatibility evidence"
    fi
  fi
done
verify_tenant_identity_separation \
  || die "tenant.identity: endpoint or certificate separation validation failed"
final_tenant_a_endpoint="$(tenant_kubectl tenant-a config view --raw \
  -o jsonpath='{.clusters[0].cluster.server}')"
final_tenant_b_endpoint="$(tenant_kubectl tenant-b config view --raw \
  -o jsonpath='{.clusters[0].cluster.server}')"
final_tenant_a_ca_sha256="$(tenant_ca_fingerprint tenant-a)"
final_tenant_b_ca_sha256="$(tenant_ca_fingerprint tenant-b)"

for tenant in ${TENANT_NAMES}; do
  log "reconciling ${WORKERS_PER_TENANT} workers for ${tenant}"
  if ! reconcile_tenant_workers "${tenant}"; then
    handle_classified_failure classify_worker_failure "${tenant}" \
      "${tenant}-workers" \
      "${tenant}.workers: reconciliation failed without recognized compatibility evidence: ${FINAL_WORKER_FAILURE_EVIDENCE:-no observed failure evidence}"
  fi
done

for tenant in ${TENANT_NAMES}; do
  log "installing networking and storage for ${tenant}"
  if ! (install_final_tenant_addons "${tenant}"); then
    handle_classified_failure classify_addon_failure "${tenant}" \
      "${tenant}-addons" \
      "${tenant}.addons: ordinary readiness, API, or storage failure; retained all tenant resources for diagnosis"
  fi
  if ! validate_final_worker_request_capacity "${tenant}"; then
    handle_classified_failure classify_capacity_failure "${tenant}" \
      "${tenant}-capacity" \
      "${tenant}.capacity: effective scheduled pod requests exceed an owned worker Docker cap"
  fi
done

if [[ "${SKIP_CNPG:-0}" != 1 ]]; then
  install_all_cnpg
  for tenant in ${TENANT_NAMES}; do
    validate_final_worker_request_capacity "${tenant}" \
      || die "${tenant}.capacity: scheduled add-on and PostgreSQL requests exceed the owned worker Docker caps"
  done
else
  final_cnpg_state=skipped
  log "SKIP_CNPG=1: leaving tenant CloudNativePG resources unchanged"
fi

if ! (validate_exact_final_topology); then
  handle_classified_failure classify_topology_failure all final-topology \
    "final.topology: exact two-TCP, two-schema, six-worker isolation or storage topology did not validate"
fi
for tenant in ${TENANT_NAMES}; do
  final_tenant_tcp_ready "${tenant}" \
    || die "${tenant}.control-plane: did not remain Ready while reconciliation was paused"
  tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
    || die "${tenant}.kube-proxy: expected paused steady state or conntrack remediation is absent"
  tenant_workers_ready "${tenant}" \
    && final_tenant_deployment_ready "${tenant}" kube-system coredns \
    && final_tenant_daemonset_ready "${tenant}" kube-system kube-proxy \
    && final_tenant_daemonset_ready "${tenant}" kube-system konnectivity-agent \
    && final_tenant_calico_ready "${tenant}" \
    && final_tenant_local_path_ready "${tenant}" \
    && [[ "$(tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
      -o jsonpath='{.status.phase}')" == Bound ]] \
    || die "${tenant}.health: workers, add-ons, or storage are unhealthy in the paused steady state"
done
if [[ "${SKIP_CNPG:-0}" != 1 ]]; then
  log "two tenant control planes, six exclusive workers, tenant add-ons, and both three-instance PostgreSQL clusters are healthy; Kamaji reconciliation remains intentionally paused"
else
  log "two tenant control planes, six exclusive workers, and tenant add-ons are healthy; CNPG was skipped and Kamaji reconciliation remains intentionally paused"
fi
