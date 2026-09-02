#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenants.sh"

health="${EXIT_SUCCESS}"

section() {
  printf '\n== %s ==\n' "$1"
}

unhealthy() {
  printf 'unhealthy: %s\n' "$*"
  health="${EXIT_ERROR}"
}

deployment_ready() {
  local namespace="$1"
  local name="$2"
  local desired ready
  desired="$(management_kubectl -n "${namespace}" get deployment "${name}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  ready="$(management_kubectl -n "${namespace}" get deployment "${name}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
  [[ -n "${desired}" && "${ready:-0}" -eq "${desired}" ]]
}

section "tools"
if command -v just >/dev/null 2>&1; then
  printf 'just: %s (required: %s)\n' "$(just --version 2>/dev/null || printf unknown)" "${JUST_VERSION}"
  [[ "$(just --version 2>/dev/null || true)" == "just ${JUST_VERSION}" ]] \
    || unhealthy "just version mismatch"
else
  unhealthy "just not found"
fi
for tool in kind kubectl helm; do
  if [[ -x "${BIN_DIR}/${tool}" ]]; then
    printf '%s: present\n' "${tool}"
  else
    unhealthy "lab-local ${tool} is missing"
  fi
done

section "Docker"
if docker info >/dev/null 2>&1; then
  docker info --format 'server={{.ServerVersion}} cgroup={{.CgroupVersion}} cpus={{.NCPU}} memory_bytes={{.MemTotal}}'
  printf 'owned worker containers: %s\n' "$(docker ps -aq --filter "$(owned_docker_filter)" | wc -l)"
else
  unhealthy "Docker Engine is unavailable"
fi

section "management ownership and network"
if [[ -f "${MANAGEMENT_OWNERSHIP_FILE}" ]]; then
  recorded_name="$(sed -n 's/^KIND_NODE_NAME=//p' "${MANAGEMENT_OWNERSHIP_FILE}")"
  recorded_id="$(sed -n 's/^KIND_NODE_ID=//p' "${MANAGEMENT_OWNERSHIP_FILE}")"
  live_id="$(docker container inspect --format '{{.Id}}' "${recorded_name}" 2>/dev/null || true)"
  live_label="$(docker container inspect --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}' "${recorded_name}" 2>/dev/null || true)"
  if [[ -n "${recorded_id}" && "${live_id}" == "${recorded_id}" && "${live_label}" == "${KIND_CLUSTER_NAME}" ]]; then
    printf 'ownership evidence: matches live kind node\n'
  else
    unhealthy "management ownership evidence does not match the live kind node"
  fi
else
  unhealthy "management ownership evidence is absent"
fi
if [[ -f "${MANAGEMENT_NETWORK_FILE}" ]]; then
  sed -n 's/^\(DOCKER_SUBNET\|TENANT_[AB]_VIP\)=/  \1=/p' "${MANAGEMENT_NETWORK_FILE}"
  if ! validate_management_network "$(docker network inspect "$(kind_network_name)" 2>/dev/null)" 2>/dev/null; then
    unhealthy "management network assignment conflicts with current Docker state"
  fi
else
  unhealthy "management network assignment is absent"
fi

section "blocker state"
if [[ -f "${BLOCKER_FILE}" ]]; then
  sed 's/^/  /' "${BLOCKER_FILE}"
else
  printf '  none\n'
fi

section "management Kubernetes"
if [[ ! -f "${MANAGEMENT_KUBECONFIG}" ]]; then
  unhealthy "management kubeconfig is absent"
  exit "${health}"
fi
if ! management_kubectl get --raw=/readyz >/dev/null 2>&1; then
  unhealthy "management API is unreachable"
  exit "${health}"
fi
printf 'API: ready\n'
printf 'server: %s\n' "$(management_kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
printf 'nodes: %s Ready\n' "$(management_kubectl get nodes --no-headers 2>/dev/null | awk '$2 == "Ready" {count++} END {print count+0}')"

section "cert-manager"
for deployment in cert-manager cert-manager-cainjector cert-manager-webhook; do
  if deployment_ready cert-manager "${deployment}"; then
    printf '%s: ready\n' "${deployment}"
  else
    unhealthy "${deployment} is not ready"
  fi
done

section "MetalLB"
if deployment_ready metallb-system controller; then
  printf 'controller: ready\n'
else
  unhealthy "MetalLB controller is not ready"
fi
speaker_desired="$(management_kubectl -n metallb-system get daemonset speaker \
  -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || true)"
speaker_ready="$(management_kubectl -n metallb-system get daemonset speaker \
  -o jsonpath='{.status.numberReady}' 2>/dev/null || true)"
if [[ -n "${speaker_desired}" && "${speaker_ready:-0}" -eq "${speaker_desired}" ]]; then
  printf 'speaker: %s/%s ready\n' "${speaker_ready}" "${speaker_desired}"
