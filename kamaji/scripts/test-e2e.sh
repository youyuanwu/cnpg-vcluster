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
source "${SCRIPT_DIR}/lib/tenants.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/workers.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/cnpg.sh"

RESULT_LOG="${CACHE_DIR}/e2e-last-result.log"
SENTINEL_CONTAINER="${LAB_PREFIX}-e2e-sentinel"
SENTINEL_VOLUME="${LAB_PREFIX}-e2e-sentinel"
SENTINEL_CLUSTER="${LAB_PREFIX}-e2e-sentinel"
SENTINEL_KUBECONFIG="${CACHE_DIR}/e2e-sentinel-kubeconfig"

run_logged() {
  "$@" >>"${RESULT_LOG}" 2>&1
}

runtime_fingerprint() {
  if [[ -d "${RUNTIME_DIR}" ]]; then
    find "${RUNTIME_DIR}" -printf '%M %s %T@ %P\n' | sort | sha256sum
  else
    printf 'absent\n'
  fi
}

management_fingerprint() {
  local tenant
  if [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
    && management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    {
      management_kubectl get namespaces,deployments,statefulsets,daemonsets,pvc,services \
      --all-namespaces -o json \
        | RESOURCE_SCOPE=management python3 -c '
import json, sys
import os
for item in json.load(sys.stdin).get("items", []):
    metadata=item["metadata"]
    print(os.environ["RESOURCE_SCOPE"], item["kind"],
          metadata.get("namespace", ""), metadata["name"],
          metadata.get("uid", ""))
'
      for tenant in ${TENANT_NAMES}; do
        if [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
          && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
          tenant_kubectl "${tenant}" get \
            namespaces,deployments,statefulsets,daemonsets,pvc,services,clusters.postgresql.cnpg.io \
            --all-namespaces -o json 2>/dev/null \
            | RESOURCE_SCOPE="${tenant}" python3 -c '
import json, os, sys
for item in json.load(sys.stdin).get("items", []):
    metadata=item["metadata"]
    print(os.environ["RESOURCE_SCOPE"], item["kind"],
          metadata.get("namespace", ""), metadata["name"],
          metadata.get("uid", ""))
'
        else
          printf '%s unavailable\n' "${tenant}"
        fi
      done
    } | sort | sha256sum
  else
    printf 'unavailable\n'
  fi
}

worker_identities() {
  local tenant ordinal name
  for tenant in ${TENANT_NAMES}; do
    for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
      name="$(worker_name "${tenant}" "${ordinal}")"
      printf '%s=%s\n' "${name}" \
        "$(docker container inspect --format '{{.Id}}' "${name}")"
    done
  done
}

cluster_identities() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    printf '%s=%s\n' "${tenant}" \
      "$(tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
        get "cluster/$(cnpg_cluster_name "${tenant}")" \
        -o jsonpath='{.metadata.uid}')"
  done
}

storage_identities() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    tenant_kubectl "${tenant}" get pvc,pv --all-namespaces -o json \
      | TENANT="${tenant}" python3 -c '
import json, os, sys
for item in json.load(sys.stdin).get("items", []):
    metadata=item.get("metadata", {})
    spec=item.get("spec", {})
    print(
        os.environ["TENANT"],
        item.get("kind", ""),
        metadata.get("namespace", ""),
        metadata.get("name", ""),
        metadata.get("uid", ""),
        spec.get("volumeName", ""),
    )
'
  done | sort
}

marker_value() {
  local tenant="$1"
  cnpg_run_sql "${tenant}" \
    "SELECT marker FROM kamaji_verification WHERE marker='$(cnpg_database_marker "${tenant}")';" \
    | tail -1
}

healthy_observers_ready() {
  "${SCRIPT_DIR}/status.sh" >/dev/null 2>&1 \
    && "${SCRIPT_DIR}/diagnose.sh" all >/dev/null 2>&1
}

seed_markers() {
  local tenant marker
  for tenant in ${TENANT_NAMES}; do
    marker="$(cnpg_database_marker "${tenant}")"
    cnpg_run_sql "${tenant}" \
      "CREATE TABLE IF NOT EXISTS kamaji_verification(marker text PRIMARY KEY); INSERT INTO kamaji_verification(marker) VALUES ('${marker}') ON CONFLICT DO NOTHING;" \
      >/dev/null
    [[ "$(marker_value "${tenant}")" == "${marker}" ]] \
      || die "${tenant} marker seed failed"
  done
}

assert_marker_table_absent() {
  local tenant="$1"
  [[ "$(cnpg_run_sql "${tenant}" \
    "SELECT COALESCE(to_regclass('public.kamaji_verification')::text, 'absent');" \
    | tail -1)" == absent ]] \
    || die "${tenant} unexpectedly has a verification marker table"
}

bootstrap_token_inventory() {
  local tenant="$1"
  tenant_kubectl "${tenant}" -n kube-system get secrets -o json \
    | python3 -c '
import json,sys
print("\n".join(sorted(
    item["metadata"]["name"]
    for item in json.load(sys.stdin).get("items",[])
    if item.get("type") == "bootstrap.kubernetes.io/token"
)))
'
}

