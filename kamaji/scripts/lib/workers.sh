#!/usr/bin/env bash

set -Eeuo pipefail
WORKERS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${WORKERS_LIB_DIR}/common.sh"

FINAL_WORKER_FAILURE_EVIDENCE=""
FINAL_WORKER_FAILURE_CODE=""
FINAL_WORKER_FAILURE_RECOGNIZED=false

worker_container_exists() {
  docker container inspect "${SPIKE_WORKER_NAME}" >/dev/null 2>&1
}

spike_worker_owned() {
  [[ "$(docker container inspect --format "{{index .Config.Labels \"${OWNERSHIP_LABEL}\"}}" \
      "${SPIKE_WORKER_NAME}" 2>/dev/null || true)" == "${LAB_PREFIX}" ]] \
    && [[ "$(docker container inspect --format '{{index .Config.Labels "kamaji.cnpg-vcluster.io/tenant"}}' \
      "${SPIKE_WORKER_NAME}" 2>/dev/null || true)" == spike ]] \
    && [[ "$(docker container inspect --format '{{index .Config.Labels "kamaji.cnpg-vcluster.io/role"}}' \
      "${SPIKE_WORKER_NAME}" 2>/dev/null || true)" == worker ]]
}

spike_volume_owned() {
  [[ "$(docker volume inspect --format "{{index .Labels \"${OWNERSHIP_LABEL}\"}}" \
      "${SPIKE_VOLUME_NAME}" 2>/dev/null || true)" == "${LAB_PREFIX}" ]] \
    && [[ "$(docker volume inspect --format '{{index .Labels "kamaji.cnpg-vcluster.io/tenant"}}' \
      "${SPIKE_VOLUME_NAME}" 2>/dev/null || true)" == spike ]]
}

record_spike_worker_ownership() {
  local volume="$1"
  {
    printf 'WORKER_NAME=%s\n' "${SPIKE_WORKER_NAME}"
    printf 'WORKER_ID=%s\n' "$(docker container inspect --format '{{.Id}}' "${SPIKE_WORKER_NAME}")"
    printf 'WORKER_VOLUME=%s\n' "${volume}"
    if [[ -n "${volume}" ]]; then
      printf 'WORKER_VOLUME_ID=%s\n' \
        "$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${volume}")"
    fi
  } | write_secret_file "${SPIKE_WORKER_OWNERSHIP_FILE}"
}

validate_spike_worker_ownership() {
  spike_worker_owned \
    || die "spike.worker-ownership: refusing same-named unowned container ${SPIKE_WORKER_NAME}"
  [[ -f "${SPIKE_WORKER_OWNERSHIP_FILE}" ]] \
    || die "spike.worker-ownership: live worker lacks runtime ownership evidence"
  local recorded_id live_id
  recorded_id="$(sed -n 's/^WORKER_ID=//p' "${SPIKE_WORKER_OWNERSHIP_FILE}")"
  live_id="$(docker container inspect --format '{{.Id}}' "${SPIKE_WORKER_NAME}")"
  [[ "${recorded_id}" == "${live_id}" ]] \
    || die "spike.worker-ownership: live worker identity differs from its record"
}

ensure_spike_volume() {
  if docker volume inspect "${SPIKE_VOLUME_NAME}" >/dev/null 2>&1; then
    spike_volume_owned \
      || die "spike.worker-ownership: refusing same-named unowned volume ${SPIKE_VOLUME_NAME}"
  else
    docker volume create \
      --label "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
      --label "kamaji.cnpg-vcluster.io/tenant=spike" \
      --label "kamaji.cnpg-vcluster.io/role=worker-var-lib" \
      "${SPIKE_VOLUME_NAME}" >/dev/null
  fi
}

spike_worker_systemd_ready() {
  local state
  [[ "$(docker container inspect --format '{{.State.Running}}' \
      "${SPIKE_WORKER_NAME}" 2>/dev/null || true)" == true ]] \
    || return 1
  state="$(
    timeout "$(seconds_from_duration "${SYSTEMD_STATUS_TIMEOUT}")" \
      docker exec "${SPIKE_WORKER_NAME}" systemctl is-system-running --wait \
      2>/dev/null || true
  )"
  grep -Eq 'running|degraded' <<<"${state}"
}

spike_worker_substrate_ready() {
  spike_worker_systemd_ready \
    && docker exec "${SPIKE_WORKER_NAME}" test -f /sys/fs/cgroup/cgroup.controllers \
    && docker exec "${SPIKE_WORKER_NAME}" systemctl is-active containerd >/dev/null \
    && docker exec "${SPIKE_WORKER_NAME}" test -S /run/containerd/containerd.sock \
    && docker exec "${SPIKE_WORKER_NAME}" crictl info >/dev/null \
    && docker exec "${SPIKE_WORKER_NAME}" iptables --version >/dev/null \
    && [[ -z "$(docker exec "${SPIKE_WORKER_NAME}" swapon --show --noheadings 2>/dev/null)" ]]
}

start_spike_worker() {
  local persistent="$1"
  local mounts=()
  ensure_runtime_layout
  mkdir -p -m 0700 "${SPIKE_RUNTIME_DIR}"

  if worker_container_exists; then
    validate_spike_worker_ownership
    docker rm -f "${SPIKE_WORKER_NAME}" >/dev/null
    rm -f "${SPIKE_WORKER_OWNERSHIP_FILE}"
  fi
  if [[ "${persistent}" == true ]]; then
    ensure_spike_volume
    mounts+=(--mount "type=volume,src=${SPIKE_VOLUME_NAME},dst=/var/lib")
  fi

  docker run --detach \
    --name "${SPIKE_WORKER_NAME}" \
    --hostname "${SPIKE_WORKER_NAME}" \
    --label "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    --label "kamaji.cnpg-vcluster.io/tenant=spike" \
    --label "kamaji.cnpg-vcluster.io/role=worker" \
    --network kind \
    --privileged \
    --cgroupns=private \
    --security-opt seccomp=unconfined \
    --tmpfs /run \
    --tmpfs /run/lock \
    --sysctl net.ipv4.ip_forward=1 \
    --cpus "${WORKER_CPUS}" \
    --memory "${WORKER_MEMORY_MIB}m" \
    --volume /lib/modules:/lib/modules:ro \
    "${mounts[@]}" \
    "${KIND_NODE_IMAGE}" >/dev/null
  record_spike_worker_ownership "$([[ "${persistent}" == true ]] && printf '%s' "${SPIKE_VOLUME_NAME}")"

  if ! wait_for "${WORKER_START_TIMEOUT}" "systemd in ${SPIKE_WORKER_NAME}" \
    spike_worker_systemd_ready; then
    return 1
  fi
  docker exec "${SPIKE_WORKER_NAME}" swapoff -a >/dev/null 2>&1 || true
  docker exec "${SPIKE_WORKER_NAME}" modprobe br_netfilter >/dev/null 2>&1 || true
  docker exec "${SPIKE_WORKER_NAME}" sysctl -w net.ipv4.ip_forward=1 >/dev/null
  wait_for "${WORKER_START_TIMEOUT}" "worker substrate in ${SPIKE_WORKER_NAME}" \
    spike_worker_substrate_ready
}

remove_spike_worker_container() {
  if worker_container_exists; then
    validate_spike_worker_ownership
    docker rm -f "${SPIKE_WORKER_NAME}" >/dev/null
  fi
}

remove_spike_worker_and_volume() {
  remove_spike_worker_container
  if docker volume inspect "${SPIKE_VOLUME_NAME}" >/dev/null 2>&1; then
    spike_volume_owned \
      || die "spike.worker-ownership: refusing deletion of unowned volume ${SPIKE_VOLUME_NAME}"
    [[ -f "${SPIKE_WORKER_OWNERSHIP_FILE}" ]] \
      || die "spike.worker-ownership: live volume lacks runtime ownership evidence"
    local recorded_volume recorded_volume_id live_volume_id
    recorded_volume="$(sed -n 's/^WORKER_VOLUME=//p' "${SPIKE_WORKER_OWNERSHIP_FILE}")"
    recorded_volume_id="$(sed -n 's/^WORKER_VOLUME_ID=//p' "${SPIKE_WORKER_OWNERSHIP_FILE}")"
    live_volume_id="$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${SPIKE_VOLUME_NAME}")"
    [[ "${recorded_volume}" == "${SPIKE_VOLUME_NAME}" \
      && -n "${recorded_volume_id}" && "${recorded_volume_id}" == "${live_volume_id}" ]] \
      || die "spike.worker-ownership: live volume identity differs from its record"
    docker volume rm "${SPIKE_VOLUME_NAME}" >/dev/null
  fi
  rm -f "${SPIKE_WORKER_OWNERSHIP_FILE}"
}

delete_spike_node() {
  if [[ -f "$(tenant_kubeconfig spike)" ]] \
    && tenant_kubectl spike get --raw=/readyz >/dev/null 2>&1; then
    tenant_kubectl spike delete node "${SPIKE_WORKER_NAME}" \
      --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" >/dev/null
  fi
}

remove_worker_join_material() {
  if worker_container_exists && spike_worker_owned; then
    docker exec "${SPIKE_WORKER_NAME}" rm -rf /var/lib/kamaji-spike-join \
      >/dev/null 2>&1 || true
  fi
  rm -f "${SPIKE_JOIN_FILE}"
}

delete_bootstrap_token() {
  local token_id="$1"
  [[ -n "${token_id}" ]] || return 0
  if worker_container_exists && spike_worker_owned; then
    docker exec "${SPIKE_WORKER_NAME}" kubeadm \
      --kubeconfig=/var/lib/kamaji-spike-join/admin.conf \
      token delete "${token_id}" >/dev/null 2>&1 || true
  fi
}

observed_preflight_errors() {
  python3 - "${SPIKE_PREFLIGHT_EVIDENCE}" <<'PY'
import re, sys
text=open(sys.argv[1], encoding="utf-8", errors="replace").read()
items=[]
for match in re.finditer(r"\[ERROR ([^\]]+)\]", text):
    name=match.group(1).split(":",1)[0].strip()
    if name and name not in items:
        items.append(name)
print(",".join(items))
PY
}

join_failure_summary() {
  [[ -f "${SPIKE_PREFLIGHT_EVIDENCE}" ]] || {
    printf 'join failed before kubeadm execution\n'
    return
  }
  grep -E '^\s*\[ERROR |^error:|^To see the stack trace' \
    "${SPIKE_PREFLIGHT_EVIDENCE}" \
    | tail -20 \
    | sed -E \
      -e 's/([a-z0-9]{6})\.[a-z0-9]{16}/REDACTED/g' \
      -e 's/[[:space:]]+/ /g' \
    | paste -sd '|' -
}

preflight_errors_are_allowed() {
  local observed="$1"
  [[ -n "${observed}" && -n "${KUBEADM_IGNORE_PREFLIGHT_ERRORS}" ]] || return 1
  OBSERVED="${observed}" ALLOWED="${KUBEADM_IGNORE_PREFLIGHT_ERRORS}" python3 -c '
import os
observed=set(filter(None,os.environ["OBSERVED"].split(",")))
allowed=set(filter(None,os.environ["ALLOWED"].split(",")))
raise SystemExit(0 if observed == allowed else 1)
'
}

kubeadm_ignore_preflight_arg() {
  printf -- '--ignore-preflight-errors=%s\n' "${KUBEADM_IGNORE_PREFLIGHT_ERRORS}"
}

spike_node_registered() {
  tenant_kubectl spike get node "${SPIKE_WORKER_NAME}" >/dev/null 2>&1
}

spike_node_ready() {
  [[ "$(tenant_kubectl spike get node "${SPIKE_WORKER_NAME}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)" == True ]]
}

join_spike_worker() {
  local join_command token_id initial_status observed ignore_arg=()
  docker exec "${SPIKE_WORKER_NAME}" mkdir -p -m 0700 /var/lib/kamaji-spike-join
  docker cp "$(tenant_kubeconfig spike)" \
    "${SPIKE_WORKER_NAME}:/var/lib/kamaji-spike-join/admin.conf" >/dev/null
  docker exec "${SPIKE_WORKER_NAME}" chmod 0600 \
    /var/lib/kamaji-spike-join/admin.conf

  join_command="$(docker exec "${SPIKE_WORKER_NAME}" kubeadm \
    --kubeconfig=/var/lib/kamaji-spike-join/admin.conf \
    token create --ttl "${KUBEADM_TOKEN_TTL}" --print-join-command 2>/dev/null)" \
    || return 1
  token_id="$(sed -E 's/.*--token ([^. ]+)\..*/\1/' <<<"${join_command}")"
  [[ "${token_id}" =~ ^[a-z0-9]{6}$ ]] || return 1
  trap 'delete_bootstrap_token "${token_id}"; remove_worker_join_material; trap - RETURN' RETURN

  {
    printf '#!/usr/bin/env bash\n'
    printf 'set -Eeuo pipefail\n'
    printf '%s' "${join_command}"
    printf ' --cri-socket unix:///run/containerd/containerd.sock'
    printf ' --node-name %q' "${SPIKE_WORKER_NAME}"
    printf ' "$@"\n'
  } | write_secret_file "${SPIKE_JOIN_FILE}"
  docker cp "${SPIKE_JOIN_FILE}" \
    "${SPIKE_WORKER_NAME}:/var/lib/kamaji-spike-join/join.sh" >/dev/null
  docker exec "${SPIKE_WORKER_NAME}" chmod 0600 \
    /var/lib/kamaji-spike-join/join.sh

  set +e
  timeout "$(seconds_from_duration "${WORKER_JOIN_TIMEOUT}")" \
    docker exec "${SPIKE_WORKER_NAME}" \
    /bin/bash /var/lib/kamaji-spike-join/join.sh \
    >"${SPIKE_PREFLIGHT_EVIDENCE}" 2>&1
  initial_status=$?
  set -e
  chmod 0600 "${SPIKE_PREFLIGHT_EVIDENCE}"

  if (( initial_status != 0 )); then
    observed="$(observed_preflight_errors)"
    if ! preflight_errors_are_allowed "${observed}"; then
      printf 'observed=%s\nconfigured=%s\n' \
        "${observed:-unparsed}" "${KUBEADM_IGNORE_PREFLIGHT_ERRORS:-none}" \
        >"${SPIKE_PREFLIGHT_EVIDENCE}.summary"
      chmod 0600 "${SPIKE_PREFLIGHT_EVIDENCE}.summary"
      return 1
    fi
    ignore_arg=("$(kubeadm_ignore_preflight_arg)")
    docker exec "${SPIKE_WORKER_NAME}" kubeadm reset --force \
      --cri-socket unix:///run/containerd/containerd.sock >/dev/null 2>&1 || true
    if ! timeout "$(seconds_from_duration "${WORKER_JOIN_TIMEOUT}")" \
      docker exec "${SPIKE_WORKER_NAME}" \
        /bin/bash /var/lib/kamaji-spike-join/join.sh "${ignore_arg[@]}" \
        >/dev/null 2>&1; then
      return 1
    fi
  fi
  wait_for "${WORKER_JOIN_TIMEOUT}" "spike worker registration" spike_node_registered
}

label_spike_node() {
  tenant_kubectl spike label node "${SPIKE_WORKER_NAME}" \
    "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    kamaji.cnpg-vcluster.io/tenant=spike \
    kamaji.cnpg-vcluster.io/role=worker \
    --overwrite >/dev/null
}

validate_target_worker_contract() {
  local cgroup_driver server endpoint
  server="$(tenant_kubectl spike version -o json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
  endpoint="$(management_kubectl -n "${SPIKE_NAMESPACE}" get \
    "tenantcontrolplane/${SPIKE_NAME}" -o jsonpath='{.status.controlPlaneEndpoint}')"
  cgroup_driver="$(docker exec "${SPIKE_WORKER_NAME}" \
    grep -E '^cgroupDriver:' /var/lib/kubelet/config.yaml | awk '{print $2}')"
  [[ "${server}" == "${KUBERNETES_VERSION}" \
    && "${endpoint}" == "${SPIKE_VIP}:6443" \
    && "${cgroup_driver}" == systemd ]]
}

validate_spike_allocatable() {
  local allocatable cpu_cap memory_cap
  allocatable="$(tenant_kubectl spike get node "${SPIKE_WORKER_NAME}" -o json)"
  cpu_cap="$(docker container inspect --format '{{.HostConfig.NanoCpus}}' \
    "${SPIKE_WORKER_NAME}")"
  memory_cap="$(docker container inspect --format '{{.HostConfig.Memory}}' \
    "${SPIKE_WORKER_NAME}")"
  tenant_kubectl spike get pods --all-namespaces \
    --field-selector "spec.nodeName=${SPIKE_WORKER_NAME}" -o json \
  | CPU_CAP="${cpu_cap}" MEMORY_CAP="${memory_cap}" \
    python3 "${WORKERS_LIB_DIR}/effective_requests.py" \
    >"${SPIKE_PERSISTENCE_EVIDENCE}"
  {
    printf 'ALLOCATABLE_CPU=%s\n' \
      "$(ALLOCATABLE="${allocatable}" python3 -c 'import json,os; print(json.loads(os.environ["ALLOCATABLE"])["status"]["allocatable"]["cpu"])')"
    printf 'ALLOCATABLE_MEMORY=%s\n' \
      "$(ALLOCATABLE="${allocatable}" python3 -c 'import json,os; print(json.loads(os.environ["ALLOCATABLE"])["status"]["allocatable"]["memory"])')"
    printf 'DOCKER_CPU_NANO=%s\n' "${cpu_cap}"
    printf 'DOCKER_MEMORY_BYTES=%s\n' "${memory_cap}"
  } >>"${SPIKE_PERSISTENCE_EVIDENCE}"
  chmod 0600 "${SPIKE_PERSISTENCE_EVIDENCE}"
}

recreate_persistent_spike_worker() {
  remove_spike_worker_container
  start_spike_worker true \
    && reset_worker_state_for_rejoin "${SPIKE_WORKER_NAME}" \
    && delete_spike_node \
    && join_spike_worker \
    && wait_for "${WORKER_JOIN_TIMEOUT}" "recreated Konnectivity agent" \
      spike_konnectivity_agent_available \
    && wait_spike_network_addons \
    && wait_for "${TENANT_ADDON_TIMEOUT}" "recreated worker endpoint access" \
      worker_endpoint_accessible \
    && validate_spike_allocatable
}

spike_konnectivity_agent_available() {
  [[ -n "$(spike_konnectivity_agent_container_id)" ]]
}

reset_worker_state_for_rejoin() {
  local name="$1"
  docker exec "${name}" kubeadm reset --force \
    --cri-socket unix:///run/containerd/containerd.sock >/dev/null 2>&1 || true
  docker exec "${name}" systemctl stop kubelet containerd >/dev/null 2>&1 || true
  docker exec "${name}" rm -rf \
    /var/lib/kubelet \
    /var/lib/containerd \
    /etc/kubernetes \
    /etc/cni/net.d >/dev/null
  docker exec "${name}" systemctl start containerd >/dev/null
}

spike_konnectivity_agent_container_id() {
  tenant_kubectl spike -n kube-system get pods -o json 2>/dev/null \
    | python3 -c '
import json,sys
for pod in json.load(sys.stdin).get("items",[]):
    owners=pod.get("metadata",{}).get("ownerReferences",[])
    if not any(o.get("kind")=="DaemonSet" and o.get("name")=="konnectivity-agent" for o in owners):
        continue
    for status in pod.get("status",{}).get("containerStatuses",[]):
        if status.get("name")=="konnectivity-agent" and status.get("ready"):
            print(status.get("containerID",""))
            raise SystemExit
'
}

spike_konnectivity_agent_restarted() {
  local previous="$1"
  local current
  current="$(spike_konnectivity_agent_container_id)"
  [[ -n "${current}" && "${current}" != "${previous}" ]]
}

worker_name() {
  printf 'kamaji-%s-worker-%s\n' "$1" "$2"
}

worker_volume_name() {
  printf '%s-var-lib\n' "$(worker_name "$1" "$2")"
}

worker_ownership_file() {
  printf '%s/workers/%s.env\n' "$(tenant_runtime_dir "$1")" "$(worker_name "$1" "$2")"
}

final_worker_owned() {
  local tenant="$1"
  local name="$2"
  [[ "$(docker container inspect --format "{{index .Config.Labels \"${OWNERSHIP_LABEL}\"}}" \
      "${name}" 2>/dev/null || true)" == "${LAB_PREFIX}" ]] \
    && [[ "$(docker container inspect --format \
      '{{index .Config.Labels "kamaji.cnpg-vcluster.io/tenant"}}' \
      "${name}" 2>/dev/null || true)" == "${tenant}" ]] \
    && [[ "$(docker container inspect --format \
      '{{index .Config.Labels "kamaji.cnpg-vcluster.io/role"}}' \
      "${name}" 2>/dev/null || true)" == worker ]]
}

final_volume_owned() {
  local tenant="$1"
  local volume="$2"
  [[ "$(docker volume inspect --format "{{index .Labels \"${OWNERSHIP_LABEL}\"}}" \
      "${volume}" 2>/dev/null || true)" == "${LAB_PREFIX}" ]] \
    && [[ "$(docker volume inspect --format \
      '{{index .Labels "kamaji.cnpg-vcluster.io/tenant"}}' \
      "${volume}" 2>/dev/null || true)" == "${tenant}" ]] \
    && [[ "$(docker volume inspect --format \
      '{{index .Labels "kamaji.cnpg-vcluster.io/role"}}' \
      "${volume}" 2>/dev/null || true)" == worker-var-lib ]]
}

ensure_final_worker_volume() {
  local tenant="$1"
  local ordinal="$2"
  local volume
  volume="$(worker_volume_name "${tenant}" "${ordinal}")"
  if docker volume inspect "${volume}" >/dev/null 2>&1; then
    final_volume_owned "${tenant}" "${volume}" \
      || die "${tenant}.worker-ownership: refusing same-named unowned volume ${volume}"
  else
    docker volume create \
      --label "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
      --label "kamaji.cnpg-vcluster.io/tenant=${tenant}" \
      --label "kamaji.cnpg-vcluster.io/role=worker-var-lib" \
      "${volume}" >/dev/null
  fi
}

final_worker_current() {
  local tenant="$1"
  local ordinal="$2"
  local name volume expected_cpu expected_memory
  name="$(worker_name "${tenant}" "${ordinal}")"
  volume="$(worker_volume_name "${tenant}" "${ordinal}")"
  expected_cpu="$(WORKER_CPUS="${WORKER_CPUS}" python3 -c \
    'import os; print(int(float(os.environ["WORKER_CPUS"])*1_000_000_000))')"
  expected_memory="$((WORKER_MEMORY_MIB * 1024 * 1024))"
  final_worker_owned "${tenant}" "${name}" \
    && [[ "$(docker container inspect --format '{{.Config.Image}}' "${name}")" == "${KIND_NODE_IMAGE}" ]] \
    && [[ "$(docker container inspect --format '{{.Config.Hostname}}' "${name}")" == "${name}" ]] \
    && [[ "$(docker container inspect --format '{{.HostConfig.Privileged}}' "${name}")" == true ]] \
    && [[ "$(docker container inspect --format '{{.HostConfig.CgroupnsMode}}' "${name}")" == private ]] \
    && [[ "$(docker container inspect --format '{{.HostConfig.NanoCpus}}' "${name}")" == "${expected_cpu}" ]] \
    && [[ "$(docker container inspect --format '{{.HostConfig.Memory}}' "${name}")" == "${expected_memory}" ]] \
    && [[ "$(docker container inspect --format '{{with index .NetworkSettings.Networks "kind"}}{{.NetworkID}}{{end}}' "${name}")" != "" ]] \
    && docker container inspect --format '{{range .Mounts}}{{if and (eq .Destination "/var/lib") (eq .Name "'"${volume}"'")}}yes{{end}}{{end}}' \
      "${name}" | grep -Fxq yes
}

validate_final_worker_ownership_record() {
  local tenant="$1"
  local ordinal="$2"
  local name volume record recorded_name recorded_id recorded_volume
  local recorded_volume_id live_id live_volume_id
  name="$(worker_name "${tenant}" "${ordinal}")"
  volume="$(worker_volume_name "${tenant}" "${ordinal}")"
  record="$(worker_ownership_file "${tenant}" "${ordinal}")"
  [[ -f "${record}" ]] \
    || die "${tenant}.worker-ownership: ${name} lacks runtime ownership evidence"
  recorded_name="$(sed -n 's/^WORKER_NAME=//p' "${record}")"
  recorded_id="$(sed -n 's/^WORKER_ID=//p' "${record}")"
  recorded_volume="$(sed -n 's/^WORKER_VOLUME=//p' "${record}")"
  recorded_volume_id="$(sed -n 's/^WORKER_VOLUME_ID=//p' "${record}")"
  live_id="$(docker container inspect --format '{{.Id}}' "${name}")"
  live_volume_id="$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${volume}")"
  [[ "${recorded_name}" == "${name}" \
    && -n "${recorded_id}" && "${recorded_id}" == "${live_id}" \
    && "${recorded_volume}" == "${volume}" \
    && -n "${recorded_volume_id}" && "${recorded_volume_id}" == "${live_volume_id}" ]] \
    || die "${tenant}.worker-ownership: ${name} identity differs from its record"
}

validate_final_worker_volume_ownership_record() {
  local tenant="$1"
  local ordinal="$2"
  local name volume record recorded_name recorded_volume recorded_volume_id live_volume_id
  name="$(worker_name "${tenant}" "${ordinal}")"
  volume="$(worker_volume_name "${tenant}" "${ordinal}")"
  record="$(worker_ownership_file "${tenant}" "${ordinal}")"
  [[ -f "${record}" ]] \
    || die "${tenant}.worker-ownership: ${volume} lacks runtime ownership evidence"
  recorded_name="$(sed -n 's/^WORKER_NAME=//p' "${record}")"
  recorded_volume="$(sed -n 's/^WORKER_VOLUME=//p' "${record}")"
  recorded_volume_id="$(sed -n 's/^WORKER_VOLUME_ID=//p' "${record}")"
  live_volume_id="$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${volume}")"
  [[ "${recorded_name}" == "${name}" \
    && "${recorded_volume}" == "${volume}" \
    && -n "${recorded_volume_id}" && "${recorded_volume_id}" == "${live_volume_id}" ]] \
    || die "${tenant}.worker-ownership: ${volume} identity differs from its record"
}

record_final_worker_ownership() {
  local tenant="$1"
  local ordinal="$2"
  local name volume record
  name="$(worker_name "${tenant}" "${ordinal}")"
  volume="$(worker_volume_name "${tenant}" "${ordinal}")"
  record="$(worker_ownership_file "${tenant}" "${ordinal}")"
  {
    printf 'WORKER_NAME=%s\n' "${name}"
    printf 'WORKER_ID=%s\n' "$(docker container inspect --format '{{.Id}}' "${name}")"
    printf 'WORKER_VOLUME=%s\n' "${volume}"
    printf 'WORKER_VOLUME_ID=%s\n' \
      "$(docker volume inspect --format '{{.Name}}:{{.CreatedAt}}' "${volume}")"
  } | write_secret_file "${record}"
}

final_worker_systemd_ready() {
  local name="$1"
  local state
  [[ "$(docker container inspect --format '{{.State.Running}}' \
      "${name}" 2>/dev/null || true)" == true ]] \
    || return 1
  state="$(
    timeout "$(seconds_from_duration "${SYSTEMD_STATUS_TIMEOUT}")" \
      docker exec "${name}" systemctl is-system-running --wait \
      2>/dev/null || true
  )"
  grep -Eq 'running|degraded' <<<"${state}"
}

final_worker_substrate_ready() {
  local name="$1"
  final_worker_systemd_ready "${name}" \
    && docker exec "${name}" test -f /sys/fs/cgroup/cgroup.controllers \
    && docker exec "${name}" systemctl is-active containerd >/dev/null \
    && docker exec "${name}" test -S /run/containerd/containerd.sock \
    && docker exec "${name}" crictl info >/dev/null \
    && docker exec "${name}" iptables --version >/dev/null \
    && [[ -z "$(docker exec "${name}" swapon --show --noheadings 2>/dev/null)" ]]
}

sanitize_worker_evidence() {
  sed -E \
    -e 's/([a-z0-9]{6})\.[a-z0-9]{16}/REDACTED/g' \
    -e 's/([Pp]assword|[Tt]oken|[Ss]ecret|[Aa]uthorization)([=:][^[:space:]]+)/\1=REDACTED/g'
}

capture_final_worker_failure() {
  local tenant="$1"
  local ordinal="$2"
  local attempt="$3"
  local name state running evidence_file log_tail
  name="$(worker_name "${tenant}" "${ordinal}")"
  evidence_file="${RUNTIME_DIR}/logs/${name}-${attempt}.log"
  state="$(
    docker container inspect "${name}" \
      --format 'running={{.State.Running}},exit={{.State.ExitCode}},oom={{.State.OOMKilled}},error={{json .State.Error}}' \
      2>/dev/null || printf 'inspect-unavailable'
  )"
  running="$(docker container inspect "${name}" --format '{{.State.Running}}' 2>/dev/null || true)"
  {
    printf 'worker=%s\nattempt=%s\nstate=%s\n' "${name}" "${attempt}" "${state}"
    printf 'inspect_state='
    docker container inspect "${name}" --format '{{json .State}}' 2>/dev/null \
      || printf 'unavailable'
    printf '\nlogs:\n'
    docker logs --tail 30 "${name}" 2>&1 || true
    if [[ "${running}" == true ]]; then
      printf '\nruntime:\n'
      printf 'systemd=%s\n' "$(
        timeout "$(seconds_from_duration "${SYSTEMD_STATUS_TIMEOUT}")" \
          docker exec "${name}" systemctl is-system-running --wait 2>/dev/null \
          || printf not-ready
      )"
      printf 'containerd=%s\n' "$(
        docker exec "${name}" systemctl is-active containerd 2>/dev/null \
          || printf inactive
      )"
      printf 'socket=%s\n' "$(
        docker exec "${name}" test -S /run/containerd/containerd.sock 2>/dev/null \
          && printf present || printf absent
      )"
      printf 'iptables=%s\n' "$(
        docker exec "${name}" iptables --version >/dev/null 2>&1 \
          && printf present || printf absent
      )"
      printf 'swap_entries=%s\n' "$(
        docker exec "${name}" swapon --show --noheadings 2>/dev/null | wc -l
      )"
    else
      printf '\nruntime=not-observed-container-not-running\n'
    fi
  } | sanitize_worker_evidence | write_secret_file "${evidence_file}"
  log_tail="$(
    sed -n '/^logs:$/,$p' "${evidence_file}" | tail -10 \
      | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g'
  )"
  printf '%s,evidence=%s,log_tail=%s\n' "${state}" "${evidence_file}" "${log_tail:-none}"
}

