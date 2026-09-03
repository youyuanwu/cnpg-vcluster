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

report_cnpg_status() {
  local tenant="$1"
  local cluster desired ready image crd_ready cluster_count phase
  local ready_instances current_primary pod_count placement_count pvc_count pv_count
  cluster="$(cnpg_cluster_name "${tenant}")"
  desired="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.spec.replicas}' \
    2>/dev/null || true)"
  ready="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.status.readyReplicas}' \
    2>/dev/null || true)"
  image="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].image}' \
    2>/dev/null || true)"
  crd_ready=0
  local crd
  while IFS= read -r crd; do
    [[ "$(tenant_kubectl "${tenant}" get "crd/${crd}" \
      -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' \
      2>/dev/null || true)" == True ]] && crd_ready=$((crd_ready + 1))
  done < <(cnpg_expected_crds)
  printf 'CloudNativePG layer:\n'
  printf 'CNPG operator: %s/%s Ready image=%s CRDs=%s/11\n' \
    "${ready:-0}" "${desired:-0}" "${image:-absent}" "${crd_ready}"
  cnpg_operator_ready "${tenant}" \
    || unhealthy "${tenant} CloudNativePG CRDs, webhooks, RBAC, or operator are not ready"

  cluster_count="$(
    { tenant_kubectl "${tenant}" get clusters.postgresql.cnpg.io \
        --all-namespaces --no-headers 2>/dev/null || true; } | wc -l
  )"
  phase="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "cluster/${cluster}" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  ready_instances="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "cluster/${cluster}" -o jsonpath='{.status.readyInstances}' \
    2>/dev/null || true)"
  current_primary="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "cluster/${cluster}" -o jsonpath='{.status.currentPrimary}' \
    2>/dev/null || true)"
  pod_count="$(
    { tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
        -l "cnpg.io/cluster=${cluster}" --no-headers 2>/dev/null || true; } | wc -l
  )"
  placement_count="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
    -l "cnpg.io/cluster=${cluster}" -o json 2>/dev/null \
    | python3 -c 'import json,sys; print(len({i.get("spec",{}).get("nodeName","") for i in json.load(sys.stdin).get("items",[]) if i.get("spec",{}).get("nodeName")}))' \
    2>/dev/null || printf 0)"
  pvc_count="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pvc \
    -l "cnpg.io/cluster=${cluster}" -o json 2>/dev/null \
    | python3 -c 'import json,sys; print(sum(1 for i in json.load(sys.stdin).get("items",[]) if i.get("status",{}).get("phase")=="Bound"))' \
    2>/dev/null || printf 0)"
  pv_count="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pvc \
    -l "cnpg.io/cluster=${cluster}" -o json 2>/dev/null \
    | python3 -c 'import json,sys; print(len({i.get("spec",{}).get("volumeName","") for i in json.load(sys.stdin).get("items",[]) if i.get("spec",{}).get("volumeName")}))' \
    2>/dev/null || printf 0)"
  printf 'PostgreSQL cluster: count=%s phase=%s ready=%s/%s primary=%s\n' \
    "${cluster_count}" "${phase:-absent}" "${ready_instances:-0}" \
    "${CNPG_INSTANCE_COUNT}" "${current_primary:-absent}"
  printf 'PostgreSQL resources: pods=%s placements=%s PVCs=%s Bound PVs=%s\n' \
    "${pod_count}" "${placement_count}" "${pvc_count}" "${pv_count}"
  printf 'PostgreSQL services: %s\n' \
    "$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get services \
      -l "cnpg.io/cluster=${cluster}" -o name 2>/dev/null | paste -sd, - || true)"
  [[ "${cluster_count}" -eq 1 \
    && "${pod_count}" -eq "${CNPG_INSTANCE_COUNT}" \
    && "${placement_count}" -eq "${CNPG_INSTANCE_COUNT}" \
    && "${pvc_count}" -eq "${CNPG_INSTANCE_COUNT}" \
    && "${pv_count}" -eq "${CNPG_INSTANCE_COUNT}" ]] \
    && cnpg_tenant_ready "${tenant}" \
    || unhealthy "${tenant} PostgreSQL cluster, services, placements, claims, or volumes are not healthy"
}