assert_effective_request_fixtures() {
  local fixture sidecar_fixture
  fixture='{"items":[{"spec":{"containers":[{"resources":{"requests":{"cpu":"50m","memory":"32Mi"}}}],"initContainers":[{"resources":{"requests":{"cpu":"900m","memory":"700Mi"}}}],"overhead":{"cpu":"50m","memory":"100Mi"}},"status":{"phase":"Running"}}]}'
  printf '%s\n' "${fixture}" \
    | CPU_CAP=1000000000 MEMORY_CAP=838860800 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null
  ! printf '%s\n' "${fixture}" \
    | CPU_CAP=949999999 MEMORY_CAP=838860800 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1
  ! printf '%s\n' "${fixture}" \
    | CPU_CAP=1000000000 MEMORY_CAP=838860799 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1
  sidecar_fixture='{"items":[{"spec":{"containers":[{"resources":{"requests":{"cpu":"100m","memory":"64Mi"}}}],"initContainers":[{"name":"sidecar","restartPolicy":"Always","resources":{"requests":{"cpu":"200m","memory":"128Mi"}}},{"name":"setup","resources":{"requests":{"cpu":"500m","memory":"512Mi"}}}],"overhead":{"cpu":"50m","memory":"64Mi"}},"status":{"phase":"Running"}}]}'
  printf '%s\n' "${sidecar_fixture}" \
    | CPU_CAP=750000000 MEMORY_CAP=738197504 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null
  ! printf '%s\n' "${sidecar_fixture}" \
    | CPU_CAP=749999999 MEMORY_CAP=738197504 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1
  ! printf '%s\n' "${sidecar_fixture}" \
    | CPU_CAP=750000000 MEMORY_CAP=738197503 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1
}

assert_observer_read_only() {
  local output="$1"
  local expected_status="$2"
  shift 2
  local before_runtime before_management status
  before_runtime="$(runtime_fingerprint)"
  before_management="$(management_fingerprint)"
  set +e
  "$@" >"${output}" 2>&1
  status=$?
  set -e
  cat "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${expected_status}" ]] \
    || die "observer returned ${status}, expected ${expected_status}"
  [[ "$(runtime_fingerprint)" == "${before_runtime}" ]] \
    || die "observer mutated runtime state"
  [[ "$(management_fingerprint)" == "${before_management}" ]] \
    || die "observer mutated management resources"
  for layer in tools Docker management Kamaji TenantControlPlane endpoint workers add-ons storage CloudNativePG PostgreSQL; do
    grep -Fqi "${layer}" "${output}" \
      || die "observer output omitted FR-014 layer ${layer}"
  done
}

assert_capacity_failure() {
  local variable="$1"
  local value="$2"
  local reason="$3"
  local before_runtime before_owned output status
  before_runtime="$(runtime_fingerprint)"
  before_owned="$(docker ps -aq --filter "$(owned_docker_filter)" | sort)"
  local -a fixture_env=("${variable}=${value}" KAMAJI_PREFLIGHT_FORBID_MUTATION_FIXTURE=1)
  if [[ "${reason}" == inotify-watches ]]; then
    fixture_env+=("KAMAJI_PREFLIGHT_INOTIFY_INSTANCES_FIXTURE=${MIN_INOTIFY_INSTANCES}")
  fi
  set +e
  output="$(env "${fixture_env[@]}" "${SCRIPT_DIR}/preflight.sh" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && grep -Fq "capacity.${reason}" <<<"${output}" \
    || die "${reason} preflight fixture did not fail with exit 1"
  [[ "$(runtime_fingerprint)" == "${before_runtime}" ]] \
    && [[ "$(docker ps -aq --filter "$(owned_docker_filter)" | sort)" == "${before_owned}" ]] \
    || die "${reason} preflight fixture mutated retained state"
}

assert_sentinels_present() {
  docker container inspect "${SENTINEL_CONTAINER}" >/dev/null
  docker volume inspect "${SENTINEL_VOLUME}" >/dev/null
  kind get clusters | grep -Fxq "${SENTINEL_CLUSTER}"
  KUBECONFIG="${SENTINEL_KUBECONFIG}" kubectl config get-contexts \
    -o name | grep -Fxq "kind-${SENTINEL_CLUSTER}"
}

cleanup_sentinels() {
  docker rm -f "${SENTINEL_CONTAINER}" >/dev/null 2>&1 || true
  docker volume rm "${SENTINEL_VOLUME}" >/dev/null 2>&1 || true
  timeout "$(seconds_from_duration "${KIND_CREATE_TIMEOUT}")" \
    kind delete cluster --name "${SENTINEL_CLUSTER}" \
    --kubeconfig "${SENTINEL_KUBECONFIG}" >/dev/null 2>&1 || true
  rm -f "${SENTINEL_KUBECONFIG}"
}

finish_e2e() {
  local status=$?
  trap - EXIT
  set +e
  printf 'E2E_EXIT=%s\n' "${status}" >>"${RESULT_LOG}"
  if (( status != 0 )); then
    "${SCRIPT_DIR}/destroy.sh" >>"${RESULT_LOG}" 2>&1 || true
    "${SCRIPT_DIR}/destroy.sh" >>"${RESULT_LOG}" 2>&1 || true
  fi
  cleanup_sentinels
  exit "${status}"
}

write_blocked_result_fixture() {
  local revision="$1"
  local code="$2"
  local evidence="$3"
  local cleanup="${4:-proved}"
  {
    printf 'result=blocked\n'
    printf 'compatibility_revision=%s\n' "${revision}"
    printf 'first_failing_prerequisite=tenant-a-workers\n'
    printf 'blocker_code=%s\n' "${code}"
    printf 'blocker_evidence=%s\n' "${evidence}"
    printf 'cleanup=%s\n' "${cleanup}"
    printf 'final_tenants=absent\n'
    printf 'final_workers=absent\n'
    printf 'final_volumes=absent\n'
    printf 'final_runtime=absent\n'
  } | write_secret_file "${FINAL_RESULT_FILE}"
  {
    printf 'owner=final\n'
    printf 'code=%s\n' "${code}"
    printf 'prerequisite=tenant-a-workers\n'
    printf 'message=%s\n' "${evidence}"
  } | write_secret_file "${BLOCKER_FILE}"
}

