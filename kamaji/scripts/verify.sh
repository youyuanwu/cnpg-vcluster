#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenants.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/addons.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/cnpg.sh"

CURRENT_TENANT=""
VERIFY_SUCCEEDED=0
VERIFY_RUNTIME_DIR="${RUNTIME_DIR}/verification"

assert_equals() {
  local expected="$1"
  local actual="$2"
  local description="$3"
  [[ "${actual}" == "${expected}" ]] \
    || die "${description}: expected ${expected}, got ${actual}"
}

other_tenant() {
  case "$1" in
    tenant-a) printf 'tenant-b\n' ;;
    tenant-b) printf 'tenant-a\n' ;;
    *) die "unknown tenant: $1" ;;
  esac
}

cleanup_verification_artifacts() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    if [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
      && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
      tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" delete pod,secret \
        -l "kamaji.cnpg-vcluster.io/role in (sql-client,cross-auth)" \
        --ignore-not-found --wait=false >/dev/null 2>&1 || true
    fi
  done
  rm -rf "${VERIFY_RUNTIME_DIR}"
}

diagnose_failure() {
  "${SCRIPT_DIR}/diagnose.sh" "${CURRENT_TENANT:-all}" >&2 || true
}

finish_verification() {
  local status=$?
  trap - EXIT
  set +e
  cleanup_verification_artifacts
  if [[ "${status}" -ne "${EXIT_SUCCESS}" \
    && "${status}" -ne "${EXIT_BLOCKED}" \
    && "${VERIFY_SUCCEEDED}" -eq 0 ]]; then
    diagnose_failure
  fi
  set -e
  exit "${status}"
}

blocked_result_is_current() {
  local code
  [[ -f "${FINAL_RESULT_FILE}" ]] \
    && grep -Fxq 'result=blocked' "${FINAL_RESULT_FILE}" \
    && code="$(sed -n 's/^blocker_code=//p' "${FINAL_RESULT_FILE}")" \
    && [[ " ${COMPATIBILITY_BLOCKER_CODES} " == *" ${code} "* ]]
}

verify_management_topology() {
  management_kubectl get --raw=/readyz >/dev/null
  [[ "$(management_kubectl get datastore default -o jsonpath='{.status.ready}')" == true ]] \
    || die "management DataStore/default is not ready"

  local topology
  topology="$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
    --all-namespaces -o json)"
  TOPOLOGY="${topology}" \
  TENANT_A_NAMESPACE="${TENANT_A_NAMESPACE}" \
  TENANT_B_NAMESPACE="${TENANT_B_NAMESPACE}" \
  TENANT_A_SCHEMA="${TENANT_A_SCHEMA}" \
  TENANT_B_SCHEMA="${TENANT_B_SCHEMA}" \
  python3 -c '
import json, os
items=json.loads(os.environ["TOPOLOGY"]).get("items", [])
expected={
    ("tenant-a", os.environ["TENANT_A_NAMESPACE"], os.environ["TENANT_A_SCHEMA"]),
    ("tenant-b", os.environ["TENANT_B_NAMESPACE"], os.environ["TENANT_B_SCHEMA"]),
}
actual={(i["metadata"]["name"], i["metadata"]["namespace"],
         i.get("spec", {}).get("dataStoreSchema", "")) for i in items}
assert actual == expected
'

  local worker_count volume_count
  worker_count="$(docker ps -aq --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker' | wc -l)"
  volume_count="$(docker volume ls -q --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker-var-lib' | wc -l)"
  assert_equals 6 "${worker_count}" "owned worker container count"
  assert_equals 6 "${volume_count}" "owned worker volume count"
  validate_disjoint_worker_sets
  assert_equals 1 "$(management_kubectl get nodes --no-headers | wc -l)" \
    "management node count"
}

