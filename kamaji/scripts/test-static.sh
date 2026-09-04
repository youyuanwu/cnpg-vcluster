#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/management.sh"

failures=0
checks=0

check() {
  local description="$1"
  shift
  checks=$((checks + 1))
  if "$@"; then
    :
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

files_identical_to_main() {
  local relative="$1"
  git -C "${LAB_ROOT}/.." show "main:${relative}" | cmp - "${LAB_ROOT}/../${relative}"
}

runtime_fingerprint() {
  if [[ -d "${RUNTIME_DIR}" ]]; then
    find "${RUNTIME_DIR}" -printf '%M %s %T@ %P\n' | sort | sha256sum
  else
    printf 'absent\n'
  fi
}

owned_docker_count() {
  docker ps -aq --filter "$(owned_docker_filter)" | wc -l
  docker volume ls -q --filter "$(owned_docker_filter)" | wc -l
  docker ps -aq --filter 'name=^kamaji-prerequisite-probe-' | wc -l
}

capacity_fixture_rejects() {
  local fixture_name="$1"
  local fixture_value="$2"
  local expected_reason="$3"
  local before_runtime before_docker before_clusters output status

  before_runtime="$(runtime_fingerprint)"
  before_docker="$(owned_docker_count)"
  before_clusters="$("${BIN_DIR}/kind" get clusters 2>/dev/null | sort || true)"
  set +e
  output="$(
    env \
      "KAMAJI_PREFLIGHT_${fixture_name}=${fixture_value}" \
      KAMAJI_PREFLIGHT_FORBID_MUTATION_FIXTURE=1 \
      "${SCRIPT_DIR}/preflight.sh" 2>&1
  )"
  status=$?
  set -e

  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && grep -Fq "capacity.${expected_reason}" <<<"${output}" \
    && [[ "$(runtime_fingerprint)" == "${before_runtime}" ]] \
    && [[ "$(owned_docker_count)" == "${before_docker}" ]] \
    && [[ "$("${BIN_DIR}/kind" get clusters 2>/dev/null | sort || true)" == "${before_clusters}" ]]
}

inotify_watch_fixture_rejects() {
  KAMAJI_PREFLIGHT_INOTIFY_INSTANCES_FIXTURE="${MIN_INOTIFY_INSTANCES}" \
    capacity_fixture_rejects INOTIFY_WATCHES_FIXTURE \
      "$((MIN_INOTIFY_WATCHES - 1))" inotify-watches
}

observer_is_read_only() {
  local before_runtime before_resources after_resources output status resources_ok=1
  before_runtime="$(runtime_fingerprint)"
  if [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
    && management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    before_resources="$(
      management_kubectl get namespaces,deployments,statefulsets,daemonsets,pvc \
        --all-namespaces -o name 2>/dev/null | sort
    )" || resources_ok=0
  else
    before_resources="unavailable"
  fi
  set +e
  output="$("$@" 2>&1)"
  status=$?
  set -e
  after_resources="${before_resources}"
  if [[ "${before_resources}" != unavailable ]]; then
    after_resources="$(
      management_kubectl get namespaces,deployments,statefulsets,daemonsets,pvc \
        --all-namespaces -o name 2>/dev/null | sort
    )" || resources_ok=0
  fi
  [[ "${status}" -eq "${EXIT_SUCCESS}" || "${status}" -eq "${EXIT_ERROR}" ]] \
    && [[ "$(runtime_fingerprint)" == "${before_runtime}" ]] \
    && (( resources_ok == 1 )) \
    && [[ "${after_resources}" == "${before_resources}" ]] \
    && [[ -n "${output}" ]]
}

hostile_observer_is_rejected() (
  local fixture_dir="${TOOLS_TMP_DIR}/observer-hostile-fixture"
  rm -rf "${fixture_dir}"
  mkdir -p -m 0700 "${fixture_dir}"
  trap 'rm -rf "${fixture_dir}"' EXIT
  RUNTIME_DIR="${fixture_dir}"
  MANAGEMENT_KUBECONFIG="${fixture_dir}/missing-kubeconfig"

  hostile_exit_observer() {
    printf 'hostile observer output\n'
    touch "${RUNTIME_DIR}/mutated"
    return "${EXIT_BLOCKED}"
  }

  hostile_mutation_observer() {
    printf 'hostile observer output\n'
    touch "${RUNTIME_DIR}/mutated"
    return "${EXIT_SUCCESS}"
  }

  ! observer_is_read_only hostile_exit_observer \
    && rm -f "${RUNTIME_DIR}/mutated" \
    && ! observer_is_read_only hostile_mutation_observer
)

optional_marker_policy_fixture() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/cnpg.sh"
  local state="$1"

  cnpg_run_sql() {
    local tenant="$1"
    local sql="$2"
    if [[ "${sql}" == *"to_regclass"* ]]; then
      if [[ "${state}" == unseeded ]]; then
        printf 'absent\n'
      else
        printf 'kamaji_verification\n'
      fi
    elif [[ "${state}" == seeded-correct ]]; then
      cnpg_database_marker "${tenant}"
    else
      printf 'wrong-marker\n'
    fi
  }

  case "${state}" in
    unseeded|seeded-correct)
      cnpg_verify_marker_if_present tenant-b
      ;;
    seeded-wrong)
      ! cnpg_verify_marker_if_present tenant-b
      ;;
  esac
)

control_plane_oom_fixture() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/tenants.sh"

  management_kubectl() {
    cat <<'JSON'
{"items":[{"metadata":{"name":"tenant-a-abc"},"status":{"containerStatuses":[
  {"name":"kube-apiserver","restartCount":2,"state":{"running":{"startedAt":"now"}},"lastState":{"terminated":{"reason":"OOMKilled","exitCode":137}}},
  {"name":"kube-scheduler","restartCount":0,"state":{"running":{"startedAt":"now"}},"lastState":{}}
]}}]}
JSON
  }

  output="$(tenant_control_plane_container_statuses tenant-a)"
  grep -Fq 'container=kube-apiserver restarts=2 state=running last_reason=OOMKilled last_exit=137' \
    <<<"${output}" \
    && grep -Fq 'container=kube-scheduler restarts=0 state=running last_reason=none last_exit=none' \
      <<<"${output}" \
    && tenant_control_plane_oom_evidence tenant-a \
      | grep -Fq 'last_reason=OOMKilled'
)

control_plane_status_absent_fixture() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/tenants.sh"

  management_kubectl() {
    return 1
  }

  local output status
  set +e
  output="$(tenant_control_plane_container_statuses tenant-a 2>&1)"
  status=$?
  set -e
  [[ "${status}" -eq "${EXIT_SUCCESS}" && -z "${output}" ]]
)

services_claiming_vip_fixture() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/tenants.sh"

  management_kubectl() {
    cat <<'JSON'
{"items":[
  {"metadata":{"namespace":"tenant-system","name":"spec-ip"},"spec":{"loadBalancerIP":"172.18.255.254"}},
  {"metadata":{"namespace":"tenant-system","name":"external-ip"},"spec":{"externalIPs":["172.18.255.254"]}},
  {"metadata":{"namespace":"tenant-system","name":"annotation-ip","annotations":{"metallb.io/loadBalancerIPs":"172.18.255.253,172.18.255.254"}},"spec":{}},
  {"metadata":{"name":"status-ip"},"spec":{},"status":{"loadBalancer":{"ingress":[{"ip":"172.18.255.254"}]}}},
  {"metadata":{"namespace":"tenant-system","name":"other-ip"},"spec":{"loadBalancerIP":"172.18.255.252"}}
]}
JSON
  }

  [[ "$(services_claiming_vip "172.18.255.254")" == $'tenant-system/spec-ip\ntenant-system/external-ip\ntenant-system/annotation-ip\ndefault/status-ip' ]]
)

spike_refusal_is_no_mutation() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/tenants.sh"
  local planted=0 runtime_existed=0 tenants_existed=0
  local before_runtime before_docker before_tcps output status
  [[ -d "${RUNTIME_DIR}" ]] && runtime_existed=1
  [[ -d "${RUNTIME_DIR}/tenants" ]] && tenants_existed=1
  if ! final_tenant_state_reason >/dev/null 2>&1; then
    mkdir -p -m 0700 "$(dirname "$(tenant_kubeconfig tenant-a)")"
    printf 'final-state-fixture\n' | write_secret_file "$(tenant_kubeconfig tenant-a)"
    planted=1
  fi
  before_runtime="$(runtime_fingerprint)"
  before_docker="$(owned_docker_count)"
  before_tcps="$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
    --all-namespaces -o json 2>/dev/null | sha256sum || true)"
  set +e
  output="$("${SCRIPT_DIR}/create-spike.sh" 2>&1)"
  status=$?
  set -e
  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && grep -Fq 'spike.final-state-refusal' <<<"${output}" \
    && [[ "$(runtime_fingerprint)" == "${before_runtime}" ]] \
    && [[ "$(owned_docker_count)" == "${before_docker}" ]] \
    && [[ "$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
      --all-namespaces -o json 2>/dev/null | sha256sum || true)" == "${before_tcps}" ]]
  local result=$?
  if (( planted == 1 )); then
    rm -f "$(tenant_kubeconfig tenant-a)"
    rmdir "$(dirname "$(tenant_kubeconfig tenant-a)")" 2>/dev/null || true
    (( tenants_existed == 1 )) \
      || rmdir "${RUNTIME_DIR}/tenants" 2>/dev/null || true
    (( runtime_existed == 1 )) \
      || rmdir "${RUNTIME_DIR}" 2>/dev/null || true
  fi
  return "${result}"
)

tenant_addon_render_fixture() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/tenants.sh"
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/addons.sh"
  local fixture_dir="${TOOLS_TMP_DIR}/tenant-addon-render-fixture"
  rm -rf "${fixture_dir}"
  mkdir -p -m 0700 "${fixture_dir}"
  trap 'rm -rf "${fixture_dir}"' EXIT
  RUNTIME_DIR="${fixture_dir}"
  render_tenant_addons tenant-a
  render_tenant_addons tenant-b
  grep -Fq "value: \"${TENANT_A_POD_CIDR}\"" \
    "$(tenant_addon_dir tenant-a)/calico.yaml" \
    && grep -Fq "value: \"${TENANT_B_POD_CIDR}\"" \
      "$(tenant_addon_dir tenant-b)/calico.yaml" \
    && grep -Fq "\"paths\":[\"${TENANT_A_STORAGE_PATH}\"]" \
      "$(tenant_addon_dir tenant-a)/local-path.yaml" \
    && grep -Fq "\"paths\":[\"${TENANT_B_STORAGE_PATH}\"]" \
      "$(tenant_addon_dir tenant-b)/local-path.yaml" \
    && ! cmp -s "$(tenant_addon_dir tenant-a)/calico.yaml" \
      "$(tenant_addon_dir tenant-b)/calico.yaml" \
    && ! cmp -s "$(tenant_addon_dir tenant-a)/local-path.yaml" \
      "$(tenant_addon_dir tenant-b)/local-path.yaml"
)

cleanup_polarity_is_explicit() (
  local fixture_dir="${TOOLS_TMP_DIR}/cleanup-polarity-fixture"
  rm -rf "${fixture_dir}"
  mkdir -p -m 0700 "${fixture_dir}"
  trap 'rm -rf "${fixture_dir}"' EXIT
  touch "${fixture_dir}/component"

  cleanup_fixture_component() {
    rm -f "${fixture_dir}/component"
  }

  cleanup_if_introduced 0 cleanup_fixture_component
  [[ -f "${fixture_dir}/component" ]] \
    && cleanup_if_introduced 1 cleanup_fixture_component \
    && [[ ! -e "${fixture_dir}/component" ]]
)

fresh_cert_manager_failure_is_targeted() (
  local fixture_dir="${TOOLS_TMP_DIR}/cert-manager-failure-fixture"
  local output status component
  rm -rf "${fixture_dir}"
  mkdir -p -m 0700 "${fixture_dir}"
  trap 'rm -rf "${fixture_dir}"' EXIT
  CERT_MANAGER_RENDERED_MANIFEST="${fixture_dir}/cert-manager.yaml"
  for component in kubernetes metallb kamaji datastore; do
    touch "${fixture_dir}/${component}"
  done

  management_helm() {
    case "$1" in
      status)
        return 1
        ;;
      template)
        printf 'image: %s\nimage: %s\nimage: %s\nimage: %s\n' \
          "${CERT_MANAGER_CONTROLLER_IMAGE}" \
          "${CERT_MANAGER_WEBHOOK_IMAGE}" \
          "${CERT_MANAGER_CAINJECTOR_IMAGE}" \
          "${CERT_MANAGER_STARTUPAPICHECK_IMAGE}"
        ;;
      upgrade)
        touch "${fixture_dir}/cert-manager"
        return 1
        ;;
      uninstall)
        rm -f "${fixture_dir}/cert-manager"
        ;;
      *)
        return 1
        ;;
    esac
  }

  management_kubectl() {
    if [[ " $* " == *" delete namespace cert-manager "* ]]; then
      rm -f "${fixture_dir}/cert-manager"
    fi
    if [[ " $* " == *" get namespace cert-manager "* ]]; then
      [[ ! -f "${fixture_dir}/cert-manager" ]] \
        || printf 'namespace/cert-manager\n'
      return 0
    fi
    return 0
  }

  set +e
  output="$(reconcile_cert_manager 2>&1)"
  status=$?
  set -e

  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && [[ "${output}" == "[kamaji-lab] ERROR: management.cert-manager: ${CERT_MANAGER_VERSION} installation or readiness failed" ]] \
    && [[ ! -e "${fixture_dir}/cert-manager" ]] \
    && [[ "$(find "${fixture_dir}" -maxdepth 1 -type f ! -name cert-manager.yaml \
      -printf '%f\n' | sort)" == $'datastore\nkamaji\nkubernetes\nmetallb' ]]
)