assert_verify_rejects_blocked_record() {
  local description="$1"
  local output status
  set +e
  output="$("${SCRIPT_DIR}/verify.sh" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    || die "${description} blocker record returned ${status}, expected exit 1"
}

assert_blocked_residual_rejected() {
  local description="$1"
  local expected="$2"
  local injected="${3:-}"
  local verify_output verify_status status_output status_status
  set +e
  verify_output="$(
    KAMAJI_TEST_BLOCKED_RESIDUAL="${injected}" \
      "${SCRIPT_DIR}/verify.sh" 2>&1
  )"
  verify_status=$?
  status_output="$(
    KAMAJI_TEST_BLOCKED_RESIDUAL="${injected}" \
      "${SCRIPT_DIR}/status.sh" 2>&1
  )"
  status_status=$?
  set -e
  printf '%s\n%s\n' "${verify_output}" "${status_output}" >>"${RESULT_LOG}"
  [[ "${verify_status}" -eq "${EXIT_ERROR}" \
    && "${status_status}" -eq "${EXIT_ERROR}" \
    && "${verify_output}" == *"${expected}"* \
    && "${status_output}" == *"${expected}"* ]] \
    || die "${description} residual was not rejected with the exact diagnostic"
}

assert_exact_blocked_residual_validation() {
  local vip_service="${LAB_PREFIX}-blocked-vip-fixture"

  assert_blocked_residual_rejected datastore-used-by \
    "tenant-a DataStore/default status.usedBy residual is present" \
    "tenant-a.datastore-used-by:present"
  assert_blocked_residual_rejected datastore-prefix \
    "tenant-a datastore prefix residual is present" \
    "tenant-a.datastore-prefix:present"
  assert_blocked_residual_rejected datastore-user \
    "tenant-a datastore user residual is present" \
    "tenant-a.datastore-user:present"
  assert_blocked_residual_rejected datastore-role \
    "tenant-a datastore role residual is present" \
    "tenant-a.datastore-role:present"
  assert_blocked_residual_rejected datastore-inspection \
    "tenant-a datastore prefix residual inspection failed" \
    "tenant-a.datastore-prefix:inspection-failed"

  mkdir -p -m 0700 "${SPIKE_RUNTIME_DIR}"
  assert_blocked_residual_rejected spike-runtime \
    "spike runtime subtree remains"
  rm -rf "${SPIKE_RUNTIME_DIR}"

  mkdir -p -m 0700 "$(dirname "$(tenant_kubeconfig spike)")"
  : >"$(tenant_kubeconfig spike)"
  assert_blocked_residual_rejected spike-kubeconfig \
    "spike kubeconfig remains"
  rm -rf "${SPIKE_RUNTIME_DIR}"

  (
    trap 'management_kubectl -n default delete service "${vip_service}" \
      --ignore-not-found --wait=true --timeout="${SPIKE_DELETE_TIMEOUT}" \
      >/dev/null 2>&1 || true' EXIT
    load_management_network
    management_kubectl -n default create service clusterip "${vip_service}" \
      --tcp=443:443 >/dev/null
    management_kubectl -n default patch service "${vip_service}" --type=merge \
      -p "{\"spec\":{\"externalIPs\":[\"${TENANT_A_VIP}\"]}}" >/dev/null
    assert_blocked_residual_rejected vip-claim \
      "borrowed tenant VIP ${TENANT_A_VIP} claim is present"
  )
}

assert_blocker_record_validation() {
  local saved_result="${CACHE_DIR}/e2e-saved-final-result.env"
  cp "${FINAL_RESULT_FILE}" "${saved_result}"

  write_blocked_result_fixture stale-revision worker-substrate \
    "recognized fixture evidence"
  assert_verify_rejects_blocked_record stale-revision

  write_blocked_result_fixture "${COMPATIBILITY_REVISION}" worker-substrate \
    "recognized fixture evidence"
  sed -i '/^blocker_evidence=/d' "${FINAL_RESULT_FILE}"
  assert_verify_rejects_blocked_record truncated

  write_blocked_result_fixture "${COMPATIBILITY_REVISION}" worker-substrate \
    "recognized fixture evidence"
  sed -i 's/^code=.*/code=kubeadm-bootstrap/' "${BLOCKER_FILE}"
  assert_verify_rejects_blocked_record mismatched

  write_blocked_result_fixture "${COMPATIBILITY_REVISION}" worker-substrate none
  assert_verify_rejects_blocked_record missing-evidence

  write_blocked_result_fixture "${COMPATIBILITY_REVISION}" worker-substrate \
    "recognized fixture evidence" failed
  assert_verify_rejects_blocked_record missing-cleanup

  write_blocked_result_fixture "${COMPATIBILITY_REVISION}" worker-substrate \
    "recognized fixture evidence"
  assert_verify_rejects_blocked_record residual-resource

  cp "${saved_result}" "${FINAL_RESULT_FILE}"
  rm -f "${saved_result}" "${BLOCKER_FILE}"
}