verify_management_absence() {
  local name tenant pv
  for name in \
    clusters.postgresql.cnpg.io \
    cnpg-validating-webhook-configuration \
    cnpg-mutating-webhook-configuration \
    cnpg-manager \
    cnpg-manager-rolebinding; do
    case "${name}" in
      clusters.*)
        ! management_kubectl get "crd/${name}" >/dev/null 2>&1 \
          || die "management API unexpectedly owns CNPG CRD ${name}"
        ;;
      *webhook*)
        ! management_kubectl get "validatingwebhookconfiguration/${name}" \
          >/dev/null 2>&1 \
          && ! management_kubectl get "mutatingwebhookconfiguration/${name}" \
            >/dev/null 2>&1 \
          || die "management API unexpectedly owns CNPG webhook ${name}"
        ;;
      cnpg-manager)
        ! management_kubectl get "clusterrole/${name}" >/dev/null 2>&1 \
          || die "management API unexpectedly owns CNPG ClusterRole ${name}"
        ;;
      *)
        ! management_kubectl get "clusterrolebinding/${name}" >/dev/null 2>&1 \
          || die "management API unexpectedly owns CNPG ClusterRoleBinding ${name}"
        ;;
    esac
  done
  ! management_kubectl get namespace "${CNPG_NAMESPACE}" >/dev/null 2>&1 \
    || die "management API unexpectedly contains ${CNPG_NAMESPACE}"
  ! management_kubectl get namespace "${DATABASE_NAMESPACE}" >/dev/null 2>&1 \
    || die "management API unexpectedly contains ${DATABASE_NAMESPACE}"
  for name in pods services pvc pv; do
    [[ -z "$(management_kubectl get "${name}" --all-namespaces \
      -l cnpg.io/cluster --no-headers 2>/dev/null)" ]] \
      || die "management API unexpectedly contains tenant CNPG ${name}"
  done
  for tenant in ${TENANT_NAMES}; do
    while IFS= read -r pv; do
      [[ -n "${pv}" ]] || continue
      ! management_kubectl get "pv/${pv}" >/dev/null 2>&1 \
        || die "management API unexpectedly contains ${tenant} database PV ${pv}"
    done < <(tenant_kubectl "${tenant}" get pv -o name 2>/dev/null \
      | sed 's#persistentvolume/##')
  done
}

verify_tenant_foundation() {
  local tenant="$1"
  local other ready_workers
  other="$(other_tenant "${tenant}")"
  final_tenant_tcp_ready "${tenant}" \
    || die "${tenant} TenantControlPlane is not Ready"
  tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
    || die "${tenant} pause/remediation steady state is absent"
  tenant_workers_ready "${tenant}" \
    || die "${tenant} workers are not all Ready"
  final_tenant_managed_addons_ready "${tenant}" \
    && final_tenant_calico_ready "${tenant}" \
    && final_tenant_local_path_ready "${tenant}" \
    || die "${tenant} add-ons or tenant-local storage are not ready"
  [[ "$(tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
    -o jsonpath='{.status.phase}')" == Bound ]] \
    || die "${tenant} storage smoke claim is not Bound"
  ready_workers="$(tenant_kubectl "${tenant}" get nodes --no-headers \
    | awk '$2 == "Ready" {count++} END {print count+0}')"
  assert_equals "${WORKERS_PER_TENANT}" "${ready_workers}" \
    "${tenant} Ready worker count"
  local node
  for node in $(tenant_kubectl "${tenant}" get nodes -o name | sed 's#node/##'); do
    ! tenant_kubectl "${other}" get "node/${node}" >/dev/null 2>&1 \
      || die "${node} appears in both tenant APIs"
    ! management_kubectl get "node/${node}" >/dev/null 2>&1 \
      || die "${node} appears in the management API"
  done
}

verify_cnpg_objects() {
  local tenant="$1"
  local other cluster cluster_count pod_count pvc_json pv_count node_count
  other="$(other_tenant "${tenant}")"
  cluster="$(cnpg_cluster_name "${tenant}")"
  cnpg_tenant_ready "${tenant}" \
    || die "${tenant} CNPG operator or PostgreSQL cluster is not ready"

  cluster_count="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get clusters.postgresql.cnpg.io --no-headers | wc -l)"
  assert_equals 1 "${cluster_count}" "${tenant} CNPG Cluster count"
  ! tenant_kubectl "${other}" -n "${DATABASE_NAMESPACE}" get \
    "cluster.postgresql.cnpg.io/${cluster}" >/dev/null 2>&1 \
    || die "${tenant} database Cluster identity appears in ${other}"

  pod_count="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
    -l "cnpg.io/cluster=${cluster}" --no-headers | wc -l)"
  assert_equals "${CNPG_INSTANCE_COUNT}" "${pod_count}" \
    "${tenant} PostgreSQL pod count"
  node_count="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
    -l "cnpg.io/cluster=${cluster}" -o json \
    | python3 -c 'import json,sys; print(len({i.get("spec",{}).get("nodeName","") for i in json.load(sys.stdin).get("items",[]) if i.get("spec",{}).get("nodeName")}))')"
  assert_equals "${CNPG_INSTANCE_COUNT}" "${node_count}" \
    "${tenant} distinct PostgreSQL placement count"

  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
    -l "cnpg.io/cluster=${cluster}" -o json \
    | POSTGRES_IMAGE="${POSTGRES_IMAGE}" \
      CNPG_CONTROLLER_IMAGE="${CNPG_CONTROLLER_IMAGE}" \
      CNPG_REQUEST_CPU="${CNPG_REQUEST_CPU}" \
      CNPG_REQUEST_MEMORY="${CNPG_REQUEST_MEMORY}" \
      CNPG_LIMIT_CPU="${CNPG_LIMIT_CPU}" \
      CNPG_LIMIT_MEMORY="${CNPG_LIMIT_MEMORY}" \
      python3 -c '