etcd_probe_states_are_distinct() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/tenants.sh"
  local mode=present

  etcd_maintenance() {
    case "${mode}:$1:$2" in
      present:get:/*) printf '/schema/key\n' ;;
      absent:get:/*) return 0 ;;
      present:user:*|present:role:*) printf 'present\n' ;;
      absent:user:*) printf 'User %s does not exist\n' "$3" >&2; return 1 ;;
      absent:role:*) printf 'Role %s does not exist\n' "$3" >&2; return 1 ;;
      failed:*) printf 'maintenance pod failed\n' >&2; return 1 ;;
      *) return 1 ;;
    esac
  }

  [[ "$(etcd_prefix_state schema)" == present \
    && "$(etcd_user_state schema)" == present \
    && "$(etcd_role_state schema)" == present ]] || return 1
  mode=absent
  [[ "$(etcd_prefix_state schema)" == absent \
    && "$(etcd_user_state schema)" == absent \
    && "$(etcd_role_state schema)" == absent ]] || return 1
  mode=failed
  ! etcd_prefix_state schema >/dev/null \
    && ! etcd_user_state schema >/dev/null \
    && ! etcd_role_state schema >/dev/null
)

datastore_unavailable_is_not_absence() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/tenants.sh"
  KAMAJI_TEST_DATASTORE_UNAVAILABLE=1
  ! management_datastore_available
)

effective_request_fixtures() (
  local fixture sidecar_fixture
  fixture='{"items":[{"spec":{"containers":[{"resources":{"requests":{"cpu":"100m","memory":"64Mi"}}}],"initContainers":[{"resources":{"requests":{"cpu":"800m","memory":"700Mi"}}}],"overhead":{"cpu":"50m","memory":"100Mi"}},"status":{"phase":"Running"}}]}'
  printf '%s\n' "${fixture}" \
    | CPU_CAP=850000000 MEMORY_CAP=838860800 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null \
    && ! printf '%s\n' "${fixture}" \
      | CPU_CAP=849999999 MEMORY_CAP=838860800 \
        python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1 \
    && ! printf '%s\n' "${fixture}" \
      | CPU_CAP=850000000 MEMORY_CAP=838860799 \
        python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1
  sidecar_fixture='{"items":[{"spec":{"containers":[{"resources":{"requests":{"cpu":"100m","memory":"64Mi"}}}],"initContainers":[{"name":"sidecar","restartPolicy":"Always","resources":{"requests":{"cpu":"200m","memory":"128Mi"}}},{"name":"setup","resources":{"requests":{"cpu":"500m","memory":"512Mi"}}}],"overhead":{"cpu":"50m","memory":"64Mi"}},"status":{"phase":"Running"}}]}'
  printf '%s\n' "${sidecar_fixture}" \
    | CPU_CAP=750000000 MEMORY_CAP=738197504 \
      python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null \
    && ! printf '%s\n' "${sidecar_fixture}" \
      | CPU_CAP=749999999 MEMORY_CAP=738197504 \
        python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1 \
    && ! printf '%s\n' "${sidecar_fixture}" \
      | CPU_CAP=750000000 MEMORY_CAP=738197503 \
        python3 "${SCRIPT_DIR}/lib/effective_requests.py" >/dev/null 2>&1
)

host_just_resolution_fixtures() (
  local fixture_root="${TOOLS_TMP_DIR}/host-just-resolution-fixture"
  local nested_bin="${BIN_DIR}/host-just-fixture"
  local external_bin="${fixture_root}/external"
  local wrong_bin="${fixture_root}/wrong"
  local symlink_bin="${fixture_root}/symlink"
  local missing_bin="${fixture_root}/missing-parent/bin"
  local output status
  rm -rf "${fixture_root}" "${nested_bin}"
  mkdir -p -m 0700 \
    "${nested_bin}" "${external_bin}" "${wrong_bin}" "${symlink_bin}"
  trap 'rm -rf "${fixture_root}" "${nested_bin}"' EXIT
  printf '#!/usr/bin/env bash\nprintf "just %s\\n"\n' "${JUST_VERSION}" \
    >"${nested_bin}/just"
  printf '#!/usr/bin/env bash\nprintf "just %s\\n"\n' "${JUST_VERSION}" \
    >"${external_bin}/just"
  printf '#!/usr/bin/env bash\nprintf "just 0.0.0\\n"\n' \
    >"${wrong_bin}/just"
  chmod 0700 "${nested_bin}/just" "${external_bin}/just" "${wrong_bin}/just"
  ln -s "${BIN_DIR}" "${symlink_bin}/lab-bin"
  ln -s "${nested_bin}" "${symlink_bin}/nested-bin"

  cd "${LAB_ROOT}"
  HOST_PATH=".tools/bin:/usr/bin:/bin"
  ! resolve_host_just >/dev/null || return 1
  HOST_PATH=".tools/bin/host-just-fixture:/usr/bin:/bin"
  ! resolve_host_just >/dev/null || return 1
  HOST_PATH=".tools/tmp/host-just-resolution-fixture/symlink/lab-bin:/usr/bin:/bin"
  ! resolve_host_just >/dev/null || return 1
  HOST_PATH=".tools/tmp/host-just-resolution-fixture/symlink/nested-bin:/usr/bin:/bin"
  ! resolve_host_just >/dev/null || return 1
  HOST_PATH=".tools/tmp/host-just-resolution-fixture/external:/usr/bin:/bin"
  [[ "$(resolve_host_just)" == "$(readlink -f "${external_bin}/just")" ]] \
    && require_exact_just || return 1

  BIN_DIR="${missing_bin}"
  HOST_PATH="${external_bin}:/usr/bin:/bin"
  [[ ! -e "${missing_bin}" ]] \
    && [[ "$(resolve_host_just)" == "$(readlink -f "${external_bin}/just")" ]] \
    && require_exact_just || return 1

  set +e
  output="$(
    HOST_PATH="${fixture_root}/absent" \
      bash -c 'set -Eeuo pipefail; source "$1"; require_exact_just' \
      _ "${SCRIPT_DIR}/lib/common.sh" 2>&1
  )"
  status=$?
  set -e
  [[ "${status}" -eq "${EXIT_ERROR}" \
    && -n "${output}" \
    && "${output}" == *"host-installed just ${JUST_VERSION} is required"* ]] \
    || return 1

  set +e
  output="$(
    HOST_PATH="${wrong_bin}" \
      bash -c 'set -Eeuo pipefail; source "$1"; require_exact_just' \
      _ "${SCRIPT_DIR}/lib/common.sh" 2>&1
  )"
  status=$?
  set -e
  [[ "${status}" -eq "${EXIT_ERROR}" \
    && "${output}" == *"found just 0.0.0"* ]]
)

fresh_checkout_tools_bootstrap_fixture() (
  local fixture_root output status
  fixture_root="$(mktemp -d "${TOOLS_TMP_DIR}/fresh-checkout-tools.XXXXXX")"
  trap 'rm -rf "${fixture_root}"' EXIT
  git -C "${LAB_ROOT}/.." archive HEAD kamaji | tar -x -C "${fixture_root}"
  mkdir -p -m 0700 "${fixture_root}/external"
  printf '#!/usr/bin/env bash\nprintf "just %s\\n"\n' "${JUST_VERSION}" \
    >"${fixture_root}/external/just"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    "printf 'fresh bootstrap reached download\\n' >&2" \
    'exit 42' \
    >"${fixture_root}/external/curl"
  chmod 0700 "${fixture_root}/external/just" "${fixture_root}/external/curl"

  set +e
  output="$(
    cd "${fixture_root}/kamaji"
    PATH="${fixture_root}/external:/usr/bin:/bin" ./scripts/tools.sh 2>&1
  )"
  status=$?
  set -e
  [[ "${status}" -eq 42 \
    && "${output}" == *"fresh bootstrap reached download"* \
    && "${output}" != *"host-installed just"* \
    && -d "${fixture_root}/kamaji/.tools/bin" ]]
)

blocked_result_validation_fixtures() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/verify.sh"
  local fixture_dir="${TOOLS_TMP_DIR}/blocked-result-validation-fixture"
  local tenant residual_class key
  rm -rf "${fixture_dir}"
  mkdir -p -m 0700 "${fixture_dir}"
  trap 'rm -rf "${fixture_dir}"' EXIT
  RUNTIME_DIR="${fixture_dir}/runtime"
  SPIKE_RUNTIME_DIR="${RUNTIME_DIR}/tenants/spike"
  MANAGEMENT_NETWORK_FILE="${RUNTIME_DIR}/network/management.env"
  FINAL_RESULT_FILE="${fixture_dir}/final-result.env"
  BLOCKER_FILE="${fixture_dir}/blocker"
  mkdir -p -m 0700 "$(dirname "${MANAGEMENT_NETWORK_FILE}")"
  {
    printf 'TENANT_A_VIP=172.18.0.240\n'
    printf 'TENANT_B_VIP=172.18.0.241\n'
  } >"${MANAGEMENT_NETWORK_FILE}"

  blocked_management_plane_is_healthy() { return 0; }
  blocked_all_tcp_state() { printf 'absent\n'; }
  blocked_owned_worker_state() { printf 'absent\n'; }
  blocked_owned_worker_volume_state() { printf 'absent\n'; }
  blocked_management_namespace_state() { printf 'absent\n'; }
  blocked_vip_claim_state() { printf 'absent\n'; }
  datastore_used_by_tenant_state() { printf 'absent\n'; }
  datastore_used_by_spike_state() { printf 'absent\n'; }
  etcd_readonly_prefix_state() { printf 'absent\n'; }
  etcd_readonly_user_state() { printf 'absent\n'; }
  etcd_readonly_role_state() { printf 'absent\n'; }

  write_valid_blocked_records() {
    {
      printf 'result=blocked\n'
      printf 'compatibility_revision=%s\n' "${COMPATIBILITY_REVISION}"
      printf 'first_failing_prerequisite=tenant-a-workers\n'
      printf 'blocker_code=worker-substrate\n'
      printf 'blocker_evidence=recognized fixture evidence\n'
      printf 'cleanup=proved\n'
      printf 'final_tenants=absent\n'
      printf 'final_workers=absent\n'
      printf 'final_volumes=absent\n'
      printf 'final_runtime=absent\n'
    } | write_secret_file "${FINAL_RESULT_FILE}"
    {
      printf 'owner=final\n'
      printf 'code=worker-substrate\n'
      printf 'prerequisite=tenant-a-workers\n'
      printf 'message=recognized fixture evidence\n'
    } | write_secret_file "${BLOCKER_FILE}"
  }

  write_valid_blocked_records
  blocked_result_is_current || return 1

  sed -i "s/${COMPATIBILITY_REVISION}/stale-revision/" "${FINAL_RESULT_FILE}"
  ! blocked_result_is_current || return 1

  write_valid_blocked_records
  sed -i '/^blocker_evidence=/d' "${FINAL_RESULT_FILE}"
  ! blocked_result_is_current || return 1

  write_valid_blocked_records
  sed -i 's/^code=.*/code=kubeadm-bootstrap/' "${BLOCKER_FILE}"
  ! blocked_result_is_current || return 1

  write_valid_blocked_records
  sed -i 's/^blocker_evidence=.*/blocker_evidence=none/' "${FINAL_RESULT_FILE}"
  ! blocked_result_is_current || return 1

  write_valid_blocked_records
  sed -i '/^cleanup=/d' "${FINAL_RESULT_FILE}"
  ! blocked_result_is_current || return 1

  write_valid_blocked_records
  for key in tcp workers worker-volumes \
    tenant-a.namespace tenant-b.namespace spike.namespace \
    vip.172.18.0.240 vip.172.18.0.241; do
    if KAMAJI_TEST_BLOCKED_RESIDUAL="${key}:present" \
      blocked_result_is_current; then
      return 1
    fi
    [[ -n "${BLOCKED_RESIDUAL_REASON}" ]] || return 1
  done

  for tenant in ${TENANT_NAMES} spike; do
    for residual_class in datastore-used-by datastore-prefix \
      datastore-user datastore-role; do
      key="${tenant}.${residual_class}"
      if KAMAJI_TEST_BLOCKED_RESIDUAL="${key}:present" \
        blocked_result_is_current; then
        return 1
      fi
      [[ "${BLOCKED_RESIDUAL_REASON}" == *"${tenant}"*"is present"* ]] \
        || return 1
      if KAMAJI_TEST_BLOCKED_RESIDUAL="${key}:inspection-failed" \
        blocked_result_is_current; then
        return 1
      fi
      [[ "${BLOCKED_RESIDUAL_REASON}" == *"${tenant}"*"inspection failed"* ]] \
        || return 1
    done
  done

  mkdir -p -m 0700 "${SPIKE_RUNTIME_DIR}"
  ! blocked_result_is_current \
    && [[ "${BLOCKED_RESIDUAL_REASON}" == "spike runtime subtree remains" ]] \
    || return 1
  rm -rf "${SPIKE_RUNTIME_DIR}"

  ln -s "${fixture_dir}/missing-runtime-target" "${SPIKE_RUNTIME_DIR}"
  ! blocked_result_is_current \
    && [[ "${BLOCKED_RESIDUAL_REASON}" == "spike runtime subtree remains" ]] \
    || return 1
  rm -f "${SPIKE_RUNTIME_DIR}"

  mkdir -p -m 0700 "$(dirname "$(tenant_kubeconfig spike)")"
  : >"$(tenant_kubeconfig spike)"
  ! blocked_result_is_current \
    && [[ "${BLOCKED_RESIDUAL_REASON}" == "spike kubeconfig remains" ]] \
    || return 1
  rm -rf "${SPIKE_RUNTIME_DIR}"

  mkdir -p -m 0700 "$(dirname "$(tenant_kubeconfig spike)")"
  ln -s "${fixture_dir}/missing-kubeconfig-target" "$(tenant_kubeconfig spike)"
  ! blocked_result_is_current \
    && [[ "${BLOCKED_RESIDUAL_REASON}" == "spike kubeconfig remains" ]] \
    || return 1
  rm -rf "${SPIKE_RUNTIME_DIR}"

  blocked_result_is_current
)