start_final_worker_container() {
  local tenant="$1"
  local ordinal="$2"
  local name volume
  name="$(worker_name "${tenant}" "${ordinal}")"
  volume="$(worker_volume_name "${tenant}" "${ordinal}")"
  ensure_final_worker_volume "${tenant}" "${ordinal}"
  docker run --detach \
    --name "${name}" \
    --hostname "${name}" \
    --label "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    --label "kamaji.cnpg-vcluster.io/tenant=${tenant}" \
    --label "kamaji.cnpg-vcluster.io/role=worker" \
    --network kind \
    --privileged \
    --cgroupns=private \
    --security-opt seccomp=unconfined \
    --tmpfs /run \
    --tmpfs /run/lock \
    --sysctl net.ipv4.ip_forward=1 \
    --cpus "${WORKER_CPUS}" \
    --memory "${WORKER_MEMORY_MIB}m" \
    --volume /lib/modules:/lib/modules:ro \
    --mount "type=volume,src=${volume},dst=/var/lib" \
    "${KIND_NODE_IMAGE}" >/dev/null
  record_final_worker_ownership "${tenant}" "${ordinal}"
  wait_for "${WORKER_START_TIMEOUT}" "systemd in ${name}" \
    final_worker_systemd_ready "${name}" \
    || return 1
  docker exec "${name}" swapoff -a >/dev/null 2>&1 || true
  docker exec "${name}" modprobe br_netfilter >/dev/null 2>&1 || true
  docker exec "${name}" sysctl -w net.ipv4.ip_forward=1 >/dev/null
  wait_for "${WORKER_START_TIMEOUT}" "worker substrate in ${name}" \
    final_worker_substrate_ready "${name}"
}