report_unavailable_cluster_layers() {
  section "cert-manager"
  printf 'unavailable: management API is not ready\n'
  section "MetalLB"
  printf 'unavailable: management API is not ready\n'
  section "Kamaji controller and datastore"
  printf 'unavailable: management API is not ready\n'
  section "worker compatibility spike"
  printf 'control plane, endpoint, worker, add-ons, and storage: absent or unavailable\n'
  section "final tenant topology"
  local tenant
  for tenant in ${TENANT_NAMES}; do
    printf '\n-- %s --\n' "${tenant}"
    printf 'TenantControlPlane and API endpoint: absent or unavailable\n'
    printf 'workers: absent or unavailable\n'
    printf 'add-ons and storage: absent or unavailable\n'
    printf 'CloudNativePG operator: absent or unavailable\n'
    printf 'PostgreSQL cluster: absent or unavailable\n'
  done
}

section "tools"
if host_just="$(resolve_host_just)"; then
  printf 'just: %s at %s (required: %s)\n' \
    "$("${host_just}" --version 2>/dev/null || printf unknown)" \
    "${host_just}" "${JUST_VERSION}"
  [[ "$("${host_just}" --version 2>/dev/null || true)" == "just ${JUST_VERSION}" ]] \
    || unhealthy "just version mismatch"
else
  unhealthy "allowed host just not found outside ${BIN_DIR}"
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

section "host inotify"
inotify_instances="$(read_inotify_value max_user_instances)"
inotify_watches="$(read_inotify_value max_user_watches)"
printf 'max_user_instances: %s (required: %s)\n' \
  "${inotify_instances}" "${MIN_INOTIFY_INSTANCES}"
printf 'max_user_watches: %s (required: %s)\n' \
  "${inotify_watches}" "${MIN_INOTIFY_WATCHES}"
(( inotify_instances >= MIN_INOTIFY_INSTANCES )) \
  || unhealthy "host inotify instance limit is below the required floor"
(( inotify_watches >= MIN_INOTIFY_WATCHES )) \
  || unhealthy "host inotify watch limit is below the required floor"
if [[ -f "${HOST_SYSCTL_STATE_FILE}" ]]; then
  printf 'original values: recorded in secure runtime state\n'
else
  printf 'original values: not recorded\n'
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
  report_unavailable_cluster_layers
  exit "${health}"
fi
if ! management_kubectl get --raw=/readyz >/dev/null 2>&1; then
  unhealthy "management API is unreachable"
  report_unavailable_cluster_layers
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
if [[ -f "${MANAGEMENT_NETWORK_FILE}" ]]; then
  load_management_network
fi
if [[ -n "${TENANT_A_VIP:-}" ]] \
  && spike_vip_claims="$(services_claiming_vip "${TENANT_A_VIP}" 2>/dev/null)"; then
  if final_tenant_exists tenant-a; then
    spike_vip_claims="$(
      grep -Fvx "${TENANT_A_NAMESPACE}/tenant-a" <<<"${spike_vip_claims}" || true
    )"
  fi
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

section "final tenant topology"
if [[ -f "${FINAL_RESULT_FILE}" ]]; then
  sed 's/^/  /' "${FINAL_RESULT_FILE}"
else
  printf '  result evidence: absent\n'
fi
final_tcp_count="$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
  --all-namespaces --no-headers 2>/dev/null | wc -l)"
final_worker_count="$(docker ps -aq --filter "$(owned_docker_filter)" \
  --filter 'label=kamaji.cnpg-vcluster.io/role=worker' | wc -l)"
