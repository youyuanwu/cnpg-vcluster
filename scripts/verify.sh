#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/platform.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenant.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/cnpg.sh"

CURRENT_TENANT=""
VERIFY_SUCCEEDED=0

diagnose_failure() {
  "${SCRIPT_DIR}/diagnose.sh" "${1:-}" >&2 || true
}

verify_exit() {
  local rc=$?
  trap - EXIT
  if [[ "${rc}" -ne 0 && "${VERIFY_SUCCEEDED}" -eq 0 ]]; then
    diagnose_failure "${CURRENT_TENANT}"
  fi
  exit "${rc}"
}
trap verify_exit EXIT

assert_equals() {
  local expected="$1"
  local actual="$2"
  local description="$3"
  [[ "${actual}" == "${expected}" ]] \
    || die "${description}: expected ${expected}, got ${actual}"
}

verify_platform_and_topology() {
  kubectl_host get --raw=/readyz >/dev/null
  platform_ready || die "vCluster Platform is not ready"

  local tenant nodes
  for tenant in ${TENANT_NAMES}; do
    platform_has_tenant "${tenant}" || die "${tenant} is not linked to Platform"
    nodes="$(kubectl_tenant "${tenant}" get nodes --no-headers | wc -l)"
    assert_equals "${WORKERS_PER_TENANT}" "${nodes}" "${tenant} worker count"
    tenant_expected_nodes_ready "${tenant}" || die "${tenant} workers are not all Ready"
  done

  local overlap
  overlap="$(comm -12 \
    <(kubectl_tenant tenant-a get nodes -o name | sort) \
    <(kubectl_tenant tenant-b get nodes -o name | sort))"
  [[ -z "${overlap}" ]] || die "worker names overlap across tenants: ${overlap}"
}

verify_host_isolation() {
  ! kubectl_host get crd clusters.postgresql.cnpg.io >/dev/null 2>&1 \
    || die "CloudNativePG CRDs unexpectedly exist in the central cluster"
  ! kubectl_host get validatingwebhookconfiguration \
    cnpg-validating-webhook-configuration >/dev/null 2>&1 \
    || die "CloudNativePG validating webhook unexpectedly exists in the central cluster"
  ! kubectl_host get mutatingwebhookconfiguration \
    cnpg-mutating-webhook-configuration >/dev/null 2>&1 \
    || die "CloudNativePG mutating webhook unexpectedly exists in the central cluster"
  ! kubectl_host get clusterrole cnpg-manager >/dev/null 2>&1 \
    || die "CloudNativePG manager RBAC unexpectedly exists in the central cluster"
  ! kubectl_host get clusterrolebinding cnpg-manager-rolebinding >/dev/null 2>&1 \
    || die "CloudNativePG manager binding unexpectedly exists in the central cluster"
  [[ -z "$(kubectl_host get services -A --no-headers 2>/dev/null \
    | awk '$2 == "cnpg-webhook-service" {print}')" ]] \
    || die "CloudNativePG webhook Service unexpectedly exists in the central cluster"

  local kind_nodes
  kind_nodes="$(kubectl_host get nodes -o name)"
  ! grep -q "${LAB_PREFIX}-tenant-" <<<"${kind_nodes}" \
    || die "private tenant workers unexpectedly exist in the central API"

  local resource
  for resource in pods services pvc pv; do
    [[ -z "$(kubectl_host get "${resource}" -A \
      -l cnpg.io/cluster --no-headers 2>/dev/null)" ]] \
      || die "tenant CNPG ${resource} unexpectedly exist in the central API"
  done
}

