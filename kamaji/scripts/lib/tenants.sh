#!/usr/bin/env bash

set -Eeuo pipefail
TENANTS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${TENANTS_LIB_DIR}/common.sh"
# shellcheck disable=SC1091
source "${TENANTS_LIB_DIR}/network.sh"

spike_tcp_ref() {
  printf 'tenantcontrolplane.kamaji.clastix.io/%s\n' "${SPIKE_NAME}"
}

services_claiming_vip() {
  local vip="$1"
  management_kubectl get services --all-namespaces -o json \
    | CLAIMED_VIP="${vip}" python3 -c '
import json
import os
import sys

vip = os.environ["CLAIMED_VIP"]
for item in json.load(sys.stdin).get("items", []):
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {}).get("loadBalancer", {})
    annotations = metadata.get("annotations", {})
    claims = set(filter(None, [
        spec.get("loadBalancerIP", ""),
        *spec.get("externalIPs", []),
        *(value.strip() for key in (
            "metallb.io/loadBalancerIPs",
            "metallb.universe.tf/loadBalancerIPs",
        ) for value in annotations.get(key, "").split(",")),
        *(entry.get("ip", "") for entry in status.get("ingress", [])),
    ]))
    if vip in claims:
        print(f"{metadata.get('namespace', 'default')}/{metadata.get('name', '')}")
'
}

final_tenant_state_reason() {
  local tenant
  if [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
    && management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    if management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
      --all-namespaces -o json 2>/dev/null \
      | FINAL_NAMES="${TENANT_NAMES}" \
        FINAL_SCHEMAS="${TENANT_A_SCHEMA} ${TENANT_B_SCHEMA}" \
      python3 -c '
import json, os, sys
items = json.load(sys.stdin).get("items", [])
names = set(os.environ["FINAL_NAMES"].split())
schemas = set(os.environ["FINAL_SCHEMAS"].split())
raise SystemExit(0 if any(
    item["metadata"]["name"] in names
    or item.get("spec", {}).get("dataStoreSchema") in schemas
    for item in items
) else 1)
'; then
      printf 'final TenantControlPlane or schema declaration exists\n'
      return
    fi

    if management_kubectl get secrets --all-namespaces -o json 2>/dev/null \
      | FINAL_SCHEMAS="${TENANT_A_SCHEMA} ${TENANT_B_SCHEMA}" \
      python3 -c '
import base64, json, os, sys
schemas = set(os.environ["FINAL_SCHEMAS"].split())
for item in json.load(sys.stdin).get("items", []):
    value = item.get("data", {}).get("DB_SCHEMA")
    if value:
        try:
            if base64.b64decode(value).decode() in schemas:
                raise SystemExit(0)
        except Exception:
            pass
raise SystemExit(1)
'; then
      printf 'final datastore schema credential exists\n'
      return
    fi
  fi

  for tenant in ${TENANT_NAMES}; do
    if [[ -e "$(tenant_kubeconfig "${tenant}")" ]]; then
      printf 'final tenant kubeconfig exists: %s\n' "${tenant}"
      return
    fi
    if docker ps -aq \
      --filter "$(owned_docker_filter)" \
      --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}" | grep -q .; then
      printf 'final tenant worker exists: %s\n' "${tenant}"
      return
    fi
    if docker volume ls -q \
      --filter "$(owned_docker_filter)" \
      --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}" | grep -q .; then
      printf 'final tenant worker volume exists: %s\n' "${tenant}"
      return
    fi
  done
  return 1
}

refuse_spike_with_final_state() {
  local reason
  if reason="$(final_tenant_state_reason)"; then
    die "spike.final-state-refusal: ${reason}"
  fi
}