final_volume_count="$(docker volume ls -q --filter "$(owned_docker_filter)" \
  --filter 'label=kamaji.cnpg-vcluster.io/role=worker-var-lib' | wc -l)"
printf '  final TCPs: %s\n' "${final_tcp_count}"
printf '  final workers: %s\n' "${final_worker_count}"
printf '  final worker volumes: %s\n' "${final_volume_count}"

for tenant in ${TENANT_NAMES}; do
  namespace="$(tenant_namespace "${tenant}")"
  printf '\n-- %s --\n' "${tenant}"
  if final_tenant_exists "${tenant}"; then
    pause_value="$(tenant_reconciliation_pause_value "${tenant}")"
    remediation_revision="$(tenant_reconciliation_remediation_revision "${tenant}")"
    printf 'TCP: %s endpoint=%s schema=%s paused=%s remediation=%s\n' \
      "$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
        -o jsonpath='{.status.kubernetesResources.version.status}' 2>/dev/null || printf unknown)" \
      "$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
        -o jsonpath='{.status.controlPlaneEndpoint}' 2>/dev/null || printf unknown)" \
      "$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
        -o jsonpath='{.spec.dataStoreSchema}' 2>/dev/null || printf unknown)" \
      "${pause_value:-false}" \
      "${remediation_revision:-absent}"
    tenant_reconciliation_is_paused "${tenant}" \
      || unhealthy "${tenant} Kamaji reconciliation is not intentionally paused"
    [[ "${remediation_revision}" == "${COMPATIBILITY_REVISION}" ]] \
      || unhealthy "${tenant} kube-proxy remediation revision is absent or stale"
    final_tenant_tcp_ready "${tenant}" \
      || unhealthy "${tenant} TenantControlPlane is not Ready"
    control_plane_status="$(
      tenant_control_plane_container_statuses "${tenant}" || true
    )"
    printf 'control-plane containers:\n%s\n' \
      "${control_plane_status:-  status unavailable}"
    if grep -Eq 'state=terminated:OOMKilled|last_reason=OOMKilled' \
      <<<"${control_plane_status}"; then
      unhealthy "${tenant} control plane has OOMKilled container evidence"
    fi
  else
    printf 'TCP: absent\n'
  fi
  if [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
    && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
    printf 'API: ready\n'
    ready_workers="$(tenant_kubectl "${tenant}" get nodes --no-headers 2>/dev/null \
      | awk '$2 == "Ready" {count++} END {print count+0}')"
    printf 'Ready workers: %s/%s\n' "${ready_workers}" "${WORKERS_PER_TENANT}"
    [[ "${ready_workers}" -eq "${WORKERS_PER_TENANT}" ]] \
      || unhealthy "${tenant} does not have ${WORKERS_PER_TENANT} Ready workers"
    if ! (validate_exact_tenant_node_set "${tenant}" >/dev/null); then
      unhealthy "${tenant} complete Node-name set or ownership labels differ from the exact expected three"
    fi
    conntrack_value="$(tenant_kubectl "${tenant}" -n kube-system get configmap kube-proxy \
        -o jsonpath='{.data.config\.conf}' 2>/dev/null \
        | sed -n 's/^  maxPerCore: //p' || true)"
    printf 'kube-proxy conntrack.maxPerCore: %s\n' "${conntrack_value:-absent}"
    [[ "${conntrack_value}" == "${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}" ]] \
      || unhealthy "${tenant} kube-proxy conntrack.maxPerCore is not ${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}"
    printf 'add-ons and storage:\n'
    for component in \
      "deployment kube-system coredns CoreDNS" \
      "daemonset kube-system kube-proxy kube-proxy" \
      "daemonset kube-system konnectivity-agent Konnectivity" \
      "daemonset kube-system calico-node Calico-node" \
      "deployment kube-system calico-kube-controllers Calico-controllers" \
      "deployment local-path-storage local-path-provisioner local-path"; do
      read -r kind component_namespace component_name component_label <<<"${component}"
      if [[ "${kind}" == deployment ]]; then
        desired="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
          get deployment "${component_name}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
        ready="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
          get deployment "${component_name}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
        component_ok=false
        final_tenant_deployment_ready "${tenant}" "${component_namespace}" "${component_name}" \
          && component_ok=true
      else
        desired="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
          get daemonset "${component_name}" -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || true)"
        ready="$(tenant_kubectl "${tenant}" -n "${component_namespace}" \
          get daemonset "${component_name}" -o jsonpath='{.status.numberReady}' 2>/dev/null || true)"
        component_ok=false
        final_tenant_daemonset_ready "${tenant}" "${component_namespace}" "${component_name}" \
          && component_ok=true
      fi
      printf '%s: %s/%s Ready\n' "${component_label}" "${ready:-0}" "${desired:-0}"
      [[ "${component_ok}" == true ]] \
        || unhealthy "${tenant} ${component_label} replicas are not Ready"
    done
    default_classes="$(tenant_kubectl "${tenant}" get storageclass -o json 2>/dev/null \
      | python3 -c 'import json,sys; print(sum(1 for i in json.load(sys.stdin).get("items",[]) if i.get("metadata",{}).get("annotations",{}).get("storageclass.kubernetes.io/is-default-class")=="true"))' \
      || printf 0)"
    printf 'default storage classes: %s\n' "${default_classes}"
    final_tenant_local_path_ready "${tenant}" \
      || unhealthy "${tenant} local-path storage is not ready or uniquely default"
    smoke_pvc="$(tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
      -o jsonpath='{.status.phase}' 2>/dev/null || printf absent)"
    printf 'smoke PVC: %s\n' "${smoke_pvc}"
    [[ "${smoke_pvc}" == Bound ]] || unhealthy "${tenant} smoke PVC is not Bound"
    report_cnpg_status "${tenant}"
  else
    printf 'API: kubeconfig absent or unreachable\n'
    final_tenant_exists "${tenant}" \
      && unhealthy "${tenant} API is unreachable"
  fi