exact_node_set_rejects_extra_node() (
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/lib/workers.sh"
  tenant_kubectl() {
    if [[ " $* " == *" get nodes "* ]]; then
      printf '%s\n' \
        kamaji-tenant-a-worker-1 \
        kamaji-tenant-a-worker-2 \
        kamaji-tenant-a-worker-3 \
        unexpected-notready-node
      return
    fi
    return 1
  }
  set +e
  (validate_exact_tenant_node_set tenant-a >/dev/null 2>&1)
  local status=$?
  set -e
  [[ "${status}" -eq "${EXIT_ERROR}" ]]
)

require_exact_just

for script in "${LAB_ROOT}"/scripts/*.sh "${LAB_ROOT}"/scripts/lib/*.sh; do
  check "bash syntax: ${script#${LAB_ROOT}/}" bash -n "${script}"
done

check "complete just task surface" bash -c '
  tasks="$(just --justfile "$1" --list --unsorted)"
  for task in tools preflight prepare-host create-management spike destroy-spike create repair status diagnose verify destroy-tenant destroy test-static test-inotify-negative test-kube-proxy-restart test-e2e; do
    grep -Eq "^    ${task}([[:space:]]|$)" <<<"${tasks}" || exit 1
  done
' _ "${LAB_ROOT}/Justfile"
check "Kamaji has no Makefile" test ! -e "${LAB_ROOT}/Makefile"
check "root Makefile is byte-for-byte unchanged" files_identical_to_main Makefile
check "root README is a two-lab index with preserved vCluster Make commands" bash -c '
  file="$1/../README.md"
  grep -Fq "vCluster lab" "$file" &&
  grep -Fq "Kamaji lab" "$file" &&
  grep -Fq "make create" "$file" &&
  grep -Fq "cd kamaji" "$file" &&
  grep -Fq "just create" "$file"
' _ "${LAB_ROOT}"
check "vcluster tree is unchanged from main" \
  git -C "${LAB_ROOT}/.." diff --quiet main -- vcluster
check "no PAW artifact is tracked" \
  test -z "$(git -C "${LAB_ROOT}/.." ls-files .paw)"
check "no symlink exists below kamaji" \
  test -z "$(find "${LAB_ROOT}" -type l -print)"
check "scripts/config/task runner do not couple to vcluster paths" bash -c '
  ! grep -R -F "vcluster/" \
    "$1/scripts" "$1/config" "$1/Justfile" \
    --exclude=test-static.sh
' _ "${LAB_ROOT}"

check "tools verifies the host just prerequisite" \
  has_text 'require_exact_just' "${LAB_ROOT}/scripts/tools.sh"
check "host just lookup rejects the lab binary directory" bash -c '
  grep -Fq "HOST_PATH" "$1/scripts/lib/common.sh" &&
  grep -Fq "canonical_executable" "$1/scripts/lib/common.sh" &&
  grep -Fq "canonical_missing_path" "$1/scripts/lib/common.sh" &&
  grep -Fq '\''"${canonical_bin}/"*) return 1'\'' "$1/scripts/lib/common.sh" &&
  grep -Fq "resolve_host_just" "$1/scripts/status.sh"
' _ "${LAB_ROOT}"
check "host just resolution handles missing bins and rejects lab paths" \
  host_just_resolution_fixtures
check "fresh checkout tools reaches bootstrap after host just resolution" \
  fresh_checkout_tools_bootstrap_fixture
check "tools never downloads or installs just" bash -c '
  ! grep -E "(curl|wget|install|cp|mv).*(JUST_|just-)" "$1/scripts/tools.sh"
' _ "${LAB_ROOT}"
check "recipes never install just" bash -c '
  ! grep -E "(curl|wget|install|apt|dnf|yum|brew).*(just)" "$1/Justfile"
' _ "${LAB_ROOT}"
check "just source and checksum are documented" \
  has_text '^JUST_ARCHIVE_SHA256=4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d$' \
  "${LAB_ROOT}/config/versions.env"

check "kind 0.33.0 pin is exact" \
  has_text '^KIND_VERSION=v0\.33\.0$' "${LAB_ROOT}/config/versions.env"
check "Kubernetes 1.36.4 node digest is exact" \
  has_text '^KIND_NODE_IMAGE=kindest/node:v1\.36\.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed$' \
  "${LAB_ROOT}/config/versions.env"
check "kubectl 1.36.4 checksum is exact" \
  has_text '^KUBECTL_SHA256=8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a$' \
  "${LAB_ROOT}/config/versions.env"
check "Helm 3.21.4 checksum is exact" \
  has_text '^HELM_SHA256=61f88ab166748cb19604d7884cb100ae9ccb13804ddeb98e08af167eacbb6a14$' \
  "${LAB_ROOT}/config/versions.env"
check "extracted Helm binary checksum is exact" \
  has_text '^HELM_BINARY_SHA256=cd27ec335b9c961a0a098cce870fded88429210edc898fd213da0b16e67333bd$' \
  "${LAB_ROOT}/config/versions.env"
check "Kamaji release source commit and checksum are exact" bash -c '
  grep -Fqx "KAMAJI_TAG_COMMIT=80f32baafe34cba9d739c41208c21090dbe1827d" "$1" &&
  grep -Fqx "KAMAJI_SOURCE_SHA256=9615c91762f149900a3b36f76db52917f56d0890f6272de5f8bc3f4ebf21f9db" "$1"
' _ "${LAB_ROOT}/config/versions.env"
check "Kamaji controller digest is exact" \
  has_text '^KAMAJI_IMAGE=clastix/kamaji@sha256:10fe540fa1876131abf89f88694c258c9a2e88b5069ec1d05b0c0dcec185e3f3$' \
  "${LAB_ROOT}/config/versions.env"
check "locked kamaji-etcd package checksum is exact" \
  has_text '^KAMAJI_ETCD_CHART_SHA256=b8e88d5f535c0d328b46a3ffb5b543d0056e9370cb503e5a11e998d1f555f209$' \
  "${LAB_ROOT}/config/versions.env"
check "cert-manager OCI descriptor digest is exact" \
  has_text '^CERT_MANAGER_OCI_DIGEST=sha256:15c0b46d9006ce8eb9ff14d1bf54d1bbfcc587bb9e24cd9fe186fb8fec56af1f$' \
  "${LAB_ROOT}/config/versions.env"
check "cert-manager workload image digests are exact" bash -c '
  source "$1"
  for image in \
    "$CERT_MANAGER_CONTROLLER_IMAGE" "$CERT_MANAGER_WEBHOOK_IMAGE" \
    "$CERT_MANAGER_CAINJECTOR_IMAGE" "$CERT_MANAGER_STARTUPAPICHECK_IMAGE"; do
    [[ "$image" =~ :v1\.21\.1@sha256:[0-9a-f]{64}$ ]] || exit 1
  done
  [[ "$CERT_MANAGER_IMAGE_PROVENANCE" == cert-manager-v1.21.1:* ]]
' _ "${LAB_ROOT}/config/versions.env"
check "MetalLB v0.16.1 manifest checksum is exact" \
  has_text '^METALLB_MANIFEST_SHA256=bf25feebb7582ca7df845efd52ffbc2960d6cbf4cfc972f47fded9f788b67f0b$' \
  "${LAB_ROOT}/config/versions.env"
check "MetalLB workload image digests are exact" bash -c '
  source "$1"
  [[ "$METALLB_CONTROLLER_IMAGE" =~ :v0\.16\.1@sha256:[0-9a-f]{64}$ ]] &&
  [[ "$METALLB_SPEAKER_IMAGE" =~ :v0\.16\.1@sha256:[0-9a-f]{64}$ ]] &&
  [[ "$METALLB_IMAGE_PROVENANCE" == metallb-v0.16.1:* ]]
' _ "${LAB_ROOT}/config/versions.env"
check "Calico 3.32.2 manifest checksum is exact" \
  has_text '^CALICO_MANIFEST_SHA256=a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02$' \
  "${LAB_ROOT}/config/versions.env"
check "Local Path 0.0.37 manifest checksum is exact" \
  has_text '^LOCAL_PATH_MANIFEST_SHA256=9781b39c24f3f651bd6d6e41b561e04e4904bbdb6d4f8c7a6009df3a702dcd65$' \
  "${LAB_ROOT}/config/versions.env"
check "CNPG 1.30.0 manifest checksum is exact" \
  has_text '^CNPG_MANIFEST_SHA256=f8bede43fe4ee0d478c2355b204a36876b2ae4faac60f2a9452280b293da3b88$' \
  "${LAB_ROOT}/config/versions.env"
check "CNPG controller image digest is exact" \
  has_text '^CNPG_CONTROLLER_IMAGE=ghcr.io/cloudnative-pg/cloudnative-pg:1\.30\.0@sha256:a2701eb97cdd2a34b1fdb2cb51987f544b706e40bec72ae7146cd8580efefebb$' \
  "${LAB_ROOT}/config/versions.env"
check "PostgreSQL 18.4 image digest is exact" \
  has_text '^POSTGRES_IMAGE=ghcr.io/cloudnative-pg/postgresql:18\.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185$' \
  "${LAB_ROOT}/config/versions.env"
check "worker and verification images use digests" bash -c '
  grep -Eq "^KIND_NODE_IMAGE=.+@sha256:[0-9a-f]{64}$" "$1" &&
  grep -Eq "^VERIFY_IMAGE=.+@sha256:[0-9a-f]{64}$" "$1"
' _ "${LAB_ROOT}/config/versions.env"
check "every directly selected image uses a digest" bash -c '
  while IFS="=" read -r name value; do
    [[ "$name" == *_IMAGE && "$value" =~ @sha256:[0-9a-f]{64}$ ]] || exit 1
  done < <(grep -E "^[A-Z0-9_]+_IMAGE=" "$1")
' _ "${LAB_ROOT}/config/versions.env"
check "kind config uses the approved management image" \
  has_text 'image: kindest/node:v1\.36\.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed' \
  "${LAB_ROOT}/config/kind.yaml"
check "kind CIDRs match settings" bash -c '
  source "$1/config/settings.env"
  pod="$(awk "/podSubnet:/ {print \$2}" "$1/config/kind.yaml")"
  service="$(awk "/serviceSubnet:/ {print \$2}" "$1/config/kind.yaml")"
  [[ "$pod" == "$MANAGEMENT_POD_CIDR" && "$service" == "$MANAGEMENT_SERVICE_CIDR" ]]
' _ "${LAB_ROOT}"

check "prepared Kamaji source archive checksum" \
  sha256_check "${KAMAJI_SOURCE_SHA256}" "${CACHE_DIR}/kamaji-${KAMAJI_VERSION}.tar.gz"
check "prepared upstream Chart.lock checksum" \
  sha256_check "${KAMAJI_CHART_LOCK_SHA256}" "${KAMAJI_CHART_DIR}/Chart.lock"
check "prepared dependency package checksum" \
  sha256_check "${KAMAJI_ETCD_CHART_SHA256}" \
  "${KAMAJI_CHART_DIR}/charts/kamaji-etcd-${KAMAJI_ETCD_CHART_VERSION}.tgz"
check "cert-manager package checksum" \
  sha256_check "${CERT_MANAGER_CHART_SHA256}" \
  "${INPUTS_DIR}/cert-manager-${CERT_MANAGER_VERSION}.tgz"
check "installed Helm binary integrity" \
  sha256_check "${HELM_BINARY_SHA256}" "${BIN_DIR}/helm"
check "direct manifest input checksums" bash -c '
  source "$1/config/versions.env"
  cd "$1"
  printf "%s  %s\n" "$METALLB_MANIFEST_SHA256" ".tools/inputs/metallb-native-${METALLB_VERSION}.yaml" |
    sha256sum -c - >/dev/null &&
  printf "%s  %s\n" "$CALICO_MANIFEST_SHA256" ".tools/inputs/calico-${CALICO_VERSION}.yaml" |
    sha256sum -c - >/dev/null &&
  printf "%s  %s\n" "$LOCAL_PATH_MANIFEST_SHA256" ".tools/inputs/local-path-${LOCAL_PATH_VERSION}.yaml" |
    sha256sum -c - >/dev/null &&
  printf "%s  %s\n" "$CNPG_MANIFEST_SHA256" ".tools/inputs/cnpg-${CNPG_VERSION}.yaml" |
    sha256sum -c - >/dev/null
' _ "${LAB_ROOT}"
check "cert-manager and MetalLB reverify and pin inputs before use" bash -c '
  file="$1/scripts/lib/management.sh"
  grep -Fq '\''sha256_check "${CERT_MANAGER_CHART_SHA256}"'\'' "$file" &&
  grep -Fq '\''image.digest=${CERT_MANAGER_CONTROLLER_IMAGE##*@}'\'' "$file" &&
  grep -Fq '\''startupapicheck.image.digest=${CERT_MANAGER_STARTUPAPICHECK_IMAGE##*@}'\'' "$file" &&
  grep -Fq '\''sha256_check "${METALLB_MANIFEST_SHA256}"'\'' "$file" &&
  grep -Fq "render_metallb_manifest" "$file" &&
  grep -Fq "live workloads do not use the approved image digests" "$file"
' _ "${LAB_ROOT}"
check "independent CNPG operator manifest matches verified provenance" bash -c '
  source "$1/config/versions.env"
  [[ "$CNPG_MANIFEST_URL" == "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/v1.30.0/releases/cnpg-1.30.0.yaml" ]] &&
  printf "%s  %s\n" "$CNPG_MANIFEST_SHA256" "$1/manifests/cnpg/operator.yaml" |
    sha256sum -c - >/dev/null &&
  [[ ! -L "$1/manifests/cnpg/operator.yaml" ]]
' _ "${LAB_ROOT}"
check "rendered spike add-on checksums are exact" bash -c '
  source "$1/config/versions.env"
  printf "%s  %s\n" "$CALICO_SPIKE_RENDER_SHA256" "$1/manifests/addons/calico.yaml" |
    sha256sum -c - >/dev/null &&
  printf "%s  %s\n" "$LOCAL_PATH_SPIKE_RENDER_SHA256" "$1/manifests/addons/local-path.yaml" |
    sha256sum -c - >/dev/null
' _ "${LAB_ROOT}"
check "Calico render uses exact spike CIDR and image pins" bash -c '
  source "$1/config/versions.env"
  grep -Fq "value: \"10.66.0.0/16\"" "$1/manifests/addons/calico.yaml" &&
  grep -Fq "$CALICO_CNI_IMAGE" "$1/manifests/addons/calico.yaml" &&
  grep -Fq "$CALICO_NODE_IMAGE" "$1/manifests/addons/calico.yaml" &&
  grep -Fq "$CALICO_KUBE_CONTROLLERS_IMAGE" "$1/manifests/addons/calico.yaml" &&
  ! grep -Eq "image: quay.io/calico/.+:v3.32.2$" "$1/manifests/addons/calico.yaml"
' _ "${LAB_ROOT}"
check "Local Path render is default, persistent, and pinned" bash -c '
  source "$1/config/settings.env"
  source "$1/config/versions.env"
  grep -Fq "storageclass.kubernetes.io/is-default-class: \"true\"" "$1/manifests/addons/local-path.yaml" &&
  grep -Fq "\"paths\":[\"${SPIKE_STORAGE_PATH}\"]" "$1/manifests/addons/local-path.yaml" &&
  grep -Fq "$LOCAL_PATH_PROVISIONER_IMAGE" "$1/manifests/addons/local-path.yaml" &&
  grep -Fq "$VERIFY_IMAGE" "$1/manifests/addons/local-path.yaml"
' _ "${LAB_ROOT}"

check "deterministic Kamaji render validates" \
  "${SCRIPT_DIR}/render-kamaji.sh" validate
check "Kamaji renderer has one non-recursive post-render path" bash -c '
  grep -Fq '\''case "${1:-post-render}" in'\'' "$1" &&
  ! grep -Fq '\''render-kamaji.sh post-render'\'' "$1"
' _ "${LAB_ROOT}/scripts/render-kamaji.sh"
check "rendered direct images are digest pinned" bash -c '
  source "$1/config/versions.env"
  for image in "$KAMAJI_IMAGE" "$KAMAJI_ETCD_IMAGE" "$KAMAJI_ETCD_JOB_IMAGE" "$KAMAJI_KUBECTL_JOB_IMAGE"; do
    grep -Fq "$image" "$1/.tools/rendered/kamaji.yaml" || exit 1
  done
' _ "${LAB_ROOT}"
check "transitive image inventory is complete and digest-only" bash -c '
  [[ "$(wc -l <"$1")" -eq 4 ]] &&
  ! grep -Ev "@sha256:[0-9a-f]{64}$" "$1" | grep -q .
' _ "${KAMAJI_IMAGE_INVENTORY}"
check "render contains no moving latest reference" \
  not_has_text '(^|[:/@])latest($|@|:)' "${KAMAJI_IMAGE_INVENTORY}"
check "direct datastore image provenance is recorded" bash -c '
  grep -Fq "KAMAJI_ETCD_IMAGE_PROVENANCE=kamaji-etcd-0.15.0:values.image" "$1" &&
  grep -Fq "KAMAJI_ETCD_JOB_IMAGE_PROVENANCE=kamaji-etcd-0.15.0:values.jobs.etcd" "$1" &&
  grep -Fq "KAMAJI_KUBECTL_JOB_IMAGE_PROVENANCE=kamaji-etcd-0.15.0:values.jobs.kubectl" "$1"
' _ "${LAB_ROOT}/config/versions.env"

check "capacity thresholds are exact" bash -c '
  source "$1/config/settings.env"
  [[ "$MIN_DOCKER_CPUS" == 12 &&
     "$MIN_DOCKER_MEMORY_GIB" == 24 &&
     "$MIN_DOCKER_STORAGE_GIB" == 30 &&
     "$MIN_INOTIFY_INSTANCES" == 1024 &&
     "$MIN_INOTIFY_WATCHES" == 524288 ]]
' _ "${LAB_ROOT}"
check "spike control-plane resource split matches aggregate budget" bash -c '
  source "$1/config/settings.env"
  [[ "$TENANT_API_SERVER_REQUEST_CPU" == 125m &&
     "$TENANT_CONTROLLER_MANAGER_REQUEST_CPU" == 75m &&
     "$TENANT_SCHEDULER_REQUEST_CPU" == 50m &&
     "$TENANT_API_SERVER_REQUEST_MEMORY" == 256Mi &&
     "$TENANT_CONTROLLER_MANAGER_REQUEST_MEMORY" == 128Mi &&
     "$TENANT_SCHEDULER_REQUEST_MEMORY" == 128Mi &&
     "$TENANT_API_SERVER_LIMIT_CPU" == 500m &&
     "$TENANT_CONTROLLER_MANAGER_LIMIT_CPU" == 300m &&
     "$TENANT_SCHEDULER_LIMIT_CPU" == 200m &&
     "$TENANT_API_SERVER_LIMIT_MEMORY" == 1Gi &&
     "$TENANT_CONTROLLER_MANAGER_LIMIT_MEMORY" == 256Mi &&
     "$TENANT_SCHEDULER_LIMIT_MEMORY" == 256Mi &&
     "$TENANT_CONTROL_PLANE_REQUEST_MEMORY" == 512Mi &&
     "$TENANT_CONTROL_PLANE_LIMIT_MEMORY" == 1536Mi ]]
' _ "${LAB_ROOT}"
check "CPU threshold fixture rejects before mutation" \
  capacity_fixture_rejects CPU_FIXTURE 11 cpu
check "memory threshold fixture rejects before mutation" \
  capacity_fixture_rejects MEMORY_BYTES_FIXTURE $((23 * 1024 * 1024 * 1024)) memory
check "storage threshold fixture rejects before mutation" \
  capacity_fixture_rejects STORAGE_BYTES_FIXTURE $((29 * 1024 * 1024 * 1024)) storage
check "inotify instance threshold fixture rejects before mutation" \
  capacity_fixture_rejects INOTIFY_INSTANCES_FIXTURE 1023 inotify-instances
check "inotify watch threshold fixture rejects before mutation" \
  inotify_watch_fixture_rejects

check "host preparation records applies verifies and exposes full restore" bash -c '
  file="$1/scripts/lib/host.sh"
  grep -Fq "HOST_SYSCTL_STATE_FILE" "$file" &&
  grep -Fq "record_original_inotify_values" "$file" &&
  grep -Fq "sudo sysctl -q -w" "$file" &&
  grep -Fq "require_host_inotify_capacity" "$file" &&
  grep -Fq "restore_recorded_inotify_values" "$file" &&
  grep -Fq "host inotify values cannot be restored while nested workers exist" "$file" &&
  grep -Fq "rm -f \"\${HOST_SYSCTL_STATE_FILE}\"" "$file" &&
  ! grep -R -F "sysctl -q -w" "$1/scripts/create.sh" "$1/scripts/create-spike.sh" "$1/scripts/preflight.sh"
' _ "${LAB_ROOT}"
check "negative inotify fixture recipe is static and non-mutating" bash -c '
  grep -Fq "KAMAJI_PREFLIGHT_INOTIFY_INSTANCES_FIXTURE" "$1/scripts/test-inotify-negative.sh" &&
  grep -Fq "KAMAJI_PREFLIGHT_INOTIFY_WATCHES_FIXTURE" "$1/scripts/test-inotify-negative.sh" &&
  grep -Fq "KAMAJI_PREFLIGHT_FORBID_MUTATION_FIXTURE=1" "$1/scripts/test-inotify-negative.sh"
' _ "${LAB_ROOT}"

check "finite timeout declarations are environment-overridable" bash -c '
  export DOWNLOAD_TIMEOUT=37s
  source "$1/config/settings.env"
  [[ "$DOWNLOAD_TIMEOUT" == 37s ]] &&
  [[ "$(grep -Ec "_TIMEOUT:=[0-9]+[smh]" "$1/config/settings.env")" -ge 4 ]]
' _ "${LAB_ROOT}"
while IFS= read -r timeout_var; do
  check "timeout ${timeout_var} is consumed" \
    grep -R -Fq --exclude=test-static.sh "\${${timeout_var}}" "${LAB_ROOT}/scripts"
done < <(sed -n 's/^: "${\([A-Z_]*_TIMEOUT\):=.*/\1/p' "${LAB_ROOT}/config/settings.env")

