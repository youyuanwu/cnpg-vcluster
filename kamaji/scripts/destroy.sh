#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/host.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/management.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/destroy-spike.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/destroy-tenant.sh"

validate_owned_docker_inventory() {
  local tenant ordinal object expected_containers expected_volumes
  expected_containers="${SPIKE_WORKER_NAME}"
  expected_volumes="${SPIKE_VOLUME_NAME}"
  for tenant in ${TENANT_NAMES}; do
    for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
      expected_containers+=" $(worker_name "${tenant}" "${ordinal}")"
      expected_volumes+=" $(worker_volume_name "${tenant}" "${ordinal}")"
    done
  done
  while IFS= read -r object; do
    [[ -n "${object}" ]] || continue
    [[ " ${expected_containers} " == *" ${object} "* ]] \
      || die "cleanup.ownership-refusal: unexpected ownership-labelled container ${object}"
  done < <(docker ps -a --filter "$(owned_docker_filter)" --format '{{.Names}}')
  while IFS= read -r object; do
    [[ -n "${object}" ]] || continue
    [[ " ${expected_volumes} " == *" ${object} "* ]] \
      || die "cleanup.ownership-refusal: unexpected ownership-labelled volume ${object}"
  done < <(docker volume ls --filter "$(owned_docker_filter)" --format '{{.Name}}')
}

verify_no_owned_lab_resources() {
  [[ -z "$(docker ps -aq --filter "$(owned_docker_filter)")" ]] \
    || die "cleanup: owned Docker containers remain"
  [[ -z "$(docker volume ls -q --filter "$(owned_docker_filter)")" ]] \
    || die "cleanup: owned Docker volumes remain"
  ! kind_cluster_reported && ! management_container_exists \
    || die "cleanup: owned management cluster remains"
}

main() {
  require_exact_just
  local management_available=false
  validate_owned_docker_inventory
  if ensure_owned_management_access; then
    management_available=true
    cleanup_spike_resources
    destroy_one_tenant tenant-a false full
    destroy_one_tenant tenant-b false full
    destroy_kamaji_shared_resources
    destroy_metallb_shared_resources
    destroy_cert_manager_shared_resources
  else
    remove_spike_worker_and_volume
    rm -rf "${SPIKE_RUNTIME_DIR}"
    remove_tenant_workers tenant-a
    remove_tenant_workers tenant-b
  fi

  [[ -z "$(docker ps -aq --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker')" ]] \
    || die "cleanup: nested workers remain before host sysctl restoration"
  if [[ "${management_available}" == true ]] || management_state_present; then
    delete_owned_kind_cluster
  fi
  restore_recorded_inotify_values
  verify_no_owned_lab_resources
  rm -rf "${RUNTIME_DIR}"
  log "removed the owned Kamaji lab and restored recorded host inotify values"
}

main "$@"