start_final_worker_with_retry() {
  local tenant="$1"
  local ordinal="$2"
  local first_evidence second_evidence
  FINAL_WORKER_FAILURE_CODE=""
  FINAL_WORKER_FAILURE_EVIDENCE=""
  FINAL_WORKER_FAILURE_RECOGNIZED=false
  if start_final_worker_container "${tenant}" "${ordinal}"; then
    return 0
  fi
  FINAL_WORKER_FAILURE_CODE=worker-substrate
  first_evidence="$(capture_final_worker_failure "${tenant}" "${ordinal}" first-attempt)"
  remove_final_worker_container "${tenant}" "${ordinal}"
  sleep 2
  if start_final_worker_container "${tenant}" "${ordinal}"; then
    return 0
  fi
  second_evidence="$(capture_final_worker_failure "${tenant}" "${ordinal}" retry)"
  FINAL_WORKER_FAILURE_EVIDENCE="first_attempt=${first_evidence}; retry=${second_evidence}"
  if [[ "${first_evidence}" != *"inspect-unavailable"* \
    && "${second_evidence}" != *"inspect-unavailable"* ]]; then
    FINAL_WORKER_FAILURE_RECOGNIZED=true
  fi
  return 1
}

final_worker_node_ready() {
  local tenant="$1"
  local name="$2"
  [[ "$(tenant_kubectl "${tenant}" get node "${name}" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' \
    2>/dev/null || true)" == True ]]
}