inject_tenant_remediation_drift() {
  local tenant="$1"
  local manifest="${CACHE_DIR}/${tenant}-kube-proxy-drift.json"
  management_kubectl -n "$(tenant_namespace "${tenant}")" \
    annotate "$(tenant_tcp_ref "${tenant}")" \
    kamaji.cnpg-vcluster.io/kube-proxy-remediation- >/dev/null
  tenant_kubectl "${tenant}" -n kube-system get configmap kube-proxy -o json \
    >"${manifest}"
  KUBE_PROXY_MANIFEST="${manifest}" python3 -c '
import json, os, re
path=os.environ["KUBE_PROXY_MANIFEST"]
data=json.load(open(path, encoding="utf-8"))
config, count=re.subn(r"(?m)^  maxPerCore:.*$", "  maxPerCore: 1",
                      data["data"]["config.conf"])
assert count == 1
data["data"]["config.conf"]=config
metadata=data["metadata"]
for key in ("managedFields", "resourceVersion", "uid", "creationTimestamp"):
    metadata.pop(key, None)
data.pop("status", None)
open(path, "w", encoding="utf-8").write(json.dumps(data))
'
  tenant_kubectl "${tenant}" replace -f "${manifest}" >/dev/null
  rm -f "${manifest}"
  ! tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
    || die "${tenant} remediation drift fixture did not create drift"
}

recognized_blocker_cleanup() {
  local create_status="$1"
  [[ "${create_status}" -eq "${EXIT_BLOCKED}" ]] \
    || die "unexpected create failure exit ${create_status}"
  [[ -f "${BLOCKER_FILE}" && -f "${FINAL_RESULT_FILE}" ]] \
    || die "blocked create did not retain blocker/result evidence"
  code="$(sed -n 's/^code=//p' "${BLOCKER_FILE}")"
  [[ " ${COMPATIBILITY_BLOCKER_CODES} " == *" ${code} "* ]] \
    || die "blocked create used unrecognized code ${code}"
  grep -Fxq 'result=blocked' "${FINAL_RESULT_FILE}" \
    && grep -Fxq 'cleanup=proved' "${FINAL_RESULT_FILE}" \
    || die "blocked create did not prove tenant cleanup"
  cat "${BLOCKER_FILE}" "${FINAL_RESULT_FILE}" >>"${RESULT_LOG}"
  run_logged "${SCRIPT_DIR}/destroy.sh"
  run_logged "${SCRIPT_DIR}/destroy.sh"
  assert_sentinels_present
  [[ ! -e "${RUNTIME_DIR}" ]] || die "blocked cleanup retained runtime state"
  warn "end-to-end lifecycle encountered recognized compatibility blocker ${code}; see ${RESULT_LOG}"
  exit "${EXIT_BLOCKED}"
}

test_unowned_refusals() {
  local output status name volume
  name="$(worker_name tenant-a 1)"
  docker run -d --name "${name}" "${VERIFY_IMAGE}" sleep 3600 >/dev/null
  set +e
  output="$("${SCRIPT_DIR}/destroy-tenant.sh" tenant-a 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && docker container inspect "${name}" >/dev/null \
    || die "unowned same-name worker was not refused and preserved"
  docker rm -f "${name}" >/dev/null

  volume="$(worker_volume_name tenant-a 1)"
  docker volume create "${volume}" >/dev/null
  set +e
  output="$("${SCRIPT_DIR}/destroy-tenant.sh" tenant-a 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && docker volume inspect "${volume}" >/dev/null \
    || die "unowned same-name worker volume was not refused and preserved"
  docker volume rm "${volume}" >/dev/null

  docker run -d --name "$(management_node_name)" \
    --label "io.x-k8s.kind.cluster=${KIND_CLUSTER_NAME}" \
    "${VERIFY_IMAGE}" sleep 3600 >/dev/null
  set +e
  output="$("${SCRIPT_DIR}/destroy.sh" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && docker container inspect "$(management_node_name)" >/dev/null \
    || die "unowned same-name management object was not refused and preserved"
  docker rm -f "$(management_node_name)" >/dev/null

  name="$(worker_name tenant-a 1)"
  volume="$(worker_volume_name tenant-a 1)"
  docker volume create \
    --label "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    --label kamaji.cnpg-vcluster.io/tenant=tenant-a \
    --label kamaji.cnpg-vcluster.io/role=worker-var-lib \
    "${volume}" >/dev/null
  docker run -d --name "${name}" \
    --label "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    --label kamaji.cnpg-vcluster.io/tenant=tenant-a \
    --label kamaji.cnpg-vcluster.io/role=worker \
    --mount "type=volume,src=${volume},dst=/var/lib" \
    "${VERIFY_IMAGE}" sleep 3600 >/dev/null
  set +e
  output="$("${SCRIPT_DIR}/destroy.sh" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" \
    && -n "$(docker container inspect "${name}" 2>/dev/null)" ]] \
    || die "owned worker without a record was not refused and preserved"
  mkdir -p -m 0700 "$(dirname "$(worker_ownership_file tenant-a 1)")"
  {
    printf 'WORKER_NAME=%s\n' "${name}"
    printf 'WORKER_ID=wrong-id\n'
    printf 'WORKER_VOLUME=%s\n' "${volume}"
    printf 'WORKER_VOLUME_ID=%s\n' \
      "$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${volume}")"
  } | write_secret_file "$(worker_ownership_file tenant-a 1)"
  set +e
  output="$("${SCRIPT_DIR}/destroy.sh" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" \
    && -n "$(docker container inspect "${name}" 2>/dev/null)" ]] \
    || die "mismatched worker record was not refused and preserved"
  docker rm -f "${name}" >/dev/null
  docker volume rm "${volume}" >/dev/null
  rm -rf "${RUNTIME_DIR}"

  docker run -d --name "${LAB_PREFIX}-unexpected-owned-sentinel" \
    --label "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    --label kamaji.cnpg-vcluster.io/role=unexpected \
    "${VERIFY_IMAGE}" sleep 3600 >/dev/null
  set +e
  output="$("${SCRIPT_DIR}/destroy.sh" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "${output}" >>"${RESULT_LOG}"
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && docker container inspect "${LAB_PREFIX}-unexpected-owned-sentinel" >/dev/null \
    || die "unexpected ownership-labelled sentinel was swept"
  docker rm -f "${LAB_PREFIX}-unexpected-owned-sentinel" >/dev/null
}

