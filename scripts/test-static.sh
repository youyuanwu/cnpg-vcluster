#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

failures=0

check() {
  local description="$1"
  shift
  if "$@"; then
    printf 'ok - %s\n' "${description}"
  else
    printf 'not ok - %s\n' "${description}" >&2
    failures=$((failures + 1))
  fi
}

has_text() {
  grep -Eq "$1" "$2"
}

not_has_text() {
  ! grep -Eq "$1" "$2"
}

count_equals() {
  local expected="$1"
  local pattern="$2"
  shift 2
  local actual
  actual="$(grep -hEc "${pattern}" "$@" | awk '{sum += $1} END {print sum + 0}')"
  [[ "${actual}" -eq "${expected}" ]]
}

for script in "${REPO_ROOT}"/scripts/*.sh "${REPO_ROOT}"/scripts/lib/*.sh; do
  check "bash syntax: ${script#${REPO_ROOT}/}" bash -n "${script}"
done

check "exactly two tenant configurations" \
  test "$(find "${REPO_ROOT}/config/tenants" -maxdepth 1 -name 'tenant-*.yaml' | wc -l)" -eq 2
check "tenant A private nodes enabled" \
  bash -c "grep -A1 '^privateNodes:' '${REPO_ROOT}/config/tenants/tenant-a.yaml' | grep -q 'enabled: true'"
check "tenant B private nodes enabled" \
  bash -c "grep -A1 '^privateNodes:' '${REPO_ROOT}/config/tenants/tenant-b.yaml' | grep -q 'enabled: true'"
check "node-to-node VPN disabled for direct bridge connectivity" \
  bash -c "for file in '${REPO_ROOT}'/config/tenants/*.yaml; do grep -A1 'nodeToNode:' \"\$file\" | grep -q 'enabled: false' || exit 1; done"
check "tenant pod CIDRs are unique" \
  test "$(grep -h 'podCIDR:' "${REPO_ROOT}"/config/tenants/*.yaml | sort -u | wc -l)" -eq 2
check "tenant service CIDRs are unique" \
  test "$(grep -h 'serviceCIDR:' "${REPO_ROOT}"/config/tenants/*.yaml | sort -u | wc -l)" -eq 2
check "no sync configuration exists" \
  not_has_text '^[[:space:]]*sync:' "${REPO_ROOT}"/config/tenants/*.yaml
check "no Docker/vind tenant driver fallback" \
  bash -c "for file in '${REPO_ROOT}'/scripts/*.sh; do [[ \"\${file##*/}\" == test-static.sh ]] && continue; ! grep -Eq '(--driver[ =]+docker|vind)' \"\$file\" || exit 1; done"
check "create uses Helm driver only" \
  has_text 'driver helm' "${REPO_ROOT}/scripts/lib/tenant.sh"
check "join tokens are not executed as shell command strings" \
  not_has_text '(eval|bash -c.*join_url|sh -c.*join_url)' "${REPO_ROOT}/scripts/lib/tenant.sh"
check "worker containers have ownership labels" \
  has_text 'cnpg-vcluster.role=private-worker' "${REPO_ROOT}/scripts/lib/workers.sh"
check "worker containers use private cgroup namespace" \
  has_text 'cgroupns=private' "${REPO_ROOT}/scripts/lib/workers.sh"
check "worker identities use explicit unique hostnames" \
  has_text 'hostname' "${REPO_ROOT}/scripts/lib/workers.sh"
check "all kubeconfig calls are explicit" \
  not_has_text '(^|[[:space:]])kubectl[[:space:]]' "${REPO_ROOT}/scripts/create.sh"
check "Platform credentials remain runtime-only" \
  has_text 'credentials/platform-admin-password' "${REPO_ROOT}/scripts/lib/platform.sh"
check "forced preflight failure is supported" \
  has_text 'FORCE_PREFLIGHT_FAILURE' "${REPO_ROOT}/scripts/create.sh"
check "Free-tier activation failure is explicit" \
  has_text 'platform-free-tier-activation-required' "${REPO_ROOT}/scripts/lib/tenant.sh"
check "two CNPG cluster manifests" \
  test "$(find "${REPO_ROOT}/manifests/cnpg" -maxdepth 1 -name 'cluster-tenant-*.yaml' | wc -l)" -eq 2
check "both CNPG clusters have three instances" \
  count_equals 2 '^[[:space:]]+instances: 3$' "${REPO_ROOT}"/manifests/cnpg/cluster-tenant-*.yaml
check "both CNPG clusters require hostname anti-affinity" \
  count_equals 2 '^[[:space:]]+podAntiAffinityType: required$' "${REPO_ROOT}"/manifests/cnpg/cluster-tenant-*.yaml
check "CNPG operator manifest checksum" \
  sha256_check "${CNPG_MANIFEST_SHA256}" "${REPO_ROOT}/manifests/cnpg/operator.yaml"
check "CNPG operator provenance is pinned" \
  has_text '^CNPG_MANIFEST_URL=.*/v1\.30\.0/releases/cnpg-1\.30\.0\.yaml$' "${REPO_ROOT}/config/versions.env"
check "PostgreSQL image digest is pinned" \
  has_text '^POSTGRES_IMAGE=.+@sha256:' "${REPO_ROOT}/config/versions.env"
check "worker base image digest is pinned" \
  has_text '^WORKER_BASE_IMAGE=.+@sha256:' "${REPO_ROOT}/config/versions.env"
check "finite configured timeouts" \
  test "$(grep -Ec '^[A-Z_]+_TIMEOUT=[0-9]+[smh]$' "${REPO_ROOT}/config/settings.env")" -ge 8
check "runtime state is ignored" \
  has_text '^\.runtime/$' "${REPO_ROOT}/.gitignore"
check "runtime directories use restrictive mode" \
  has_text 'mkdir -p -m 0700' "${REPO_ROOT}/scripts/lib/common.sh"
check "runtime shell uses restrictive umask" \
  has_text '^umask 077$' "${REPO_ROOT}/scripts/lib/common.sh"

if (( failures > 0 )); then
  die "${failures} static check(s) failed"
fi

log "all static checks passed"