final_worker_node_registered() {
  local tenant="$1"
  local name="$2"
  tenant_kubectl "${tenant}" get node "${name}" >/dev/null 2>&1
}

label_final_worker_node() {
  local tenant="$1"
  local name="$2"
  tenant_kubectl "${tenant}" label node "${name}" \
    "${OWNERSHIP_LABEL}=${LAB_PREFIX}" \
    "kamaji.cnpg-vcluster.io/tenant=${tenant}" \
    kamaji.cnpg-vcluster.io/role=worker \
    --overwrite >/dev/null
}

join_final_worker() {
  local tenant="$1"
  local ordinal="$2"
  local name runtime join_file evidence join_command token_id="" status
  name="$(worker_name "${tenant}" "${ordinal}")"
  runtime="$(tenant_runtime_dir "${tenant}")/join/${name}"
  join_file="${runtime}/join.sh"
  evidence="${runtime}/kubeadm.log"
  mkdir -p -m 0700 "${runtime}"

  docker exec "${name}" mkdir -p -m 0700 /var/lib/kamaji-final-join
  trap 'trap - RETURN INT TERM HUP; cleanup_final_worker_join_material "${tenant}" "${name}" "${runtime}" "${token_id}"' RETURN
  trap 'return 130' INT TERM HUP
  docker cp "$(tenant_kubeconfig "${tenant}")" \
    "${name}:/var/lib/kamaji-final-join/admin.conf" >/dev/null
  docker exec "${name}" chmod 0600 /var/lib/kamaji-final-join/admin.conf

  if ! join_command="$(docker exec "${name}" kubeadm \
    --kubeconfig=/var/lib/kamaji-final-join/admin.conf \
    token create --ttl "${KUBEADM_TOKEN_TTL}" --print-join-command 2>/dev/null)"; then
    return 1
  fi
  token_id="$(sed -E 's/.*--token ([^. ]+)\..*/\1/' <<<"${join_command}")"
  if [[ ! "${token_id}" =~ ^[a-z0-9]{6}$ ]]; then
    return 1
  fi

  {
    printf '#!/usr/bin/env bash\n'
    printf 'set -Eeuo pipefail\n'
    printf '%s' "${join_command}"
    printf ' --cri-socket unix:///run/containerd/containerd.sock'
    printf ' --node-name %q' "${name}"
    printf ' "$@"\n'
  } | write_secret_file "${join_file}"
  docker cp "${join_file}" "${name}:/var/lib/kamaji-final-join/join.sh" >/dev/null
  docker exec "${name}" chmod 0600 /var/lib/kamaji-final-join/join.sh

  set +e
  timeout "$(seconds_from_duration "${WORKER_JOIN_TIMEOUT}")" \
    docker exec "${name}" /bin/bash /var/lib/kamaji-final-join/join.sh \
    >"${evidence}" 2>&1
  status=$?
  set -e
  chmod 0600 "${evidence}"

  if [[ "${KAMAJI_TEST_FAIL_AFTER_FINAL_JOIN:-}" == "${name}" \
    && "${status}" -eq 0 ]]; then
    FINAL_WORKER_FAILURE_EVIDENCE="${name} injected ordinary failure after successful join"
    return 1
  fi
  if (( status != 0 )); then
    FINAL_WORKER_FAILURE_EVIDENCE="$(
      grep -E '^\s*\[ERROR |^error:|^To see the stack trace' "${evidence}" \
        | tail -20 \
        | sed -E \
          -e 's/([a-z0-9]{6})\.[a-z0-9]{16}/REDACTED/g' \
          -e 's/[[:space:]]+/ /g' \
        | paste -sd '|' - || true
    )"
  fi
  (( status == 0 )) || return 1
  wait_for "${WORKER_JOIN_TIMEOUT}" "${name} registration" \
    final_worker_node_registered "${tenant}" "${name}"
}

