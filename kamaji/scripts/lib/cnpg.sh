#!/usr/bin/env bash

set -Eeuo pipefail
CNPG_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${CNPG_LIB_DIR}/common.sh"

cnpg_cluster_name() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_CNPG_CLUSTER}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_CNPG_CLUSTER}" ;;
    *) die "unknown CNPG tenant: $1" ;;
  esac
}

cnpg_database_name() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_DATABASE}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_DATABASE}" ;;
    *) die "unknown CNPG tenant: $1" ;;
  esac
}

cnpg_database_owner() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_DATABASE_OWNER}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_DATABASE_OWNER}" ;;
    *) die "unknown CNPG tenant: $1" ;;
  esac
}

cnpg_database_marker() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_DATABASE_MARKER}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_DATABASE_MARKER}" ;;
    *) die "unknown CNPG tenant: $1" ;;
  esac
}

cnpg_app_secret_name() {
  printf '%s-app\n' "$(cnpg_cluster_name "$1")"
}

cnpg_expected_crds() {
  cat <<'EOF'
backups.postgresql.cnpg.io
clusterimagecatalogs.postgresql.cnpg.io
clusters.postgresql.cnpg.io
databaseroles.postgresql.cnpg.io
databases.postgresql.cnpg.io
failoverquorums.postgresql.cnpg.io
imagecatalogs.postgresql.cnpg.io
poolers.postgresql.cnpg.io
publications.postgresql.cnpg.io
scheduledbackups.postgresql.cnpg.io
subscriptions.postgresql.cnpg.io
EOF
}

cnpg_crds_ready() {
  local tenant="$1"
  local crd
  while IFS= read -r crd; do
    [[ "$(tenant_kubectl "${tenant}" get "crd/${crd}" \
      -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' \
      2>/dev/null || true)" == True ]] || return 1
  done < <(cnpg_expected_crds)
}

cnpg_operator_ready() {
  local tenant="$1"
  local desired ready updated available generation observed image related_image endpoints
  desired="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.spec.replicas}' \
    2>/dev/null || true)"
  ready="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.status.readyReplicas}' \
    2>/dev/null || true)"
  updated="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.status.updatedReplicas}' \
    2>/dev/null || true)"
  available="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.status.availableReplicas}' \
    2>/dev/null || true)"
  generation="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.metadata.generation}' \
    2>/dev/null || true)"
  observed="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager -o jsonpath='{.status.observedGeneration}' \
    2>/dev/null || true)"
  image="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].image}' \
    2>/dev/null || true)"
  related_image="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get deployment cnpg-controller-manager \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="manager")].env[?(@.name=="OPERATOR_IMAGE_NAME")].value}' \
    2>/dev/null || true)"
  endpoints="$(tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
    get endpoints cnpg-webhook-service \
    -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  [[ "${desired:-0}" -gt 0 \
    && "${ready:-0}" -eq "${desired}" \
    && "${updated:-0}" -eq "${desired}" \
    && "${available:-0}" -eq "${desired}" \
    && "${observed:-0}" -ge "${generation:-1}" \
    && "${image}" == "${CNPG_CONTROLLER_IMAGE}" \
    && "${related_image}" == "${CNPG_CONTROLLER_IMAGE}" \
    && -n "${endpoints}" ]] \
    && cnpg_crds_ready "${tenant}" \
    && tenant_kubectl "${tenant}" get validatingwebhookconfiguration \
      cnpg-validating-webhook-configuration >/dev/null 2>&1 \
    && tenant_kubectl "${tenant}" get mutatingwebhookconfiguration \
      cnpg-mutating-webhook-configuration >/dev/null 2>&1 \
    && tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" \
      get serviceaccount cnpg-manager >/dev/null 2>&1 \
    && tenant_kubectl "${tenant}" get clusterrole cnpg-manager >/dev/null 2>&1 \
    && tenant_kubectl "${tenant}" get clusterrolebinding \
      cnpg-manager-rolebinding >/dev/null 2>&1
}