verify_tenant_networking_and_no_sync() {
  local tenant="$1"
  local namespace="cnpg-verify-${tenant}"
  local name="${tenant}-canary"
  local marker="${tenant}-network-marker"
  local server_node client_node result

  kubectl_tenant "${tenant}" delete namespace "${namespace}" \
    --ignore-not-found --wait=false >/dev/null
  kubectl_tenant "${tenant}" wait --for=delete "namespace/${namespace}" \
    --timeout=60s >/dev/null 2>&1 || true

  cat <<YAML | kubectl_tenant "${tenant}" apply -f - >/dev/null
apiVersion: v1
kind: Namespace
metadata:
  name: ${namespace}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${name}-data
  namespace: ${namespace}
  labels:
    cnpg-vcluster.io/verification: ${tenant}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 64Mi
---
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${namespace}
  labels:
    app: ${name}
    cnpg-vcluster.io/verification: ${tenant}
spec:
  containers:
    - name: http
      image: ${VERIFY_IMAGE}
      command: [sh, -c]
      args: ["echo '${marker}' > /data/index.html && httpd -f -p 8080 -h /data"]
      ports:
        - containerPort: 8080
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ${name}-data
---
apiVersion: v1
kind: Service
metadata:
  name: ${name}
  namespace: ${namespace}
  labels:
    cnpg-vcluster.io/verification: ${tenant}
spec:
  selector:
    app: ${name}
  ports:
    - port: 8080
      targetPort: 8080
YAML

  kubectl_tenant "${tenant}" -n "${namespace}" wait --for=condition=Ready \
    "pod/${name}" --timeout="${PVC_TIMEOUT}" >/dev/null
  kubectl_tenant "${tenant}" -n "${namespace}" get pvc "${name}-data" \
    -o jsonpath='{.status.phase}' | grep -qx Bound
  server_node="$(kubectl_tenant "${tenant}" -n "${namespace}" get pod "${name}" \
    -o jsonpath='{.spec.nodeName}')"
  client_node="$(kubectl_tenant "${tenant}" get nodes -o name \
    | sed 's#node/##' | grep -vx "${server_node}" | head -1)"
  [[ -n "${client_node}" ]] || die "could not choose cross-node client for ${tenant}"

  kubectl_tenant "${tenant}" -n "${namespace}" run "${name}-client" \
    --restart=Never \
    --image="${VERIFY_IMAGE}" \
    --overrides="{\"spec\":{\"nodeName\":\"${client_node}\"}}" \
    --command -- sh -c \
    "wget -qO- http://${name}.${namespace}.svc.cluster.local:8080" >/dev/null
  wait_for "${NETWORK_VERIFY_TIMEOUT}" "${tenant} cross-node DNS and Service check" \
    bash -c "[[ \$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl --request-timeout=30s -n '${namespace}' get pod '${name}-client' -o jsonpath='{.status.phase}' 2>/dev/null) == Succeeded ]]" \
    || die "${tenant} cross-node DNS/Service canary failed"
  result="$(kubectl_tenant "${tenant}" -n "${namespace}" logs "${name}-client")"
  assert_equals "${marker}" "${result}" "${tenant} cross-node Service marker"

  local resource
  for resource in pods services pvc; do
    [[ -z "$(kubectl_host get "${resource}" -A \
      -l "cnpg-vcluster.io/verification=${tenant}" --no-headers 2>/dev/null)" ]] \
      || die "${tenant} verification ${resource} unexpectedly synchronized to kind"
  done
  ! kubectl_host get namespace "${namespace}" >/dev/null 2>&1 \
    || die "${tenant} verification namespace unexpectedly exists in kind"

  kubectl_tenant "${tenant}" delete namespace "${namespace}" \
    --wait=false >/dev/null
}