import json, os, sys
items=json.load(sys.stdin).get("items", [])
for pod in items:
    postgres=next(c for c in pod["spec"]["containers"] if c["name"] == "postgres")
    images=[c["image"] for c in pod["spec"].get("initContainers", [])
            + pod["spec"].get("containers", [])]
    resources=postgres.get("resources", {})
    assert postgres["image"] == os.environ["POSTGRES_IMAGE"]
    assert os.environ["CNPG_CONTROLLER_IMAGE"] in images
    assert all("@sha256:" in image for image in images
               if "cloudnative-pg" in image)
    assert resources.get("requests", {}) == {
        "cpu": os.environ["CNPG_REQUEST_CPU"],
        "memory": os.environ["CNPG_REQUEST_MEMORY"],
    }
    assert resources.get("limits", {}) == {
        "cpu": os.environ["CNPG_LIMIT_CPU"],
        "memory": os.environ["CNPG_LIMIT_MEMORY"],
    }
'

  pvc_json="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pvc \
    -l "cnpg.io/cluster=${cluster}" -o json)"
  PVC_JSON="${pvc_json}" CNPG_INSTANCE_COUNT="${CNPG_INSTANCE_COUNT}" \
    CNPG_STORAGE_SIZE="${CNPG_STORAGE_SIZE}" python3 -c '
import json, os
items=json.loads(os.environ["PVC_JSON"]).get("items", [])
assert len(items) == int(os.environ["CNPG_INSTANCE_COUNT"])
assert len({i["spec"]["volumeName"] for i in items}) == len(items)
assert all(i["status"]["phase"] == "Bound" for i in items)
assert all(i["spec"]["resources"]["requests"]["storage"] == os.environ["CNPG_STORAGE_SIZE"]
           for i in items)
'
  pv_count="$(PVC_JSON="${pvc_json}" python3 -c \
    'import json,os; print(len({i["spec"]["volumeName"] for i in json.loads(os.environ["PVC_JSON"])["items"]}))')"
  assert_equals "${CNPG_INSTANCE_COUNT}" "${pv_count}" "${tenant} database PV count"
  while IFS= read -r pv; do
    [[ "$(tenant_kubectl "${tenant}" get "pv/${pv}" \
      -o jsonpath='{.status.phase}')" == Bound ]] \
      || die "${tenant} database PV ${pv} is not Bound"
  done < <(PVC_JSON="${pvc_json}" python3 -c \
    'import json,os; [print(i["spec"]["volumeName"]) for i in json.loads(os.environ["PVC_JSON"])["items"]]')

  [[ -n "$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get endpoints "${cluster}-rw" \
    -o jsonpath='{.subsets[*].addresses[*].ip}')" ]] \
    || die "${tenant} read/write Service has no ready endpoint"
  validate_final_worker_request_capacity "${tenant}" \
    || die "${tenant} scheduled requests exceed worker container capacity"
}