cnpg_cluster_ready() {
  local tenant="$1"
  local cluster
  cluster="$(cnpg_cluster_name "${tenant}")"
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" \
    get "cluster.postgresql.cnpg.io/${cluster}" -o json 2>/dev/null \
    | CNPG_INSTANCE_COUNT="${CNPG_INSTANCE_COUNT}" python3 -c '
import json, os, sys
status=json.load(sys.stdin).get("status", {})
expected=int(os.environ["CNPG_INSTANCE_COUNT"])
instances=status.get("instances", 0)
ready=status.get("readyInstances", 0)
primary=status.get("currentPrimary", "")
phase=status.get("phase", "")
raise SystemExit(0 if instances == expected and ready == expected
                 and primary and phase == "Cluster in healthy state" else 1)
'
}

cnpg_instance_pods_ready() {
  local tenant="$1"
  local cluster
  cluster="$(cnpg_cluster_name "${tenant}")"
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
    -l "cnpg.io/cluster=${cluster}" -o json 2>/dev/null \
    | CNPG_INSTANCE_COUNT="${CNPG_INSTANCE_COUNT}" python3 -c '
import json, os, sys
items=json.load(sys.stdin).get("items", [])
expected=int(os.environ["CNPG_INSTANCE_COUNT"])
ready=sum(
    1 for pod in items
    if pod.get("status", {}).get("phase") == "Running"
    and any(c.get("type") == "Ready" and c.get("status") == "True"
            for c in pod.get("status", {}).get("conditions", []))
)
raise SystemExit(0 if len(items) == expected and ready == expected else 1)
'
}

cnpg_storage_ready() {
  local tenant="$1"
  local cluster
  cluster="$(cnpg_cluster_name "${tenant}")"
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pvc \
    -l "cnpg.io/cluster=${cluster}" -o json 2>/dev/null \
    | CNPG_INSTANCE_COUNT="${CNPG_INSTANCE_COUNT}" \
      TENANT_STORAGE_CLASS="${TENANT_STORAGE_CLASS}" python3 -c '
import json, os, sys
items=json.load(sys.stdin).get("items", [])
expected=int(os.environ["CNPG_INSTANCE_COUNT"])
raise SystemExit(0 if len(items) == expected
                 and all(i.get("status", {}).get("phase") == "Bound"
                         and i.get("spec", {}).get("volumeName")
                         and i.get("spec", {}).get("storageClassName")
                             == os.environ["TENANT_STORAGE_CLASS"]
                         for i in items)
                 else 1)
'
}

cnpg_placements_ready() {
  local tenant="$1"
  local cluster
  cluster="$(cnpg_cluster_name "${tenant}")"
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" get pods \
    -l "cnpg.io/cluster=${cluster}" -o json 2>/dev/null \
    | CNPG_INSTANCE_COUNT="${CNPG_INSTANCE_COUNT}" python3 -c '
import json, os, sys
items=json.load(sys.stdin).get("items", [])
expected=int(os.environ["CNPG_INSTANCE_COUNT"])
nodes={i.get("spec", {}).get("nodeName", "") for i in items}
nodes.discard("")
raise SystemExit(0 if len(items) == expected and len(nodes) == expected else 1)
'
}

cnpg_tenant_ready() {
  local tenant="$1"
  cnpg_operator_ready "${tenant}" \
    && cnpg_cluster_ready "${tenant}" \
    && cnpg_instance_pods_ready "${tenant}" \
    && cnpg_storage_ready "${tenant}" \
    && cnpg_placements_ready "${tenant}"
}

render_cnpg_operator() {
  local manifest="${LAB_ROOT}/manifests/cnpg/operator.yaml"
  sha256_check "${CNPG_MANIFEST_SHA256}" "${manifest}"
  CNPG_OPERATOR_MANIFEST="${manifest}" \
  CNPG_CONTROLLER_IMAGE="${CNPG_CONTROLLER_IMAGE}" \
  python3 -c '
import os, sys
from pathlib import Path
source=Path(os.environ["CNPG_OPERATOR_MANIFEST"]).read_text(encoding="utf-8")
tag="ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0"
replacements={
    "image: " + tag: "image: " + os.environ["CNPG_CONTROLLER_IMAGE"],
    "value: " + tag: "value: " + os.environ["CNPG_CONTROLLER_IMAGE"],
}
for old, new in replacements.items():
    if source.count(old) != 1:
        raise SystemExit("CNPG operator manifest does not contain one expected " + old)
    source=source.replace(old, new, 1)
sys.stdout.write(source)
'
}