done

if [[ -f "${FINAL_RESULT_FILE}" ]] \
  && grep -Fxq 'result=pass' "${FINAL_RESULT_FILE}"; then
  grep -Fxq 'cnpg=installed' "${FINAL_RESULT_FILE}" \
    || unhealthy "passing final result does not record CNPG as installed"
  [[ "${final_tcp_count}" -eq 2 ]] || unhealthy "passing final result lacks exactly two TCPs"
  [[ "${final_worker_count}" -eq 6 ]] || unhealthy "passing final result lacks exactly six workers"
  [[ "${final_volume_count}" -eq 6 ]] || unhealthy "passing final result lacks exactly six volumes"
  for tenant in ${TENANT_NAMES}; do
    tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
      || unhealthy "${tenant} passing result lacks the paused kube-proxy steady state"
  done
elif [[ -f "${FINAL_RESULT_FILE}" ]] \
  && grep -Fxq 'result=blocked' "${FINAL_RESULT_FILE}"; then
  blocked_result_records_are_current_consistent \
    || unhealthy "blocked final result and blocker records are stale, incomplete, or inconsistent"
  [[ "${final_tcp_count}" -eq 0 ]] \
    || unhealthy "blocked final result retains a TenantControlPlane"
  [[ "${final_worker_count}" -eq 0 ]] \
    || unhealthy "blocked final result retains a worker"
  [[ "${final_volume_count}" -eq 0 ]] \
    || unhealthy "blocked final result retains a worker volume"
  for tenant in ${TENANT_NAMES}; do
    [[ ! -e "$(tenant_runtime_dir "${tenant}")" ]] \
      || unhealthy "blocked final result retains ${tenant} runtime state"
    management_namespace_absent "$(tenant_namespace "${tenant}")" \
      || unhealthy "blocked final result retains ${tenant} management namespace"
  done
fi

exit "${health}"