write_and_verify_marker() {
  local tenant="$1"
  local marker other_marker result
  marker="$(cnpg_database_marker "${tenant}")"
  other_marker="$(cnpg_database_marker "$(other_tenant "${tenant}")")"
  result="$(cnpg_run_sql "${tenant}" \
    "CREATE TABLE IF NOT EXISTS kamaji_verification(marker text PRIMARY KEY); INSERT INTO kamaji_verification(marker) VALUES ('${marker}') ON CONFLICT DO NOTHING; SELECT marker FROM kamaji_verification ORDER BY marker;")"
  assert_equals "${marker}" "$(tail -1 <<<"${result}")" \
    "${tenant} SQL marker"
  result="$(cnpg_run_sql "${tenant}" \
    "SELECT marker FROM kamaji_verification WHERE marker='${other_marker}';")"
  [[ -z "${result}" ]] \
    || die "${tenant} can read the opposite tenant SQL marker"
}

verify_marker() {
  local tenant="$1"
  local marker result
  marker="$(cnpg_database_marker "${tenant}")"
  result="$(cnpg_run_sql "${tenant}" \
    "SELECT marker FROM kamaji_verification WHERE marker='${marker}';")"
  assert_equals "${marker}" "$(tail -1 <<<"${result}")" \
    "${tenant} retained SQL marker"
}

cross_kubernetes_identity_rejected() {
  local source="$1"
  local target="$2"
  local source_json target_json cross_config output status
  source_json="${VERIFY_RUNTIME_DIR}/${source}-source.json"
  target_json="${VERIFY_RUNTIME_DIR}/${target}-target.json"
  cross_config="${VERIFY_RUNTIME_DIR}/${source}-to-${target}.json"
  tenant_kubectl "${source}" config view --raw -o json >"${source_json}"
  tenant_kubectl "${target}" config view --raw -o json >"${target_json}"
  SOURCE_CONFIG="${source_json}" TARGET_CONFIG="${target_json}" \
  CROSS_CONFIG="${cross_config}" python3 -c '
import json, os
source=json.load(open(os.environ["SOURCE_CONFIG"], encoding="utf-8"))
target=json.load(open(os.environ["TARGET_CONFIG"], encoding="utf-8"))
target_cluster=target["clusters"][0]["cluster"]
source["clusters"]=[{"name": "cross-target", "cluster": target_cluster}]
source["contexts"]=[{"name": "cross", "context": {
    "cluster": "cross-target",
    "user": source["contexts"][0]["context"]["user"],
}}]
source["current-context"]="cross"
with open(os.environ["CROSS_CONFIG"], "w", encoding="utf-8") as stream:
    json.dump(source, stream)
'
  chmod 0600 "${source_json}" "${target_json}" "${cross_config}"
  tenant_kubectl "${target}" get --raw=/readyz >/dev/null \
    || die "${target} API connectivity failed before cross-identity test"
  set +e
  output="$(
    KUBECONFIG="${cross_config}" kubectl \
      --request-timeout="${KUBECTL_REQUEST_TIMEOUT}" get namespaces 2>&1
  )"
  status=$?
  set -e
  (( status != 0 )) \
    || die "${source} Kubernetes identity was accepted by ${target}"
  if grep -Eqi 'connection refused|i/o timeout|context deadline exceeded|no route to host|network is unreachable|dial tcp' <<<"${output}"; then
    die "${source} to ${target} Kubernetes negative test failed by connectivity, not identity rejection"
  fi
  grep -Eqi 'Unauthorized|forbidden|provide credentials|tls: bad certificate|tls: certificate required|remote error: tls' <<<"${output}" \
    || die "${source} to ${target} Kubernetes rejection was not an authentication failure"
}

