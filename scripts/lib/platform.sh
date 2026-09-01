#!/usr/bin/env bash

platform_url_file() {
  printf '%s/platform-url\n' "${RUNTIME_DIR}"
}

platform_password_file() {
  printf '%s/credentials/platform-admin-password\n' "${RUNTIME_DIR}"
}

platform_ready() {
  kubectl_host -n "${PLATFORM_NAMESPACE}" rollout status deployment/loft \
    --timeout=10s >/dev/null 2>&1
}

ensure_platform_password() {
  local file
  file="$(platform_password_file)"
  if [[ ! -s "${file}" ]]; then
    python3 - <<'PY' >"${file}"
import secrets
print(secrets.token_urlsafe(32))
PY
    chmod 0600 "${file}"
  fi
}

discover_platform_url() {
  local log_file="$1"
  local url

  if [[ -n "${PLATFORM_HOST:-}" ]]; then
    url="${PLATFORM_HOST}"
  else
    url="$(grep -Eo 'https://[A-Za-z0-9.-]+\.loft\.host' "${log_file}" | tail -1 || true)"
    if [[ -z "${url}" ]]; then
      url="$(kubectl_host -n "${PLATFORM_NAMESPACE}" get secret loft-router-domain \
        -o jsonpath='{.data.domain}' 2>/dev/null \
        | base64 -d 2>/dev/null || true)"
      [[ -z "${url}" ]] || url="https://${url}"
    fi
    if [[ -z "${url}" ]]; then
      url="$(python3 - "${VCLUSTER_CONFIG}" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if path.exists():
    data = json.loads(path.read_text())
    for key in ("platformHost", "host"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            print(value)
            break
PY
)"
    fi
  fi

  [[ -n "${url}" ]] || return 1
  printf '%s\n' "${url}" >"$(platform_url_file)"
  chmod 0600 "$(platform_url_file)"
}

platform_url() {
  [[ -s "$(platform_url_file)" ]] || return 1
  cat "$(platform_url_file)"
}

platform_curl() {
  local url="$1"
  shift
  local tls_args=()
  if [[ -n "${PLATFORM_CA_FILE:-}" ]]; then
    [[ -f "${PLATFORM_CA_FILE}" ]] || die "PLATFORM_CA_FILE does not exist: ${PLATFORM_CA_FILE}"
    tls_args=(--cacert "${PLATFORM_CA_FILE}")
  fi
  curl -fsS --max-time 20 "${tls_args[@]}" "${url}" "$@"
}

platform_reachable() {
  local url
  url="$(platform_url)" || return 1
  platform_curl "${url}/version" >/dev/null 2>&1
}

platform_reachable_from_kind() {
  local url node ca_args=()
  url="$(platform_url)"
  node="${KIND_CLUSTER_NAME}-control-plane"
  if [[ -n "${PLATFORM_CA_FILE:-}" ]]; then
    docker cp "${PLATFORM_CA_FILE}" "${node}:/root/platform-ca.crt" >/dev/null
    ca_args=(--cacert /root/platform-ca.crt)
  fi
  docker exec "${node}" curl -fsS --max-time 20 \
    "${ca_args[@]}" "${url}/version" >/dev/null 2>&1
}

ensure_platform() {
  ensure_platform_password
  local log_file="${RUNTIME_DIR}/logs/platform-start.log"
  local password
  password="$(cat "$(platform_password_file)")"

  if platform_ready && [[ -s "$(platform_url_file)" ]] && platform_reachable; then
    log "vCluster Platform is already ready at $(platform_url)"
    return
  fi

  log "installing vCluster Platform ${PLATFORM_VERSION}"
  BROWSER=/bin/true KUBECONFIG="${HOST_KUBECONFIG}" \
    vcluster_cli platform start \
      --context "$(host_context)" \
      --namespace "${PLATFORM_NAMESPACE}" \
      --version "${PLATFORM_VERSION}" \
      --values "${REPO_ROOT}/config/platform.yaml" \
      --email "admin@cnpg-vcluster.local" \
      --password "${password}" \
      --upgrade \
      >"${log_file}" 2>&1 \
    || {
      grep -Ei 'error|fatal|failed' "${log_file}" | tail -20 >&2 || true
      die "vCluster Platform installation failed; see ${log_file}"
    }

  retry_for "${PLATFORM_TIMEOUT}" "vCluster Platform deployment" platform_ready
  discover_platform_url "${log_file}" \
    || {
      record_blocker "platform-endpoint-unavailable" \
        "Could not discover the Platform HTTPS URL. Set PLATFORM_HOST to a reachable endpoint and rerun make create."
      die "could not discover Platform URL; set PLATFORM_HOST to a reachable HTTPS endpoint"
    }
  if ! wait_for "${PLATFORM_TIMEOUT}" "Platform endpoint $(platform_url)" platform_reachable; then
    record_blocker "platform-endpoint-unavailable" \
      "The Platform URL is not reachable. Set PLATFORM_HOST to a reachable HTTPS endpoint and rerun make create."
    die "timed out waiting for Platform endpoint $(platform_url)"
  fi
  if ! wait_for "${PLATFORM_TIMEOUT}" "Platform endpoint from kind" platform_reachable_from_kind; then
    record_blocker "platform-endpoint-unavailable" \
      "The Platform URL is not reachable from the kind control cluster."
    die "Platform endpoint $(platform_url) is not reachable from kind"
  fi
  log "vCluster Platform ready at $(platform_url)"
}

platform_has_tenant() {
  local tenant="$1"
  vcluster_cli platform list vclusters --output json 2>/dev/null \
    | python3 -c '
import json, sys
needle = sys.argv[1]
data = json.load(sys.stdin)
if data is None:
    items = []
elif isinstance(data, list):
    items = data
else:
    items = data.get("virtualClusters", data.get("items", []))
raise SystemExit(0 if any(
    needle in (item.get("name"), item.get("metadata", {}).get("name"))
    for item in items
) else 1)
' "${tenant}"
}