require_exact_just
ensure_tools_layout
: >"${RESULT_LOG}"
chmod 0600 "${RESULT_LOG}"
trap finish_e2e EXIT
run_logged "${SCRIPT_DIR}/tools.sh"
run_logged "${SCRIPT_DIR}/destroy.sh"
run_logged "${SCRIPT_DIR}/destroy.sh"

mkdir -p -m 0700 "${CACHE_DIR}"
: >"${RESULT_LOG}"
chmod 0600 "${RESULT_LOG}"

docker run -d --name "${SENTINEL_CONTAINER}" "${VERIFY_IMAGE}" sleep 3600 >/dev/null
docker volume create "${SENTINEL_VOLUME}" >/dev/null
kind create cluster --name "${SENTINEL_CLUSTER}" \
  --image "${KIND_NODE_IMAGE}" \
  --kubeconfig "${SENTINEL_KUBECONFIG}" \
  --wait "${KIND_CREATE_TIMEOUT}" >>"${RESULT_LOG}" 2>&1
chmod 0600 "${SENTINEL_KUBECONFIG}"
assert_sentinels_present
assert_effective_request_fixtures

assert_observer_read_only "${CACHE_DIR}/e2e-clean-status.log" \
  "${EXIT_ERROR}" "${SCRIPT_DIR}/status.sh"
assert_observer_read_only "${CACHE_DIR}/e2e-clean-diagnose.log" \
  "${EXIT_SUCCESS}" "${SCRIPT_DIR}/diagnose.sh" all
[[ ! -e "${RUNTIME_DIR}" ]] || die "clean observers created runtime state"

assert_capacity_failure KAMAJI_PREFLIGHT_CPU_FIXTURE 11 cpu
assert_capacity_failure KAMAJI_PREFLIGHT_MEMORY_BYTES_FIXTURE \
  $((23 * 1024 * 1024 * 1024)) memory
assert_capacity_failure KAMAJI_PREFLIGHT_STORAGE_BYTES_FIXTURE \
  $((29 * 1024 * 1024 * 1024)) storage
assert_capacity_failure KAMAJI_PREFLIGHT_INOTIFY_INSTANCES_FIXTURE 1023 \
  inotify-instances
KAMAJI_PREFLIGHT_INOTIFY_INSTANCES_FIXTURE="${MIN_INOTIFY_INSTANCES}" \
  assert_capacity_failure KAMAJI_PREFLIGHT_INOTIFY_WATCHES_FIXTURE \
    $((MIN_INOTIFY_WATCHES - 1)) inotify-watches

run_logged "${SCRIPT_DIR}/prepare-host.sh"
original_instances="$(sed -n 's/^MAX_USER_INSTANCES=//p' "${HOST_SYSCTL_STATE_FILE}")"
original_watches="$(sed -n 's/^MAX_USER_WATCHES=//p' "${HOST_SYSCTL_STATE_FILE}")"
assert_observer_read_only "${CACHE_DIR}/e2e-partial-status.log" \
  "${EXIT_ERROR}" "${SCRIPT_DIR}/status.sh"
assert_observer_read_only "${CACHE_DIR}/e2e-partial-diagnose.log" \
  "${EXIT_SUCCESS}" "${SCRIPT_DIR}/diagnose.sh" all

set +e
run_logged "${SCRIPT_DIR}/create.sh"
create_status=$?
set -e
if (( create_status != EXIT_SUCCESS )); then
  recognized_blocker_cleanup "${create_status}"
fi

for tenant in ${TENANT_NAMES}; do
  assert_marker_table_absent "${tenant}"
done
run_logged "${SCRIPT_DIR}/destroy-tenant.sh" tenant-a
assert_marker_table_absent tenant-b
run_logged "${SCRIPT_DIR}/create.sh"
for tenant in ${TENANT_NAMES}; do
  wait_for "${CNPG_TIMEOUT}" "${tenant} stable CNPG health after unseeded teardown recovery" \
    cnpg_tenant_ready "${tenant}" \
    || die "${tenant} did not settle after unseeded teardown recovery"
  assert_marker_table_absent "${tenant}"
done

seed_markers
worker_before="$(worker_identities)"
clusters_before="$(cluster_identities)"
storage_before="$(storage_identities)"
marker_a_before="$(marker_value tenant-a)"
marker_b_before="$(marker_value tenant-b)"
run_logged "${SCRIPT_DIR}/create.sh"
[[ "$(worker_identities)" == "${worker_before}" ]] \
  || die "repeat create replaced a healthy worker"
[[ "$(cluster_identities)" == "${clusters_before}" ]] \
  || die "repeat create replaced a CNPG Cluster"
[[ "$(storage_identities)" == "${storage_before}" ]] \
  || die "repeat create replaced a PVC or PV"
[[ "$(marker_value tenant-a)" == "${marker_a_before}" \
  && "$(marker_value tenant-b)" == "${marker_b_before}" ]] \
  || die "repeat create changed a database marker"

rm -f "${MANAGEMENT_KUBECONFIG}" "${BLOCKER_FILE}"
set +e
env KAMAJI_TEST_INJECT_RECOGNIZED_WORKER_FAILURE=tenant-a \
  "${SCRIPT_DIR}/create.sh" >>"${RESULT_LOG}" 2>&1
captured_blocker_status=$?
set -e
[[ "${captured_blocker_status}" -eq "${EXIT_ERROR}" ]] \
  || die "recognized blocker after management kubeconfig loss returned ${captured_blocker_status}, expected exit 1"