check "runtime and tools directories use mode 0700" bash -c '
  bad="$(find "$1/.tools" -type d ! -perm 0700 -print)"
  [[ -z "$bad" ]]
  if [[ -d "$1/.runtime" ]]; then
    bad="$(find "$1/.runtime" -type d ! -perm 0700 -print)"
    [[ -z "$bad" ]]
  fi
' _ "${LAB_ROOT}"
check "common shell enforces umask 077" \
  has_text '^umask 077$' "${LAB_ROOT}/scripts/lib/common.sh"
check "common shell creates mode-0600 secret files" \
  has_text 'chmod 0600.*destination' "${LAB_ROOT}/scripts/lib/common.sh"
check "preflight names missing Docker buildx" \
  has_text 'required Docker buildx plugin is unavailable' "${LAB_ROOT}/scripts/preflight.sh"
check "tools and preflight verify Helm binary checksum" bash -c '
  grep -Fq "HELM_BINARY_SHA256" "$1/scripts/tools.sh" &&
  grep -Fq "HELM_BINARY_SHA256" "$1/scripts/preflight.sh"
' _ "${LAB_ROOT}"
check "all Kubernetes operations use explicit wrappers" bash -c '
  ! grep -R -E "^[[:space:]]*kubectl[[:space:]]" "$1/scripts" \
    --include="*.sh" --exclude=common.sh --exclude=test-static.sh
' _ "${LAB_ROOT}"
check "mutating create and repair entrypoints run full preflight" bash -c '
  for file in create.sh create-spike.sh repair-tenant.sh; do
    grep -Fq '\''"${SCRIPT_DIR}/preflight.sh"'\'' "$1/scripts/$file" || exit 1
    ! grep -Fq "KAMAJI_TEST_SKIP_PREFLIGHT" "$1/scripts/$file" || exit 1
  done
  ! grep -Fq "KAMAJI_TEST_SKIP_PREFLIGHT" \
    "$1/README.md" "$1/docs/high-level-design.md"
' _ "${LAB_ROOT}"
check "kind deletion always selects an explicit kubeconfig" bash -c '
  python3 - "$1/scripts" <<'"'"'PY'"'"'
from pathlib import Path
import sys
for path in Path(sys.argv[1]).rglob("*.sh"):
    if path.name == "test-static.sh":
        continue
    lines=path.read_text(encoding="utf-8").splitlines()
    for index,line in enumerate(lines):
        if "kind delete cluster" not in line:
            continue
        command=line
        cursor=index
        while command.rstrip().endswith("\\"):
            cursor += 1
            command += " " + lines[cursor]
        assert "--kubeconfig" in command, f"{path}:{index+1}"
