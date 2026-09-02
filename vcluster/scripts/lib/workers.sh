#!/usr/bin/env bash

ensure_worker_image() {
  log "building private worker image ${WORKER_IMAGE}"
  docker build --quiet \
    --build-arg "WORKER_BASE_IMAGE=${WORKER_BASE_IMAGE}" \
    --tag "${WORKER_IMAGE}" \
    --file "${REPO_ROOT}/config/worker/Dockerfile" \
    "${REPO_ROOT}/config/worker"
}

kind_docker_network() {
  docker inspect "${KIND_CLUSTER_NAME}-control-plane" \
    --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' \
    | head -1
}

worker_container_owned() {
  local tenant="$1"
  local name="$2"
  [[ "$(docker inspect -f '{{index .Config.Labels "cnpg-vcluster.lab"}}' "${name}")" == "${LAB_PREFIX}" ]] \
    && [[ "$(docker inspect -f '{{index .Config.Labels "cnpg-vcluster.tenant"}}' "${name}")" == "${tenant}" ]] \
    && [[ "$(docker inspect -f '{{index .Config.Labels "cnpg-vcluster.role"}}' "${name}")" == private-worker ]]
}

worker_container_current() {
  local name="$1"
  local network="$2"
  local expected_image expected_memory expected_cpus
  expected_image="$(docker image inspect -f '{{.Id}}' "${WORKER_IMAGE}")"
  expected_memory=$(( ${WORKER_MEMORY%g} * 1024 * 1024 * 1024 ))
  expected_cpus=$(( WORKER_CPUS * 1000000000 ))

  [[ "$(docker inspect -f '{{.Image}}' "${name}")" == "${expected_image}" ]] \
    && [[ "$(docker inspect -f '{{.Config.Hostname}}' "${name}")" == "${name}" ]] \
    && [[ "$(docker inspect -f '{{.HostConfig.Privileged}}' "${name}")" == true ]] \
    && [[ "$(docker inspect -f '{{.HostConfig.CgroupnsMode}}' "${name}")" == private ]] \
    && [[ "$(docker inspect -f '{{.HostConfig.Memory}}' "${name}")" == "${expected_memory}" ]] \
    && [[ "$(docker inspect -f '{{.HostConfig.NanoCpus}}' "${name}")" == "${expected_cpus}" ]] \
    && docker inspect -f '{{json .NetworkSettings.Networks}}' "${name}" | grep -Fq "\"${network}\"" \
    && [[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib"}}{{.Name}}{{end}}{{end}}' "${name}")" == "${name}-var-lib" ]]
}

worker_volume_owned() {
  local tenant="$1"
  local volume="$2"
  [[ "$(docker volume inspect -f '{{index .Labels "cnpg-vcluster.lab"}}' "${volume}")" == "${LAB_PREFIX}" ]] \
    && [[ "$(docker volume inspect -f '{{index .Labels "cnpg-vcluster.tenant"}}' "${volume}")" == "${tenant}" ]]
}

ensure_worker_container() {
  local tenant="$1"
  local index="$2"
  local name
  local network
  name="$(worker_name "${tenant}" "${index}")"
  network="$(kind_docker_network)"
  [[ -n "${network}" ]] || die "could not determine kind Docker network"

  if docker container inspect "${name}" >/dev/null 2>&1; then
    worker_container_owned "${tenant}" "${name}" \
      || die "refusing to adopt same-named unowned container: ${name}"
    if worker_container_current "${name}" "${network}"; then
      if [[ "$(docker inspect -f '{{.State.Running}}' "${name}")" != true ]]; then
        docker start "${name}" >/dev/null
      fi
      return
    fi
    log "recreating stale owned worker ${name}"
    docker rm -f "${name}" >/dev/null
  fi

  log "starting private worker ${name}"
  if docker volume inspect "${name}-var-lib" >/dev/null 2>&1; then
    worker_volume_owned "${tenant}" "${name}-var-lib" \
      || die "refusing to adopt same-named unowned volume: ${name}-var-lib"
  else
    docker volume create \
      --label "cnpg-vcluster.lab=${LAB_PREFIX}" \
      --label "cnpg-vcluster.tenant=${tenant}" \
      "${name}-var-lib" >/dev/null
  fi

  docker run -d \
    --name "${name}" \
    --hostname "${name}" \
    --label "cnpg-vcluster.lab=${LAB_PREFIX}" \
    --label "cnpg-vcluster.tenant=${tenant}" \
    --label "cnpg-vcluster.role=private-worker" \
    --network "${network}" \
    --privileged \
    --cgroupns=private \
    --security-opt seccomp=unconfined \
    --tmpfs /run \
    --tmpfs /run/lock \
    --sysctl net.ipv4.ip_forward=1 \
    --cpus "${WORKER_CPUS}" \
    --memory "${WORKER_MEMORY}" \
    --mount "type=volume,src=${name}-var-lib,dst=/var/lib" \
    "${WORKER_IMAGE}" >/dev/null
}

