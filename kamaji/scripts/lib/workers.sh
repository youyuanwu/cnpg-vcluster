#!/usr/bin/env bash

set -Eeuo pipefail
WORKERS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${WORKERS_LIB_DIR}/common.sh"

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
      --ignore-not-found --wait=true >/dev/null
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
  trap 'delete_bootstrap_token "${token_id}"; remove_worker_join_material' RETURN

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
  ALLOCATABLE="${allocatable}" CPU_CAP="${cpu_cap}" MEMORY_CAP="${memory_cap}" \
    python3 -c '
import json, os, re
a=json.loads(os.environ["ALLOCATABLE"])["status"]["allocatable"]
def cpu(v):
    return int(v[:-1])*1000000 if v.endswith("m") else int(v)*1000000000
def memory(v):
    units={"Ki":1024,"Mi":1024**2,"Gi":1024**3}
    m=re.fullmatch(r"([0-9]+)(Ki|Mi|Gi)?",v)
    return int(m.group(1))*units.get(m.group(2),1)
assert cpu(a["cpu"]) <= int(os.environ["CPU_CAP"])
assert memory(a["memory"]) <= int(os.environ["MEMORY_CAP"])
print("ALLOCATABLE_CPU="+a["cpu"])
print("ALLOCATABLE_MEMORY="+a["memory"])
' | write_secret_file "${SPIKE_PERSISTENCE_EVIDENCE}"
}

recreate_persistent_spike_worker() {
  remove_spike_worker_container
  start_spike_worker true \
    && wait_for "${WORKER_JOIN_TIMEOUT}" "recreated persistent worker readiness" \
      spike_node_ready \
    && validate_spike_allocatable
}
