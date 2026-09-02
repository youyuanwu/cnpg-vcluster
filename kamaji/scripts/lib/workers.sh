#!/usr/bin/env bash

set -Eeuo pipefail
WORKERS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${WORKERS_LIB_DIR}/common.sh"

FINAL_WORKER_FAILURE_EVIDENCE=""

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
  {
    printf 'WORKER_NAME=%s\n' "${SPIKE_WORKER_NAME}"
    printf 'WORKER_ID=%s\n' "$(docker container inspect --format '{{.Id}}' "${SPIKE_WORKER_NAME}")"
    printf 'WORKER_VOLUME=%s\n' "$1"
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
  state="$(docker exec "${SPIKE_WORKER_NAME}" systemctl is-system-running --wait \
    2>/dev/null || true)"
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
  rm -f "${SPIKE_WORKER_OWNERSHIP_FILE}"
}

remove_spike_worker_and_volume() {
  remove_spike_worker_container
  if docker volume inspect "${SPIKE_VOLUME_NAME}" >/dev/null 2>&1; then
    spike_volume_owned \
      || die "spike.worker-ownership: refusing deletion of unowned volume ${SPIKE_VOLUME_NAME}"
    docker volume rm "${SPIKE_VOLUME_NAME}" >/dev/null
  fi
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
  | ALLOCATABLE="${allocatable}" CPU_CAP="${cpu_cap}" MEMORY_CAP="${memory_cap}" \
    python3 -c '
import json, os, re
a=json.loads(os.environ["ALLOCATABLE"])["status"]["allocatable"]
pods=json.load(__import__("sys").stdin).get("items",[])
def cpu(v):
    if not v: return 0
    if v.endswith("n"): return int(v[:-1])
    if v.endswith("u"): return int(v[:-1])*1000
    if v.endswith("m"): return int(v[:-1])*1000000
    return int(v)*1000000000
def memory(v):
    if not v: return 0
    units={"Ki":1024,"Mi":1024**2,"Gi":1024**3,"K":1000,"M":1000**2,"G":1000**3}
    m=re.fullmatch(r"([0-9]+)([A-Za-z]+)?",v)
    return int(m.group(1))*units.get(m.group(2),1)
cpu_requested=sum(cpu(c.get("resources",{}).get("requests",{}).get("cpu","0"))
                  for p in pods for c in p.get("spec",{}).get("containers",[]))
memory_requested=sum(memory(c.get("resources",{}).get("requests",{}).get("memory","0"))
                     for p in pods for c in p.get("spec",{}).get("containers",[]))
assert cpu_requested <= int(os.environ["CPU_CAP"])
assert memory_requested <= int(os.environ["MEMORY_CAP"])
print("ALLOCATABLE_CPU="+a["cpu"])
print("ALLOCATABLE_MEMORY="+a["memory"])
print("REQUESTED_CPU_NANO="+str(cpu_requested))
print("REQUESTED_MEMORY_BYTES="+str(memory_requested))
print("DOCKER_CPU_NANO="+os.environ["CPU_CAP"])
print("DOCKER_MEMORY_BYTES="+os.environ["MEMORY_CAP"])
' | write_secret_file "${SPIKE_PERSISTENCE_EVIDENCE}"
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
  local name record recorded_id live_id
  name="$(worker_name "${tenant}" "${ordinal}")"
  record="$(worker_ownership_file "${tenant}" "${ordinal}")"
  [[ -f "${record}" ]] \
    || die "${tenant}.worker-ownership: ${name} lacks runtime ownership evidence"
  recorded_id="$(sed -n 's/^WORKER_ID=//p' "${record}")"
  live_id="$(docker container inspect --format '{{.Id}}' "${name}")"
  [[ -n "${recorded_id}" && "${recorded_id}" == "${live_id}" ]] \
    || die "${tenant}.worker-ownership: ${name} identity differs from its record"
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
  } | write_secret_file "${record}"
}

