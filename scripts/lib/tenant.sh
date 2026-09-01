#!/usr/bin/env bash

tenant_release_exists() {
  local tenant="$1"
  KUBECONFIG="${HOST_KUBECONFIG}" helm status "${tenant}" \
    --namespace "$(tenant_namespace "${tenant}")" >/dev/null 2>&1
}

ensure_tenant_control_plane() {
  local tenant="$1"
  local namespace
  namespace="$(tenant_namespace "${tenant}")"

  if tenant_release_exists "${tenant}"; then
    log "tenant control plane ${tenant} already exists"
  else
    log "creating private-node tenant ${tenant}"
    local create_log="${RUNTIME_DIR}/logs/${tenant}-create.log"
    if ! KUBECONFIG="${HOST_KUBECONFIG}" \
      vcluster_cli create "${tenant}" \
        --driver helm \
        --context "$(host_context)" \
        --namespace "${namespace}" \
        --chart-version "${VCLUSTER_VERSION#v}" \
        --values "${REPO_ROOT}/config/tenants/${tenant}.yaml" \
        --connect=false \
        --background-proxy=false \
        --add=true >"${create_log}" 2>&1; then
      if grep -qi 'license limits are exceeded' "${create_log}"; then
        record_blocker "platform-free-tier-activation-required" \
          "Private Nodes are a vCluster Free-tier feature. Activate the self-hosted Platform Free tier in the browser, then rerun make create."
        die "vCluster Free tier is not activated. Private Nodes require Platform activation with an email account; see $(platform_url) and ${create_log}"
      fi
      tail -40 "${create_log}" >&2
      die "tenant control plane creation failed for ${tenant}"
    fi
  fi

  kubectl_host -n "${namespace}" rollout status statefulset/"${tenant}" \
    --timeout="${TENANT_TIMEOUT}"
}

write_tenant_kubeconfig() {
  local tenant="$1"
  local destination
  destination="$(tenant_kubeconfig "${tenant}")"
  mkdir -p -m 0700 "$(dirname "${destination}")"

  KUBECONFIG="${HOST_KUBECONFIG}" \
    vcluster_cli --silent connect "${tenant}" \
      --driver platform \
      --project "${PLATFORM_PROJECT}" \
      --background-proxy=false \
      --print >"${destination}"
  chmod 0600 "${destination}"
  KUBECONFIG="${destination}" kubectl version --request-timeout=20s >/dev/null
}

ensure_tenant_control_planes() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    ensure_tenant_control_plane "${tenant}"
    write_tenant_kubeconfig "${tenant}"
  done
}

tenant_expected_nodes_ready() {
  local tenant="$1"
  local ready
  ready="$(kubectl_tenant "${tenant}" get nodes \
    -l "cnpg-vcluster.io/tenant=${tenant}" \
    --no-headers 2>/dev/null \
    | awk '$2 == "Ready" {count++} END {print count + 0}')"
  [[ "${ready}" -eq "${WORKERS_PER_TENANT}" ]]
}

ensure_join_script() {
  local tenant="$1"
  local script_file="${RUNTIME_DIR}/join/${tenant}.sh"
  local token_output="${RUNTIME_DIR}/join/${tenant}.token-output"
  local join_url

  rm -f "${script_file}" "${token_output}"
  KUBECONFIG="$(tenant_kubeconfig "${tenant}")" \
    vcluster_cli token create --expires=1h >"${token_output}"
  chmod 0600 "${token_output}"
  join_url="$(grep -Eo 'https://[^[:space:]]+/node/join\?token=[^[:space:]]+' "${token_output}" | head -1)"
  [[ -n "${join_url}" ]] || die "could not extract private-node join URL for ${tenant}"
  platform_curl "${join_url}" -o "${script_file}"
  chmod 0600 "${script_file}"
  rm -f "${token_output}"
  printf '%s\n' "${script_file}"
}

join_worker() {
  local tenant="$1"
  local index="$2"
  local name script_file
  name="$(worker_name "${tenant}" "${index}")"

  if kubectl_tenant "${tenant}" wait --for=condition=Ready \
    "node/${name}" --timeout=10s >/dev/null 2>&1 \
    && docker exec "${name}" systemctl is-active kubelet >/dev/null 2>&1; then
    return
  fi

  kubectl_tenant "${tenant}" delete node "${name}" \
    --ignore-not-found --wait=false >/dev/null
  script_file="$(ensure_join_script "${tenant}")"
  log "joining ${name} to ${tenant}"
  docker cp "${script_file}" "${name}:/root/vcluster-join.sh"
  docker exec "${name}" chmod 0700 /root/vcluster-join.sh
  docker exec "${name}" /root/vcluster-join.sh \
    --force-join --node-name "${name}" \
    >"${RUNTIME_DIR}/logs/${name}-join.log" 2>&1 \
    || {
      tail -40 "${RUNTIME_DIR}/logs/${name}-join.log" >&2
      return 1
    }
}