cross_database_identity_rejected() {
  local source="$1"
  local target="$2"
  local secret client password cluster output status
  secret="cnpg-cross-auth-${source}"
  client="cnpg-cross-auth-${source}-to-${target}"
  cluster="$(cnpg_cluster_name "${target}")"
  tenant_kubectl "${target}" -n "${DATABASE_NAMESPACE}" delete pod "${client}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl "${target}" -n "${DATABASE_NAMESPACE}" delete secret "${secret}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  password="$(
    tenant_kubectl "${source}" -n "${DATABASE_NAMESPACE}" \
      get secret "$(cnpg_app_secret_name "${source}")" \
      -o jsonpath='{.data.password}' | base64 -d
  )"
  [[ -n "${password}" ]] || die "${source} database credential is absent"
  printf '%s' "${password}" \
    | tenant_kubectl "${target}" -n "${DATABASE_NAMESPACE}" \
      create secret generic "${secret}" \
      --from-file=password=/dev/stdin \
      --dry-run=client -o yaml \
    | tenant_kubectl "${target}" apply -f - >/dev/null
  unset password
  tenant_kubectl "${target}" -n "${DATABASE_NAMESPACE}" label secret "${secret}" \
    "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    "kamaji.cnpg-vcluster.io/tenant=${target}" \
    kamaji.cnpg-vcluster.io/role=cross-auth --overwrite >/dev/null
  create_cnpg_sql_client "${target}" "${client}" "${secret}"
  tenant_kubectl "${target}" -n "${DATABASE_NAMESPACE}" label pod "${client}" \
    kamaji.cnpg-vcluster.io/role=cross-auth --overwrite >/dev/null
  cnpg_run_sql "${target}" "SELECT 1;" >/dev/null \
    || die "${target} database connectivity failed before cross-authentication test"
  set +e
  output="$(
    tenant_kubectl "${target}" -n "${DATABASE_NAMESPACE}" exec "${client}" -- \
      env PGCONNECT_TIMEOUT=10 psql -X -qAt -v ON_ERROR_STOP=1 \
      -h "${cluster}-rw" \
      -U "$(cnpg_database_owner "${source}")" \
      -d "$(cnpg_database_name "${target}")" \
      -c 'SELECT 1;' 2>&1
  )"
  status=$?
  set -e
  delete_cnpg_sql_client "${target}" "${client}"
  tenant_kubectl "${target}" -n "${DATABASE_NAMESPACE}" delete secret "${secret}" \
    --ignore-not-found --wait=false >/dev/null
  (( status != 0 )) \
    || die "${source} database identity was accepted by ${target}"
  if grep -Eqi 'could not translate host|connection refused|timeout expired|no route to host|network is unreachable|server closed the connection' <<<"${output}"; then
    die "${source} to ${target} database negative test failed by connectivity, not authentication rejection"
  fi
  grep -Eqi 'password authentication failed|no pg_hba.conf entry|role .* does not exist' <<<"${output}" \
    || die "${source} to ${target} database rejection was not an authentication failure"
}

restart_replica_with_storage_reuse() {
  local tenant="$1"
  local cluster primary replica uid pvc pv new_uid new_pvc new_pv
  cluster="$(cnpg_cluster_name "${tenant}")"
  primary="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "cluster/${cluster}" -o jsonpath='{.status.currentPrimary}')"
  replica="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
    -l "cnpg.io/cluster=${cluster}" -o json \
    | PRIMARY="${primary}" python3 -c '
import json, os, sys
names=sorted(i["metadata"]["name"] for i in json.load(sys.stdin).get("items", [])
             if i["metadata"]["name"] != os.environ["PRIMARY"])
print(names[0] if names else "")
')"
  [[ -n "${replica}" ]] || die "could not select ${tenant} replica"
  uid="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "pod/${replica}" -o jsonpath='{.metadata.uid}')"
  pvc="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "pod/${replica}" -o json \
    | python3 -c 'import json,sys; print(next(v["persistentVolumeClaim"]["claimName"] for v in json.load(sys.stdin)["spec"]["volumes"] if "persistentVolumeClaim" in v))')"
  pv="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "pvc/${pvc}" -o jsonpath='{.spec.volumeName}')"

  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" delete \
    "pod/${replica}" --wait=false >/dev/null
  wait_for "${POD_RESTART_TIMEOUT}" "${tenant} replica replacement" bash -c \
    "new_uid=\$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl --request-timeout='${KUBECTL_REQUEST_TIMEOUT}' -n '${DATABASE_NAMESPACE}' get 'pod/${replica}' -o jsonpath='{.metadata.uid}' 2>/dev/null) && [[ -n \"\$new_uid\" && \"\$new_uid\" != '${uid}' ]] && KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl --request-timeout='${KUBECTL_REQUEST_TIMEOUT}' -n '${DATABASE_NAMESPACE}' wait --for=condition=Ready 'pod/${replica}' --timeout=30s >/dev/null 2>&1" \
    || die "${tenant} replica did not return Ready"
  new_uid="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "pod/${replica}" -o jsonpath='{.metadata.uid}')"
  [[ "${new_uid}" != "${uid}" ]] || die "${tenant} replica UID did not change"
  new_pvc="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "pod/${replica}" -o json \
    | python3 -c 'import json,sys; print(next(v["persistentVolumeClaim"]["claimName"] for v in json.load(sys.stdin)["spec"]["volumes"] if "persistentVolumeClaim" in v))')"
  new_pv="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "pvc/${new_pvc}" -o jsonpath='{.spec.volumeName}')"
  assert_equals "${pvc}" "${new_pvc}" "${tenant} replacement replica PVC"
  assert_equals "${pv}" "${new_pv}" "${tenant} replacement replica PV"
  wait_for "${CNPG_TIMEOUT}" "${tenant} cluster after replica replacement" \
    cnpg_tenant_ready "${tenant}" \
    || die "${tenant} cluster did not recover after replica replacement"
  verify_marker "${tenant}"
}