final_worker_systemd_ready() {
  local name="$1"
  local state
  state="$(docker exec "${name}" systemctl is-system-running --wait 2>/dev/null || true)"
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

final_worker_substrate_failure_summary() {
  local name="$1"
  local state
  state="$(docker container inspect "${name}" \
    --format 'running={{.State.Running}},exit={{.State.ExitCode}},oom={{.State.OOMKilled}},error={{.State.Error}}' \
    2>/dev/null || printf inspect-unavailable)"
  printf '%s,systemd=%s,containerd=%s,socket=%s,iptables=%s,swap=%s' \
    "${state}" \
    "$(docker exec "${name}" systemctl is-system-running 2>/dev/null || printf unavailable)" \
    "$(docker exec "${name}" systemctl is-active containerd 2>/dev/null || printf inactive)" \
    "$(docker exec "${name}" test -S /run/containerd/containerd.sock 2>/dev/null && printf present || printf absent)" \
    "$(docker exec "${name}" iptables --version >/dev/null 2>&1 && printf present || printf absent)" \
    "$(docker exec "${name}" swapon --show --noheadings 2>/dev/null | wc -l)"
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
  if start_final_worker_container "${tenant}" "${ordinal}"; then
    return 0
  fi
  FINAL_WORKER_FAILURE_EVIDENCE="$(
    final_worker_substrate_failure_summary "$(worker_name "${tenant}" "${ordinal}")"
  )"
  remove_final_worker_container "${tenant}" "${ordinal}"
  sleep 2
  start_final_worker_container "${tenant}" "${ordinal}"
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
  local name runtime join_file evidence join_command token_id status
  name="$(worker_name "${tenant}" "${ordinal}")"
  runtime="$(tenant_runtime_dir "${tenant}")/join/${name}"
  join_file="${runtime}/join.sh"
  evidence="${runtime}/kubeadm.log"
  mkdir -p -m 0700 "${runtime}"

  docker exec "${name}" mkdir -p -m 0700 /var/lib/kamaji-final-join
  docker cp "$(tenant_kubeconfig "${tenant}")" \
    "${name}:/var/lib/kamaji-final-join/admin.conf" >/dev/null
  docker exec "${name}" chmod 0600 /var/lib/kamaji-final-join/admin.conf

  if ! join_command="$(docker exec "${name}" kubeadm \
    --kubeconfig=/var/lib/kamaji-final-join/admin.conf \
    token create --ttl "${KUBEADM_TOKEN_TTL}" --print-join-command 2>/dev/null)"; then
    docker exec "${name}" rm -rf /var/lib/kamaji-final-join >/dev/null 2>&1 || true
    rm -rf "${runtime}"
    return 1
  fi
  token_id="$(sed -E 's/.*--token ([^. ]+)\..*/\1/' <<<"${join_command}")"
  if [[ ! "${token_id}" =~ ^[a-z0-9]{6}$ ]]; then
    docker exec "${name}" rm -rf /var/lib/kamaji-final-join >/dev/null 2>&1 || true
    rm -rf "${runtime}"
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

  docker exec "${name}" kubeadm \
    --kubeconfig=/var/lib/kamaji-final-join/admin.conf \
    token delete "${token_id}" >/dev/null 2>&1 || true
  docker exec "${name}" rm -rf /var/lib/kamaji-final-join >/dev/null 2>&1 || true
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
  rm -rf "${runtime}"
  (( status == 0 )) || return 1
  wait_for "${WORKER_JOIN_TIMEOUT}" "${name} registration" \
    final_worker_node_registered "${tenant}" "${name}"
}

remove_final_worker_container() {
  local tenant="$1"
  local ordinal="$2"
  local name
  name="$(worker_name "${tenant}" "${ordinal}")"
  if docker container inspect "${name}" >/dev/null 2>&1; then
    final_worker_owned "${tenant}" "${name}" \
      || die "${tenant}.worker-ownership: refusing deletion of unowned container ${name}"
    docker rm -f "${name}" >/dev/null
  fi
  rm -f "$(worker_ownership_file "${tenant}" "${ordinal}")"
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
    docker volume rm "${volume}" >/dev/null
  fi
}

