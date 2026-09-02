#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

require_command curl
require_command git
require_command install
require_command sha256sum
require_command tar
require_command timeout
require_exact_just
ensure_tools_layout

download_file() {
  local name="$1"
  local url="$2"
  local expected="$3"
  local destination="$4"

  if sha256_matches "${expected}" "${destination}"; then
    return
  fi

  log "downloading ${name}"
  rm -f "${destination}"
  curl -fL --retry 3 --retry-delay 2 \
    --max-time "$(seconds_from_duration "${DOWNLOAD_TIMEOUT}")" \
    "${url}" -o "${destination}"
  sha256_check "${expected}" "${destination}"
  chmod 0600 "${destination}"
}

install_binary() {
  local name="$1"
  local version="$2"
  local url="$3"
  local expected="$4"
  local cache_file="${CACHE_DIR}/${name}-${version}"

  download_file "${name} ${version}" "${url}" "${expected}" "${cache_file}"
  if [[ ! -x "${BIN_DIR}/${name}" ]] \
    || ! sha256_matches "${expected}" "${BIN_DIR}/${name}"; then
    install -m 0755 "${cache_file}" "${BIN_DIR}/${name}"
  fi
}

install_binary kind "${KIND_VERSION}" "${KIND_URL}" "${KIND_SHA256}"
install_binary kubectl "${KUBECTL_VERSION}" "${KUBECTL_URL}" "${KUBECTL_SHA256}"

helm_archive="${CACHE_DIR}/helm-${HELM_VERSION}-linux-amd64.tar.gz"
download_file "Helm ${HELM_VERSION}" "${HELM_URL}" "${HELM_SHA256}" "${helm_archive}"
if [[ ! -x "${BIN_DIR}/helm" ]] \
  || ! sha256_matches "${HELM_BINARY_SHA256}" "${BIN_DIR}/helm"; then
  helm_extract="${TOOLS_TMP_DIR}/helm-extract"
  rm -rf "${helm_extract}"
  mkdir -p -m 0700 "${helm_extract}"
  tar -xzf "${helm_archive}" -C "${helm_extract}"
  install -m 0755 "${helm_extract}/linux-amd64/helm" "${BIN_DIR}/helm"
  sha256_check "${HELM_BINARY_SHA256}" "${BIN_DIR}/helm"
  rm -rf "${helm_extract}"
fi

download_file \
  "Kamaji ${KAMAJI_VERSION} source" \
  "${KAMAJI_SOURCE_URL}" \
  "${KAMAJI_SOURCE_SHA256}" \
  "${CACHE_DIR}/kamaji-${KAMAJI_VERSION}.tar.gz"

tag_commit="$(
  timeout "$(seconds_from_duration "${PREFLIGHT_ENDPOINT_TIMEOUT}")" \
    git ls-remote https://github.com/clastix/kamaji.git \
    "refs/tags/${KAMAJI_VERSION}" | awk 'NR == 1 {print $1}'
)"
[[ "${tag_commit}" == "${KAMAJI_TAG_COMMIT}" ]] \
  || die "Kamaji tag ${KAMAJI_VERSION} resolved to ${tag_commit:-nothing}, expected ${KAMAJI_TAG_COMMIT}"

rm -rf "${SOURCE_DIR:?}/${KAMAJI_SOURCE_DIR}" "${KAMAJI_CHART_DIR}"
tar -xzf "${CACHE_DIR}/kamaji-${KAMAJI_VERSION}.tar.gz" -C "${SOURCE_DIR}"
[[ -d "${SOURCE_DIR}/${KAMAJI_SOURCE_DIR}/charts/kamaji" ]] \
  || die "Kamaji source archive did not contain the expected chart"
cp -a "${SOURCE_DIR}/${KAMAJI_SOURCE_DIR}/charts/kamaji" "${KAMAJI_CHART_DIR}"
mkdir -p -m 0700 "${KAMAJI_CHART_DIR}/charts"

