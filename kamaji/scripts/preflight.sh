#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

gib=$((1024 * 1024 * 1024))

capacity_failure() {
  local code="$1"
  shift
  printf '[kamaji-lab] ERROR: capacity.%s: %s\n' "${code}" "$*" >&2
  exit "${EXIT_ERROR}"
}

require_prepared_input() {
  local expected="$1"
  local path="$2"
  [[ -f "${path}" ]] || die "prepared input missing: ${path}; run just tools"
  sha256_check "${expected}" "${path}"
}

verify_remote_image() {
  local reference="$1"
  local expected="${reference##*@}"
  local output
  output="$(
    timeout "$(seconds_from_duration "${PREFLIGHT_ENDPOINT_TIMEOUT}")" \
      docker buildx imagetools inspect "${reference}" 2>&1
  )" || die "required image endpoint unavailable for ${reference}: ${output}"
  [[ "$(awk '$1 == "Digest:" {print $2; exit}' <<<"${output}")" == "${expected}" ]] \
    || die "remote image digest did not match ${reference}"
}

check_network_non_overlap() {
  local docker_subnets
  docker_subnets="$(
    docker network ls -q \
      | while IFS= read -r network_id; do
          [[ -n "${network_id}" ]] || continue
          docker network inspect \
            --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' \
            "${network_id}"
        done
  )"
  CONFIGURED_CIDRS="${MANAGEMENT_POD_CIDR} ${MANAGEMENT_SERVICE_CIDR} ${TENANT_A_POD_CIDR} ${TENANT_A_SERVICE_CIDR} ${TENANT_B_POD_CIDR} ${TENANT_B_SERVICE_CIDR}" \
  DOCKER_SUBNETS="${docker_subnets}" \
  python3 -c '
import ipaddress
import os

configured = [ipaddress.ip_network(value) for value in os.environ["CONFIGURED_CIDRS"].split()]
docker = [ipaddress.ip_network(value) for value in os.environ["DOCKER_SUBNETS"].split() if value]
for index, left in enumerate(configured):
    for right in configured[index + 1:]:
        if left.overlaps(right):
            raise SystemExit(f"configured networks overlap: {left} and {right}")
    for right in docker:
        if left.version == right.version and left.overlaps(right):
            raise SystemExit(f"configured network {left} overlaps Docker network {right}")
'
}

require_command curl
require_command df
require_command docker
require_command python3
require_command sha256sum
require_command timeout
require_exact_just
docker buildx version >/dev/null 2>&1 \
  || die "required Docker buildx plugin is unavailable; install buildx before running preflight"

[[ -x "${BIN_DIR}/kind" && -x "${BIN_DIR}/kubectl" && -x "${BIN_DIR}/helm" ]] \
  || die "lab-local tools are incomplete; run just tools"
sha256_check "${KIND_SHA256}" "${BIN_DIR}/kind"
sha256_check "${KUBECTL_SHA256}" "${BIN_DIR}/kubectl"
sha256_check "${HELM_BINARY_SHA256}" "${BIN_DIR}/helm"
"${BIN_DIR}/helm" version --short | grep -Fq "${HELM_VERSION}" \
  || die "checksum-verified lab-local Helm does not report ${HELM_VERSION}"

[[ "$(docker info --format '{{.OSType}}')" == "linux" ]] \
  || die "Docker Engine must run Linux containers"
[[ "$(docker info --format '{{.CgroupVersion}}')" == "2" ]] \
  || die "cgroup v2 is required"
[[ -r /sys/fs/cgroup/cgroup.controllers ]] \
  || die "host cgroup v2 controllers are unavailable"
if docker info --format '{{json .SecurityOptions}}' | grep -q 'rootless'; then
  die "rootless Docker is not supported"
fi

detected_cpus="${KAMAJI_PREFLIGHT_CPU_FIXTURE:-$(docker info --format '{{.NCPU}}')}"
detected_memory="${KAMAJI_PREFLIGHT_MEMORY_BYTES_FIXTURE:-$(docker info --format '{{.MemTotal}}')}"
docker_root="$(docker info --format '{{.DockerRootDir}}')"
detected_storage="${KAMAJI_PREFLIGHT_STORAGE_BYTES_FIXTURE:-$(( $(df -Pk "${docker_root}" | awk 'NR == 2 {print $4}') * 1024 ))}"