[[ -f "${MANAGEMENT_KUBECONFIG}" \
  && "$(stat -c '%a' "${MANAGEMENT_KUBECONFIG}")" == 600 ]] \
  || die "management kubeconfig was not securely re-exported before tenant capture"
[[ ! -e "${BLOCKER_FILE}" \
  && "$(worker_identities)" == "${worker_before}" \
  && "$(cluster_identities)" == "${clusters_before}" \
  && "$(storage_identities)" == "${storage_before}" \
  && "$(marker_value tenant-a)" == "${marker_a_before}" \
  && "$(marker_value tenant-b)" == "${marker_b_before}" \
  && "$(docker ps -aq --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker' | wc -l)" -eq 6 ]] \
  || die "fail-closed tenant capture did not preserve both tenants, PVCs, markers, and six workers"
grep -Fxq 'result=error' "${FINAL_RESULT_FILE}" \
  && grep -Fxq 'cleanup=not-attempted' "${FINAL_RESULT_FILE}" \
  && grep -Fq 'retained_tenants=tenant-a tenant-b' "${FINAL_RESULT_FILE}" \
  || die "captured recognized blocker did not record truthful retained state"
run_logged "${SCRIPT_DIR}/create.sh"

repair_workers_before="$(worker_identities)"
repair_clusters_before="$(cluster_identities)"
repair_storage_before="$(storage_identities)"
repair_marker_a_before="$(marker_value tenant-a)"
repair_marker_b_before="$(marker_value tenant-b)"
management_kubectl -n "$(tenant_namespace tenant-a)" \
  label "$(tenant_tcp_ref tenant-a)" "${OWNERSHIP_LABEL}-" >/dev/null
set +e
"${SCRIPT_DIR}/repair-tenant.sh" tenant-a >>"${RESULT_LOG}" 2>&1
repair_refusal_status=$?
set -e
[[ "${repair_refusal_status}" -eq "${EXIT_ERROR}" ]] \
  || die "repair did not refuse a TCP without exact ownership"
[[ "$(worker_identities)" == "${repair_workers_before}" \
  && "$(cluster_identities)" == "${repair_clusters_before}" \
  && "$(storage_identities)" == "${repair_storage_before}" \
  && "$(marker_value tenant-a)" == "${repair_marker_a_before}" \
  && "$(marker_value tenant-b)" == "${repair_marker_b_before}" ]] \
  || die "ownership-gated repair refusal changed healthy identities or data"
management_kubectl -n "$(tenant_namespace tenant-a)" \
  label "$(tenant_tcp_ref tenant-a)" \
  "${OWNERSHIP_LABEL}=${LAB_PREFIX}" --overwrite >/dev/null
inject_tenant_remediation_drift tenant-a
run_logged "${SCRIPT_DIR}/repair-tenant.sh" tenant-a
load_management_network
tenant_kube_proxy_steady_state_is_preserved tenant-a \
  && final_tenant_tcp_ready tenant-a \
  && tenant_workers_ready tenant-a \
  && cnpg_tenant_ready tenant-a \
  || die "repair did not restore the Ready paused remediation state"
[[ "$(worker_identities)" == "${repair_workers_before}" \
  && "$(cluster_identities)" == "${repair_clusters_before}" \
  && "$(storage_identities)" == "${repair_storage_before}" \
  && "$(marker_value tenant-a)" == "${repair_marker_a_before}" \
  && "$(marker_value tenant-b)" == "${repair_marker_b_before}" ]] \
  || die "repair replaced a worker, CNPG Cluster, PVC/PV, or marker"

rm -f "${BLOCKER_FILE}"
set +e
env KAMAJI_TEST_INJECT_ADDON_FAILURE=tenant-a \
  "${SCRIPT_DIR}/create.sh" >>"${RESULT_LOG}" 2>&1
ordinary_status=$?
set -e
[[ "${ordinary_status}" -eq "${EXIT_ERROR}" ]] \
  || die "ordinary add-on failure returned ${ordinary_status}, expected exit 1"
[[ ! -e "${BLOCKER_FILE}" ]] \
  || die "ordinary add-on failure created a compatibility blocker record"
grep -Fxq 'result=error' "${FINAL_RESULT_FILE}" \
  && grep -Fxq 'cleanup=not-attempted' "${FINAL_RESULT_FILE}" \
  || die "ordinary add-on failure did not record an error result"
[[ "$(worker_identities)" == "${worker_before}" \
  && "$(cluster_identities)" == "${clusters_before}" \
  && "$(marker_value tenant-a)" == "${marker_a_before}" \
  && "$(marker_value tenant-b)" == "${marker_b_before}" ]] \
  || die "ordinary add-on failure damaged a healthy tenant or database"

set +e
env KAMAJI_TEST_DATASTORE_UNAVAILABLE=1 \
  "${SCRIPT_DIR}/destroy-tenant.sh" tenant-a >>"${RESULT_LOG}" 2>&1
datastore_status=$?
set -e
[[ "${datastore_status}" -eq "${EXIT_ERROR}" ]] \
  || die "datastore-unavailable targeted teardown did not return exit 1"
[[ "$(worker_identities)" == "${worker_before}" \
  && "$(cluster_identities)" == "${clusters_before}" \
  && "$(marker_value tenant-a)" == "${marker_a_before}" \
  && "$(marker_value tenant-b)" == "${marker_b_before}" \
  && -d "$(tenant_runtime_dir tenant-a)" ]] \
  || die "datastore-unavailable targeted teardown removed retry evidence or tenant data"