sha256_check "${KAMAJI_CHART_LOCK_SHA256}" "${KAMAJI_CHART_DIR}/Chart.lock"
grep -Fq "version: ${KAMAJI_ETCD_CHART_VERSION}" "${KAMAJI_CHART_DIR}/Chart.lock" \
  || die "Kamaji Chart.lock does not select kamaji-etcd ${KAMAJI_ETCD_CHART_VERSION}"
grep -Fq "digest: ${KAMAJI_CHART_LOCK_DIGEST}" "${KAMAJI_CHART_DIR}/Chart.lock" \
  || die "Kamaji Chart.lock digest differs from the approved release lock"

download_file \
  "kamaji-etcd chart ${KAMAJI_ETCD_CHART_VERSION}" \
  "${KAMAJI_ETCD_CHART_URL}" \
  "${KAMAJI_ETCD_CHART_SHA256}" \
  "${KAMAJI_CHART_DIR}/charts/kamaji-etcd-${KAMAJI_ETCD_CHART_VERSION}.tgz"

download_file \
  "MetalLB ${METALLB_VERSION} manifest" \
  "${METALLB_MANIFEST_URL}" \
  "${METALLB_MANIFEST_SHA256}" \
  "${INPUTS_DIR}/metallb-native-${METALLB_VERSION}.yaml"
download_file \
  "Calico ${CALICO_VERSION} manifest" \
  "${CALICO_MANIFEST_URL}" \
  "${CALICO_MANIFEST_SHA256}" \
  "${INPUTS_DIR}/calico-${CALICO_VERSION}.yaml"
download_file \
  "Local Path Provisioner ${LOCAL_PATH_VERSION} manifest" \
  "${LOCAL_PATH_MANIFEST_URL}" \
  "${LOCAL_PATH_MANIFEST_SHA256}" \
  "${INPUTS_DIR}/local-path-${LOCAL_PATH_VERSION}.yaml"
download_file \
  "CloudNativePG ${CNPG_VERSION} manifest" \
  "${CNPG_MANIFEST_URL}" \
  "${CNPG_MANIFEST_SHA256}" \
  "${INPUTS_DIR}/cnpg-${CNPG_VERSION}.yaml"

cert_pull_dir="${TOOLS_TMP_DIR}/cert-manager-pull"
rm -rf "${cert_pull_dir}"
mkdir -p -m 0700 "${cert_pull_dir}"
cert_pull_output="$(
  timeout "$(seconds_from_duration "${DOWNLOAD_TIMEOUT}")" \
    "${BIN_DIR}/helm" pull "${CERT_MANAGER_OCI}" \
    --version "${CERT_MANAGER_VERSION}" \
    --destination "${cert_pull_dir}" 2>&1
)"
printf '%s\n' "${cert_pull_output}" \
  | grep -Fq "Digest: ${CERT_MANAGER_OCI_DIGEST}" \
  || die "cert-manager OCI descriptor digest did not match ${CERT_MANAGER_OCI_DIGEST}"
cert_chart="${cert_pull_dir}/cert-manager-${CERT_MANAGER_VERSION}.tgz"
sha256_check "${CERT_MANAGER_CHART_SHA256}" "${cert_chart}"
install -m 0600 "${cert_chart}" "${INPUTS_DIR}/cert-manager-${CERT_MANAGER_VERSION}.tgz"
printf '%s\n' "${CERT_MANAGER_OCI_DIGEST}" \
  | write_secret_file "${INPUTS_DIR}/cert-manager-${CERT_MANAGER_VERSION}.digest"
rm -rf "${cert_pull_dir}"

find "${TOOLS_DIR}" -type d -exec chmod 0700 {} +
"${SCRIPT_DIR}/render-kamaji.sh" render

log "verified tool versions"
"${BIN_DIR}/kind" version
"${BIN_DIR}/kubectl" version --client=true
"${BIN_DIR}/helm" version --short
just --version
log "prepared Kamaji chart and immutable inputs under ${TOOLS_DIR}"