render_spike_tenant() {
  ensure_runtime_layout
  mkdir -p -m 0700 "${SPIKE_RUNTIME_DIR}"
  load_management_network
  SPIKE_TEMPLATE="${LAB_ROOT}/config/tenants/spike.yaml" \
  SPIKE_RENDERED_MANIFEST="${SPIKE_RENDERED_MANIFEST}" \
  SPIKE_VIP="${TENANT_A_VIP}" \
  SPIKE_NAMESPACE="${SPIKE_NAMESPACE}" \
  SPIKE_NAME="${SPIKE_NAME}" \
  SPIKE_SCHEMA="${SPIKE_SCHEMA}" \
  SPIKE_DATASTORE_USER="${SPIKE_DATASTORE_USER}" \
  SPIKE_SERVICE_CIDR="${SPIKE_SERVICE_CIDR}" \
  SPIKE_POD_CIDR="${SPIKE_POD_CIDR}" \
  SPIKE_DNS_SERVICE_IP="${SPIKE_DNS_SERVICE_IP}" \
  SPIKE_CLUSTER_DOMAIN="${SPIKE_CLUSTER_DOMAIN}" \
  OWNERSHIP_LABEL="${OWNERSHIP_LABEL}" \
  LAB_PREFIX="${LAB_PREFIX}" \
  KUBERNETES_VERSION="${KUBERNETES_VERSION}" \
  TENANT_API_SERVER_REQUEST_CPU="${TENANT_API_SERVER_REQUEST_CPU}" \
  TENANT_API_SERVER_REQUEST_MEMORY="${TENANT_API_SERVER_REQUEST_MEMORY}" \
  TENANT_API_SERVER_LIMIT_CPU="${TENANT_API_SERVER_LIMIT_CPU}" \
  TENANT_API_SERVER_LIMIT_MEMORY="${TENANT_API_SERVER_LIMIT_MEMORY}" \
  TENANT_CONTROLLER_MANAGER_REQUEST_CPU="${TENANT_CONTROLLER_MANAGER_REQUEST_CPU}" \
  TENANT_CONTROLLER_MANAGER_REQUEST_MEMORY="${TENANT_CONTROLLER_MANAGER_REQUEST_MEMORY}" \
  TENANT_CONTROLLER_MANAGER_LIMIT_CPU="${TENANT_CONTROLLER_MANAGER_LIMIT_CPU}" \
  TENANT_CONTROLLER_MANAGER_LIMIT_MEMORY="${TENANT_CONTROLLER_MANAGER_LIMIT_MEMORY}" \
  TENANT_SCHEDULER_REQUEST_CPU="${TENANT_SCHEDULER_REQUEST_CPU}" \
  TENANT_SCHEDULER_REQUEST_MEMORY="${TENANT_SCHEDULER_REQUEST_MEMORY}" \
  TENANT_SCHEDULER_LIMIT_CPU="${TENANT_SCHEDULER_LIMIT_CPU}" \
  TENANT_SCHEDULER_LIMIT_MEMORY="${TENANT_SCHEDULER_LIMIT_MEMORY}" \
  KONNECTIVITY_AGENT_REPOSITORY="${KONNECTIVITY_AGENT_REPOSITORY}" \
  KONNECTIVITY_AGENT_VERSION_DIGEST="${KONNECTIVITY_AGENT_VERSION_DIGEST}" \
  KONNECTIVITY_SERVER_REPOSITORY="${KONNECTIVITY_SERVER_REPOSITORY}" \
  KONNECTIVITY_SERVER_VERSION_DIGEST="${KONNECTIVITY_SERVER_VERSION_DIGEST}" \
  python3 -c '
import os, re
from pathlib import Path
source = Path(os.environ["SPIKE_TEMPLATE"]).read_text(encoding="utf-8")
names = set(re.findall(r"\$\{([A-Z0-9_]+)\}", source))
missing = sorted(name for name in names if name not in os.environ)
if missing:
    raise SystemExit("missing spike template values: " + ", ".join(missing))
for name in names:
    source = source.replace("${" + name + "}", os.environ[name])
destination = Path(os.environ["SPIKE_RENDERED_MANIFEST"])
destination.write_text(source, encoding="utf-8")
destination.chmod(0o600)
'
}

