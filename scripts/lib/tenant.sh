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
      --driver helm \
      --context "$(host_context)" \
      --namespace "$(tenant_namespace "${tenant}")" \
      --background-proxy=true \
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

  if [[ -s "${script_file}" ]]; then
    printf '%s\n' "${script_file}"
    return
  fi

  KUBECONFIG="$(tenant_kubeconfig "${tenant}")" \
    vcluster_cli token create --expires=1h >"${token_output}"
  chmod 0600 "${token_output}"
  join_url="$(grep -Eo 'https://[^[:space:]]+/node/join\?token=[^[:space:]]+' "${token_output}" | head -1)"
  [[ -n "${join_url}" ]] || die "could not extract private-node join URL for ${tenant}"
  curl -kfsSL "${join_url}" -o "${script_file}"
  chmod 0600 "${script_file}"
  rm -f "${token_output}"
  printf '%s\n' "${script_file}"
}

join_worker() {
  local tenant="$1"
  local index="$2"
  local name script_file
  name="$(worker_name "${tenant}" "${index}")"

  if kubectl_tenant "${tenant}" get node "${name}" >/dev/null 2>&1; then
    return
  fi

  script_file="$(ensure_join_script "${tenant}")"
  log "joining ${name} to ${tenant}"
  docker cp "${script_file}" "${name}:/root/vcluster-join.sh"
  docker exec "${name}" chmod 0700 /root/vcluster-join.sh
  docker exec "${name}" /root/vcluster-join.sh --node-name "${name}" \
    >"${RUNTIME_DIR}/logs/${name}-join.log" 2>&1 \
    || {
      tail -40 "${RUNTIME_DIR}/logs/${name}-join.log" >&2
      return 1
    }
}

label_tenant_nodes() {
  local tenant="$1"
  kubectl_tenant "${tenant}" label nodes --all --overwrite \
    "cnpg-vcluster.io/tenant=${tenant}" \
    "node-role.kubernetes.io/postgres="
}

tenant_addons_ready() {
  local tenant="$1"
  tenant_expected_nodes_ready "${tenant}" \
    && kubectl_tenant "${tenant}" get storageclass \
      -o jsonpath='{range .items[*]}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' \
      | grep -qx true \
    && kubectl_tenant "${tenant}" -n kube-system get pods --no-headers \
      | awk 'BEGIN {found=0} /coredns|flannel|kube-proxy|local-path/ {found=1; if ($3 != "Running" && $3 != "Completed") exit 1} END {exit found ? 0 : 1}'
}

ensure_tenant_workers() {
  local tenant index name
  for tenant in ${TENANT_NAMES}; do
    if ! tenant_expected_nodes_ready "${tenant}"; then
      for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
        join_worker "${tenant}" "${index}" \
          || die "private worker join failed for $(worker_name "${tenant}" "${index}"); container workers are outside vCluster's support matrix"
      done
    fi

    retry_for "${NODE_TIMEOUT}" "${tenant} private nodes" \
      bash -c "KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl wait --for=condition=Ready nodes --all --timeout=20s >/dev/null 2>&1 && [[ \$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl get nodes --no-headers | wc -l) -eq ${WORKERS_PER_TENANT} ]]"
    label_tenant_nodes "${tenant}"
    retry_for "${ADDON_TIMEOUT}" "${tenant} networking and storage" tenant_addons_ready "${tenant}"

    for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
      name="$(worker_name "${tenant}" "${index}")"
      retry_for 2m "Platform reachability from ${name}" platform_reachable_from_worker "${name}"
    done
  done
}