PY
' _ "${LAB_ROOT}"
check "management wrapper sets kubeconfig and context" bash -c '
  grep -Fq "KUBECONFIG=\"\${MANAGEMENT_KUBECONFIG}\"" "$1" &&
  grep -Fq -- "--context \"\$(management_context)\"" "$1"
' _ "${LAB_ROOT}/scripts/lib/common.sh"
check "tenant wrapper sets explicit kubeconfig" \
  has_text 'KUBECONFIG=.*tenant_kubeconfig' "${LAB_ROOT}/scripts/lib/common.sh"
check "scripts do not print kubeconfigs, tokens, or passwords" bash -c '
  ! grep -E "(cat|base64|sed|awk).*(admin\\.conf|kubeconfig|bootstrap.token|password)" \
    "$1/scripts/status.sh" "$1/scripts/diagnose.sh"
' _ "${LAB_ROOT}"

check "only compatibility paths can return blocked status" bash -c '
  refs="$(grep -R -l "EXIT_BLOCKED" "$1/scripts" --include="*.sh" --exclude=test-static.sh)"
  [[ "$(printf "%s\n" "${refs}" | sort)" == "$(printf "%s\n" "$1/scripts/create-spike.sh" "$1/scripts/create.sh" "$1/scripts/verify.sh" "$1/scripts/test-e2e.sh" | sort)" ]] &&
  grep -Fq "return \"\${EXIT_BLOCKED}\"" "$1/scripts/create-spike.sh" &&
  grep -Fq "record_spike_blocker" "$1/scripts/create-spike.sh" &&
  grep -Fq "record_final_blocker" "$1/scripts/create.sh" &&
  grep -Fq "blocked_result_is_current" "$1/scripts/verify.sh" &&
  grep -Fq "exit \"\${EXIT_BLOCKED}\"" "$1/scripts/verify.sh" &&
  grep -Fq "recognized_blocker_cleanup" "$1/scripts/test-e2e.sh"
' _ "${LAB_ROOT}"
check "blocked verification requires current consistent records and clean residuals" \
  blocked_result_validation_fixtures
check "blocked residual inspection uses read-only datastore probes" bash -c '
  predicate="$(sed -n "/^blocked_residual_state_is_allowed()/,/^}/p" "$1/scripts/lib/tenants.sh")"
  grep -Fq "etcd_readonly_prefix_state" <<<"${predicate}" &&
  grep -Fq "etcd_readonly_user_state" <<<"${predicate}" &&
  grep -Fq "etcd_readonly_role_state" <<<"${predicate}" &&
  ! grep -Fq "etcd_maintenance" <<<"${predicate}"
' _ "${LAB_ROOT}"
check "management provides a pinned read-only datastore inspector" bash -c '
  file="$1/scripts/lib/management.sh"
  grep -Fq "reconcile_etcd_inspector" "$file" &&
  grep -Fq '\''"image": os.environ["ETCD_IMAGE"]'\'' "$file" &&
  grep -Fq '\''"watch", "/__kamaji_readonly_inspector__"'\'' "$file" &&
  grep -Fq "automountServiceAccountToken" "$file" &&
  grep -Fq "etcd_inspector_is_ready" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "read-only datastore inspector" "$1/scripts/status.sh"
' _ "${LAB_ROOT}"
check "ordinary final failures have explicit non-blocker classifiers" bash -c '
  file="$1/scripts/create.sh"
  for classifier in classify_kube_proxy_failure classify_addon_failure \
    classify_topology_failure classify_capacity_failure classify_worker_failure; do
    grep -Fq "${classifier}()" "$file" || exit 1
  done
  grep -Fq "KAMAJI_TEST_INJECT_ADDON_FAILURE" "$1/scripts/lib/addons.sh" &&
  grep -Fq "ordinary add-on failure" "$1/scripts/test-e2e.sh" &&
  grep -Fq "ordinary add-on failure created a compatibility blocker record" \
    "$1/scripts/test-e2e.sh"
' _ "${LAB_ROOT}"
check "status is read-only and never returns blocker status" \
  observer_is_read_only "${SCRIPT_DIR}/status.sh"
check "diagnostics is read-only and never returns blocker status" \
  observer_is_read_only "${SCRIPT_DIR}/diagnose.sh" all
check "hostile exit-2 mutating observer is rejected" \
  hostile_observer_is_rejected

check "spike TCP is isolated, explicit, and target-versioned" bash -c '
  source "$1/config/settings.env"
  source "$1/config/versions.env"
  tcp="$1/config/tenants/spike.yaml"
  grep -Fq "name: \${SPIKE_NAME}" "$tcp" &&
  grep -Fq "namespace: \${SPIKE_NAMESPACE}" "$tcp" &&
  grep -Fq "replicas: 1" "$tcp" &&
  grep -Fq "version: \${KUBERNETES_VERSION}" "$tcp" &&
  grep -Fq "dataStoreSchema: \${SPIKE_SCHEMA}" "$tcp" &&
  grep -Fq "op: add" "$tcp" &&
  grep -Fq "path: /cgroupDriver" "$tcp" &&
  grep -Fq "value: systemd" "$tcp" &&
  grep -Fq "coreDNS: {}" "$tcp" &&
  grep -Fq "kubeProxy: {}" "$tcp" &&
  grep -Fq "hostNetwork: true" "$tcp" &&
  grep -A3 -F "effect: NoSchedule" "$tcp" | grep -Fq "NoSchedule" &&
  grep -Fq "version: \${KONNECTIVITY_AGENT_VERSION_DIGEST}" "$tcp" &&
  grep -Fq "version: \${KONNECTIVITY_SERVER_VERSION_DIGEST}" "$tcp"
' _ "${LAB_ROOT}"
check "spike CIDRs are explicit and non-overlapping" bash -c '
  source "$1/config/settings.env"
  python3 - "$MANAGEMENT_POD_CIDR" "$MANAGEMENT_SERVICE_CIDR" \
    "$TENANT_A_POD_CIDR" "$TENANT_A_SERVICE_CIDR" \
    "$TENANT_B_POD_CIDR" "$TENANT_B_SERVICE_CIDR" \
    "$SPIKE_POD_CIDR" "$SPIKE_SERVICE_CIDR" <<'"'"'PY'"'"'
import ipaddress,sys
nets=[ipaddress.ip_network(v) for v in sys.argv[1:]]
assert all(not a.overlaps(b) for i,a in enumerate(nets) for b in nets[i+1:])
PY
' _ "${LAB_ROOT}"
check "spike CIDRs participate in Docker overlap checks" bash -c '
  grep -F "CONFIGURED_CIDRS=" "$1/scripts/preflight.sh" \
    | grep -Fq '\''${SPIKE_POD_CIDR} ${SPIKE_SERVICE_CIDR}'\'' &&
  [[ "$(grep -F "EXCLUDED_CIDRS=" "$1/scripts/lib/network.sh" \
    | grep -Fc '\''${SPIKE_POD_CIDR} ${SPIKE_SERVICE_CIDR}'\'')" -eq 2 ]]
' _ "${LAB_ROOT}"
check "kubeadm allowlist has one settings source and exact consumers" bash -c '
  unset KUBEADM_IGNORE_PREFLIGHT_ERRORS
  [[ "$(grep -R -h "^: .*KUBEADM_IGNORE_PREFLIGHT_ERRORS" "$1/config" | wc -l)" -eq 1 ]] &&
  source "$1/config/settings.env" &&
  source "$1/scripts/lib/workers.sh" &&
  expected_arg="--ignore-preflight-errors=${KUBEADM_IGNORE_PREFLIGHT_ERRORS}" &&
  [[ "$(kubeadm_ignore_preflight_arg)" == "${expected_arg}" ]] &&
  rendered="KUBEADM_IGNORE_PREFLIGHT_ERRORS=\"${KUBEADM_IGNORE_PREFLIGHT_ERRORS}\"" &&
  grep -Fq "${rendered}" "$1/README.md" &&
  grep -Fq "${rendered}" "$1/docs/high-level-design.md"
' _ "${LAB_ROOT}"
check "spike entrypoint implements ordered compatibility ladder" bash -c '
  mapfile -t lines < <(grep -n "^current_rung=" "$1/scripts/create-spike.sh" | cut -d: -f1)
  [[ "${#lines[@]}" -eq 7 ]] &&
  (( lines[0] < lines[1] && lines[1] < lines[2] && lines[2] < lines[3] && lines[3] < lines[4] && lines[4] < lines[5] && lines[5] < lines[6] )) &&
  grep -Fq "upstream-equivalent-join" "$1/scripts/create-spike.sh" &&
  grep -Fq "target-systemd-fixed-vip" "$1/scripts/create-spike.sh" &&
  grep -Fq "cni-konnectivity" "$1/scripts/create-spike.sh" &&
  grep -Fq "persistent-worker-storage" "$1/scripts/create-spike.sh"
' _ "${LAB_ROOT}"
check "spike always cleans exact ephemeral resources" bash -c '
  grep -Fq "trap finish_spike EXIT" "$1/scripts/create-spike.sh" &&
  for action in delete_spike_storage_smoke delete_spike_node remove_spike_worker_and_volume delete_spike_tenant; do
    grep -Fq "$action" "$1/scripts/destroy-spike.sh" || exit 1
  done &&
  grep -Fq "verify_spike_vip_is_free" "$1/scripts/destroy-spike.sh" &&
  grep -Fq "services_claiming_vip" "$1/scripts/status.sh" &&
  grep -Fq "rm -rf \"\${SPIKE_RUNTIME_DIR}\"" "$1/scripts/destroy-spike.sh"
' _ "${LAB_ROOT}"
check "borrowed VIP claimant parser handles every claim shape" \
  services_claiming_vip_fixture
check "just spike refuses final state with exit 1 and no mutation" \
  spike_refusal_is_no_mutation
check "cleanup proof attestations require successful cleanup" bash -c '
  result_block="$(sed -n "/^write_spike_result()/,/^}/p" "$1/scripts/create-spike.sh")"
  finish_block="$(sed -n "/^finish_spike()/,/^}/p" "$1/scripts/create-spike.sh")"
  grep -Fq '\''[[ "${cleanup_proved}" == true ]]'\'' <<<"${result_block}" &&
  grep -Fq "cleanup=failed" <<<"${result_block}" &&
  grep -Fq "result=cleanup-failed" <<<"${finish_block}" &&
  grep -Fq "current_rung=cleanup" <<<"${finish_block}"
' _ "${LAB_ROOT}"
check "spike result records the exact configured allowlist value" \
  has_text 'kubeadm_ignore_allowlist="%s".*KUBEADM_IGNORE_PREFLIGHT_ERRORS' \
  "${LAB_ROOT}/scripts/create-spike.sh"
check "successful spike clears only spike-owned blocker evidence" bash -c '
  grep -Fq "clear_owned_spike_blocker" "$1/scripts/create-spike.sh" &&
  ! grep -Eq '\''rm -f .*BLOCKER_FILE'\'' "$1/scripts/create-spike.sh"
' _ "${LAB_ROOT}"
check "final-state refusal precedes spike evidence clearing and mutation" bash -c '
  refusal="$(grep -n "^refuse_spike_with_final_state$" "$1/scripts/create-spike.sh" | cut -d: -f1)"
  clear="$(grep -n "^clear_owned_spike_evidence$" "$1/scripts/create-spike.sh" | cut -d: -f1)"
  trap_line="$(grep -n "^trap finish_spike EXIT$" "$1/scripts/create-spike.sh" | cut -d: -f1)"
  management="$(grep -n "^reconcile_management_plane$" "$1/scripts/create-spike.sh" | cut -d: -f1)"
  [[ -n "$refusal" && -n "$clear" && -n "$trap_line" && -n "$management" ]] &&
  (( refusal < clear && clear < trap_line && trap_line < management ))
' _ "${LAB_ROOT}"
check "spike token and join material are short-lived and secret" bash -c '
  grep -Fq "token create --ttl \"\${KUBEADM_TOKEN_TTL}\"" "$1/scripts/lib/workers.sh" &&
  grep -Fq "token delete" "$1/scripts/lib/workers.sh" &&
  grep -Fq "chmod 0600" "$1/scripts/lib/workers.sh" &&
  grep -Fq "remove_worker_join_material" "$1/scripts/lib/workers.sh" &&
  ! grep -Eq "(log|echo).*(join_command|token_id)" "$1/scripts/lib/workers.sh"
' _ "${LAB_ROOT}"
check "final worker join cleanup is return signal and stale safe" bash -c '
  file="$1/scripts/lib/workers.sh"
  grep -Fq "cleanup_final_worker_join_material" "$file" &&
  grep -Fq "trap - RETURN INT TERM HUP" "$file" &&
  grep -Fq "cleanup_stale_final_worker_join_material" "$file" &&
  grep -Fq "KAMAJI_TEST_FAIL_AFTER_FINAL_JOIN" "$file" &&
  grep -Fq "interrupted final-worker join retained" "$1/scripts/test-e2e.sh"
' _ "${LAB_ROOT}"
check "datastore probes distinguish present absent and inspection failure" \
  etcd_probe_states_are_distinct
check "unavailable datastore is not treated as absence" \
  datastore_unavailable_is_not_absence
check "target bootstrap RBAC patch is resource-name scoped" bash -c '
  grep -Fq "resourceNames: [kubeadm-config, kubelet-config]" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "verbs: [get]" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "system:bootstrappers:kubeadm:default-node-token" "$1/scripts/lib/tenants.sh" &&
  ! grep -A8 "name: kubeadm:bootstrap-config-reader" "$1/scripts/lib/tenants.sh" |
    grep -Eq "verbs:.*(list|watch|\\*)"
