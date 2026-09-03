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
  kind delete cluster --name "${SENTINEL_CLUSTER}" \
    --kubeconfig "${SENTINEL_KUBECONFIG}" >/dev/null 2>&1 || true
  rm -f "${SENTINEL_KUBECONFIG}"
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
}

require_exact_just
ensure_tools_layout
: >"${RESULT_LOG}"
chmod 0600 "${RESULT_LOG}"
run_logged "${SCRIPT_DIR}/tools.sh"
run_logged "${SCRIPT_DIR}/destroy.sh"
run_logged "${SCRIPT_DIR}/destroy.sh"

mkdir -p -m 0700 "${CACHE_DIR}"
: >"${RESULT_LOG}"
chmod 0600 "${RESULT_LOG}"
trap cleanup_sentinels EXIT

docker run -d --name "${SENTINEL_CONTAINER}" "${VERIFY_IMAGE}" sleep 3600 >/dev/null
docker volume create "${SENTINEL_VOLUME}" >/dev/null
kind create cluster --name "${SENTINEL_CLUSTER}" \
  --image "${KIND_NODE_IMAGE}" \
  --kubeconfig "${SENTINEL_KUBECONFIG}" \
  --wait "${KIND_CREATE_TIMEOUT}" >>"${RESULT_LOG}" 2>&1
chmod 0600 "${SENTINEL_KUBECONFIG}"
assert_sentinels_present

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
marker_a_before="$(marker_value tenant-a)"
marker_b_before="$(marker_value tenant-b)"
run_logged "${SCRIPT_DIR}/create.sh"
[[ "$(worker_identities)" == "${worker_before}" ]] \
  || die "repeat create replaced a healthy worker"
[[ "$(cluster_identities)" == "${clusters_before}" ]] \
  || die "repeat create replaced a CNPG Cluster"
[[ "$(marker_value tenant-a)" == "${marker_a_before}" \
  && "$(marker_value tenant-b)" == "${marker_b_before}" ]] \
  || die "repeat create changed a database marker"
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

trap - EXIT
cleanup_sentinels
log "complete Kamaji lifecycle, recovery, teardown, restoration, and sentinel coverage passed"