apply_cnpg_operator() {
  local tenant="$1"
  render_cnpg_operator \
    | tenant_kubectl "${tenant}" apply --server-side --force-conflicts \
      --field-manager=kamaji-cnpg-lab -f - >/dev/null
}

tenant_namespace_absent() {
  local tenant="$1"
  local namespace="$2"
  ! tenant_kubectl "${tenant}" get namespace "${namespace}" >/dev/null 2>&1
}

cnpg_operator_absent() {
  local tenant="$1"
  local crd
  while IFS= read -r crd; do
    ! tenant_kubectl "${tenant}" get "crd/${crd}" >/dev/null 2>&1 \
      || return 1
  done < <(cnpg_expected_crds)
}

delete_cnpg_for_tenant() {
  local tenant="$1"
  local cluster crd
  cluster="$(cnpg_cluster_name "${tenant}")"
  [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
    && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1 \
    || return 0

  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" delete pod,secret \
    -l "kamaji.cnpg-vcluster.io/role in (sql-client,cross-auth)" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" delete \
    "cluster.postgresql.cnpg.io/${cluster}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" delete namespace "${DATABASE_NAMESPACE}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" delete \
    mutatingwebhookconfiguration/cnpg-mutating-webhook-configuration \
    validatingwebhookconfiguration/cnpg-validating-webhook-configuration \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  while IFS= read -r crd; do
    tenant_kubectl "${tenant}" delete "crd/${crd}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  done < <(cnpg_expected_crds)
  tenant_kubectl "${tenant}" delete clusterrole \
    cnpg-database-editor-role cnpg-database-viewer-role cnpg-manager \
    cnpg-publication-editor-role cnpg-publication-viewer-role \
    cnpg-subscription-editor-role cnpg-subscription-viewer-role \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" delete clusterrolebinding cnpg-manager-rolebinding \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" delete namespace "${CNPG_NAMESPACE}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  local deadline=$((SECONDS + $(seconds_from_duration "${CNPG_TIMEOUT}")))
  until cnpg_operator_absent "${tenant}" \
    && tenant_namespace_absent "${tenant}" "${DATABASE_NAMESPACE}" \
    && tenant_namespace_absent "${tenant}" "${CNPG_NAMESPACE}"; do
    while IFS= read -r crd; do
      tenant_kubectl "${tenant}" delete "crd/${crd}" \
        --ignore-not-found --wait=false >/dev/null 2>&1 || true
    done < <(cnpg_expected_crds)
    tenant_kubectl "${tenant}" delete namespace \
      "${DATABASE_NAMESPACE}" "${CNPG_NAMESPACE}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if (( SECONDS >= deadline )); then
      die "${tenant}.cleanup: CNPG resources or namespaces did not finish deleting"
    fi
    sleep "${WAIT_POLL_INTERVAL}"
  done

  while IFS= read -r crd; do
    ! tenant_kubectl "${tenant}" get "crd/${crd}" >/dev/null 2>&1 \
      || die "${tenant}.cleanup: CNPG CRD ${crd} remains"
  done < <(cnpg_expected_crds)
  ! tenant_kubectl "${tenant}" get namespace "${DATABASE_NAMESPACE}" >/dev/null 2>&1 \
    || die "${tenant}.cleanup: database namespace remains"
  ! tenant_kubectl "${tenant}" get namespace "${CNPG_NAMESPACE}" >/dev/null 2>&1 \
    || die "${tenant}.cleanup: CNPG operator namespace remains"
}

install_cnpg_for_tenant() {
  local tenant="$1"
  local cluster manifest
  cluster="$(cnpg_cluster_name "${tenant}")"
  manifest="${LAB_ROOT}/manifests/cnpg/cluster-${tenant}.yaml"

  log "installing tenant-owned CloudNativePG ${CNPG_VERSION} in ${tenant}"
  apply_cnpg_operator "${tenant}"
  local -a crds=()
  while IFS= read -r crd; do
    crds+=("crd/${crd}")
  done < <(cnpg_expected_crds)
  tenant_kubectl "${tenant}" wait --for=condition=Established \
    "${crds[@]}" --timeout="${CNPG_TIMEOUT}" >/dev/null
  tenant_kubectl "${tenant}" -n "${CNPG_NAMESPACE}" rollout status \
    deployment/cnpg-controller-manager --timeout="${CNPG_TIMEOUT}" >/dev/null
  wait_for "${CNPG_TIMEOUT}" "${tenant} CNPG operator readiness" \
    cnpg_operator_ready "${tenant}" \
    || die "${tenant}.cnpg-operator: CRDs, webhooks, RBAC, or controller did not become ready"

  log "reconciling ${cluster} with ${CNPG_INSTANCE_COUNT} PostgreSQL instances"
  tenant_kubectl "${tenant}" apply -f "${manifest}" >/dev/null
  wait_for "${CNPG_TIMEOUT}" "${tenant} PostgreSQL cluster readiness" \
    cnpg_tenant_ready "${tenant}" \
    || die "${tenant}.postgresql: ${cluster} did not become healthy with distinct bound instances"
}

install_all_cnpg() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    install_cnpg_for_tenant "${tenant}"
  done
}