' _ "${LAB_ROOT}"
check "container network bootstrap patches are narrow" bash -c '
  grep -Fq "name: kubernetes-services-endpoint" "$1/scripts/lib/addons.sh" &&
  grep -Fq "KUBERNETES_SERVICE_HOST" "$1/scripts/lib/addons.sh" &&
  grep -Fq "open /proc/sys/net/netfilter/nf_conntrack_max: permission denied" "$1/scripts/lib/addons.sh" &&
  grep -Fq "effect: NoSchedule" "$1/config/tenants/spike.yaml"
' _ "${LAB_ROOT}"
check "spike datastore cleanup is exact and health-gated" bash -c '
  grep -Fq '\''del "/${SPIKE_SCHEMA}/" --prefix'\'' "$1/scripts/lib/tenants.sh" &&
  grep -Fq '\''user delete "${SPIKE_DATASTORE_USER}"'\'' "$1/scripts/lib/tenants.sh" &&
  grep -Fq '\''role delete "${SPIKE_SCHEMA}"'\'' "$1/scripts/lib/tenants.sh" &&
  grep -Fq "DataStore/default became unhealthy" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "status.usedBy" "$1/scripts/lib/tenants.sh"
' _ "${LAB_ROOT}"

check "final tenant templates define exactly two isolated TCPs" bash -c '
  source "$1/config/settings.env"
  for tenant in tenant-a tenant-b; do
    file="$1/config/tenants/${tenant}.yaml"
    [[ -s "$file" ]] || exit 1
    grep -Fq "name: ${tenant}" "$file" || exit 1
    grep -Fq "namespace: kamaji-${tenant}" "$file" || exit 1
    grep -Fq "dataStoreSchema: kamaji-${tenant}" "$file" || exit 1
    grep -Fq "dataStoreUsername: kamaji-${tenant}" "$file" || exit 1
    grep -Fq "replicas: 1" "$file" || exit 1
    grep -Fq "version: \${KUBERNETES_VERSION}" "$file" || exit 1
    grep -Fq "path: /cgroupDriver" "$file" || exit 1
    grep -Fq "value: systemd" "$file" || exit 1
    grep -Fq "coreDNS: {}" "$file" || exit 1
    grep -Fq "kubeProxy: {}" "$file" || exit 1
    grep -Fq "version: \${KONNECTIVITY_AGENT_VERSION_DIGEST}" "$file" || exit 1
    grep -Fq "version: \${KONNECTIVITY_SERVER_VERSION_DIGEST}" "$file" || exit 1
  done
  grep -Fq "$TENANT_A_POD_CIDR" "$1/config/tenants/tenant-a.yaml" &&
  grep -Fq "$TENANT_A_SERVICE_CIDR" "$1/config/tenants/tenant-a.yaml" &&
  grep -Fq "$TENANT_A_DNS_SERVICE_IP" "$1/config/tenants/tenant-a.yaml" &&
  grep -Fq "$TENANT_A_CLUSTER_DOMAIN" "$1/config/tenants/tenant-a.yaml" &&
  grep -Fq "$TENANT_A_CERT_DNS" "$1/config/tenants/tenant-a.yaml" &&
  grep -Fq "$TENANT_B_POD_CIDR" "$1/config/tenants/tenant-b.yaml" &&
  grep -Fq "$TENANT_B_SERVICE_CIDR" "$1/config/tenants/tenant-b.yaml" &&
  grep -Fq "$TENANT_B_DNS_SERVICE_IP" "$1/config/tenants/tenant-b.yaml" &&
  grep -Fq "$TENANT_B_CLUSTER_DOMAIN" "$1/config/tenants/tenant-b.yaml" &&
  grep -Fq "$TENANT_B_CERT_DNS" "$1/config/tenants/tenant-b.yaml"