verify_tenant_components() {
  local tenant="$1"
  local cluster cluster_count pod_count default_count pvc_count pv_count node_count
  cluster="$(cnpg_cluster_name "${tenant}")"

  default_count="$(kubectl_tenant "${tenant}" get storageclass -o json \
    | python3 -c '
import json, sys
items=json.load(sys.stdin).get("items", [])
keys=("storageclass.kubernetes.io/is-default-class", "storageclass.beta.kubernetes.io/is-default-class")
print(sum(1 for item in items if any(item.get("metadata", {}).get("annotations", {}).get(k) == "true" for k in keys)))
')"
  assert_equals 1 "${default_count}" "${tenant} default StorageClass count"

  kubectl_tenant "${tenant}" -n kube-system get pods -o json \
    | python3 -c '
import json, sys
items=json.load(sys.stdin).get("items", [])
need=("coredns", "flannel", "kube-proxy", "local-path")
names=[item["metadata"]["name"] for item in items if item.get("status", {}).get("phase") == "Running"]
missing=[part for part in need if not any(part in name for name in names)]
raise SystemExit("missing running system components: "+", ".join(missing) if missing else 0)
'

  kubectl_tenant "${tenant}" get crd clusters.postgresql.cnpg.io >/dev/null
  kubectl_tenant "${tenant}" get validatingwebhookconfiguration \
    cnpg-validating-webhook-configuration >/dev/null
  kubectl_tenant "${tenant}" get mutatingwebhookconfiguration \
    cnpg-mutating-webhook-configuration >/dev/null
  kubectl_tenant "${tenant}" -n cnpg-system get service cnpg-webhook-service >/dev/null
  kubectl_tenant "${tenant}" -n cnpg-system get serviceaccount cnpg-manager >/dev/null
  kubectl_tenant "${tenant}" get clusterrole cnpg-manager >/dev/null
  kubectl_tenant "${tenant}" get clusterrolebinding cnpg-manager-rolebinding >/dev/null
  cnpg_cluster_ready "${tenant}" || die "${cluster} is not fully ready"

  cluster_count="$(kubectl_tenant "${tenant}" -n database get clusters.postgresql.cnpg.io \
    --no-headers | wc -l)"
  assert_equals 1 "${cluster_count}" "${tenant} CNPG Cluster count"
  pod_count="$(kubectl_tenant "${tenant}" -n database get pods \
    -l "cnpg.io/cluster=${cluster}" --no-headers | wc -l)"
  assert_equals 3 "${pod_count}" "${tenant} PostgreSQL instance pod count"

  wait_for "${PVC_TIMEOUT}" "${tenant} CNPG storage binding" \
    bash -c "[[ \$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl --request-timeout=30s -n database get pvc -l 'cnpg.io/cluster=${cluster}' --no-headers 2>/dev/null | awk '\$2 == \"Bound\" {count++} END {print count + 0}') -eq 3 ]]" \
    || die "${tenant} CNPG claims did not bind within ${PVC_TIMEOUT}"
  pvc_count="$(kubectl_tenant "${tenant}" -n database get pvc \
    -l "cnpg.io/cluster=${cluster}" --no-headers | awk '$2 == "Bound" {count++} END {print count + 0}')"
  assert_equals 3 "${pvc_count}" "${tenant} bound CNPG PVC count"
  pv_count="$(kubectl_tenant "${tenant}" get pv \
    -l "cnpg.io/cluster=${cluster}" --no-headers | awk '$5 == "Bound" {count++} END {print count + 0}')"
  if [[ "${pv_count}" -eq 0 ]]; then
    pv_count="$(kubectl_tenant "${tenant}" get pv -o json \
      | python3 -c '
import json, sys
cluster=sys.argv[1]
items=json.load(sys.stdin).get("items", [])
print(sum(1 for item in items if item.get("spec", {}).get("claimRef", {}).get("name", "").startswith(cluster) and item.get("status", {}).get("phase") == "Bound"))
' "${cluster}")"
  fi
  assert_equals 3 "${pv_count}" "${tenant} bound CNPG PV count"

  node_count="$(kubectl_tenant "${tenant}" -n database get pods \
    -l "cnpg.io/cluster=${cluster}" -o json \
    | python3 -c '
import json, sys
items=json.load(sys.stdin).get("items", [])
print(len({item.get("spec", {}).get("nodeName") for item in items if item.get("spec", {}).get("nodeName")}))
')"
  assert_equals 3 "${node_count}" "${tenant} distinct PostgreSQL node count"
}