spike_tcp_ready() {
  local status version endpoint service_ip schema datastore
  status="$(management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
    -o jsonpath='{.status.kubernetesResources.version.status}' 2>/dev/null || true)"
  version="$(management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
    -o jsonpath='{.status.kubernetesResources.version.version}' 2>/dev/null || true)"
  endpoint="$(management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
    -o jsonpath='{.status.controlPlaneEndpoint}' 2>/dev/null || true)"
  service_ip="$(management_kubectl -n "${SPIKE_NAMESPACE}" get service "${SPIKE_NAME}" \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  schema="$(management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
    -o jsonpath='{.status.storage.setup.schema}' 2>/dev/null || true)"
  datastore="$(management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
    -o jsonpath='{.status.storage.dataStoreName}' 2>/dev/null || true)"
  [[ "${status}" == Ready \
    && "${version}" == "${KUBERNETES_VERSION}" \
    && "${endpoint}" == "${SPIKE_VIP}:6443" \
    && "${service_ip}" == "${SPIKE_VIP}" \
    && "${schema}" == "${SPIKE_SCHEMA}" \
    && "${datastore}" == default ]]
}

reconcile_spike_tenant() {
  render_spike_tenant
  if ! management_kubectl apply -f "${SPIKE_RENDERED_MANIFEST}" >/dev/null; then
    die "spike.control-plane: TenantControlPlane manifest was rejected"
  fi
  if ! wait_for "${TENANT_CONTROL_PLANE_TIMEOUT}" \
    "spike TenantControlPlane readiness" spike_tcp_ready; then
    return 1
  fi
}

export_spike_kubeconfig() {
  require_command base64
  local secret_name
  secret_name="$(management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
    -o jsonpath='{.status.kubeconfig.admin.secretName}')"
  [[ -n "${secret_name}" ]] || die "spike.kubeconfig: status did not report an admin Secret"
  management_kubectl -n "${SPIKE_NAMESPACE}" get secret "${secret_name}" \
    -o jsonpath='{.data.admin\.conf}' | base64 -d \
    | write_secret_file "$(tenant_kubeconfig spike)"
  [[ "$(stat -c '%a' "$(tenant_kubeconfig spike)")" == 600 ]] \
    || die "spike.kubeconfig: exported admin.conf is not mode 0600"
  if ! tenant_kubectl spike get --raw=/readyz >/dev/null 2>&1; then
    die "spike.kubeconfig: exported API identity is unreachable"
  fi
  local server
  server="$(tenant_kubectl spike version -o json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
  [[ "${server}" == "${KUBERNETES_VERSION}" ]] \
    || die "spike.kubeconfig: expected ${KUBERNETES_VERSION}, found ${server}"
}