' _ "${LAB_ROOT}"
check "final tenant identities are pairwise distinct" bash -c '
  source "$1/config/settings.env"
  values=(
    "$TENANT_A_NAMESPACE" "$TENANT_B_NAMESPACE"
    "$TENANT_A_SCHEMA" "$TENANT_B_SCHEMA"
    "$TENANT_A_DATASTORE_USER" "$TENANT_B_DATASTORE_USER"
    "$TENANT_A_POD_CIDR" "$TENANT_B_POD_CIDR"
    "$TENANT_A_SERVICE_CIDR" "$TENANT_B_SERVICE_CIDR"
    "$TENANT_A_DNS_SERVICE_IP" "$TENANT_B_DNS_SERVICE_IP"
    "$TENANT_A_CLUSTER_DOMAIN" "$TENANT_B_CLUSTER_DOMAIN"
    "$TENANT_A_CERT_DNS" "$TENANT_B_CERT_DNS"
    "$TENANT_A_STORAGE_PATH" "$TENANT_B_STORAGE_PATH"
  )
  for ((i=0; i<${#values[@]}; i+=2)); do
    [[ "${values[i]}" != "${values[i+1]}" ]] || exit 1
  done
' _ "${LAB_ROOT}"
check "tenant add-ons render independently with exact CIDRs and paths" \
  tenant_addon_render_fixture
check "final worker names and volumes are exactly two by three" bash -c '
  source "$1/scripts/lib/workers.sh"
  expected=$'"'"'kamaji-tenant-a-worker-1\nkamaji-tenant-a-worker-2\nkamaji-tenant-a-worker-3\nkamaji-tenant-b-worker-1\nkamaji-tenant-b-worker-2\nkamaji-tenant-b-worker-3'"'"'
  actual=""
  for tenant in $TENANT_NAMES; do
    for ordinal in $(seq 1 "$WORKERS_PER_TENANT"); do
      actual+=$(worker_name "$tenant" "$ordinal")$'"'"'\n'"'"'
      [[ "$(worker_volume_name "$tenant" "$ordinal")" == "$(worker_name "$tenant" "$ordinal")-var-lib" ]] || exit 1
    done
  done
  [[ "${actual%$'\''\n'\''}" == "$expected" ]]
' _ "${LAB_ROOT}"
check "worker reconciliation covers current stopped stale and partial states" bash -c '
  file="$1/scripts/lib/workers.sh"
  grep -Fq "final_worker_current" "$file" &&
  grep -Fq "docker start" "$file" &&
  grep -Fq "remove_final_worker_container" "$file" &&
  grep -Fq "final_worker_node_registered" "$file" &&
  grep -Fq "reset_worker_state_for_rejoin" "$file" &&
  grep -Fq "token create --ttl" "$file" &&
  grep -Fq "token delete" "$file" &&
  grep -Fq "validate_disjoint_worker_sets" "$file" &&
  grep -Fq "WORKER_READY_GRACE_TIMEOUT" "$file" &&
  grep -Fq "stopped-worker readiness grace" "$1/scripts/test-e2e.sh"
' _ "${LAB_ROOT}"
check "semantic runtime names contain no workflow phase number" bash -c '
  ! grep -E "phase[0-9]" "$1/config/settings.env" "$1/README.md" \
    "$1/docs/high-level-design.md"
' _ "${LAB_ROOT}"
check "worker ownership rejects same-named unowned objects" bash -c '
  grep -Fq "refusing same-named unowned container" "$1" &&
  grep -Fq "refusing same-named unowned volume" "$1" &&
  grep -Fq "worker-var-lib" "$1" &&
  grep -Fq "WORKER_VOLUME_ID" "$1" &&
  grep -Fq "validate_final_worker_ownership_record" "$1" &&
  grep -Fq "validate_final_worker_volume_ownership_record" "$1"
' _ "${LAB_ROOT}/scripts/lib/workers.sh"
check "kube-proxy remediation is pre-join and retained as steady state" bash -c '
  tenant_file="$1/scripts/lib/tenants.sh"
  create_file="$1/scripts/create.sh"
  grep -Fq "maxPerCore: " "$tenant_file" &&
  grep -Fq "kamaji.clastix.io/paused=true" "$tenant_file" &&
  grep -Fq "Kamaji reverted conntrack.maxPerCore" "$tenant_file" &&
  grep -Fq "immediate_reversion=not-observed" "$tenant_file" &&
  patch_line="$(grep -n "configure_tenant_kube_proxy_conntrack" "$create_file" | tail -1 | cut -d: -f1)" &&
  worker_line="$(grep -n "reconcile_tenant_workers" "$create_file" | tail -1 | cut -d: -f1)" &&
  (( patch_line < worker_line )) &&
  ! grep -Fq "unpause_tenant_reconciliation" "$create_file" &&
  grep -Fq "tenant_kube_proxy_steady_state_is_preserved" "$create_file" &&
  grep -Fq "final.repeat-create" "$create_file"
' _ "${LAB_ROOT}"
check "kube-proxy restart regression is credential-safe lifecycle coverage" bash -c '
  file="$1/scripts/test-kube-proxy-restart.sh"
  test -x "$file" &&
  grep -Fq "delete pod" "$file" &&
  grep -Fq "replacement kube-proxy readiness" "$file" &&
  grep -Fq "permission denied" "$file" &&
  grep -Fq "tenant_kube_proxy_steady_state_is_preserved" "$file" &&
  ! grep -Eq "(cat|echo|printf).*(admin\\.conf|kubeconfig|token|password)" "$file"
' _ "${LAB_ROOT}"
check "worker failures preserve observed inspect and log evidence" bash -c '
  file="$1/scripts/lib/workers.sh"
  grep -Fq "docker container inspect" "$file" &&
  grep -Fq "docker logs --tail 30" "$file" &&
  grep -Fq "runtime=not-observed-container-not-running" "$file" &&
  grep -Fq "write_secret_file \"\${evidence_file}\"" "$file" &&
  grep -Fq "FINAL_WORKER_FAILURE_CODE=cni-konnectivity" "$file" &&
  grep -Fq "FINAL_WORKER_FAILURE_CODE=kubeadm-bootstrap" "$file" &&
  ! grep -Fq "FINAL_WORKER_FAILURE_EVIDENCE\" == *\"running=false\"" "$1/scripts/create.sh"
' _ "${LAB_ROOT}"
check "systemd wait probes consume a finite configured timeout" bash -c '
  grep -Fq "SYSTEMD_STATUS_TIMEOUT" "$1/config/settings.env" &&
  [[ "$(grep -c "seconds_from_duration.*SYSTEMD_STATUS_TIMEOUT" "$1/scripts/lib/workers.sh")" -ge 3 ]] &&
  [[ "$(grep -c "State.Running" "$1/scripts/lib/workers.sh")" -ge 3 ]]
' _ "${LAB_ROOT}"
check "final create reuses compatibility and rejects every spike residual" bash -c '
  file="$1/scripts/create.sh"
  grep -Fq "compatibility_result_is_current_pass" "$file" &&
  grep -Fq "cleanup_spike_resources" "$file" &&
  grep -Fq "verify_no_spike_residuals" "$file" &&
  grep -Fq "verify_initial_final_identities_free" "$file" &&
  grep -Fq "first_failing_prerequisite" "$file"
' _ "${LAB_ROOT}"
check "blocked final path removes every tenant-owned runtime layer" bash -c '
  grep -Fq "cleanup_final_topology" "$1/scripts/create.sh" &&
  grep -Fq "delete_final_tenant_smoke" "$1/scripts/create.sh" &&
  grep -Fq "remove_tenant_workers" "$1/scripts/create.sh" &&
  grep -Fq "delete_final_tenant_control_plane" "$1/scripts/create.sh" &&
  grep -Fq "exact etcd prefix" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "exact etcd user" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "exact etcd role" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "final_tenant_existed_at_start" "$1/scripts/create.sh"
' _ "${LAB_ROOT}"
check "final topology gates exact TCP schema worker storage and isolation counts" bash -c '
  file="$1/scripts/create.sh"
  grep -Fq "assert actual == expected" "$file" &&
  grep -Fq "expected six owned workers and six owned volumes" "$file" &&
  grep -Fq "management API contains tenant workers" "$file" &&
  grep -Fq "appears in both tenants" "$file" &&
  grep -Fq "storage appears in" "$file" &&
  grep -Fq "smoke PVC is not Bound" "$file" &&
  grep -Fq "complete Node set differs from the exact expected workers" \
    "$1/scripts/lib/workers.sh"
' _ "${LAB_ROOT}"
check "complete Node set rejects an extra unlabeled NotReady node" \
  exact_node_set_rejects_extra_node
check "runtime result captures endpoint and CA identity separation" bash -c '
  grep -Fq "verify_tenant_identity_separation" "$1" &&
  grep -Fq "tenant_a_endpoint=%s" "$1" &&
  grep -Fq "tenant_b_endpoint=%s" "$1" &&
  grep -Fq "tenant_a_ca_sha256=%s" "$1" &&
  grep -Fq "tenant_b_ca_sha256=%s" "$1"
' _ "${LAB_ROOT}/scripts/create.sh"
check "capacity accounting uses Docker caps and scheduled pod requests" bash -c '
  source "$1/config/settings.env"
  [[ "$WORKER_TOTAL_CPUS" == 7.5 && "$WORKER_TOTAL_MEMORY_GIB" == 15 &&
     "$MANAGEMENT_REQUEST_CPUS" == 1.2 &&
     "$MANAGEMENT_REQUEST_MEMORY_GIB" == 2.25 &&
     "$KIND_RESERVE_CPUS" == 2 && "$KIND_RESERVE_MEMORY_GIB" == 3 ]] &&
  grep -Fq "validate_final_worker_request_capacity" "$1/scripts/lib/workers.sh" &&
  grep -Fq "initContainers" "$1/scripts/lib/effective_requests.py" &&
  grep -Fq "restartPolicy" "$1/scripts/lib/effective_requests.py" &&
  grep -Fq "overhead" "$1/scripts/lib/effective_requests.py"
' _ "${LAB_ROOT}"
check "effective request fixtures cover init containers native sidecars and pod overhead" \
  effective_request_fixtures
check "ephemeral SQL and cross-auth clients have explicit resources" bash -c '
  source "$1/config/settings.env"
  [[ "$SQL_CLIENT_REQUEST_CPU" == 25m && "$SQL_CLIENT_REQUEST_MEMORY" == 32Mi &&
     "$SQL_CLIENT_LIMIT_CPU" == 100m && "$SQL_CLIENT_LIMIT_MEMORY" == 128Mi ]] &&
  block="$(sed -n "/^create_cnpg_sql_client()/,/^}/p" "$1/scripts/lib/cnpg.sh")" &&
  grep -Fq "requests:" <<<"$block" &&
  grep -Fq '\''cpu: ${SQL_CLIENT_REQUEST_CPU}'\'' <<<"$block" &&
  grep -Fq '\''memory: ${SQL_CLIENT_LIMIT_MEMORY}'\'' <<<"$block"
' _ "${LAB_ROOT}"
check "CNPG tenant manifests enforce exact independent database topology" bash -c '
  source "$1/config/settings.env"
  source "$1/config/versions.env"
  for tenant in tenant-a tenant-b; do
    file="$1/manifests/cnpg/cluster-${tenant}.yaml"
    [[ -s "$file" ]] || exit 1
    grep -Fq "name: ${tenant}-postgres" "$file" || exit 1
    grep -Fq "namespace: ${DATABASE_NAMESPACE}" "$file" || exit 1
    grep -Fq "instances: ${CNPG_INSTANCE_COUNT}" "$file" || exit 1
    grep -Fq "imageName: ${POSTGRES_IMAGE}" "$file" || exit 1
    grep -Fq "podAntiAffinityType: required" "$file" || exit 1
    grep -Fq "topologyKey: kubernetes.io/hostname" "$file" || exit 1
    grep -Fq "size: ${CNPG_STORAGE_SIZE}" "$file" || exit 1
    grep -Fq "storageClass: ${TENANT_STORAGE_CLASS}" "$file" || exit 1
    grep -Fq "cpu: ${CNPG_REQUEST_CPU}" "$file" || exit 1
    grep -Fq "memory: ${CNPG_REQUEST_MEMORY}" "$file" || exit 1
    grep -Fq "cpu: \"${CNPG_LIMIT_CPU}\"" "$file" || exit 1
    grep -Fq "memory: ${CNPG_LIMIT_MEMORY}" "$file" || exit 1
  done
  grep -Fq "database: ${TENANT_A_DATABASE}" "$1/manifests/cnpg/cluster-tenant-a.yaml" &&
  grep -Fq "owner: ${TENANT_A_DATABASE_OWNER}" "$1/manifests/cnpg/cluster-tenant-a.yaml" &&
  grep -Fq "database: ${TENANT_B_DATABASE}" "$1/manifests/cnpg/cluster-tenant-b.yaml" &&
  grep -Fq "owner: ${TENANT_B_DATABASE_OWNER}" "$1/manifests/cnpg/cluster-tenant-b.yaml" &&
  [[ "$TENANT_A_DATABASE" != "$TENANT_B_DATABASE" ]] &&
  [[ "$TENANT_A_DATABASE_OWNER" != "$TENANT_B_DATABASE_OWNER" ]]
' _ "${LAB_ROOT}"
check "CNPG installation is per-tenant digest-pinned and readiness-gated" bash -c '
  file="$1/scripts/lib/cnpg.sh"
  grep -Fq "for tenant in \${TENANT_NAMES}" "$file" &&
  grep -Fq "sha256_check \"\${CNPG_MANIFEST_SHA256}\"" "$file" &&
  grep -Fq "CNPG_CONTROLLER_IMAGE" "$file" &&
  grep -Fq "OPERATOR_IMAGE_NAME" "$file" &&
  grep -Fq -- "--server-side --force-conflicts" "$file" &&
  grep -Fq "cnpg_expected_crds" "$file" &&
  grep -Fq "cnpg_operator_ready" "$file" &&
  grep -Fq "cnpg_tenant_ready" "$file" &&
  grep -Fq "automountServiceAccountToken: false" "$file" &&
  ! grep -Fq "vcluster/" "$file"
' _ "${LAB_ROOT}"
check "create gates CNPG after add-ons and preserves skip and pause behavior" bash -c '
  file="$1/scripts/create.sh"
  addon_line="$(grep -n "install_final_tenant_addons" "$file" | tail -1 | cut -d: -f1)"
  cnpg_line="$(grep -n "^  install_all_cnpg$" "$file" | cut -d: -f1)"
  topology_line="$(grep -n "validate_exact_final_topology" "$file" | tail -1 | cut -d: -f1)"
  [[ -n "$addon_line" && -n "$cnpg_line" && -n "$topology_line" ]] &&
  (( addon_line < cnpg_line && cnpg_line < topology_line )) &&
  grep -Fq '\''"${SKIP_CNPG:-0}" != 1'\'' "$file" &&
  grep -Fq "final_cnpg_state=skipped" "$file" &&
  grep -Fq "final_result=partial" "$file" &&
  grep -Fq "cnpg=%s" "$file" &&
  grep -Fq "validate_final_worker_request_capacity" "$file" &&
  ! grep -Fq "unpause_tenant_reconciliation" "$file"
' _ "${LAB_ROOT}"
check "behavioral verifier covers both negative identities and recovery paths" bash -c '
  file="$1/scripts/verify.sh"
  test -x "$file" &&
  grep -Fq "verify_management_absence" "$file" &&
  grep -Fq "verify_management_topology" "$file" &&
  grep -Fq "tenant_kube_proxy_steady_state_is_preserved" "$file" &&
  grep -Fq "cross_kubernetes_identity_rejected tenant-a tenant-b" "$file" &&
  grep -Fq "cross_kubernetes_identity_rejected tenant-b tenant-a" "$file" &&
  grep -Fq "cross_database_identity_rejected tenant-a tenant-b" "$file" &&
  grep -Fq "cross_database_identity_rejected tenant-b tenant-a" "$file" &&
  grep -Fq "not an authentication failure" "$file" &&
  grep -Fq "failed by connectivity, not authentication rejection" "$file" &&
  grep -Fq "restart_replica_with_storage_reuse" "$file" &&
  grep -Fq "failover_to_different_primary" "$file" &&
  grep -Fq "replacement replica PVC" "$file" &&
  grep -Fq "replacement replica PV" "$file" &&
  grep -Fq "retained SQL marker" "$file" &&
  grep -Fq -- "--all-namespaces --no-headers" "$file" &&
  grep -Fq "storageClassName" "$file"
' _ "${LAB_ROOT}"
check "cross-auth ownership and exact cleanup are interruption safe" bash -c '
  block="$(sed -n "/^cross_database_identity_rejected()/,/^}/p" "$1/scripts/verify.sh")"
  grep -Fq -- "--local -f -" <<<"$block" &&
  grep -Fq "kamaji.cnpg-vcluster.io/role=cross-auth" <<<"$block" &&
  grep -Fq "trap - RETURN" <<<"$block" &&
  grep -Fq "trap - RETURN INT TERM HUP" <<<"$block" &&
  grep -Fq "trap '\''return 130'\'' INT TERM HUP" <<<"$block" &&
  grep -Fq "KAMAJI_TEST_FAIL_AFTER_CROSS_AUTH_SECRET" <<<"$block" &&
  ! grep -Fq "label secret" <<<"$block"
' _ "${LAB_ROOT}"
check "CNPG Kubernetes calls always carry explicit kubeconfigs" bash -c '
  ! grep -E "^[[:space:]]*kubectl[[:space:]]" \
    "$1/scripts/lib/cnpg.sh" "$1/scripts/verify.sh" &&
  grep -Fq '\''KUBECONFIG="${cross_config}" kubectl'\'' "$1/scripts/verify.sh" &&
  grep -Fq "tenant_kubectl" "$1/scripts/lib/cnpg.sh"
' _ "${LAB_ROOT}"
check "CNPG clients never log credentials" bash -c '
  ! grep -Eq "(echo|log|warn).*(password|PGPASSWORD|client-key-data|admin\\.conf)" \
    "$1/scripts/lib/cnpg.sh" "$1/scripts/verify.sh" &&
  ! grep -Eq "printf.*password.*(tee|>[^|])" "$1/scripts/verify.sh" &&
  grep -Fq "valueFrom:" "$1/scripts/lib/cnpg.sh" &&
  grep -Fq "secretKeyRef:" "$1/scripts/lib/cnpg.sh" &&
  grep -Fq "unset password" "$1/scripts/verify.sh"
' _ "${LAB_ROOT}"
check "status and diagnostics cover both final tenants and exit shapes" bash -c '
  grep -Fq "final tenant topology" "$1/scripts/status.sh" &&
  grep -Fq "passing final result lacks exactly two TCPs" "$1/scripts/status.sh" &&
  grep -Fq "blocked final result and blocker records are stale, incomplete, or inconsistent" "$1/scripts/status.sh" &&
  grep -Fq "blocked final result residual proof failed" "$1/scripts/status.sh" &&
  grep -Fq "healthy owned management infrastructure only" "$1/scripts/status.sh" &&
  grep -Fq "kube-proxy conntrack.maxPerCore" "$1/scripts/status.sh" &&
  grep -Fq "CoreDNS" "$1/scripts/status.sh" &&
  grep -Fq "Konnectivity" "$1/scripts/status.sh" &&
  grep -Fq "local-path" "$1/scripts/status.sh" &&
  grep -Fq "Kamaji reconciliation is not intentionally paused" "$1/scripts/status.sh" &&
  grep -Fq "final result evidence" "$1/scripts/diagnose.sh" &&
  grep -Fq "for tenant in \${TENANT_NAMES}" "$1/scripts/diagnose.sh" &&
  grep -Fq "exit \"\${health}\"" "$1/scripts/diagnose.sh"
' _ "${LAB_ROOT}"
check "status and diagnostics include tenant-owned CNPG and PostgreSQL layers" bash -c '
  grep -Fq "report_cnpg_status" "$1/scripts/status.sh" &&
  grep -Fq "CNPG operator:" "$1/scripts/status.sh" &&
  grep -Fq "PostgreSQL cluster:" "$1/scripts/status.sh" &&
  grep -Fq "PostgreSQL services:" "$1/scripts/status.sh" &&
  grep -Fq "CloudNativePG and PostgreSQL" "$1/scripts/diagnose.sh" &&
  grep -Fq "clusters.postgresql.cnpg.io,pods,services,endpoints,pvc" "$1/scripts/diagnose.sh" &&
  grep -Fq "get pv -o wide" "$1/scripts/diagnose.sh" &&
  grep -Fq "cnpg_tenant_ready" "$1/scripts/diagnose.sh"
' _ "${LAB_ROOT}"

check "tenant teardown is exact and verifies the survivor" bash -c '
  file="$1/scripts/destroy-tenant.sh"
  test -x "$file" &&
  grep -Fq "delete_cnpg_for_tenant" "$file" &&
  grep -Fq "delete_tenant_test_resources" "$file" &&
  grep -Fq "remove_tenant_workers" "$file" &&
  grep -Fq "delete_final_tenant_control_plane" "$file" &&
  grep -Fq "tenant_management_secrets" "$file" &&
  grep -Fq "DataStore/default status.usedBy" "$file" &&
  grep -Fq "exact datastore schema" "$file" &&
  grep -Fq "shared datastore is unavailable during cleanup proof" "$file" &&
  grep -Fq "verify_surviving_tenant_health" "$file" &&
  grep -Fq "cnpg_verify_marker_if_present" "$file" &&
  grep -Fq "container identity drifted" "$file"
' _ "${LAB_ROOT}"
check "targeted teardown requires datastore inspection before mutation" bash -c '
  file="$1/scripts/destroy-tenant.sh"
  require_line="$(grep -n "require_management_datastore_inspection" "$file" | head -1 | cut -d: -f1)"
  cnpg_line="$(grep -n "delete_cnpg_for_tenant" "$file" | tail -1 | cut -d: -f1)"
  [[ -n "$require_line" && -n "$cnpg_line" && "$require_line" -lt "$cnpg_line" ]] &&
  grep -Fq "KAMAJI_TEST_DATASTORE_UNAVAILABLE" "$1/scripts/lib/tenants.sh" &&
  grep -Fq "datastore-unavailable targeted teardown removed retry evidence" \
    "$1/scripts/test-e2e.sh"
' _ "${LAB_ROOT}"
check "survivor marker policy accepts unseeded and requires seeded identity" \
  optional_marker_policy_fixture unseeded
check "survivor marker policy accepts the correct seeded marker" \
  optional_marker_policy_fixture seeded-correct
check "survivor marker policy rejects an incorrect seeded marker" \
  optional_marker_policy_fixture seeded-wrong
check "control-plane diagnostics expose restart and OOMKilled evidence" \
  control_plane_oom_fixture
check "control-plane diagnostics tolerate absent pod JSON" \
  control_plane_status_absent_fixture
check "create failure records tenant control-plane OOMKilled cause" bash -c '
  grep -Fq "all_tenant_control_plane_oom_evidence" "$1/scripts/create.sh" &&
  grep -Fq "tenant control-plane OOMKilled" "$1/scripts/create.sh" &&
  grep -Fq "control plane has OOMKilled container evidence" "$1/scripts/status.sh" &&
  grep -Fq "control plane has OOMKilled container evidence" "$1/scripts/diagnose.sh"
' _ "${LAB_ROOT}"
check "full teardown removes shared resources then restores host state" bash -c '
  file="$1/scripts/destroy.sh"
  test -x "$file" &&
  grep -Fq "cleanup_spike_resources" "$file" &&
  grep -Fq "destroy_one_tenant tenant-a false" "$file" &&
  grep -Fq "destroy_kamaji_shared_resources" "$file" &&
  grep -Fq "destroy_metallb_shared_resources" "$file" &&
  grep -Fq "destroy_cert_manager_shared_resources" "$file" &&
  grep -Fq "restore_recorded_inotify_values" "$file" &&
  grep -Fq "delete_owned_kind_cluster" "$file" &&
  grep -Fq "verify_no_owned_lab_resources" "$file" &&
  grep -Fq "rm -rf \"\${RUNTIME_DIR}\"" "$file" &&
  kind_line="$(grep -n "delete_owned_kind_cluster" "$file" | tail -1 | cut -d: -f1)" &&
  restore_line="$(grep -n "restore_recorded_inotify_values" "$file" | tail -1 | cut -d: -f1)" &&
  (( kind_line < restore_line ))
' _ "${LAB_ROOT}"
check "full teardown refuses unexpected ownership-labelled objects" bash -c '
  grep -Fq "validate_owned_docker_inventory" "$1/scripts/destroy.sh" &&
  ! grep -Fq "remove_owned_residual_docker_resources" "$1/scripts/destroy.sh" &&
  grep -Fq "unexpected ownership-labelled sentinel was swept" \
    "$1/scripts/test-e2e.sh"
' _ "${LAB_ROOT}"
check "all delete waits are finite or explicitly non-waiting" bash -c '
  python3 - "$1/scripts" <<'"'"'PY'"'"'
from pathlib import Path
import sys
for path in Path(sys.argv[1]).rglob("*.sh"):
    if path.name == "test-static.sh":
        continue
    lines=path.read_text(encoding="utf-8").splitlines()
    for index,line in enumerate(lines):
        if not any(token in line for token in (
            "management_kubectl", "tenant_kubectl", "management_helm",
            "kind delete cluster",
        )):
            continue
        command=line
        cursor=index
        while command.rstrip().endswith("\\"):
            cursor += 1
            command += " " + lines[cursor]
        if " delete " in f" {command} ":
            bounded = "--wait=false" in command or "--timeout" in command
            if "kind delete cluster" in command and index > 0:
                bounded = bounded or "timeout " in lines[index - 1]
            assert bounded, (
                f"{path}:{index+1}: {command}"
            )
        if "management_helm uninstall" in command:
            assert "--timeout" in command, f"{path}:{index+1}: {command}"
PY
' _ "${LAB_ROOT}"
check "Kamaji teardown names kubectl-applied hook RBAC and exact CRDs" bash -c '
  file="$1/scripts/lib/management.sh"
  grep -Fq "serviceaccount/kamaji-etcd" "$file" &&
  grep -Fq "role.rbac.authorization.k8s.io/kamaji-etcd-gen-certs-role" "$file" &&
  grep -Fq "rolebinding.rbac.authorization.k8s.io/kamaji-etcd-gen-certs-rolebinding" "$file" &&
  grep -Fq "crd/datastores.kamaji.clastix.io" "$file" &&
  grep -Fq "crd/kubeconfiggenerators.kamaji.clastix.io" "$file" &&
  grep -Fq "crd/tenantcontrolplanes.kamaji.clastix.io" "$file" &&
  grep -Fq -- "--kubeconfig \"\${MANAGEMENT_KUBECONFIG}\"" "$file"
' _ "${LAB_ROOT}"
check "lifecycle E2E covers clean partial healthy recovery refusal and teardown" bash -c '
  file="$1/scripts/test-e2e.sh"
  test -x "$file" &&
  grep -Fq "e2e-clean-status.log" "$file" &&
  grep -Fq "e2e-partial-status.log" "$file" &&
  grep -Fq "e2e-healthy-status.log" "$file" &&
  grep -Fq "assert_marker_table_absent" "$file" &&
  grep -Fq "stable CNPG health after unseeded teardown recovery" "$file" &&
  grep -Fq "seed_markers" "$file" &&
  grep -Fq "repeat create replaced a healthy worker" "$file" &&
  grep -Fq "deleted tenant kubeconfig was not securely re-exported" "$file" &&
  grep -Fq "stale-expired-join" "$file" &&
  grep -Fq "retained-worker-value" "$file" &&
  grep -Fq "destroy-tenant.sh" "$file" &&
  grep -Fq "test_unowned_refusals" "$file" &&
  grep -Fq "assert_sentinels_present" "$file" &&
  grep -Fq "did not restore the recorded host inotify values" "$file" &&
  grep -Fq "captured lifecycle output contains credential material" "$file"
' _ "${LAB_ROOT}"
check "observer fingerprint covers tenant CNPG resources" bash -c '
  block="$(sed -n "/^management_fingerprint()/,/^}/p" "$1/scripts/test-e2e.sh")"
  grep -Fq "clusters.postgresql.cnpg.io" <<<"${block}" &&
  grep -Fq "RESOURCE_SCOPE=\"\${tenant}\"" <<<"${block}"