reconcile_final_worker() {
  local tenant="$1"
  local ordinal="$2"
  local name state
  name="$(worker_name "${tenant}" "${ordinal}")"
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
        FINAL_WORKER_FAILURE_EVIDENCE="$(final_worker_substrate_failure_summary "${name}")"
        return 1
      fi
    else
      state="$(docker container inspect --format '{{.State.Running}}' "${name}")"
      if [[ "${state}" != true ]]; then
        docker start "${name}" >/dev/null
      fi
      wait_for "${WORKER_START_TIMEOUT}" "systemd in ${name}" \
        final_worker_systemd_ready "${name}" || {
          FINAL_WORKER_FAILURE_EVIDENCE="$(final_worker_substrate_failure_summary "${name}")"
          return 1
        }
      record_final_worker_ownership "${tenant}" "${ordinal}"
    fi
  else
    tenant_kubectl "${tenant}" delete node "${name}" \
      --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" \
      >/dev/null 2>&1 || true
    if ! start_final_worker_with_retry "${tenant}" "${ordinal}"; then
      FINAL_WORKER_FAILURE_EVIDENCE="$(final_worker_substrate_failure_summary "${name}")"
      return 1
    fi
  fi

  tenant_kube_proxy_conntrack_is_zero "${tenant}" || {
    FINAL_WORKER_FAILURE_EVIDENCE="kube-proxy conntrack.maxPerCore is not 0"
    return 1
  }
  if ! final_worker_node_ready "${tenant}" "${name}"; then
    if docker exec "${name}" test -f /etc/kubernetes/kubelet.conf \
      || docker exec "${name}" test -f /var/lib/kubelet/config.yaml; then
      reset_worker_state_for_rejoin "${name}" || return 1
    fi
    tenant_kubectl "${tenant}" delete node "${name}" \
      --ignore-not-found --wait=true --timeout="${WORKER_JOIN_TIMEOUT}" \
      >/dev/null 2>&1 || true
    join_final_worker "${tenant}" "${ordinal}" || return 1
  fi
  label_final_worker_node "${tenant}" "${name}"
}

reconcile_tenant_workers() {
  local tenant="$1"
  local ordinal
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

validate_disjoint_worker_sets() {
  local tenant expected actual names all_names
  expected="${WORKERS_PER_TENANT}"
  all_names=""
  for tenant in ${TENANT_NAMES}; do
    names="$(tenant_kubectl "${tenant}" get nodes \
      -l "${OWNERSHIP_LABEL}=${LAB_PREFIX},kamaji.cnpg-vcluster.io/tenant=${tenant}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)"
    actual="$(grep -c . <<<"${names}" || true)"
    [[ "${actual}" -eq "${expected}" ]] \
      || die "${tenant}.workers: expected ${expected} owned nodes, found ${actual}"
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
    | CPU_CAP="${cpu_cap}" MEMORY_CAP="${memory_cap}" python3 -c '
import json, os, re
pods=json.load(__import__("sys").stdin).get("items",[])
def cpu(value):
    if not value: return 0
    if value.endswith("n"): return int(value[:-1])
    if value.endswith("u"): return int(value[:-1])*1000
    if value.endswith("m"): return int(value[:-1])*1000000
    return int(value)*1000000000
def memory(value):
    if not value: return 0
    units={"Ki":1024,"Mi":1024**2,"Gi":1024**3,"K":1000,"M":1000**2,"G":1000**3}
    match=re.fullmatch(r"([0-9]+)([A-Za-z]+)?", value)
    return int(match.group(1))*units.get(match.group(2),1)
requested_cpu=sum(cpu(c.get("resources",{}).get("requests",{}).get("cpu","0"))
                  for p in pods for c in p.get("spec",{}).get("containers",[]))
requested_memory=sum(memory(c.get("resources",{}).get("requests",{}).get("memory","0"))
                     for p in pods for c in p.get("spec",{}).get("containers",[]))
assert requested_cpu <= int(os.environ["CPU_CAP"])
assert requested_memory <= int(os.environ["MEMORY_CAP"])
'
  done
}