write_and_read_marker() {
  local tenant="$1"
  local marker="${tenant}-private-node-marker"
  local result
  result="$(with_psql_client "${tenant}" -c \
    "CREATE TABLE IF NOT EXISTS verification(marker text PRIMARY KEY); INSERT INTO verification(marker) VALUES ('${marker}') ON CONFLICT DO NOTHING; SELECT marker FROM verification WHERE marker='${marker}';")"
  assert_equals "${marker}" "$(tail -1 <<<"${result}")" "${tenant} SQL marker"
}

verify_marker() {
  local tenant="$1"
  local marker="${tenant}-private-node-marker"
  local result
  result="$(with_psql_client "${tenant}" -c \
    "SELECT marker FROM verification WHERE marker='${marker}';")"
  assert_equals "${marker}" "$(tail -1 <<<"${result}")" "${tenant} persisted SQL marker"
}

restart_replica() {
  local tenant="$1"
  local cluster primary replica uid pvc pv new_pvc new_pv
  cluster="$(cnpg_cluster_name "${tenant}")"
  primary="$(kubectl_tenant "${tenant}" -n database get cluster "${cluster}" \
    -o jsonpath='{.status.currentPrimary}')"
  replica="$(kubectl_tenant "${tenant}" -n database get pods \
    -l "cnpg.io/cluster=${cluster}" -o json \
    | python3 -c '
import json, sys
primary=sys.argv[1]
names=sorted(item["metadata"]["name"] for item in json.load(sys.stdin).get("items", []) if item["metadata"]["name"] != primary)
print(names[0] if names else "")
' "${primary}")"
  [[ -n "${replica}" ]] || die "could not select a replica for ${tenant}"
  uid="$(kubectl_tenant "${tenant}" -n database get pod "${replica}" -o jsonpath='{.metadata.uid}')"
  pvc="$(kubectl_tenant "${tenant}" -n database get pod "${replica}" -o json \
    | python3 -c '
import json, sys
volumes=json.load(sys.stdin).get("spec", {}).get("volumes", [])
claims=[v["persistentVolumeClaim"]["claimName"] for v in volumes if "persistentVolumeClaim" in v]
print(claims[0] if claims else "")
')"
  [[ -n "${pvc}" ]] || die "could not find replica PVC for ${tenant}"
  pv="$(kubectl_tenant "${tenant}" -n database get pvc "${pvc}" \
    -o jsonpath='{.spec.volumeName}')"
  [[ -n "${pv}" ]] || die "could not find replica PV for ${tenant}"

  kubectl_tenant "${tenant}" -n database delete pod "${replica}" --wait=false >/dev/null
  wait_for "${POD_RESTART_TIMEOUT}" "${tenant} replica restart" \
    bash -c "new_uid=\$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl -n database get pod '${replica}' -o jsonpath='{.metadata.uid}' 2>/dev/null) && [[ -n \"\$new_uid\" && \"\$new_uid\" != '${uid}' ]] && KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl -n database wait --for=condition=Ready pod/'${replica}' --timeout=20s >/dev/null 2>&1" \
    || {
      diagnose_failure "${tenant}"
      die "${tenant} replica did not restart within ${POD_RESTART_TIMEOUT}"
    }
  kubectl_tenant "${tenant}" -n database get pvc "${pvc}" \
    -o jsonpath='{.status.phase}' | grep -qx Bound
  new_pvc="$(kubectl_tenant "${tenant}" -n database get pod "${replica}" -o json \
    | python3 -c '
import json, sys
volumes=json.load(sys.stdin).get("spec", {}).get("volumes", [])
claims=[v["persistentVolumeClaim"]["claimName"] for v in volumes if "persistentVolumeClaim" in v]
print(claims[0] if claims else "")
')"
  assert_equals "${pvc}" "${new_pvc}" "${tenant} restarted replica PVC"
  new_pv="$(kubectl_tenant "${tenant}" -n database get pvc "${new_pvc}" \
    -o jsonpath='{.spec.volumeName}')"
  assert_equals "${pv}" "${new_pv}" "${tenant} restarted replica PV"
  retry_for "${CNPG_TIMEOUT}" "${tenant} cluster after replica restart" cnpg_cluster_ready "${tenant}"
  verify_marker "${tenant}"
}