' _ "${LAB_ROOT}"

check "management values disable telemetry" \
  has_text '^  disabled: true$' "${LAB_ROOT}/config/kamaji-values.yaml"
check "management values pin controller input" bash -c '
  grep -Fq "repository: clastix/kamaji" "$1" &&
  grep -Fq "tag: 26.8.6-edge" "$1"
' _ "${LAB_ROOT}/config/kamaji-values.yaml"
check "management values enforce datastore capacity" bash -c '
  grep -Fq "replicas: 3" "$1" &&
  grep -Fq "size: 1Gi" "$1" &&
  grep -Fq "retentionPolicyWhenDeleted: Retain" "$1" &&
  grep -Fq "cpu: 100m" "$1" &&
  grep -Fq "memory: 256Mi" "$1" &&
  grep -Fq "cpu: 500m" "$1" &&
  grep -Fq "memory: 512Mi" "$1"
' _ "${LAB_ROOT}/config/kamaji-values.yaml"
check "management values use locked datastore image inputs" bash -c '
  grep -Fq "tag: v3.5.17" "$1" &&
  grep -Fq "tag: v3.5.6" "$1" &&
  grep -Fq "tag: v1.36" "$1" &&
  ! grep -Eq "dependency.*(update|build)" "$1"
' _ "${LAB_ROOT}/config/kamaji-values.yaml"
check "MetalLB template defines exactly two explicit non-auto VIPs" bash -c '
  [[ "$(grep -c "/32" "$1")" -eq 2 ]] &&
  grep -Fq "autoAssign: false" "$1" &&
  grep -Fq "kind: L2Advertisement" "$1" &&
  grep -Fq "kamaji-tenant-vips" "$1"
' _ "${LAB_ROOT}/manifests/metallb/pool.yaml.tpl"
check "network library derives and revalidates Docker VIPs" bash -c '
  grep -Fq "docker network inspect" "$1" &&
  grep -Fq "broadcast_address" "$1" &&
  grep -Fq "recorded VIP is assigned to a Docker endpoint" "$1" &&
  grep -Fq "EXCLUDED_CIDRS" "$1"
' _ "${LAB_ROOT}/scripts/lib/network.sh"
check "management ownership fails closed" bash -c '
  set +e
  output="$(
    {
      source "$1/scripts/lib/management.sh"
      MANAGEMENT_OWNERSHIP_FILE="$1/.runtime/nonexistent-ownership-fixture"
      validate_management_ownership
    } 2>&1
  )"
  status=$?
  set -e
  [[ "$status" -eq 1 ]] && grep -Fq "management.ownership-refusal" <<<"$output"
' _ "${LAB_ROOT}"
check "introduced-component cleanup polarity is explicit" \
  cleanup_polarity_is_explicit
check "fresh cert-manager failure is targeted and preserves dependencies" \
  fresh_cert_manager_failure_is_targeted
check "management scripts contain no fallback artifact path" bash -c '
  ! grep -Eiq "(fallback|stable\\.clastix|license|activation|vcluster/)" \
    "$1/scripts/create-management.sh" "$1/scripts/lib/management.sh"
' _ "${LAB_ROOT}"
check "create-management stops at zero TCPs and workers" bash -c '
  grep -Fq "expected zero TenantControlPlanes" "$1" &&
  grep -Fq "expected zero owned worker containers" "$1"
' _ "${LAB_ROOT}/scripts/create-management.sh"
check "management reconcile preserves compatibility blocker evidence" \
  not_has_text 'rm -f .*BLOCKER_FILE' "${LAB_ROOT}/scripts/create-management.sh"
check "management Helm uses the locked local chart and deterministic renderer" bash -c '
  grep -Fq '\''"${KAMAJI_CHART_DIR}"'\'' "$1" &&
  grep -Fq -- "--post-renderer" "$1" &&
  grep -Fq -- "--no-hooks" "$1" &&
  grep -Fq "KAMAJI_POST_HOOKS_MANIFEST" "$1" &&
  ! grep -Fq "helm dependency" "$1"
' _ "${LAB_ROOT}/scripts/lib/management.sh"

check "lab-local state is ignored narrowly" bash -c '
  [[ "$(cat "$1/.gitignore")" == $'"'"'.tools/\n.runtime/'"'"' ]]
' _ "${LAB_ROOT}"
check "README documents checksum-verified just installation" \
  has_text '4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d' \
  "${LAB_ROOT}/README.md"
check "README documents edge and no-activation status" \
  has_text '26\.8\.6-edge.*no account|no account.*26\.8\.6-edge' "${LAB_ROOT}/README.md"
check "README documents telemetry opt-out" \
  has_text 'telemetry\.disabled: true' "${LAB_ROOT}/README.md"
check "README documents exit meanings" \
  has_text '[Ee]xit status `2`.*compatibility blocker' "${LAB_ROOT}/README.md"
check "design documents privileged shared-kernel boundary" \
  has_text 'share the Docker host kernel' "${LAB_ROOT}/docs/high-level-design.md"
check "design records deterministic transitive image inventory" \
  has_text 'transitive image inventory' "${LAB_ROOT}/docs/high-level-design.md"
check "design retains explicit Konnectivity digest action" bash -c '
  grep -Fq "KONNECTIVITY_AGENT_IMAGE" "$1" &&
  grep -Fq "KONNECTIVITY_SERVER_IMAGE" "$1"
' _ "${LAB_ROOT}/docs/high-level-design.md"
check "documentation covers lifecycle and support boundaries" bash -c '
  grep -Fq "just destroy-tenant tenant-a" "$1/README.md" &&
  grep -Fq "just destroy" "$1/README.md" &&
  grep -Fq "just test-e2e" "$1/README.md" &&
  grep -Fq "intentional" "$1/README.md" &&
  grep -Fq "inotify" "$1/docs/high-level-design.md" &&
  grep -Fq "Teardown" "$1/docs/high-level-design.md" &&
  grep -Fq "public edge" "$1/docs/high-level-design.md"
' _ "${LAB_ROOT}"
check "repair recipe and dataplane alternatives are documented" bash -c '
  test -x "$1/scripts/repair-tenant.sh" &&
  grep -Fq "repair tenant:" "$1/Justfile" &&
  grep -Fq "validate_final_tenant_ownership" "$1/scripts/repair-tenant.sh" &&
  grep -Fq "Alternatives considered" "$1/docs/high-level-design.md" &&
  grep -Fq "Self-managed kube-proxy" "$1/docs/high-level-design.md" &&
  grep -Fq "Kube-proxy-free Calico/eBPF" "$1/docs/high-level-design.md"
' _ "${LAB_ROOT}"
check "third-party notices and Apache license are tracked and linked" bash -c '
  test -s "$1/THIRD_PARTY_NOTICES.md" &&
  test -s "$1/licenses/Apache-2.0.txt" &&
  grep -Fq "Kamaji NOTICE" "$1/THIRD_PARTY_NOTICES.md" &&
  for project in Kamaji cert-manager MetalLB Calico "Local Path" CloudNativePG; do
    grep -Fq "$project" "$1/THIRD_PARTY_NOTICES.md" || exit 1
  done &&
  grep -Fq "THIRD_PARTY_NOTICES.md" "$1/README.md"
' _ "${LAB_ROOT}"

if (( failures > 0 )); then
  die "${failures} static check(s) failed"
fi

log "all ${checks} Kamaji static checks passed"