cleanup_final_worker_join_material() {
  local tenant="$1"
  local name="$2"
  local runtime="$3"
  local token_id="${4:-}"
  if docker container inspect "${name}" >/dev/null 2>&1 \
    && final_worker_owned "${tenant}" "${name}"; then
    if [[ -n "${token_id}" ]] \
      && docker exec "${name}" test -f /var/lib/kamaji-final-join/admin.conf; then
      docker exec "${name}" kubeadm \
        --kubeconfig=/var/lib/kamaji-final-join/admin.conf \
        token delete "${token_id}" >/dev/null 2>&1 || true
    fi
    docker exec "${name}" rm -rf /var/lib/kamaji-final-join \
      >/dev/null 2>&1 || true
  fi
  rm -rf "${runtime}"
}

cleanup_stale_final_worker_join_material() {
  local tenant="$1"
  local ordinal="$2"
  local name runtime token_id=""
  name="$(worker_name "${tenant}" "${ordinal}")"
  runtime="$(tenant_runtime_dir "${tenant}")/join/${name}"
  if docker exec "${name}" test -f /var/lib/kamaji-final-join/join.sh \
    >/dev/null 2>&1; then
    token_id="$(docker exec "${name}" sed -n -E \
      's/.*--token ([a-z0-9]{6})\.[a-z0-9]{16}.*/\1/p' \
      /var/lib/kamaji-final-join/join.sh 2>/dev/null || true)"
  fi
  cleanup_final_worker_join_material "${tenant}" "${name}" "${runtime}" "${token_id}"
}