cnpg_sql_client_name() {
  printf 'cnpg-verify-%s-%s\n' "$1" "$(date +%s%N)"
}

delete_cnpg_sql_client() {
  local tenant="$1"
  local client="$2"
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" delete pod "${client}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}

create_cnpg_sql_client() {
  local tenant="$1"
  local client="$2"
  local password_secret="$3"
  cat <<EOF | tenant_kubectl "${tenant}" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${client}
  namespace: ${DATABASE_NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
    kamaji.cnpg-vcluster.io/tenant: ${tenant}
    kamaji.cnpg-vcluster.io/role: sql-client
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  containers:
    - name: psql
      image: ${POSTGRES_IMAGE}
      command: [sleep, "3600"]
      env:
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef:
              name: ${password_secret}
              key: password
EOF
  tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" wait \
    --for=condition=Ready "pod/${client}" --timeout="${SQL_CLIENT_TIMEOUT}" \
    >/dev/null
}

cnpg_run_sql() {
  local tenant="$1"
  local sql="$2"
  local cluster client result status=0
  cluster="$(cnpg_cluster_name "${tenant}")"
  client="$(cnpg_sql_client_name "${tenant}")"
  delete_cnpg_sql_client "${tenant}" "${client}"
  create_cnpg_sql_client "${tenant}" "${client}" "$(cnpg_app_secret_name "${tenant}")"
  result="$(
    tenant_kubectl "${tenant}" -n "${DATABASE_NAMESPACE}" exec "${client}" -- \
      env PGCONNECT_TIMEOUT=10 psql -X -qAt -v ON_ERROR_STOP=1 \
      -h "${cluster}-rw" \
      -U "$(cnpg_database_owner "${tenant}")" \
      -d "$(cnpg_database_name "${tenant}")" \
      -c "${sql}"
  )" || status=$?
  delete_cnpg_sql_client "${tenant}" "${client}"
  (( status == 0 )) || return "${status}"
  printf '%s\n' "${result}"
}

cnpg_verify_marker() {
  local tenant="$1"
  local marker result
  marker="$(cnpg_database_marker "${tenant}")"
  result="$(cnpg_run_sql "${tenant}" \
    "SELECT marker FROM kamaji_verification WHERE marker='${marker}';")"
  [[ "$(tail -1 <<<"${result}")" == "${marker}" ]]
}

cnpg_verify_marker_if_present() {
  local tenant="$1"
  local relation
  relation="$(cnpg_run_sql "${tenant}" \
    "SELECT COALESCE(to_regclass('public.kamaji_verification')::text, 'absent');")" \
    || return 1
  if [[ "$(tail -1 <<<"${relation}")" == absent ]]; then
    return 0
  fi
  cnpg_verify_marker "${tenant}"
}
