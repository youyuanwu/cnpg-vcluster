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
source "${SCRIPT_DIR}/destroy-spike.sh"

current_rung=preflight
result=error
blocker_code=""
blocker_message=""
observed_preflight_snapshot=none
join_failure_snapshot=none
persistence_snapshot=""
cleanup_proved=false

write_spike_result() {
  ensure_runtime_layout
  {
    printf 'result=%s\n' "${result}"
    printf 'first_failing_rung=%s\n' "$([[ "${result}" == pass ]] && printf none || printf '%s' "${current_rung}")"
    printf 'blocker_code=%s\n' "${blocker_code:-none}"
    printf 'blocker_evidence=%s\n' "${blocker_message:-none}"
    printf 'kubernetes_version=%s\n' "${KUBERNETES_VERSION}"
    printf 'spike_vip=%s\n' "${SPIKE_VIP:-unallocated}"
    printf 'kubeadm_observed_preflight=%s\n' "${observed_preflight_snapshot}"
    printf 'kubeadm_failure_evidence=%s\n' "${join_failure_snapshot}"
    printf 'kubeadm_ignore_allowlist="%s"\n' "${KUBEADM_IGNORE_PREFLIGHT_ERRORS}"
    if [[ -n "${persistence_snapshot}" ]]; then
      printf '%s\n' "${persistence_snapshot}"
    fi
    if [[ "${cleanup_proved}" == true ]]; then
      printf 'cleanup=proved\n'
      printf 'schema_cleanup=proved\n'
      printf 'credential_cleanup=proved\n'
      printf 'datastore_used_by_cleanup=proved\n'
      printf 'shared_datastore_healthy=proved\n'
    else
      printf 'cleanup=failed\n'
    fi
  } | write_secret_file "${SPIKE_RESULT_FILE}"
}

finish_spike() {
  local status=$?
  trap - EXIT
  set +e
  if [[ -f "${SPIKE_PREFLIGHT_EVIDENCE}" ]]; then
    observed_preflight_snapshot="$(observed_preflight_errors 2>/dev/null || true)"
    [[ -n "${observed_preflight_snapshot}" ]] || observed_preflight_snapshot=none
    join_failure_snapshot="$(join_failure_summary 2>/dev/null || true)"
    [[ -n "${join_failure_snapshot}" ]] || join_failure_snapshot=none
  fi
  if [[ -f "${SPIKE_PERSISTENCE_EVIDENCE}" ]]; then
    persistence_snapshot="$(cat "${SPIKE_PERSISTENCE_EVIDENCE}")"
  fi
  if (cleanup_spike_resources); then
    cleanup_proved=true
  else
    warn "spike cleanup failed"
    status="${EXIT_ERROR}"
    result=cleanup-failed
    current_rung=cleanup
  fi
  if [[ "${cleanup_proved}" != true ]]; then
    :
  elif [[ "${status}" -eq "${EXIT_SUCCESS}" ]]; then
    result=pass
    clear_owned_spike_blocker
  elif [[ "${status}" -eq "${EXIT_BLOCKED}" ]]; then
    result=blocked
    record_spike_blocker "${blocker_code}" "${blocker_message}"
  else
    result=error
  fi
  write_spike_result
  set -e
  exit "${status}"
}

blocked() {
  blocker_code="$1"
  current_rung="$2"
  shift 2
  blocker_message="$*"
  return "${EXIT_BLOCKED}"
}

require_exact_just
refuse_spike_with_final_state
clear_owned_spike_evidence
trap finish_spike EXIT
cleanup_spike_resources
reconcile_management_plane
load_management_network
SPIKE_VIP="${TENANT_A_VIP}"

current_rung=control-plane
if ! reconcile_spike_tenant; then
  die "spike.control-plane: ${KUBERNETES_VERSION} control plane did not become Ready"
fi
export_spike_kubeconfig
reconcile_spike_bootstrap_rbac
datastore_used_by_spike \
  || die "spike.control-plane: DataStore/default did not record the spike consumer"

current_rung=upstream-equivalent-join
if ! start_spike_worker false; then
  blocked worker-substrate "${current_rung}" \
    "privileged ${KIND_NODE_IMAGE} failed the upstream-equivalent substrate checks"
fi
if ! join_spike_worker; then
  blocked kubeadm-bootstrap "${current_rung}" \
    "upstream-equivalent privileged kindest/node kubeadm join failed: $(join_failure_summary 2>/dev/null || printf unavailable)"
fi
delete_spike_node
remove_spike_worker_container

current_rung=target-systemd-fixed-vip
if ! start_spike_worker true; then
  blocked worker-substrate "${current_rung}" \
    "target worker with Docker caps and persistent /var/lib failed substrate checks"
fi
if ! join_spike_worker || ! validate_target_worker_contract; then
  blocked kubeadm-bootstrap "${current_rung}" \
    "Kubernetes ${KUBERNETES_VERSION}, systemd kubelet, fixed VIP join failed"
fi

current_rung=cni-konnectivity
if ! install_spike_network_addons \
  || ! wait_for "${TENANT_ADDON_TIMEOUT}" "spike Ready node" spike_node_ready; then
  blocked cni-konnectivity "${current_rung}" \
    "$(spike_network_failure_summary)"
fi
label_spike_node
verify_spike_addon_images

current_rung=persistent-worker-storage
if ! install_spike_storage_addon \
  || ! create_spike_storage_writer \
  || ! validate_spike_allocatable; then
  blocked persistent-worker-storage "${current_rung}" \
    "default Local Path storage, bound PVC, or allocatable resource cap failed"
fi
delete_spike_storage_writer_pod
if ! recreate_persistent_spike_worker \
  || ! verify_spike_storage_reader; then
  blocked persistent-worker-storage "${current_rung}" \
    "persistent /var/lib value did not survive owned worker recreation"
fi

current_rung=complete
log "worker compatibility ladder passed"