remove_final_worker_container() {
  local tenant="$1"
  local ordinal="$2"
  local name
  name="$(worker_name "${tenant}" "${ordinal}")"
  if docker container inspect "${name}" >/dev/null 2>&1; then
    final_worker_owned "${tenant}" "${name}" \
      || die "${tenant}.worker-ownership: refusing deletion of unowned container ${name}"
    validate_final_worker_ownership_record "${tenant}" "${ordinal}"
    docker rm -f "${name}" >/dev/null
  fi
}

remove_final_worker_and_volume() {
  local tenant="$1"
  local ordinal="$2"
  local volume
  volume="$(worker_volume_name "${tenant}" "${ordinal}")"
  remove_final_worker_container "${tenant}" "${ordinal}"
  if docker volume inspect "${volume}" >/dev/null 2>&1; then
    final_volume_owned "${tenant}" "${volume}" \
      || die "${tenant}.worker-ownership: refusing deletion of unowned volume ${volume}"
    validate_final_worker_volume_ownership_record "${tenant}" "${ordinal}"
    docker volume rm "${volume}" >/dev/null
  fi
  rm -f "$(worker_ownership_file "${tenant}" "${ordinal}")"
}

reconcile_final_worker() {
  local tenant="$1"
  local ordinal="$2"
  local name state had_credentials=false
  name="$(worker_name "${tenant}" "${ordinal}")"
  FINAL_WORKER_FAILURE_CODE=""
  FINAL_WORKER_FAILURE_EVIDENCE=""
  FINAL_WORKER_FAILURE_RECOGNIZED=false
  if docker container inspect "${name}" >/dev/null 2>&1; then
    final_worker_owned "${tenant}" "${name}" \
      || die "${tenant}.worker-ownership: refusing same-named unowned container ${name}"
    validate_final_worker_ownership_record "${tenant}" "${ordinal}"
    if ! final_worker_current "${tenant}" "${ordinal}"; then
      tenant_kubectl "${tenant}" delete node "${name}" \
        --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" \
        >/dev/null 2>&1 || true
      remove_final_worker_container "${tenant}" "${ordinal}"
      if ! start_final_worker_with_retry "${tenant}" "${ordinal}"; then
        return 1
      fi
    else
      state="$(docker container inspect --format '{{.State.Running}}' "${name}")"
      if [[ "${state}" != true ]]; then
        docker start "${name}" >/dev/null
      fi
      wait_for "${WORKER_START_TIMEOUT}" "systemd in ${name}" \
        final_worker_systemd_ready "${name}" || {
          FINAL_WORKER_FAILURE_CODE=worker-substrate
          FINAL_WORKER_FAILURE_EVIDENCE="$(
            capture_final_worker_failure "${tenant}" "${ordinal}" restart
          )"
          return 1
        }
      record_final_worker_ownership "${tenant}" "${ordinal}"
      if docker exec "${name}" test -f /etc/kubernetes/kubelet.conf; then
        had_credentials=true
      fi
    fi
  else
    tenant_kubectl "${tenant}" delete node "${name}" \
      --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" \
      >/dev/null 2>&1 || true
    if ! start_final_worker_with_retry "${tenant}" "${ordinal}"; then
      return 1
    fi
  fi

  tenant_kube_proxy_conntrack_is_zero "${tenant}" || {
    FINAL_WORKER_FAILURE_CODE=cni-konnectivity
    FINAL_WORKER_FAILURE_EVIDENCE="kube-proxy conntrack.maxPerCore is not 0"
    return 1
  }
  cleanup_stale_final_worker_join_material "${tenant}" "${ordinal}"
  if ! final_worker_node_ready "${tenant}" "${name}"; then
    if [[ "${had_credentials}" == true ]] \
      && wait_for "${WORKER_READY_GRACE_TIMEOUT}" "${name} Ready grace" \
        final_worker_node_ready "${tenant}" "${name}"; then
      label_final_worker_node "${tenant}" "${name}"
      return
    fi
    FINAL_WORKER_FAILURE_CODE=kubeadm-bootstrap
    if docker exec "${name}" test -f /etc/kubernetes/kubelet.conf \
      || docker exec "${name}" test -f /var/lib/kubelet/config.yaml; then
      if ! reset_worker_state_for_rejoin "${name}"; then
        FINAL_WORKER_FAILURE_EVIDENCE="${name} kubeadm state reset failed"
        return 1
      fi
    fi
    tenant_kubectl "${tenant}" delete node "${name}" \
      --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" \
      >/dev/null 2>&1 || true
    if ! join_final_worker "${tenant}" "${ordinal}"; then
      [[ -n "${FINAL_WORKER_FAILURE_EVIDENCE}" ]] \
        || FINAL_WORKER_FAILURE_EVIDENCE="${name} kubeadm join failed without a parsed error summary"
      if [[ "${FINAL_WORKER_FAILURE_EVIDENCE}" == *"[ERROR "* ]]; then
        FINAL_WORKER_FAILURE_RECOGNIZED=true
      fi
      return 1
    fi
  fi
  if ! label_final_worker_node "${tenant}" "${name}"; then
    FINAL_WORKER_FAILURE_CODE=kubeadm-bootstrap
    FINAL_WORKER_FAILURE_EVIDENCE="${name} registered but ownership labels did not reconcile"
    return 1
  fi
}

