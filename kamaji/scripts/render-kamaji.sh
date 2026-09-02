#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

post_render() {
  INVENTORY_PATH="${KAMAJI_IMAGE_INVENTORY}" \
  KAMAJI_IMAGE_TAGGED="${KAMAJI_IMAGE_TAGGED}" \
  KAMAJI_IMAGE="${KAMAJI_IMAGE}" \
  KAMAJI_ETCD_IMAGE_TAGGED="${KAMAJI_ETCD_IMAGE_TAGGED}" \
  KAMAJI_ETCD_IMAGE="${KAMAJI_ETCD_IMAGE}" \
  KAMAJI_ETCD_JOB_IMAGE_TAGGED="${KAMAJI_ETCD_JOB_IMAGE_TAGGED}" \
  KAMAJI_ETCD_JOB_IMAGE="${KAMAJI_ETCD_JOB_IMAGE}" \
  KAMAJI_KUBECTL_JOB_IMAGE_TAGGED="${KAMAJI_KUBECTL_JOB_IMAGE_TAGGED}" \
  KAMAJI_KUBECTL_JOB_IMAGE="${KAMAJI_KUBECTL_JOB_IMAGE}" \
  python3 -c '
import os
import re
import sys

data = sys.stdin.read()
replacements = (
    (os.environ["KAMAJI_IMAGE_TAGGED"], os.environ["KAMAJI_IMAGE"]),
    (os.environ["KAMAJI_ETCD_IMAGE_TAGGED"], os.environ["KAMAJI_ETCD_IMAGE"]),
    (os.environ["KAMAJI_ETCD_JOB_IMAGE_TAGGED"], os.environ["KAMAJI_ETCD_JOB_IMAGE"]),
    (os.environ["KAMAJI_KUBECTL_JOB_IMAGE_TAGGED"], os.environ["KAMAJI_KUBECTL_JOB_IMAGE"]),
)
for tagged, pinned in replacements:
    data = data.replace(tagged, pinned)

workload_kinds = {"CronJob", "DaemonSet", "Deployment", "Job", "Pod", "StatefulSet"}
images = set()
for document in re.split(r"(?m)^---\s*$", data):
    kind_match = re.search(r"(?m)^kind:\s*(\S+)\s*$", document)
    if not kind_match or kind_match.group(1) not in workload_kinds:
        continue
    images.update(re.findall(r"(?m)^\s*image:\s*[\"\x27]?([^\"\x27\s]+)", document))
images = sorted(images)
with open(os.environ["INVENTORY_PATH"], "w", encoding="utf-8") as inventory:
    inventory.write("\n".join(images) + "\n")
os.chmod(os.environ["INVENTORY_PATH"], 0o600)
sys.stdout.write(data)
'
}

validate_render() {
  local manifest="$1"
  local expected
  for expected in \
    "${KAMAJI_IMAGE}" \
    "${KAMAJI_ETCD_IMAGE}" \
    "${KAMAJI_ETCD_JOB_IMAGE}" \
    "${KAMAJI_KUBECTL_JOB_IMAGE}"; do
    grep -Fq "image: ${expected}" "${manifest}" \
      || grep -Fq "image: \"${expected}\"" "${manifest}" \
      || die "rendered chart is missing approved image ${expected}"
  done

  [[ "$(wc -l <"${KAMAJI_IMAGE_INVENTORY}")" -eq 4 ]] \
    || die "expected four transitive chart images"
  if grep -Ev '@sha256:[0-9a-f]{64}$' "${KAMAJI_IMAGE_INVENTORY}" | grep -q .; then
    die "transitive image inventory contains a non-digest reference"
  fi
  if grep -Eiq '(^|[:/@])latest($|@|:)' "${KAMAJI_IMAGE_INVENTORY}"; then
    die "transitive image inventory contains a moving latest reference"
  fi
}

render_chart() {
  require_command helm
  require_command python3
  [[ -f "${KAMAJI_CHART_DIR}/Chart.lock" ]] \
    || die "prepared Kamaji chart is missing; run just tools"
  [[ -f "${KAMAJI_CHART_DIR}/charts/kamaji-etcd-${KAMAJI_ETCD_CHART_VERSION}.tgz" ]] \
    || die "locked kamaji-etcd package is missing; run just tools"
  ensure_tools_layout

  helm template kamaji "${KAMAJI_CHART_DIR}" \
    --namespace "${MANAGEMENT_NAMESPACE}" \
    --include-crds \
    --kube-version "${KUBERNETES_VERSION#v}" \
    --set-string "image.repository=clastix/kamaji" \
    --set-string "image.tag=${KAMAJI_VERSION}" \
    --set "kubeconfigGenerator.enabled=false" \
    --set "kamaji-etcd.selfSignedCertificates.enabled=false" \
    --set "kamaji-etcd.certManager.enabled=true" \
    --set-string "kamaji-etcd.image.repository=quay.io/coreos/etcd" \
    --set-string "kamaji-etcd.image.tag=v3.5.17" \
    --set-string "kamaji-etcd.jobs.kubectl.image=clastix/kubectl" \
    --set-string "kamaji-etcd.jobs.kubectl.tag=v1.36" \
    --set-string "kamaji-etcd.jobs.etcd.image=quay.io/coreos/etcd" \
    --set-string "kamaji-etcd.jobs.etcd.tag=v3.5.6" \
    | post_render >"${KAMAJI_RENDERED_MANIFEST}"
  chmod 0600 "${KAMAJI_RENDERED_MANIFEST}"
  validate_render "${KAMAJI_RENDERED_MANIFEST}"
  sha256sum "${KAMAJI_RENDERED_MANIFEST}" \
    | write_secret_file "${KAMAJI_RENDERED_MANIFEST}.sha256"
  log "rendered Kamaji chart with four digest-pinned transitive images"
}

case "${1:-render}" in
  render)
    render_chart
    ;;
  post-render)
    ensure_tools_layout
    post_render
    ;;
  validate)
    validate_render "${KAMAJI_RENDERED_MANIFEST}"
    ;;
  *)
    die "usage: ${0##*/} [render|post-render|validate]"
    ;;
esac