stopped_worker="$(worker_name tenant-a 1)"
stopped_id="$(docker container inspect --format '{{.Id}}' "${stopped_worker}")"
stopped_uid="$(tenant_kubectl tenant-a get node "${stopped_worker}" -o jsonpath='{.metadata.uid}')"
docker exec "${stopped_worker}" mkdir -p /var/lib/kamaji-e2e /var/lib/containerd/kamaji-e2e
docker exec "${stopped_worker}" sh -c \
  'printf stopped-data > /var/lib/kamaji-e2e/stopped; printf stopped-cache > /var/lib/containerd/kamaji-e2e/cache'
docker stop "${stopped_worker}" >/dev/null
run_logged env SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh"
[[ "$(docker container inspect --format '{{.Id}}' "${stopped_worker}")" == "${stopped_id}" \
  && "$(tenant_kubectl tenant-a get node "${stopped_worker}" -o jsonpath='{.metadata.uid}')" == "${stopped_uid}" \
  && "$(docker exec "${stopped_worker}" cat /var/lib/kamaji-e2e/stopped)" == stopped-data \
  && "$(docker exec "${stopped_worker}" cat /var/lib/containerd/kamaji-e2e/cache)" == stopped-cache ]] \
  || die "stopped-worker readiness grace replaced node, container, data, or cache identity"

interrupted_worker="$(worker_name tenant-a 3)"
tokens_before="$(bootstrap_token_inventory tenant-a)"
tenant_kubectl tenant-a delete node "${interrupted_worker}" \
  --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" >/dev/null
docker exec "${interrupted_worker}" kubeadm reset --force \
  --cri-socket unix:///run/containerd/containerd.sock >/dev/null 2>&1 || true
set +e
env SKIP_CNPG=1 KAMAJI_TEST_FAIL_AFTER_FINAL_JOIN="${interrupted_worker}" \
  "${SCRIPT_DIR}/create.sh" >>"${RESULT_LOG}" 2>&1
join_status=$?
set -e
[[ "${join_status}" -eq "${EXIT_ERROR}" ]] \
  || die "injected post-join failure did not remain an ordinary exit 1"
[[ ! -e "$(tenant_runtime_dir tenant-a)/join/${interrupted_worker}" ]] \
  && ! docker exec "${interrupted_worker}" test -e /var/lib/kamaji-final-join \
  && [[ "$(bootstrap_token_inventory tenant-a)" == "${tokens_before}" ]] \
  || die "interrupted final-worker join retained admin.conf, join script, token, or host material"
run_logged env SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh"
tenant_workers_ready tenant-a \
  || die "worker did not recover after interruption-safe join cleanup"

cat <<EOF | tenant_kubectl tenant-b apply -f - >/dev/null
apiVersion: v1
kind: Node
metadata:
  name: unexpected-notready-node
spec:
  unschedulable: true
EOF
set +e
env SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh" >>"${RESULT_LOG}" 2>&1
topology_status=$?
"${SCRIPT_DIR}/status.sh" >>"${RESULT_LOG}" 2>&1
status_topology=$?
set -e
[[ "${topology_status}" -eq "${EXIT_ERROR}" \
  && "${status_topology}" -eq "${EXIT_ERROR}" ]] \
  || die "extra unlabeled NotReady Node was not rejected by create and status"
tenant_kubectl tenant-b delete node unexpected-notready-node \
  --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" >/dev/null
run_logged env SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh"
for tenant in ${TENANT_NAMES}; do
  wait_for "${CNPG_TIMEOUT}" "${tenant} stable CNPG health after repeat create" \
    cnpg_tenant_ready "${tenant}" \
    || die "${tenant} did not settle after repeat create"
done
wait_for "${CNPG_TIMEOUT}" "healthy read-only observers" healthy_observers_ready \
  || die "healthy status/diagnostics did not settle"

assert_observer_read_only "${CACHE_DIR}/e2e-healthy-status.log" \
  "${EXIT_SUCCESS}" "${SCRIPT_DIR}/status.sh"
assert_observer_read_only "${CACHE_DIR}/e2e-healthy-diagnose.log" \
  "${EXIT_SUCCESS}" "${SCRIPT_DIR}/diagnose.sh" all

rm -f "$(tenant_kubeconfig tenant-a)"
run_logged env SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh"
[[ -f "$(tenant_kubeconfig tenant-a)" \
  && "$(stat -c '%a' "$(tenant_kubeconfig tenant-a)")" == 600 ]] \
  || die "deleted tenant kubeconfig was not securely re-exported"
grep -Fxq 'result=partial' "${FINAL_RESULT_FILE}" \
  && grep -Fxq 'cnpg=skipped' "${FINAL_RESULT_FILE}" \
  || die "SKIP_CNPG=1 did not record a partial skipped result"

partial_worker="$(worker_name tenant-a 3)"
partial_ids_before="$(worker_identities)"
docker cp "$(tenant_kubeconfig tenant-a)" \
  "${partial_worker}:/var/lib/kamaji-e2e-admin.conf" >/dev/null
docker exec "${partial_worker}" kubeadm \
  --kubeconfig=/var/lib/kamaji-e2e-admin.conf token create --ttl 1s \
  >/dev/null
sleep 2
docker exec "${partial_worker}" rm -f /var/lib/kamaji-e2e-admin.conf
tenant_kubectl tenant-a delete node "${partial_worker}" \
  --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" >/dev/null
docker exec "${partial_worker}" kubeadm reset --force \
  --cri-socket unix:///run/containerd/containerd.sock >/dev/null 2>&1 || true
docker exec "${partial_worker}" mkdir -p /var/lib/kamaji-final-join
docker exec "${partial_worker}" sh -c \
  'printf stale-expired-join > /var/lib/kamaji-final-join/expired'
run_logged env SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh"
[[ "$(worker_identities)" == "${partial_ids_before}" ]] \
  || die "partial/expired join recovery replaced an unaffected container"