join_failure_is_substrate() {
  local log_file="$1"
  grep -Eqi \
    'unsupported|SystemVerification|kernel|cgroup|swap|product_uuid|preflight.*(fatal|error)' \
    "${log_file}"
}

label_tenant_nodes() {
  local tenant="$1"
  kubectl_tenant "${tenant}" label nodes --all --overwrite \
    "cnpg-vcluster.io/tenant=${tenant}" \
    "node-role.kubernetes.io/postgres="
}

tenant_addons_ready() {
  local tenant="$1"
  tenant_expected_nodes_ready "${tenant}" || return 1
  kubectl_tenant "${tenant}" get storageclass \
    -o jsonpath='{range .items[*]}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' \
    | grep -qx true || return 1
  kubectl_tenant "${tenant}" -n kube-system get pods -o json \
    | python3 -c '
import json, sys
items=json.load(sys.stdin).get("items", [])
required=("coredns", "flannel", "kube-proxy", "local-path")
ready=[]
for item in items:
    conditions=item.get("status", {}).get("conditions", [])
    if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
        ready.append(item.get("metadata", {}).get("name", ""))
missing=[name for name in required if not any(name in pod for pod in ready)]
raise SystemExit(0 if not missing else 1)
' || return 1
  kubectl_tenant "${tenant}" -n kube-system get daemonsets -o json \
    | python3 -c '
import json, sys
items=json.load(sys.stdin).get("items", [])
required=("flannel", "kube-proxy")
for component in required:
    matches=[item for item in items if component in item.get("metadata", {}).get("name", "")]
    if not matches:
        raise SystemExit(1)
    status=matches[0].get("status", {})
    if status.get("desiredNumberScheduled") != status.get("numberReady") or status.get("numberReady", 0) < 1:
        raise SystemExit(1)
' || return 1
  kubectl_tenant "${tenant}" -n kube-system get deployments -o json \
    | python3 -c '
import json, sys
items=json.load(sys.stdin).get("items", [])
required=("coredns", "local-path")
for component in required:
    matches=[item for item in items if component in item.get("metadata", {}).get("name", "")]
    if not matches or matches[0].get("status", {}).get("availableReplicas", 0) < 1:
        raise SystemExit(1)
' || return 1
}

ensure_tenant_workers() {
  local tenant index
  for tenant in ${TENANT_NAMES}; do
    for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
      if ! join_worker "${tenant}" "${index}"; then
        "${REPO_ROOT}/scripts/diagnose.sh" "${tenant}" >&2 || true
        if join_failure_is_substrate \
          "${RUNTIME_DIR}/logs/$(worker_name "${tenant}" "${index}")-join.log"; then
          record_blocker "private-worker-substrate-unsupported" \
            "The experimental systemd container $(worker_name "${tenant}" "${index}") could not join ${tenant} after supported prerequisites were checked."
        fi
        die "private worker join failed for $(worker_name "${tenant}" "${index}")"
      fi
    done

    if ! wait_for "${NODE_TIMEOUT}" "${tenant} private nodes" \
      bash -c "KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl --request-timeout=30s wait --for=condition=Ready nodes --all --timeout=20s >/dev/null 2>&1 && [[ \$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl --request-timeout=30s get nodes --no-headers | wc -l) -eq ${WORKERS_PER_TENANT} ]]"; then
      "${REPO_ROOT}/scripts/diagnose.sh" "${tenant}" >&2 || true
      die "${tenant} private nodes did not become Ready"
    fi
    label_tenant_nodes "${tenant}"
    for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
      wait_for "${WORKER_BOOT_TIMEOUT}" \
        "joined services in $(worker_name "${tenant}" "${index}")" \
        worker_services_ready "$(worker_name "${tenant}" "${index}")" \
        || {
          die "joined worker services are unhealthy for ${tenant}"
        }
    done
    retry_for "${ADDON_TIMEOUT}" "${tenant} networking and storage" tenant_addons_ready "${tenant}"
  done
}
