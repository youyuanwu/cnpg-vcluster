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

command_returns_one() {
  set +e
  "$@" >/dev/null 2>&1
  local status=$?
  set -e
  [[ "${status}" -eq "${EXIT_ERROR}" ]]
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
  for component in kubernetes metallb kamaji datastore; do
    touch "${fixture_dir}/${component}"
  done

  management_helm() {
    case "$1" in
      status)
        return 1
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
    return 0
  }

  set +e
  output="$(reconcile_cert_manager 2>&1)"
  status=$?
  set -e

  [[ "${status}" -eq "${EXIT_ERROR}" ]] \
    && [[ "${output}" == "[kamaji-lab] ERROR: management.cert-manager: ${CERT_MANAGER_VERSION} installation or readiness failed" ]] \
    && [[ ! -e "${fixture_dir}/cert-manager" ]] \
    && [[ "$(find "${fixture_dir}" -maxdepth 1 -type f -printf '%f\n' | sort)" == $'datastore\nkamaji\nkubernetes\nmetallb' ]]
)

require_exact_just

for script in "${LAB_ROOT}"/scripts/*.sh "${LAB_ROOT}"/scripts/lib/*.sh; do
  check "bash syntax: ${script#${LAB_ROOT}/}" bash -n "${script}"
done

check "complete just task surface" bash -c '
  tasks="$(just --justfile "$1" --list --unsorted)"
  for task in tools preflight create-management spike destroy-spike create status diagnose verify destroy-tenant destroy test-static test-e2e; do
    grep -Eq "^    ${task}([[:space:]]|$)" <<<"${tasks}" || exit 1
  done
' _ "${LAB_ROOT}/Justfile"
check "Kamaji has no Makefile" test ! -e "${LAB_ROOT}/Makefile"
check "root Makefile is byte-for-byte unchanged" files_identical_to_main Makefile
check "root README remains unchanged through Phase 3" files_identical_to_main README.md
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
check "MetalLB v0.16.1 manifest checksum is exact" \
  has_text '^METALLB_MANIFEST_SHA256=bf25feebb7582ca7df845efd52ffbc2960d6cbf4cfc972f47fded9f788b67f0b$' \
  "${LAB_ROOT}/config/versions.env"
check "Calico 3.32.2 manifest checksum is exact" \
  has_text '^CALICO_MANIFEST_SHA256=a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02$' \
  "${LAB_ROOT}/config/versions.env"
check "Local Path 0.0.37 manifest checksum is exact" \
  has_text '^LOCAL_PATH_MANIFEST_SHA256=9781b39c24f3f651bd6d6e41b561e04e4904bbdb6d4f8c7a6009df3a702dcd65$' \
  "${LAB_ROOT}/config/versions.env"
check "CNPG 1.30.0 manifest checksum is exact" \
  has_text '^CNPG_MANIFEST_SHA256=f8bede43fe4ee0d478c2355b204a36876b2ae4faac60f2a9452280b293da3b88$' \
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
  source "$1/config/versions.env"
  grep -Fq "storageclass.kubernetes.io/is-default-class: \"true\"" "$1/manifests/addons/local-path.yaml" &&
  grep -Fq "\"paths\":[\"/var/lib/kamaji-local-path\"]" "$1/manifests/addons/local-path.yaml" &&
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
     "$MIN_DOCKER_STORAGE_GIB" == 30 ]]
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
     "$TENANT_API_SERVER_LIMIT_MEMORY" == 512Mi &&
     "$TENANT_CONTROLLER_MANAGER_LIMIT_MEMORY" == 256Mi &&
     "$TENANT_SCHEDULER_LIMIT_MEMORY" == 256Mi ]]
' _ "${LAB_ROOT}"
check "CPU threshold fixture rejects before mutation" \
  capacity_fixture_rejects CPU_FIXTURE 11 cpu
check "memory threshold fixture rejects before mutation" \
  capacity_fixture_rejects MEMORY_BYTES_FIXTURE $((23 * 1024 * 1024 * 1024)) memory
check "storage threshold fixture rejects before mutation" \
  capacity_fixture_rejects STORAGE_BYTES_FIXTURE $((29 * 1024 * 1024 * 1024)) storage

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

check "only recognized spike paths can return blocked status" bash -c '
  refs="$(grep -R -l "EXIT_BLOCKED" "$1/scripts" --include="*.sh" --exclude=test-static.sh)"
  [[ "$(printf "%s\n" "$refs" | sort)" == "$(printf "%s\n" "$1/scripts/create-spike.sh" "$1/scripts/lib/common.sh" | sort)" ]] &&
  grep -A12 "^compatibility_blocker()" "$1/scripts/lib/common.sh" |
    grep -Fq "return \"\${EXIT_BLOCKED}\"" &&
  grep -Fq "return \"\${EXIT_BLOCKED}\"" "$1/scripts/create-spike.sh" &&
  grep -Fq "record_spike_blocker" "$1/scripts/create-spike.sh"
' _ "${LAB_ROOT}"
check "unimplemented normal path returns 1, never 2" \
  command_returns_one "${SCRIPT_DIR}/unavailable.sh" create "later phase"
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
check "kubeadm allowlist has one settings source and exact consumers" bash -c '
  [[ "$(grep -R -h "^: .*KUBEADM_IGNORE_PREFLIGHT_ERRORS" "$1/config" | wc -l)" -eq 1 ]] &&
  grep -Fq -- '\''--ignore-preflight-errors=${KUBEADM_IGNORE_PREFLIGHT_ERRORS}'\'' "$1/scripts/lib/workers.sh" &&
  grep -Fq "KUBEADM_IGNORE_PREFLIGHT_ERRORS" "$1/README.md" &&
  grep -Fq "KUBEADM_IGNORE_PREFLIGHT_ERRORS" "$1/docs/high-level-design.md"
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
  grep -Fq "rm -rf \"\${SPIKE_RUNTIME_DIR}\"" "$1/scripts/destroy-spike.sh"
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
  ! grep -Eiq "(fallback|stable\\.clastix|license|activation|vcluster)" \
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
check "Phase 3 retains explicit Konnectivity digest action" bash -c '
  grep -Fq "KONNECTIVITY_AGENT_IMAGE" "$1" &&
  grep -Fq "KONNECTIVITY_SERVER_IMAGE" "$1"
' _ "${LAB_ROOT}/docs/high-level-design.md"

if (( failures > 0 )); then
  die "${failures} static check(s) failed"
fi

log "all ${checks} Phase 3 static checks passed"