else
  unhealthy "MetalLB speaker is not ready"
fi
pool_addresses="$(management_kubectl -n metallb-system get ipaddresspool kamaji-tenant-vips \
  -o jsonpath='{.spec.addresses[*]}' 2>/dev/null || true)"
[[ -n "${pool_addresses}" ]] && printf 'pool: %s\n' "${pool_addresses}" \
  || unhealthy "MetalLB tenant VIP pool is absent"

section "Kamaji"
if deployment_ready "${MANAGEMENT_NAMESPACE}" kamaji; then
  printf 'controller: ready\n'
else
  unhealthy "Kamaji controller is not ready"
fi
if [[ -n "$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get endpoints kamaji-webhook-service \
  -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null)" ]]; then
  printf 'webhook: ready\n'
else
  unhealthy "Kamaji webhook has no ready endpoint"
fi
etcd_desired="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get statefulset kamaji-etcd \
  -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
etcd_ready="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get statefulset kamaji-etcd \
  -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
if [[ "${etcd_desired:-0}" -eq "${KAMAJI_ETCD_REPLICAS}" && "${etcd_ready:-0}" -eq "${KAMAJI_ETCD_REPLICAS}" ]]; then
  printf 'datastore replicas: %s/%s ready\n' "${etcd_ready}" "${etcd_desired}"
else
  unhealthy "Kamaji datastore is not ${KAMAJI_ETCD_REPLICAS}/${KAMAJI_ETCD_REPLICAS} ready"
fi
pvc_count="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pvc -o json 2>/dev/null \
  | python3 -c 'import json,sys; print(sum(1 for item in json.load(sys.stdin).get("items", []) if item["metadata"]["name"].startswith("data-kamaji-etcd-") and item.get("status", {}).get("phase") == "Bound"))' \
  || printf 0)"
[[ "${pvc_count}" -eq "${KAMAJI_ETCD_REPLICAS}" ]] \
  && printf 'datastore PVCs: %s Bound\n' "${pvc_count}" \
  || unhealthy "expected ${KAMAJI_ETCD_REPLICAS} bound datastore PVCs"
datastore_ready="$(management_kubectl get datastore default -o jsonpath='{.status.ready}' 2>/dev/null || true)"
[[ "${datastore_ready}" == "true" ]] && printf 'DataStore/default: ready\n' \
  || unhealthy "DataStore/default is not ready"
printf 'TenantControlPlanes: %s\n' \
  "$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io --all-namespaces --no-headers 2>/dev/null | wc -l)"

section "worker compatibility spike"
if [[ -f "${SPIKE_RESULT_FILE}" ]]; then
  sed 's/^/  /' "${SPIKE_RESULT_FILE}"
else
  printf '  result evidence: absent\n'
fi
spike_tcp_count="$(
  { management_kubectl -n "${SPIKE_NAMESPACE}" get \
      tenantcontrolplane "${SPIKE_NAME}" --no-headers 2>/dev/null || true; } | wc -l
)"
spike_worker_count="$(docker ps -aq --filter "$(owned_docker_filter)" \
  --filter 'label=kamaji.cnpg-vcluster.io/tenant=spike' | wc -l)"
spike_volume_count="$(docker volume ls -q --filter "$(owned_docker_filter)" \
  --filter 'label=kamaji.cnpg-vcluster.io/tenant=spike' | wc -l)"
load_management_network
if spike_vip_claims="$(services_claiming_vip "${TENANT_A_VIP}" 2>/dev/null)"; then
  if [[ -n "${spike_vip_claims}" ]]; then
    spike_vip_claim_count="$(grep -c '^' <<<"${spike_vip_claims}")"
  else
    spike_vip_claim_count=0
  fi
else
  spike_vip_claim_count=unknown
fi
printf '  residual TCPs: %s\n' "${spike_tcp_count}"
printf '  residual workers: %s\n' "${spike_worker_count}"
printf '  residual volumes: %s\n' "${spike_volume_count}"
printf '  residual VIP claims: %s\n' "${spike_vip_claim_count}"
[[ "${spike_tcp_count}" -eq 0 ]] || unhealthy "spike TenantControlPlane remains"
[[ "${spike_worker_count}" -eq 0 ]] || unhealthy "spike worker remains"
[[ "${spike_volume_count}" -eq 0 ]] || unhealthy "spike worker volume remains"
[[ "${spike_vip_claim_count}" == 0 ]] || unhealthy "spike borrowed VIP remains claimed or could not be inspected"
[[ ! -e "$(tenant_kubeconfig spike)" ]] || unhealthy "spike kubeconfig remains"
[[ ! -e "${SPIKE_RUNTIME_DIR}" ]] || unhealthy "spike runtime subtree remains"

exit "${health}"