failover_to_different_primary() {
  local tenant="$1"
  local cluster old_primary
  cluster="$(cnpg_cluster_name "${tenant}")"
  old_primary="$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "cluster/${cluster}" -o jsonpath='{.status.currentPrimary}')"
  [[ -n "${old_primary}" ]] || die "could not identify ${tenant} primary"
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" delete \
    "pod/${old_primary}" --wait=false >/dev/null
  wait_for "${FAILOVER_TIMEOUT}" "${tenant} primary promotion" bash -c \
    "new_primary=\$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl --request-timeout='${KUBECTL_REQUEST_TIMEOUT}' -n '${DATABASE_NAMESPACE}' get 'cluster/${cluster}' -o jsonpath='{.status.currentPrimary}' 2>/dev/null) && [[ -n \"\$new_primary\" && \"\$new_primary\" != '${old_primary}' ]]" \
    || die "${tenant} did not promote a different primary"
  wait_for "${CNPG_TIMEOUT}" "${tenant} cluster after primary failover" \
    cnpg_tenant_ready "${tenant}" \
    || die "${tenant} cluster did not return healthy after primary failover"
  verify_marker "${tenant}"
}

main() {
  require_exact_just
  if blocked_result_is_current; then
    log "verification blocked by the recorded worker compatibility result"
    exit "${EXIT_BLOCKED}"
  fi
  [[ -f "${FINAL_RESULT_FILE}" ]] \
    && grep -Fxq 'result=pass' "${FINAL_RESULT_FILE}" \
    || die "a passing full create result is required before verification"
  ensure_runtime_layout
  mkdir -p -m 0700 "${VERIFY_RUNTIME_DIR}"
  trap finish_verification EXIT

  [[ -f "${MANAGEMENT_NETWORK_FILE}" ]] \
    || die "management network assignment is absent"
  load_management_network
  verify_management_topology
  verify_management_absence
  local tenant
  for tenant in ${TENANT_NAMES}; do
    CURRENT_TENANT="${tenant}"
    verify_tenant_foundation "${tenant}"
    verify_cnpg_objects "${tenant}"
    write_and_verify_marker "${tenant}"
  done

  cross_kubernetes_identity_rejected tenant-a tenant-b
  cross_kubernetes_identity_rejected tenant-b tenant-a
  cross_database_identity_rejected tenant-a tenant-b
  cross_database_identity_rejected tenant-b tenant-a

  for tenant in ${TENANT_NAMES}; do
    CURRENT_TENANT="${tenant}"
    restart_replica_with_storage_reuse "${tenant}"
    failover_to_different_primary "${tenant}"
    tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
      || die "${tenant} kube-proxy pause/remediation drifted during database recovery"
  done

  verify_management_absence
  VERIFY_SUCCEEDED=1
  log "management absence, exact tenant topology, CNPG ownership, SQL separation, negative authentication, replica storage reuse, and primary failover checks passed"
}

main "$@"
