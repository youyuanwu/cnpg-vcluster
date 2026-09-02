#!/usr/bin/env bash

cnpg_cluster_name() {
  printf '%s-postgres\n' "$1"
}

cnpg_cluster_ready() {
  local tenant="$1"
  local cluster
  cluster="$(cnpg_cluster_name "${tenant}")"
  kubectl_tenant "${tenant}" -n database get cluster "${cluster}" -o json 2>/dev/null \
    | python3 -c '
import json, sys
obj = json.load(sys.stdin)
status = obj.get("status", {})
raise SystemExit(0 if status.get("readyInstances") == 3 else 1)
'
}

install_cnpg_for_tenant() {
  local tenant="$1"
  local cluster
  cluster="$(cnpg_cluster_name "${tenant}")"

  log "installing CloudNativePG ${CNPG_VERSION} in ${tenant}"
  kubectl_tenant "${tenant}" apply --server-side \
    -f "${REPO_ROOT}/manifests/cnpg/operator.yaml" >/dev/null
  kubectl_tenant "${tenant}" -n cnpg-system rollout status \
    deployment/cnpg-controller-manager --timeout="${CNPG_TIMEOUT}"
  kubectl_tenant "${tenant}" wait --for=condition=Established \
    crd/clusters.postgresql.cnpg.io --timeout="${CNPG_TIMEOUT}"

  log "deploying ${cluster} in ${tenant}"
  kubectl_tenant "${tenant}" apply \
    -f "${REPO_ROOT}/manifests/cnpg/cluster-${tenant}.yaml" >/dev/null
  retry_for "${CNPG_TIMEOUT}" "${tenant} CloudNativePG cluster" \
    cnpg_cluster_ready "${tenant}"
}

install_all_cnpg() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    install_cnpg_for_tenant "${tenant}"
  done
}

with_psql_client() {
  local tenant="$1"
  shift
  local cluster client
  cluster="$(cnpg_cluster_name "${tenant}")"
  client="cnpg-verify-$(date +%s%N)"

  cat <<YAML | kubectl_tenant "${tenant}" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${client}
  namespace: database
spec:
  restartPolicy: Never
  containers:
    - name: client
      image: ${POSTGRES_IMAGE}
      command: [sleep, "3600"]
      env:
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef:
              name: ${cluster}-app
              key: password
YAML
  kubectl_tenant "${tenant}" -n database wait --for=condition=Ready \
    "pod/${client}" --timeout="${CNPG_TIMEOUT}" >/dev/null

  local rc=0
  kubectl_tenant "${tenant}" -n database exec "${client}" -- \
    psql -v ON_ERROR_STOP=1 -At \
      -h "${cluster}-rw" -U app -d app "$@" || rc=$?
  kubectl_tenant "${tenant}" -n database delete pod "${client}" \
    --ignore-not-found --wait=false >/dev/null
  return "${rc}"
}