reconcile_spike_bootstrap_rbac() {
  cat <<'EOF' | tenant_kubectl spike apply -f - >/dev/null
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: kubeadm:bootstrap-config-reader
  namespace: kube-system
rules:
  - apiGroups: [""]
    resources: [configmaps]
    resourceNames: [kubeadm-config, kubelet-config]
    verbs: [get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: kubeadm:bootstrap-config-reader
  namespace: kube-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: kubeadm:bootstrap-config-reader
subjects:
  - apiGroup: rbac.authorization.k8s.io
    kind: Group
    name: system:bootstrappers:kubeadm:default-node-token
EOF
}

datastore_used_by_spike() {
  management_kubectl get datastore default \
    -o jsonpath='{.status.usedBy[*]}' 2>/dev/null \
    | tr ' ' '\n' | grep -Fxq "${SPIKE_NAMESPACE}/${SPIKE_NAME}"
}

etcd_maintenance_finished() {
  local pod="$1"
  local phase
  phase="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" \
    -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [[ "${phase}" == Succeeded || "${phase}" == Failed ]]
}

etcd_maintenance() {
  local operation="$1"
  shift
  local pod="${LAB_PREFIX}-etcd-maintenance"
  local manifest="${RUNTIME_DIR}/management/etcd-maintenance.yaml"
  local existing_label phase exit_code

  if management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" >/dev/null 2>&1; then
    existing_label="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" -o json \
      | OWNERSHIP_LABEL="${OWNERSHIP_LABEL}" python3 -c 'import json,os,sys; print(json.load(sys.stdin)["metadata"].get("labels",{}).get(os.environ["OWNERSHIP_LABEL"],""))')"
    [[ "${existing_label}" == "${LAB_PREFIX}" ]] \
      || die "spike.datastore-cleanup: refusing unowned maintenance pod ${pod}"
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete pod "${pod}" \
      --wait=true >/dev/null
  fi

  ETCD_OPERATION="${operation}" \
  ETCD_ARGUMENTS="$(printf '%s\n' "$@" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().splitlines()))')" \
  ETCD_POD="${pod}" \
  ETCD_NAMESPACE="${MANAGEMENT_NAMESPACE}" \
  ETCD_IMAGE="${KAMAJI_ETCD_IMAGE}" \
  OWNERSHIP_LABEL="${OWNERSHIP_LABEL}" \
  LAB_PREFIX="${LAB_PREFIX}" \
  ETCD_MANIFEST="${manifest}" \
  python3 -c '
import json, os
from pathlib import Path
args = [
  "--endpoints=https://kamaji-etcd-0.kamaji-etcd." + os.environ["ETCD_NAMESPACE"] + ".svc.cluster.local:2379",
  "--cacert=/etc/etcd/client/ca.crt",
  "--cert=/etc/etcd/client/tls.crt",
  "--key=/etc/etcd/client/tls.key",
  os.environ["ETCD_OPERATION"],
] + json.loads(os.environ["ETCD_ARGUMENTS"])
payload = {
 "apiVersion": "v1", "kind": "Pod",
 "metadata": {"name": os.environ["ETCD_POD"], "namespace": os.environ["ETCD_NAMESPACE"],
              "labels": {os.environ["OWNERSHIP_LABEL"]: os.environ["LAB_PREFIX"]}},
 "spec": {"restartPolicy": "Never", "containers": [{
   "name": "etcdctl", "image": os.environ["ETCD_IMAGE"], "command": ["etcdctl"], "args": args,
   "volumeMounts": [{"name": "client", "mountPath": "/etc/etcd/client", "readOnly": True}]
 }], "volumes": [{"name": "client", "projected": {"defaultMode": 256, "sources": [
   {"secret": {"name": "kamaji-etcd-ca", "items": [{"key": "tls.crt", "path": "ca.crt"}]}},
   {"secret": {"name": "kamaji-etcd-client-certs", "items": [
     {"key": "tls.crt", "path": "tls.crt"}, {"key": "tls.key", "path": "tls.key"}]}}
 ]}}]}
}
def scalar(value):
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, (int, float)): return str(value)
    return json.dumps(value)
def emit(value, indent=0):
    out=[]; pad=" " * indent
    if isinstance(value, dict):
      for key,item in value.items():
        if isinstance(item,(dict,list)):
          out.append(f"{pad}{key}:")
          out.extend(emit(item,indent+2))
        else: out.append(f"{pad}{key}: {scalar(item)}")
    elif isinstance(value,list):
      for item in value:
        if isinstance(item,dict):
          first=True
          for key,child in item.items():
            prefix=f"{pad}- " if first else f"{pad}  "
            first=False
            if isinstance(child,(dict,list)):
              out.append(f"{prefix}{key}:")
              out.extend(emit(child,indent+4))
            else: out.append(f"{prefix}{key}: {scalar(child)}")
        else: out.append(f"{pad}- {scalar(item)}")
    return out
