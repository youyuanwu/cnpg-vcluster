#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

require_command curl
require_command tar
require_command sha256sum

download_binary() {
  local name="$1"
  local version="$2"
  local expected="$3"
  local url="$4"
  local destination="${BIN_DIR}/${name}"
  local cache_file="${CACHE_DIR}/${name}-${version}"

  if [[ -x "${destination}" ]] && sha256_check "${expected}" "${destination}"; then
    return
  fi

  log "downloading ${name} ${version}"
  curl -fL --retry 3 --retry-delay 2 "${url}" -o "${cache_file}"
  sha256_check "${expected}" "${cache_file}"
  install -m 0755 "${cache_file}" "${destination}"
}

download_binary \
  kind "${KIND_VERSION}" "${KIND_SHA256}" \
  "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-amd64"

download_binary \
  kubectl "${KUBECTL_VERSION}" "${KUBECTL_SHA256}" \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"

download_binary \
  vcluster "${VCLUSTER_VERSION}" "${VCLUSTER_SHA256}" \
  "https://github.com/loft-sh/vcluster/releases/download/${VCLUSTER_VERSION}/vcluster-linux-amd64"

HELM_ARCHIVE="${CACHE_DIR}/helm-${HELM_VERSION}-linux-amd64.tar.gz"
if [[ ! -x "${BIN_DIR}/helm" ]]; then
  log "downloading helm ${HELM_VERSION}"
  curl -fL --retry 3 --retry-delay 2 \
    "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
    -o "${HELM_ARCHIVE}"
  sha256_check "${HELM_SHA256}" "${HELM_ARCHIVE}"
  rm -rf "${CACHE_DIR}/linux-amd64"
  tar -xzf "${HELM_ARCHIVE}" -C "${CACHE_DIR}"
  install -m 0755 "${CACHE_DIR}/linux-amd64/helm" "${BIN_DIR}/helm"
  rm -rf "${CACHE_DIR}/linux-amd64"
else
  "${BIN_DIR}/helm" version --short | grep -Fq "${HELM_VERSION}" \
    || die "unexpected cached helm version"
fi

log "tool versions"
"${BIN_DIR}/kind" version
"${BIN_DIR}/kubectl" version --client
"${BIN_DIR}/helm" version --short
"${BIN_DIR}/vcluster" version
