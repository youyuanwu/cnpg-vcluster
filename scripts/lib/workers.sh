#!/usr/bin/env bash

ensure_worker_image() {
  if docker image inspect "${WORKER_IMAGE}" >/dev/null 2>&1; then
    return
  fi
  log "building private worker image ${WORKER_IMAGE}"
  docker build \
    --build-arg "WORKER_BASE_IMAGE=${WORKER_BASE_IMAGE}" \
    --tag "${WORKER_IMAGE}" \
    --file "${REPO_ROOT}/config/worker/Dockerfile" \
    "${REPO_ROOT}/config/worker"
}

ensure_worker_container() {
  local tenant="$1"
  local index="$2"
  local name
  name="$(worker_name "${tenant}" "${index}")"

  if docker container inspect "${name}" >/dev/null 2>&1; then
    if [[ "$(docker inspect -f '{{.State.Running}}' "${name}")" != true ]]; then
      docker start "${name}" >/dev/null
    fi
    return
  fi

  log "starting private worker ${name}"
  docker volume create \
    --label "cnpg-vcluster.lab=${LAB_PREFIX}" \
    --label "cnpg-vcluster.tenant=${tenant}" \
    "${name}-var-lib" >/dev/null

  docker run -d \
    --name "${name}" \
    --hostname "${name}" \
    --label "cnpg-vcluster.lab=${LAB_PREFIX}" \
    --label "cnpg-vcluster.tenant=${tenant}" \
    --label "cnpg-vcluster.role=private-worker" \
    --network kind \
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
      retry_for 2m "systemd in ${name}" worker_systemd_ready "${name}"
    done
  done
}

platform_reachable_from_worker() {
  local name="$1"
  local url
  url="$(platform_url)"
  docker exec "${name}" curl -kfsS --max-time 20 "${url}" >/dev/null 2>&1
}
