#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

verify_locked_chart() {
  [[ -f "${KAMAJI_CHART_DIR}/Chart.lock" ]] \
    || die "prepared Kamaji Chart.lock is missing; run just tools"
  [[ -f "${KAMAJI_CHART_DIR}/charts/kamaji-etcd-${KAMAJI_ETCD_CHART_VERSION}.tgz" ]] \
    || die "locked kamaji-etcd package is missing; run just tools"
  sha256_check "${KAMAJI_CHART_LOCK_SHA256}" "${KAMAJI_CHART_DIR}/Chart.lock"
  sha256_check "${KAMAJI_ETCD_CHART_SHA256}" \
    "${KAMAJI_CHART_DIR}/charts/kamaji-etcd-${KAMAJI_ETCD_CHART_VERSION}.tgz"
  grep -Fq "version: ${KAMAJI_ETCD_CHART_VERSION}" "${KAMAJI_CHART_DIR}/Chart.lock" \
    || die "Kamaji Chart.lock does not select kamaji-etcd ${KAMAJI_ETCD_CHART_VERSION}"
  grep -Fq "digest: ${KAMAJI_CHART_LOCK_DIGEST}" "${KAMAJI_CHART_DIR}/Chart.lock" \
    || die "Kamaji Chart.lock digest differs from the approved release lock"
}

post_render() {
  ensure_tools_layout
  local capture="${KAMAJI_CAPTURE_RENDER:-0}"
  INVENTORY_PATH="${KAMAJI_IMAGE_INVENTORY}" \
  RENDERED_PATH="${KAMAJI_RENDERED_MANIFEST}" \
  CAPTURE_RENDER="${capture}" \
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

if os.environ["CAPTURE_RENDER"] == "1":
    for path, content in (
        (os.environ["INVENTORY_PATH"], "\n".join(sorted(images)) + "\n"),
        (os.environ["RENDERED_PATH"], data),
    ):
        with open(path, "w", encoding="utf-8") as output:
            output.write(content)
        os.chmod(path, 0o600)
sys.stdout.write(data)
'
}

validate_render() {
  local manifest="$1"
  local expected
  [[ -f "${manifest}" ]] || die "rendered Kamaji manifest is missing"
  grep -Fq -- '- --disable-telemetry' "${manifest}" \
    || die "rendered chart does not disable telemetry"
  for expected in \
    "${KAMAJI_IMAGE}" \
    "${KAMAJI_ETCD_IMAGE}" \
    "${KAMAJI_ETCD_JOB_IMAGE}" \
    "${KAMAJI_KUBECTL_JOB_IMAGE}"; do
    grep -Fq "image: ${expected}" "${manifest}" \
      || grep -Fq "image: \"${expected}\"" "${manifest}" \
      || die "rendered chart is missing approved image ${expected}"
  done
  grep -Fq 'replicas: 3' "${manifest}" \
    || die "rendered chart is missing the three-member datastore"
  [[ "$(grep -c 'storage: 1Gi' "${manifest}")" -eq 1 ]] \
    || die "rendered chart does not request 1Gi datastore claims"
  for resource in 100m 128Mi 500m 512Mi 256Mi; do
    grep -Fq "${resource}" "${manifest}" \
      || die "rendered chart is missing capacity value ${resource}"
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
  verify_locked_chart
  ensure_tools_layout
  helm template kamaji "${KAMAJI_CHART_DIR}" \
    --namespace "${MANAGEMENT_NAMESPACE}" \
    --include-crds \
    --kube-version "${KUBERNETES_VERSION#v}" \
    --values "${LAB_ROOT}/config/kamaji-values.yaml" \
    | KAMAJI_CAPTURE_RENDER=1 post_render >/dev/null
  validate_render "${KAMAJI_RENDERED_MANIFEST}"
  sha256sum "${KAMAJI_RENDERED_MANIFEST}" \
    | write_secret_file "${KAMAJI_RENDERED_MANIFEST}.sha256"
  log "rendered locked Kamaji chart with four digest-pinned transitive images"
}

case "${1:-post-render}" in
  post-render)
    post_render
    ;;
  render)
    render_chart
    ;;
  validate)
    verify_locked_chart
    validate_render "${KAMAJI_RENDERED_MANIFEST}"
    ;;
  *)
    die "usage: ${0##*/} [render|validate]"
    ;;
esac