(( detected_cpus >= MIN_DOCKER_CPUS )) \
  || capacity_failure cpu "requires ${MIN_DOCKER_CPUS}, detected ${detected_cpus}"
(( detected_memory >= MIN_DOCKER_MEMORY_GIB * gib )) \
  || capacity_failure memory "requires ${MIN_DOCKER_MEMORY_GIB} GiB, detected $((detected_memory / gib)) GiB"
(( detected_storage >= MIN_DOCKER_STORAGE_GIB * gib )) \
  || capacity_failure storage "requires ${MIN_DOCKER_STORAGE_GIB} GiB, detected $((detected_storage / gib)) GiB"

check_network_non_overlap

require_prepared_input "${KAMAJI_SOURCE_SHA256}" "${CACHE_DIR}/kamaji-${KAMAJI_VERSION}.tar.gz"
require_prepared_input "${KAMAJI_CHART_LOCK_SHA256}" "${KAMAJI_CHART_DIR}/Chart.lock"
require_prepared_input "${KAMAJI_ETCD_CHART_SHA256}" "${KAMAJI_CHART_DIR}/charts/kamaji-etcd-${KAMAJI_ETCD_CHART_VERSION}.tgz"
require_prepared_input "${CERT_MANAGER_CHART_SHA256}" "${INPUTS_DIR}/cert-manager-${CERT_MANAGER_VERSION}.tgz"
require_prepared_input "${METALLB_MANIFEST_SHA256}" "${INPUTS_DIR}/metallb-native-${METALLB_VERSION}.yaml"
require_prepared_input "${CALICO_MANIFEST_SHA256}" "${INPUTS_DIR}/calico-${CALICO_VERSION}.yaml"
require_prepared_input "${LOCAL_PATH_MANIFEST_SHA256}" "${INPUTS_DIR}/local-path-${LOCAL_PATH_VERSION}.yaml"
require_prepared_input "${CNPG_MANIFEST_SHA256}" "${INPUTS_DIR}/cnpg-${CNPG_VERSION}.yaml"
grep -Fxq "${CERT_MANAGER_OCI_DIGEST}" "${INPUTS_DIR}/cert-manager-${CERT_MANAGER_VERSION}.digest" \
  || die "cert-manager OCI descriptor record is missing or incorrect"
"${SCRIPT_DIR}/render-kamaji.sh" render

for image in \
  "${KIND_NODE_IMAGE}" \
  "${KAMAJI_IMAGE_TAGGED}@${KAMAJI_IMAGE##*@}" \
  "${KAMAJI_ETCD_IMAGE_TAGGED}@${KAMAJI_ETCD_IMAGE##*@}" \
  "${KAMAJI_ETCD_JOB_IMAGE_TAGGED}@${KAMAJI_ETCD_JOB_IMAGE##*@}" \
  "${KAMAJI_KUBECTL_JOB_IMAGE_TAGGED}@${KAMAJI_KUBECTL_JOB_IMAGE##*@}" \
  "${CERT_MANAGER_OCI_REFERENCE}@${CERT_MANAGER_OCI_DIGEST}" \
  "${CNPG_CONTROLLER_IMAGE}" \
  "${KONNECTIVITY_AGENT_IMAGE}" \
  "${KONNECTIVITY_SERVER_IMAGE}" \
  "${POSTGRES_IMAGE}" \
  "${VERIFY_IMAGE}"; do
  verify_remote_image "${image}"
done

if [[ "${KAMAJI_PREFLIGHT_FORBID_MUTATION_FIXTURE:-0}" == "1" ]]; then
  die "capacity fixture unexpectedly reached the mutation gate"
fi

probe_name="kamaji-prerequisite-probe-$$"
trap 'docker rm -f "${probe_name}" >/dev/null 2>&1 || true' EXIT
timeout "$(seconds_from_duration "${PREFLIGHT_PROBE_TIMEOUT}")" \
  docker run --name "${probe_name}" --rm --privileged --cgroupns=private \
    --entrypoint sh "${VERIFY_IMAGE}" \
    -c 'test -r /proc/1/status && test -r /sys/fs/cgroup/cgroup.controllers' \
    >/dev/null \
  || die "Docker cannot run the required privileged cgroup-v2 worker probe"
trap - EXIT

log "preflight passed: ${detected_cpus} CPUs, $((detected_memory / gib)) GiB Docker memory, $((detected_storage / gib)) GiB Docker storage"
