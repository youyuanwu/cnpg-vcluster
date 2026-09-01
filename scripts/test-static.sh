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

check_fails() {
  ! "$@"
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

addon_gate_result() (
  local broken_component="$1"
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/scripts/lib/tenant.sh"
  tenant_expected_nodes_ready() { return 0; }
  kubectl_tenant() {
    if [[ "$*" == *"get storageclass"* ]]; then
      printf 'true\n'
      return
    fi
    if [[ "$*" == *"get daemonsets"* ]]; then
      printf '%s\n' '{"items":[{"metadata":{"name":"flannel"},"status":{"desiredNumberScheduled":3,"numberReady":3}},{"metadata":{"name":"kube-proxy"},"status":{"desiredNumberScheduled":3,"numberReady":3}}]}'
      return
    fi
    if [[ "$*" == *"get deployments"* ]]; then
      printf '%s\n' '{"items":[{"metadata":{"name":"coredns"},"status":{"availableReplicas":2}},{"metadata":{"name":"local-path"},"status":{"availableReplicas":1}}]}'
      return
    fi
    python3 - "${broken_component}" <<'PY'
import json
import sys

broken = sys.argv[1]
items = []
for name in ("coredns", "flannel", "kube-proxy", "local-path"):
    items.append({
        "metadata": {"name": name},
        "status": {"conditions": [{
            "type": "Ready",
            "status": "False" if name == broken else "True",
        }]},
    })
print(json.dumps({"items": items}))
PY
  }
  tenant_addons_ready tenant-a
)

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
check "create installs CNPG in both tenants" \
  has_text 'install_all_cnpg' "${REPO_ROOT}/scripts/create.sh"
check "verification checks central Services" \
  has_text 'pods services pvc pv' "${REPO_ROOT}/scripts/verify.sh"
check "verification checks admission resources" \
  has_text 'cnpg-validating-webhook-configuration' "${REPO_ROOT}/scripts/verify.sh"
check "verification checks CNPG RBAC bindings" \
  has_text 'cnpg-manager-rolebinding' "${REPO_ROOT}/scripts/verify.sh"
check "verification writes tenant-specific SQL markers" \
  has_text 'marker=.*tenant.*private-node-marker' "${REPO_ROOT}/scripts/verify.sh"
check "verification restarts replicas" \
  has_text 'restart_replica' "${REPO_ROOT}/scripts/verify.sh"
check "verification requires a different primary" \
  has_text 'new_primary.*old_primary' "${REPO_ROOT}/scripts/verify.sh"
check "verification checks cross-tenant identities" \
  has_text 'verify_cross_tenant_identity' "${REPO_ROOT}/scripts/verify.sh"
check "destroy targets labeled worker names" \
  has_text 'worker_container_owned' "${REPO_ROOT}/scripts/destroy.sh"
check "destroy deletes only named kind cluster" \
  has_text 'name.*KIND_CLUSTER_NAME' "${REPO_ROOT}/scripts/destroy.sh"
check "E2E recreates a missing worker partial state" \
  has_text 'partial_worker' "${REPO_ROOT}/scripts/test-e2e.sh"
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
check "verification image digest is pinned" \
  has_text '^VERIFY_IMAGE=.+@sha256:' "${REPO_ROOT}/config/versions.env"
check "finite configured timeouts" \
  test "$(grep -Ec '_TIMEOUT:=[0-9]+[smh]' "${REPO_ROOT}/config/settings.env")" -ge 10
check "timeout environment overrides are preserved" \
  bash -c "export KIND_TIMEOUT=37s; source '${REPO_ROOT}/config/settings.env'; [[ \"\$KIND_TIMEOUT\" == 37s ]]"
while IFS= read -r timeout_var; do
  check "timeout ${timeout_var} is consumed" \
    grep -R -Fq --exclude=test-static.sh "\${${timeout_var}}" "${REPO_ROOT}/scripts"
done < <(sed -n 's/^: "${\([A-Z_]*_TIMEOUT\):=.*/\1/p' "${REPO_ROOT}/config/settings.env")
check "add-on gate accepts all Ready components" \
  addon_gate_result ""
check "add-on gate rejects an unready component" \
  check_fails addon_gate_result flannel
check "runtime state is ignored" \
  has_text '^\.runtime/$' "${REPO_ROOT}/.gitignore"
check "runtime directories use restrictive mode" \
  has_text 'mkdir -p -m 0700' "${REPO_ROOT}/scripts/lib/common.sh"
check "runtime shell uses restrictive umask" \
  has_text '^umask 077$' "${REPO_ROOT}/scripts/lib/common.sh"
check "README documents exactly two private-node tenants" \
  has_text 'two linked tenant control planes' "${REPO_ROOT}/README.md"
check "design documents six exclusive workers" \
  has_text 'Six systemd worker containers' "${REPO_ROOT}/docs/high-level-design.md"
check "design documents no resource synchronization" \
  has_text 'disables all vCluster resource synchronization' "${REPO_ROOT}/docs/high-level-design.md"
check "design documents shared host kernel" \
  has_text 'share its Linux kernel' "${REPO_ROOT}/docs/high-level-design.md"
check "documentation records unsupported container workers" \
  has_text 'does not name privileged Docker containers as supported' "${REPO_ROOT}/docs/high-level-design.md"
check "documentation records Platform activation blocker" \
  has_text 'platform-free-tier-activation-required' "${REPO_ROOT}/README.md"
for version in \
  "${KIND_VERSION#v}" \
  "${KUBERNETES_VERSION#v}" \
  "${KUBECTL_VERSION#v}" \
  "${HELM_VERSION#v}" \
  "${VCLUSTER_VERSION#v}" \
  "${PLATFORM_VERSION}" \
  "${CNPG_VERSION}" \
  "18.4" \
  "1.37.0" \
  "Ubuntu 24.04"; do
  check "documentation names direct pin ${version}" \
    grep -Fq "${version}" "${REPO_ROOT}/docs/high-level-design.md"
done
check "tenant manifests match Kubernetes version pin" \
  bash -c "for file in '${REPO_ROOT}'/config/tenants/*.yaml; do grep -Fq 'tag: ${KUBERNETES_VERSION}' \"\$file\" || exit 1; done"
check "CNPG manifests match PostgreSQL image pin" \
  bash -c "for file in '${REPO_ROOT}'/manifests/cnpg/cluster-*.yaml; do grep -Fq 'imageName: ${POSTGRES_IMAGE}' \"\$file\" || exit 1; done"
check "fixed topology is exactly two tenants and three workers each" \
  bash -c "source '${REPO_ROOT}/config/settings.env'; [[ \"\$TENANT_NAMES\" == 'tenant-a tenant-b' && \"\$WORKERS_PER_TENANT\" == 3 ]]"

if (( failures > 0 )); then
  die "${failures} static check(s) failed"
fi

log "all static checks passed"