path=Path(os.environ["ETCD_MANIFEST"])
path.write_text(json.dumps(payload, indent=2)+"\n",encoding="utf-8")
path.chmod(0o600)
'
  management_kubectl apply -f "${manifest}" >/dev/null
  if ! wait_for "${SPIKE_DELETE_TIMEOUT}" "etcd maintenance operation" \
    etcd_maintenance_finished "${pod}"; then
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" logs "${pod}" >&2 || true
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete pod "${pod}" \
      --wait=false >/dev/null 2>&1 || true
    rm -f "${manifest}"
    return 1
  fi
  phase="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" \
    -o jsonpath='{.status.phase}')"
  exit_code="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" \
    -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}')"
  management_kubectl -n "${MANAGEMENT_NAMESPACE}" logs "${pod}" 2>/dev/null || true
  management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete pod "${pod}" \
    --wait=true >/dev/null 2>&1 || true
  rm -f "${manifest}"
  [[ "${phase}" == Succeeded && "${exit_code}" == 0 ]]
}

etcd_prefix_exists() {
  [[ -n "$(etcd_maintenance get "/${1}/" --prefix --keys-only 2>/dev/null)" ]]
}

etcd_user_exists() {
  etcd_maintenance user get "$1" >/dev/null 2>&1
}

etcd_role_exists() {
  etcd_maintenance role get "$1" >/dev/null 2>&1
}

force_delete_spike_datastore_identity() {
  etcd_maintenance del "/${SPIKE_SCHEMA}/" --prefix >/dev/null \
    || die "spike.datastore-cleanup: exact prefix deletion failed"
  if etcd_user_exists "${SPIKE_DATASTORE_USER}"; then
    etcd_maintenance user delete "${SPIKE_DATASTORE_USER}" >/dev/null \
      || die "spike.datastore-cleanup: exact user deletion failed"
  fi
  if etcd_role_exists "${SPIKE_SCHEMA}"; then
    etcd_maintenance role delete "${SPIKE_SCHEMA}" >/dev/null \
      || die "spike.datastore-cleanup: exact role deletion failed"
  fi
}

spike_tcp_absent() {
  ! management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" >/dev/null 2>&1
}

delete_spike_tenant() {
  local credential_secret=""
  if management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" >/dev/null 2>&1; then
    credential_secret="$(management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
      -o jsonpath='{.status.storage.config.secretName}' 2>/dev/null || true)"
    management_kubectl -n "${SPIKE_NAMESPACE}" delete "$(spike_tcp_ref)" \
      --wait=false >/dev/null
    if ! wait_for "${SPIKE_DELETE_TIMEOUT}" "spike TenantControlPlane deletion" \
      spike_tcp_absent; then
      die "spike.cleanup: TenantControlPlane finalizers did not complete"
    fi
  fi

  if etcd_prefix_exists "${SPIKE_SCHEMA}" \
    || etcd_user_exists "${SPIKE_DATASTORE_USER}" \
    || etcd_role_exists "${SPIKE_SCHEMA}"; then
    force_delete_spike_datastore_identity
  fi

  [[ -z "${credential_secret}" ]] \
    || ! management_kubectl -n "${SPIKE_NAMESPACE}" get secret "${credential_secret}" >/dev/null 2>&1 \
    || die "spike.cleanup: datastore credential Secret remains"
  ! datastore_used_by_spike \
    || die "spike.cleanup: DataStore/default still reports spike in status.usedBy"
  ! etcd_prefix_exists "${SPIKE_SCHEMA}" \
    || die "spike.cleanup: exact etcd prefix remains"
  ! etcd_user_exists "${SPIKE_DATASTORE_USER}" \
    || die "spike.cleanup: exact etcd user remains"
  ! etcd_role_exists "${SPIKE_SCHEMA}" \
    || die "spike.cleanup: exact etcd role remains"
  [[ "$(management_kubectl get datastore default -o jsonpath='{.status.ready}')" == true ]] \
    || die "spike.cleanup: shared DataStore/default became unhealthy"

  management_kubectl delete namespace "${SPIKE_NAMESPACE}" \
    --ignore-not-found --wait=true >/dev/null
  rm -f "$(tenant_kubeconfig spike)"
}