worker_systemd_ready() {
  local name="$1"
  local state
  state="$(docker exec "${name}" systemctl is-system-running --wait 2>/dev/null || true)"
  grep -Eq 'running|degraded' <<<"${state}"
}

ensure_all_workers() {
  ensure_worker_image
  local tenant index name
  for tenant in ${TENANT_NAMES}; do
    for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
      ensure_worker_container "${tenant}" "${index}"
      name="$(worker_name "${tenant}" "${index}")"
      retry_for "${WORKER_BOOT_TIMEOUT}" "systemd in ${name}" worker_systemd_ready "${name}"
    done
  done
}

platform_reachable_from_worker() {
  local name="$1"
  local url
  url="$(platform_url)"
  local ca_args=()
  if [[ -n "${PLATFORM_CA_FILE:-}" ]]; then
    docker cp "${PLATFORM_CA_FILE}" "${name}:/root/platform-ca.crt" >/dev/null
    ca_args=(--cacert /root/platform-ca.crt)
  fi
  docker exec "${name}" curl -fsS --max-time 20 \
    "${ca_args[@]}" "${url}/version" >/dev/null 2>&1
}

worker_prerequisites_ready() {
  local name="$1"
  worker_systemd_ready "${name}" \
    && docker exec "${name}" test -f /sys/fs/cgroup/cgroup.controllers \
    && docker exec "${name}" iptables --version >/dev/null \
    && [[ -z "$(docker exec "${name}" swapon --show --noheadings 2>/dev/null)" ]]
}

worker_services_ready() {
  local name="$1"
  docker exec "${name}" systemctl is-active \
    containerd kubelet vcluster-vpn >/dev/null 2>&1
}

workers_reach_each_other() {
  local source="$1"
  local target="$2"
  docker exec "${source}" ping -c 1 -W 5 "${target}" >/dev/null 2>&1
}

worker_substrate_failure() {
  local tenant="$1"
  shift
  "${REPO_ROOT}/scripts/diagnose.sh" "${tenant}" >&2 || true
  record_blocker "private-worker-substrate-unsupported" "$*"
  die "$*"
}

ensure_worker_connectivity() {
  local tenant index peer_index name peer
  for tenant in ${TENANT_NAMES}; do
    for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
      name="$(worker_name "${tenant}" "${index}")"
      worker_prerequisites_ready "${name}" \
        || worker_substrate_failure "${tenant}" \
          "Worker prerequisites are not satisfied in experimental container ${name}."
      wait_for "${WORKER_REACH_TIMEOUT}" "Platform reachability from ${name}" \
        platform_reachable_from_worker "${name}" \
        || {
          record_blocker "platform-endpoint-unavailable" \
            "The Platform URL is not reachable from private worker ${name}."
          die "Platform endpoint is not reachable from ${name}"
        }
      for peer_index in $(seq 1 "${WORKERS_PER_TENANT}"); do
        [[ "${peer_index}" == "${index}" ]] && continue
        peer="$(worker_name "${tenant}" "${peer_index}")"
        wait_for "${WORKER_REACH_TIMEOUT}" "${name} to ${peer} connectivity" \
          workers_reach_each_other "${name}" "${peer}" \
          || worker_substrate_failure "${tenant}" \
            "Experimental worker ${name} cannot directly reach ${peer}."
      done
    done
  done
}
