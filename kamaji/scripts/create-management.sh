#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/management.sh"

require_exact_just
"${SCRIPT_DIR}/preflight.sh"
reconcile_management_plane

tcp_count="$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
  --all-namespaces --no-headers 2>/dev/null | wc -l)"
worker_count="$(docker ps -aq --filter "$(owned_docker_filter)" | wc -l)"
[[ "${tcp_count}" -eq 0 ]] \
  || die "management.topology: expected zero TenantControlPlanes, found ${tcp_count}"
[[ "${worker_count}" -eq 0 ]] \
  || die "management.topology: expected zero owned worker containers, found ${worker_count}"

log "management plane ready with zero tenant control planes and zero workers"