reconcile_tenant_workers() {
  local tenant="$1"
  local ordinal
  if [[ "${KAMAJI_TEST_INJECT_RECOGNIZED_WORKER_FAILURE:-}" == "${tenant}" ]]; then
    FINAL_WORKER_FAILURE_CODE=worker-substrate
    FINAL_WORKER_FAILURE_EVIDENCE="${tenant} injected recognized worker blocker"
    FINAL_WORKER_FAILURE_RECOGNIZED=true
    return 1
  fi
  for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
    log "reconciling $(worker_name "${tenant}" "${ordinal}")"
    reconcile_final_worker "${tenant}" "${ordinal}" || return 1
  done
}

remove_tenant_workers() {
  local tenant="$1"
  local ordinal name
  for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
    name="$(worker_name "${tenant}" "${ordinal}")"
    if [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
      && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
      tenant_kubectl "${tenant}" delete node "${name}" \
        --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" \
        >/dev/null 2>&1 || true
    fi
    remove_final_worker_and_volume "${tenant}" "${ordinal}"
  done
}

validate_exact_tenant_node_set() {
  local tenant="$1"
  local expected actual names expected_names ordinal name
  expected="${WORKERS_PER_TENANT}"
  expected_names=""
  for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
    name="$(worker_name "${tenant}" "${ordinal}")"
    expected_names+="${name}"$'\n'
  done
  expected_names="$(grep . <<<"${expected_names}" | sort)"
  names="$(tenant_kubectl "${tenant}" get nodes \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)"
  actual="$(grep -c . <<<"${names}" || true)"
  [[ "${actual}" -eq "${expected}" && "${names}" == "${expected_names}" ]] \
    || die "${tenant}.workers: complete Node set differs from the exact expected workers"
  for name in ${expected_names}; do
    tenant_kubectl "${tenant}" get node "${name}" -o json \
      | OWNERSHIP_LABEL="${OWNERSHIP_LABEL}" \
        LAB_PREFIX="${LAB_PREFIX}" \
        EXPECTED_TENANT="${tenant}" \
        python3 -c '
import json,os,sys
labels=json.load(sys.stdin).get("metadata",{}).get("labels",{})
assert labels.get(os.environ["OWNERSHIP_LABEL"]) == os.environ["LAB_PREFIX"]
assert labels.get("kamaji.cnpg-vcluster.io/tenant") == os.environ["EXPECTED_TENANT"]
assert labels.get("kamaji.cnpg-vcluster.io/role") == "worker"
' || die "${tenant}.workers: ${name} lacks exact ownership labels"
    final_worker_node_ready "${tenant}" "${name}" \
      || die "${tenant}.workers: ${name} is not Ready"
  done
  printf '%s\n' "${names}"
}

validate_disjoint_worker_sets() {
  local tenant names all_names
  all_names=""
  for tenant in ${TENANT_NAMES}; do
    names="$(validate_exact_tenant_node_set "${tenant}")"
    all_names+="${names}"$'\n'
  done
  [[ "$(grep -c . <<<"${all_names}")" -eq "$((WORKERS_PER_TENANT * 2))" \
    && "$(grep . <<<"${all_names}" | sort -u | wc -l)" -eq "$((WORKERS_PER_TENANT * 2))" ]] \
    || die "tenant.workers: node sets are not disjoint"
}

tenant_workers_ready() {
  local tenant="$1"
  local ordinal name
  for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
    name="$(worker_name "${tenant}" "${ordinal}")"
    final_worker_node_ready "${tenant}" "${name}" || return 1
  done
}

validate_final_worker_request_capacity() {
  local tenant="$1"
  local ordinal name cpu_cap memory_cap
  for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
    name="$(worker_name "${tenant}" "${ordinal}")"
    cpu_cap="$(docker container inspect --format '{{.HostConfig.NanoCpus}}' "${name}")"
    memory_cap="$(docker container inspect --format '{{.HostConfig.Memory}}' "${name}")"
    tenant_kubectl "${tenant}" get pods --all-namespaces \
        --field-selector "spec.nodeName=${name}" -o json \
      | CPU_CAP="${cpu_cap}" MEMORY_CAP="${memory_cap}" \
        python3 "${WORKERS_LIB_DIR}/effective_requests.py" >/dev/null
    done
}