tenant_workers_ready tenant-a \
  || die "partial/expired join recovery did not restore tenant-a"

missing_worker="$(worker_name tenant-b 2)"
missing_volume="$(worker_volume_name tenant-b 2)"
volume_id_before="$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${missing_volume}")"
missing_ids_before="$(worker_identities)"
docker exec "${missing_worker}" mkdir -p /var/lib/kamaji-e2e
docker exec "${missing_worker}" sh -c \
  'printf retained-worker-value > /var/lib/kamaji-e2e/value'
docker rm -f "${missing_worker}" >/dev/null
run_logged env SKIP_CNPG=1 "${SCRIPT_DIR}/create.sh"
[[ "$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${missing_volume}")" \
  == "${volume_id_before}" ]] \
  || die "missing-worker recovery replaced its owned volume"
[[ "$(docker exec "${missing_worker}" cat /var/lib/kamaji-e2e/value)" \
  == retained-worker-value ]] \
  || die "missing-worker recovery lost the persistent value"
while IFS='=' read -r name id; do
  [[ "${name}" == "${missing_worker}" ]] && continue
  grep -Fxq "${name}=${id}" <<<"$(worker_identities)" \
    || die "missing-worker recovery replaced ${name}"
done <<<"${missing_ids_before}"

run_logged "${SCRIPT_DIR}/create.sh"
for tenant in ${TENANT_NAMES}; do
  wait_for "${CNPG_TIMEOUT}" "${tenant} stable CNPG health before verification" \
    cnpg_tenant_ready "${tenant}" \
    || die "${tenant} did not settle before verification"
done
assert_blocker_record_validation
set +e
env KAMAJI_TEST_FAIL_AFTER_CROSS_AUTH_SECRET=tenant-a \
  "${SCRIPT_DIR}/verify.sh" >>"${RESULT_LOG}" 2>&1
cross_auth_status=$?
set -e
[[ "${cross_auth_status}" -eq "${EXIT_ERROR}" ]] \
  || die "cross-auth interruption fixture did not fail with exit 1"
! tenant_kubectl tenant-b -n "${DATABASE_NAMESPACE}" \
  get secret cnpg-cross-auth-tenant-a >/dev/null 2>&1 \
  || die "cross-auth interruption retained the exact credential Secret"
cnpg_tenant_ready tenant-a && cnpg_tenant_ready tenant-b \
  || die "cross-auth interruption damaged a tenant database"
run_logged "${SCRIPT_DIR}/verify.sh"
run_logged "${SCRIPT_DIR}/destroy-tenant.sh" tenant-a
load_management_network
final_tenant_exists tenant-b \
  && final_tenant_tcp_ready tenant-b \
  && tenant_kube_proxy_steady_state_is_preserved tenant-b \
  && tenant_workers_ready tenant-b \
  && cnpg_tenant_ready tenant-b \
  && cnpg_verify_marker_if_present tenant-b \
  || die "tenant-b did not survive tenant-a teardown"
assert_observer_read_only "${CACHE_DIR}/e2e-one-tenant-status.log" \
  "${EXIT_ERROR}" "${SCRIPT_DIR}/status.sh"
assert_observer_read_only "${CACHE_DIR}/e2e-one-tenant-diagnose.log" \
  "${EXIT_ERROR}" "${SCRIPT_DIR}/diagnose.sh" all

run_logged "${SCRIPT_DIR}/destroy-tenant.sh" tenant-b
write_blocked_result_fixture "${COMPATIBILITY_REVISION}" worker-substrate \
  "recognized final compatibility fixture"
assert_exact_blocked_residual_validation
set +e
"${SCRIPT_DIR}/verify.sh" >>"${RESULT_LOG}" 2>&1
blocked_verify_status=$?
"${SCRIPT_DIR}/status.sh" >>"${RESULT_LOG}" 2>&1
blocked_status_status=$?
set -e
[[ "${blocked_verify_status}" -eq "${EXIT_BLOCKED}" \
  && "${blocked_status_status}" -eq "${EXIT_SUCCESS}" ]] \
  || die "current consistent blocker with allowed residual state was not accepted"

run_logged "${SCRIPT_DIR}/destroy.sh"
run_logged "${SCRIPT_DIR}/destroy.sh"
assert_sentinels_present
[[ ! -e "${RUNTIME_DIR}" ]] || die "full destroy retained runtime state"
[[ "$(read_inotify_value max_user_instances)" == "${original_instances}" \
  && "$(read_inotify_value max_user_watches)" == "${original_watches}" ]] \
  || die "full destroy did not restore the recorded host inotify values"
[[ -z "$(docker ps -aq --filter "$(owned_docker_filter)")" \
  && -z "$(docker volume ls -q --filter "$(owned_docker_filter)")" ]] \
  || die "full destroy retained owned Docker resources"
! kind get clusters | grep -Fxq "${KIND_CLUSTER_NAME}" \
  || die "full destroy retained the owned kind cluster"

test_unowned_refusals
assert_sentinels_present
rm -rf "${RUNTIME_DIR}"
[[ ! -e "${RUNTIME_DIR}" ]] \
  || die "ownership refusal fixtures retained runtime state"

if grep -Ehiv 'webhookconfiguration' \
  "${RESULT_LOG}" "${CACHE_DIR}"/e2e-*.log \
  | grep -Eqi \
    'BEGIN [A-Z ]*PRIVATE KEY|client-key-data:|client-certificate-data:|certificate-authority-data:|PGPASSWORD=|[a-z0-9]{6}\.[a-z0-9]{16}'; then
  die "captured lifecycle output contains credential material"
fi

log "complete Kamaji lifecycle, recovery, teardown, restoration, and sentinel coverage passed"