failover_primary() {
  local tenant="$1"
  local cluster old_primary
  cluster="$(cnpg_cluster_name "${tenant}")"
  old_primary="$(kubectl_tenant "${tenant}" -n database get cluster "${cluster}" \
    -o jsonpath='{.status.currentPrimary}')"
  [[ -n "${old_primary}" ]] || die "could not identify ${tenant} primary"

  kubectl_tenant "${tenant}" -n database delete pod "${old_primary}" --wait=false >/dev/null
  wait_for "${FAILOVER_TIMEOUT}" "${tenant} primary failover" \
    bash -c "new_primary=\$(KUBECONFIG='$(tenant_kubeconfig "${tenant}")' kubectl -n database get cluster '${cluster}' -o jsonpath='{.status.currentPrimary}' 2>/dev/null) && [[ -n \"\$new_primary\" && \"\$new_primary\" != '${old_primary}' ]]" \
    || {
      diagnose_failure "${tenant}"
      die "${tenant} did not promote a different primary within ${FAILOVER_TIMEOUT}"
    }
  retry_for "${CNPG_TIMEOUT}" "${tenant} cluster after failover" cnpg_cluster_ready "${tenant}"
  verify_marker "${tenant}"
}

verify_cross_tenant_identity() {
  local kind
  for kind in nodes pvc pv; do
    local overlap
    overlap="$(comm -12 \
      <(kubectl_tenant tenant-a get "${kind}" -A -o name | sort) \
      <(kubectl_tenant tenant-b get "${kind}" -A -o name | sort))"
    [[ -z "${overlap}" ]] || die "${kind} identities overlap across tenants: ${overlap}"
  done
  local clusters_a clusters_b
  clusters_a="$(kubectl_tenant tenant-a -n database get clusters.postgresql.cnpg.io -o name | sort)"
  clusters_b="$(kubectl_tenant tenant-b -n database get clusters.postgresql.cnpg.io -o name | sort)"
  [[ -z "$(comm -12 <(printf '%s\n' "${clusters_a}") <(printf '%s\n' "${clusters_b}"))" ]] \
    || die "database Cluster identities overlap across tenants"
  local result
  result="$(with_psql_client tenant-a -c \
    "SELECT marker FROM verification WHERE marker='tenant-b-private-node-marker';")"
  [[ -z "${result}" ]] || die "tenant B marker is visible in tenant A"
  result="$(with_psql_client tenant-b -c \
    "SELECT marker FROM verification WHERE marker='tenant-a-private-node-marker';")"
  [[ -z "${result}" ]] || die "tenant A marker is visible in tenant B"
}

main() {
  [[ ! -s "${RUNTIME_DIR}/blocker" ]] \
    || die "bootstrap is blocked: $(tr '\n' ' ' <"${RUNTIME_DIR}/blocker")"
  verify_platform_and_topology
  verify_host_isolation

  local tenant
  for tenant in ${TENANT_NAMES}; do
    CURRENT_TENANT="${tenant}"
    verify_tenant_networking_and_no_sync "${tenant}"
    verify_tenant_components "${tenant}" || {
      diagnose_failure "${tenant}"
      exit 1
    }
    write_and_read_marker "${tenant}"
  done

  verify_cross_tenant_identity
  for tenant in ${TENANT_NAMES}; do
    CURRENT_TENANT="${tenant}"
    restart_replica "${tenant}"
    failover_primary "${tenant}"
  done
  VERIFY_SUCCEEDED=1
  log "all private-node, storage, CloudNativePG, SQL, restart, and failover checks passed"
}

main "$@"
