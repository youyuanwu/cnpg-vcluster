#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

failures=0

check() {
  local description="$1"
  shift
  if "$@"; then
    printf 'ok - %s\n' "${description}"
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

require_exact_just

for script in "${LAB_ROOT}"/scripts/*.sh "${LAB_ROOT}"/scripts/lib/*.sh; do
  check "bash syntax: ${script#${LAB_ROOT}/}" bash -n "${script}"
done

check "complete just task surface" bash -c '
  tasks="$(just --justfile "$1" --list --unsorted)"
  for task in tools preflight create-management spike create status diagnose verify destroy-tenant destroy test-static test-e2e; do
    grep -Eq "^    ${task}([[:space:]]|$)" <<<"${tasks}" || exit 1
  done
' _ "${LAB_ROOT}/Justfile"
check "Kamaji has no Makefile" test ! -e "${LAB_ROOT}/Makefile"
check "root Makefile is byte-for-byte unchanged" files_identical_to_main Makefile
check "root README is unchanged in Phase 1" files_identical_to_main README.md
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

check "deterministic Kamaji render validates" \
  "${SCRIPT_DIR}/render-kamaji.sh" validate
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

check "capacity thresholds are exact" bash -c '
  source "$1/config/settings.env"
  [[ "$MIN_DOCKER_CPUS" == 12 &&
     "$MIN_DOCKER_MEMORY_GIB" == 24 &&
     "$MIN_DOCKER_STORAGE_GIB" == 30 ]]
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

check "only compatibility_blocker can return blocked status" bash -c '
  refs="$(grep -R -l "EXIT_BLOCKED" "$1/scripts" --include="*.sh" --exclude=test-static.sh)"
  [[ "$refs" == "$1/scripts/lib/common.sh" ]] &&
  grep -A12 "^compatibility_blocker()" "$1/scripts/lib/common.sh" |
    grep -Fq "return \"\${EXIT_BLOCKED}\""
' _ "${LAB_ROOT}"
check "unimplemented normal path returns 1, never 2" \
  command_returns_one "${SCRIPT_DIR}/unavailable.sh" create "later phase"
check "clean status is unhealthy status 1, never blocker 2" \
  command_returns_one "${SCRIPT_DIR}/status.sh"
check "diagnostics completes without blocker status" \
  "${SCRIPT_DIR}/diagnose.sh" all

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
  has_text 'Exit status `2`.*compatibility blocker' "${LAB_ROOT}/README.md"
check "design documents privileged shared-kernel boundary" \
  has_text 'share the Docker host kernel' "${LAB_ROOT}/docs/high-level-design.md"
check "design records deterministic transitive image inventory" \
  has_text 'transitive image inventory' "${LAB_ROOT}/docs/high-level-design.md"

if (( failures > 0 )); then
  die "${failures} static check(s) failed"
fi

log "all Phase 1 static checks passed"
