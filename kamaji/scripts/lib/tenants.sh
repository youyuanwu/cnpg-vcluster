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

tenant_namespace() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_NAMESPACE}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_NAMESPACE}" ;;
    spike) printf '%s\n' "${SPIKE_NAMESPACE}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_schema() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_SCHEMA}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_SCHEMA}" ;;
    spike) printf '%s\n' "${SPIKE_SCHEMA}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_datastore_user() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_DATASTORE_USER}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_DATASTORE_USER}" ;;
    spike) printf '%s\n' "${SPIKE_DATASTORE_USER}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_vip() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_VIP}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_VIP}" ;;
    spike) printf '%s\n' "${SPIKE_VIP}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_pod_cidr() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_POD_CIDR}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_POD_CIDR}" ;;
    spike) printf '%s\n' "${SPIKE_POD_CIDR}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_service_cidr() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_SERVICE_CIDR}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_SERVICE_CIDR}" ;;
    spike) printf '%s\n' "${SPIKE_SERVICE_CIDR}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_dns_service_ip() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_DNS_SERVICE_IP}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_DNS_SERVICE_IP}" ;;
    spike) printf '%s\n' "${SPIKE_DNS_SERVICE_IP}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_cluster_domain() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_CLUSTER_DOMAIN}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_CLUSTER_DOMAIN}" ;;
    spike) printf '%s\n' "${SPIKE_CLUSTER_DOMAIN}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_storage_path() {
  case "$1" in
    tenant-a) printf '%s\n' "${TENANT_A_STORAGE_PATH}" ;;
    tenant-b) printf '%s\n' "${TENANT_B_STORAGE_PATH}" ;;
    spike) printf '%s\n' "${SPIKE_STORAGE_PATH}" ;;
    *) die "unknown tenant: $1" ;;
  esac
}

tenant_tcp_ref() {
  case "$1" in
    spike) spike_tcp_ref ;;
    *) printf 'tenantcontrolplane.kamaji.clastix.io/%s\n' "$1" ;;
  esac
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
        print(metadata.get("namespace", "default") + "/" + metadata.get("name", ""))
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
  reconcile_tenant_bootstrap_rbac spike
}

reconcile_tenant_bootstrap_rbac() {
  local tenant="$1"
  cat <<'EOF' | tenant_kubectl "${tenant}" apply -f - >/dev/null
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

render_final_tenant() {
  local tenant="$1"
  local template="${LAB_ROOT}/config/tenants/${tenant}.yaml"
  local destination
  destination="$(tenant_rendered_manifest "${tenant}")"
  ensure_runtime_layout
  mkdir -p -m 0700 "$(tenant_runtime_dir "${tenant}")"
  TENANT_TEMPLATE="${template}" \
  TENANT_RENDERED_MANIFEST="${destination}" \
  TENANT_A_VIP="${TENANT_A_VIP}" \
  TENANT_B_VIP="${TENANT_B_VIP}" \
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
source = Path(os.environ["TENANT_TEMPLATE"]).read_text(encoding="utf-8")
names = set(re.findall(r"\$\{([A-Z0-9_]+)\}", source))
missing = sorted(name for name in names if name not in os.environ)
if missing:
    raise SystemExit("missing tenant template values: " + ", ".join(missing))
for name in names:
    source = source.replace("${" + name + "}", os.environ[name])
destination = Path(os.environ["TENANT_RENDERED_MANIFEST"])
destination.write_text(source, encoding="utf-8")
destination.chmod(0o600)
'
}

final_tenant_tcp_ready() {
  local tenant="$1"
  local namespace status version endpoint service_ip schema datastore
  namespace="$(tenant_namespace "${tenant}")"
  status="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.kubernetesResources.version.status}' 2>/dev/null || true)"
  version="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.kubernetesResources.version.version}' 2>/dev/null || true)"
  endpoint="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.controlPlaneEndpoint}' 2>/dev/null || true)"
  service_ip="$(management_kubectl -n "${namespace}" get service "${tenant}" \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  schema="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.storage.setup.schema}' 2>/dev/null || true)"
  datastore="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.storage.dataStoreName}' 2>/dev/null || true)"
  [[ "${status}" == Ready \
    && "${version}" == "${KUBERNETES_VERSION}" \
    && "${endpoint}" == "$(tenant_vip "${tenant}"):6443" \
    && "${service_ip}" == "$(tenant_vip "${tenant}")" \
    && "${schema}" == "$(tenant_schema "${tenant}")" \
    && "${datastore}" == default ]]
}

tenant_control_plane_container_statuses() {
  local tenant="$1"
  local namespace payload
  namespace="$(tenant_namespace "${tenant}")"
  payload="$(
    management_kubectl -n "${namespace}" get pods -o json 2>/dev/null || true
  )"
  [[ -n "${payload}" ]] || return 0
  TENANT="${tenant}" python3 -c '
import json, os, sys
names = {
    "kube-apiserver",
    "kube-controller-manager",
    "kube-scheduler",
    "konnectivity-server",
}
for pod in json.load(sys.stdin).get("items", []):
    pod_name = pod.get("metadata", {}).get("name", "")
    for status in pod.get("status", {}).get("containerStatuses", []):
        name = status.get("name", "")
        if name not in names:
            continue
        state = status.get("state", {})
        if state.get("running") is not None:
            current = "running"
        elif state.get("waiting") is not None:
            current = "waiting:" + state["waiting"].get("reason", "unknown")
        elif state.get("terminated") is not None:
            current = "terminated:" + state["terminated"].get("reason", "unknown")
        else:
            current = "unknown"
        terminated = status.get("lastState", {}).get("terminated", {})
        print(
            "tenant={tenant} pod={pod} container={container} restarts={restarts} "
            "state={state} last_reason={reason} last_exit={exit_code}".format(
                tenant=os.environ["TENANT"],
                pod=pod_name,
                container=name,
                restarts=status.get("restartCount", 0),
                state=current,
                reason=terminated.get("reason", "none"),
                exit_code=terminated.get("exitCode", "none"),
            )
        )
' <<<"${payload}"
}

tenant_control_plane_oom_evidence() {
  tenant_control_plane_container_statuses "$1" \
    | grep -E 'state=terminated:OOMKilled|last_reason=OOMKilled'
}

all_tenant_control_plane_oom_evidence() {
  local tenant
  for tenant in ${TENANT_NAMES}; do
    tenant_control_plane_oom_evidence "${tenant}" || true
  done
}

export_tenant_kubeconfig() {
  local tenant="$1"
  local namespace secret_name server
  namespace="$(tenant_namespace "${tenant}")"
  secret_name="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.status.kubeconfig.admin.secretName}')"
  [[ -n "${secret_name}" ]] \
    || die "${tenant}.kubeconfig: status did not report an admin Secret"
  management_kubectl -n "${namespace}" get secret "${secret_name}" \
    -o jsonpath='{.data.admin\.conf}' | base64 -d \
    | write_secret_file "$(tenant_kubeconfig "${tenant}")"
  [[ "$(stat -c '%a' "$(tenant_kubeconfig "${tenant}")")" == 600 ]] \
    || die "${tenant}.kubeconfig: exported admin.conf is not mode 0600"
  tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1 \
    || die "${tenant}.kubeconfig: tenant API is unreachable"
  server="$(tenant_kubectl "${tenant}" version -o json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
  [[ "${server}" == "${KUBERNETES_VERSION}" ]] \
    || die "${tenant}.kubeconfig: expected ${KUBERNETES_VERSION}, found ${server}"
}

reconcile_final_tenant() {
  local tenant="$1"
  render_final_tenant "${tenant}"
  management_kubectl apply -f "$(tenant_rendered_manifest "${tenant}")" >/dev/null \
    || die "${tenant}.control-plane: TenantControlPlane manifest was rejected"
  wait_for "${TENANT_CONTROL_PLANE_TIMEOUT}" "${tenant} TenantControlPlane readiness" \
    final_tenant_tcp_ready "${tenant}" \
    || die "${tenant}.control-plane: ${KUBERNETES_VERSION} control plane did not become Ready"
  export_tenant_kubeconfig "${tenant}"
  reconcile_tenant_bootstrap_rbac "${tenant}"
}

tenant_ca_fingerprint() {
  local tenant="$1"
  tenant_kubectl "${tenant}" config view --raw -o json \
    | python3 -c '
import base64, hashlib, json, sys
data=json.load(sys.stdin)["clusters"][0]["cluster"]["certificate-authority-data"]
print(hashlib.sha256(base64.b64decode(data)).hexdigest())
'
}

verify_tenant_identity_separation() {
  local a_server b_server a_ca b_ca
  a_server="$(tenant_kubectl tenant-a config view --raw \
    -o jsonpath='{.clusters[0].cluster.server}')"
  b_server="$(tenant_kubectl tenant-b config view --raw \
    -o jsonpath='{.clusters[0].cluster.server}')"
  a_ca="$(tenant_ca_fingerprint tenant-a)"
  b_ca="$(tenant_ca_fingerprint tenant-b)"
  [[ "${a_server}" == "https://${TENANT_A_VIP}:6443" \
    && "${b_server}" == "https://${TENANT_B_VIP}:6443" \
    && "${a_server}" != "${b_server}" \
    && -n "${a_ca}" && -n "${b_ca}" && "${a_ca}" != "${b_ca}" ]] \
    || die "tenant.identity: endpoints or certificate authorities are not distinct"
}

tenant_kube_proxy_conntrack_is_zero() {
  local tenant="$1"
  tenant_kubectl "${tenant}" -n kube-system get configmap kube-proxy \
    -o jsonpath='{.data.config\.conf}' 2>/dev/null \
    | grep -Eq "^  maxPerCore: ${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}$"
}

tenant_reconciliation_pause_value() {
  local tenant="$1"
  management_kubectl -n "$(tenant_namespace "${tenant}")" \
    get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.metadata.annotations.kamaji\.clastix\.io/paused}' \
    2>/dev/null || true
}

tenant_reconciliation_remediation_revision() {
  local tenant="$1"
  management_kubectl -n "$(tenant_namespace "${tenant}")" \
    get "$(tenant_tcp_ref "${tenant}")" \
    -o jsonpath='{.metadata.annotations.kamaji\.cnpg-vcluster\.io/kube-proxy-remediation}' \
    2>/dev/null || true
}

tenant_reconciliation_is_paused() {
  [[ "$(tenant_reconciliation_pause_value "$1")" == true ]]
}

tenant_kube_proxy_steady_state_is_preserved() {
  local tenant="$1"
  tenant_reconciliation_is_paused "${tenant}" \
    && [[ "$(tenant_reconciliation_remediation_revision "${tenant}")" \
      == "${COMPATIBILITY_REVISION}" ]] \
    && tenant_kube_proxy_conntrack_is_zero "${tenant}"
}

configure_tenant_kube_proxy_conntrack() {
  local tenant="$1"
  local namespace manifest
  namespace="$(tenant_namespace "${tenant}")"
  manifest="$(tenant_runtime_dir "${tenant}")/kube-proxy-configmap.json"

  management_kubectl -n "${namespace}" annotate "$(tenant_tcp_ref "${tenant}")" \
    kamaji.clastix.io/paused=true \
    kamaji.cnpg-vcluster.io/kube-proxy-remediation="${COMPATIBILITY_REVISION}" \
    --overwrite >/dev/null
  sleep 2

  tenant_kubectl "${tenant}" -n kube-system get configmap kube-proxy -o json >"${manifest}"
  KUBE_PROXY_MANIFEST="${manifest}" \
  KUBE_PROXY_VALUE="${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}" \
  python3 -c '
import json, os, re
path=os.environ["KUBE_PROXY_MANIFEST"]
data=json.load(open(path, encoding="utf-8"))
config=data["data"]["config.conf"]
config, count=re.subn(
    r"(?m)^  maxPerCore:.*$",
    "  maxPerCore: " + os.environ["KUBE_PROXY_VALUE"],
    config,
)
if count != 1:
    raise SystemExit("kube-proxy ConfigMap has no unique conntrack.maxPerCore field")
data["data"]["config.conf"]=config
metadata=data["metadata"]
for key in ("managedFields", "resourceVersion", "uid", "creationTimestamp"):
    metadata.pop(key, None)
data.pop("status", None)
open(path, "w", encoding="utf-8").write(json.dumps(data))
'
  chmod 0600 "${manifest}"
  tenant_kubectl "${tenant}" replace -f "${manifest}" >/dev/null
  rm -f "${manifest}"
  tenant_kube_proxy_conntrack_is_zero "${tenant}" \
    || die "${tenant}.kube-proxy: conntrack.maxPerCore was not set to 0"

  sleep "${KUBE_PROXY_RECONCILE_PROOF_SECONDS}"
  tenant_kube_proxy_conntrack_is_zero "${tenant}" \
    || die "${tenant}.kube-proxy: Kamaji reverted conntrack.maxPerCore"
  [[ "$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
      -o jsonpath='{.metadata.annotations.kamaji\.clastix\.io/paused}')" == true ]] \
    || die "${tenant}.kube-proxy: reconciliation pause was not retained"
  {
    printf 'compatibility_revision=%s\n' "${COMPATIBILITY_REVISION}"
    printf 'conntrack_max_per_core=%s\n' "${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}"
    printf 'kamaji_reconciliation=intentionally-paused-steady-state\n'
    printf 'immediate_reversion=not-observed\n'
  } | write_secret_file "$(tenant_kube_proxy_evidence "${tenant}")"
}

unpause_tenant_reconciliation() {
  local tenant="$1"
  local namespace
  namespace="$(tenant_namespace "${tenant}")"
  if management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    >/dev/null 2>&1; then
    management_kubectl -n "${namespace}" annotate "$(tenant_tcp_ref "${tenant}")" \
      kamaji.clastix.io/paused- \
      kamaji.cnpg-vcluster.io/kube-proxy-remediation- \
      >/dev/null 2>&1 || true
  fi
}

tenant_reconciliation_is_unpaused() {
  local tenant="$1"
  local paused
  paused="$(tenant_reconciliation_pause_value "${tenant}")"
  [[ -z "${paused}" || "${paused}" == false ]]
}

datastore_used_by_tenant_state() {
  local tenant="$1"
  local reference output status
  reference="$(tenant_namespace "${tenant}")/${tenant}"
  set +e
  output="$(management_kubectl get datastore default \
    -o jsonpath='{.status.usedBy[*]}' 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  if tr ' ' '\n' <<<"${output}" | grep -Fxq "${reference}"; then
    printf 'present\n'
  else
    printf 'absent\n'
  fi
}

datastore_used_by_tenant() {
  [[ "$(datastore_used_by_tenant_state "$1")" == present ]]
}

blocked_management_deployment_is_ready() {
  local namespace="$1"
  local name="$2"
  local desired ready
  desired="$(management_kubectl -n "${namespace}" get "deployment/${name}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null)" || return 1
  ready="$(management_kubectl -n "${namespace}" get "deployment/${name}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" || return 1
  [[ -n "${desired}" && "${ready:-0}" -eq "${desired}" ]]
}

blocked_management_daemonset_is_ready() {
  local namespace="$1"
  local name="$2"
  local desired ready
  desired="$(management_kubectl -n "${namespace}" get "daemonset/${name}" \
    -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null)" || return 1
  ready="$(management_kubectl -n "${namespace}" get "daemonset/${name}" \
    -o jsonpath='{.status.numberReady}' 2>/dev/null)" || return 1
  [[ -n "${desired}" && "${desired}" -gt 0 && "${ready:-0}" -eq "${desired}" ]]
}

blocked_management_plane_is_healthy() {
  kind_cluster_reported \
    && management_container_exists \
    && management_ownership_is_valid \
    && [[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
    && management_kubectl get --raw=/readyz >/dev/null 2>&1 \
    && blocked_management_deployment_is_ready cert-manager cert-manager \
    && blocked_management_deployment_is_ready cert-manager cert-manager-cainjector \
    && blocked_management_deployment_is_ready cert-manager cert-manager-webhook \
    && blocked_management_deployment_is_ready metallb-system controller \
    && blocked_management_daemonset_is_ready metallb-system speaker \
    && blocked_management_deployment_is_ready "${MANAGEMENT_NAMESPACE}" kamaji \
    && [[ -n "$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" \
      get endpoints kamaji-webhook-service \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null)" ]] \
    && [[ "$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" \
      get statefulset kamaji-etcd \
      -o jsonpath='{.spec.replicas}' 2>/dev/null)" \
      == "${KAMAJI_ETCD_REPLICAS}" ]] \
    && [[ "$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" \
      get statefulset kamaji-etcd \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" \
      == "${KAMAJI_ETCD_REPLICAS}" ]] \
    && [[ "$(management_kubectl get datastore default \
      -o jsonpath='{.status.ready}' 2>/dev/null)" == true ]]
}

blocked_residual_failure() {
  BLOCKED_RESIDUAL_REASON="$*"
  return 1
}

blocked_residual_probe_state() {
  local key="$1"
  shift
  case "${KAMAJI_TEST_BLOCKED_RESIDUAL:-}" in
    "${key}:present")
      printf 'present\n'
      return 0
      ;;
    "${key}:inspection-failed")
      printf 'inspection-failed\n'
      return 1
      ;;
  esac
  "$@"
}

blocked_require_absent_probe() {
  local key="$1"
  local description="$2"
  shift 2
  local state
  state="$(blocked_residual_probe_state "${key}" "$@")" || {
    blocked_residual_failure "${description} inspection failed"
    return 1
  }
  [[ "${state}" == absent ]] \
    || blocked_residual_failure "${description} is ${state}"
}

blocked_all_tcp_state() {
  local output status
  set +e
  output="$(management_kubectl get tenantcontrolplanes.kamaji.clastix.io \
    --all-namespaces -o name 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  [[ -z "${output}" ]] && printf 'absent\n' || printf 'present\n'
}

blocked_owned_worker_state() {
  local output status
  set +e
  output="$(docker ps -aq --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker' 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  [[ -z "${output}" ]] && printf 'absent\n' || printf 'present\n'
}

blocked_owned_worker_volume_state() {
  local output status
  set +e
  output="$(docker volume ls -q --filter "$(owned_docker_filter)" \
    --filter 'label=kamaji.cnpg-vcluster.io/role=worker-var-lib' 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  [[ -z "${output}" ]] && printf 'absent\n' || printf 'present\n'
}

blocked_management_namespace_state() {
  local namespace="$1"
  local output status
  set +e
  output="$(management_kubectl get namespace "${namespace}" \
    --ignore-not-found -o name 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  [[ -z "${output}" ]] && printf 'absent\n' || printf 'present\n'
}

blocked_vip_claim_state() {
  local vip="$1"
  local claims
  claims="$(services_claiming_vip "${vip}" 2>/dev/null)" || {
    printf 'inspection-failed\n'
    return 1
  }
  [[ -z "${claims}" ]] && printf 'absent\n' || printf 'present\n'
}

blocked_datastore_used_by_state() {
  case "$1" in
    spike) datastore_used_by_spike_state ;;
    *) datastore_used_by_tenant_state "$1" ;;
  esac
}

blocked_residual_state_is_allowed() {
  local tenant schema user vip
  BLOCKED_RESIDUAL_REASON=""
  blocked_management_plane_is_healthy || {
    blocked_residual_failure "owned management infrastructure is unhealthy or could not be inspected"
    return 1
  }
  blocked_require_absent_probe tcp \
    "final or spike TenantControlPlane residual" blocked_all_tcp_state \
    || return 1
  blocked_require_absent_probe workers \
    "final or spike worker residual" blocked_owned_worker_state \
    || return 1
  blocked_require_absent_probe worker-volumes \
    "final or spike worker volume residual" blocked_owned_worker_volume_state \
    || return 1

  for tenant in ${TENANT_NAMES} spike; do
    if [[ -e "$(tenant_kubeconfig "${tenant}")" ]]; then
      blocked_residual_failure "${tenant} kubeconfig remains"
      return 1
    fi
    if [[ "${tenant}" == spike ]]; then
      if [[ -e "${SPIKE_RUNTIME_DIR}" ]]; then
        blocked_residual_failure "spike runtime subtree remains"
        return 1
      fi
    else
      if [[ -e "$(tenant_runtime_dir "${tenant}")" ]]; then
        blocked_residual_failure "${tenant} runtime subtree remains"
        return 1
      fi
    fi
    blocked_require_absent_probe "${tenant}.namespace" \
      "${tenant} management namespace" blocked_management_namespace_state \
      "$(tenant_namespace "${tenant}")" || return 1
  done

  if [[ ! -f "${MANAGEMENT_NETWORK_FILE}" ]]; then
    blocked_residual_failure "management network assignment is absent"
    return 1
  fi
  # shellcheck disable=SC1090
  source "${MANAGEMENT_NETWORK_FILE}" || {
    blocked_residual_failure "management network assignment could not be read"
    return 1
  }
  for vip in "${TENANT_A_VIP:-}" "${TENANT_B_VIP:-}"; do
    if [[ -z "${vip}" ]]; then
      blocked_residual_failure "management VIP assignment is incomplete"
      return 1
    fi
    blocked_require_absent_probe "vip.${vip}" \
      "borrowed tenant VIP ${vip} claim" blocked_vip_claim_state "${vip}" \
      || return 1
  done

  for tenant in ${TENANT_NAMES} spike; do
    schema="$(tenant_schema "${tenant}")"
    user="$(tenant_datastore_user "${tenant}")"
    blocked_require_absent_probe "${tenant}.datastore-used-by" \
      "${tenant} DataStore/default status.usedBy residual" \
      blocked_datastore_used_by_state "${tenant}" || return 1
    blocked_require_absent_probe "${tenant}.datastore-prefix" \
      "${tenant} datastore prefix residual" etcd_readonly_prefix_state "${schema}" \
      || return 1
    blocked_require_absent_probe "${tenant}.datastore-user" \
      "${tenant} datastore user residual" etcd_readonly_user_state "${user}" \
      || return 1
    blocked_require_absent_probe "${tenant}.datastore-role" \
      "${tenant} datastore role residual" etcd_readonly_role_state "${schema}" \
      || return 1
  done
}

management_datastore_available() {
  [[ "${KAMAJI_TEST_DATASTORE_UNAVAILABLE:-0}" != 1 ]] \
    && [[ "$(management_kubectl get datastore default \
      -o jsonpath='{.status.ready}' 2>/dev/null || true)" == true ]] \
    && [[ "$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" \
      get statefulset kamaji-etcd \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)" \
      == "${KAMAJI_ETCD_REPLICAS}" ]]
}

require_probe_state() {
  local expected="$1"
  local description="$2"
  shift 2
  local state
  state="$("$@")" \
    || die "${description}: datastore inspection failed"
  [[ "${state}" == "${expected}" ]] \
    || die "${description}: expected ${expected}, found ${state}"
}

wait_for_probe_state() {
  local duration="$1"
  local expected="$2"
  local description="$3"
  shift 3
  local deadline=$((SECONDS + $(seconds_from_duration "${duration}")))
  local state
  while true; do
    state="$("$@")" \
      || return 1
    [[ "${state}" == "${expected}" ]] && return 0
    if (( SECONDS >= deadline )); then
      warn "timed out waiting for ${description}=${expected} after ${duration}"
      return 1
    fi
    sleep "${WAIT_POLL_INTERVAL}"
  done
}

require_management_datastore_inspection() {
  local tenant="$1"
  local schema user
  schema="$(tenant_schema "${tenant}")"
  user="$(tenant_datastore_user "${tenant}")"
  management_datastore_available \
    || die "${tenant}.datastore-cleanup: shared datastore is unavailable; no tenant resources were removed, restore it and retry"
  datastore_used_by_tenant_state "${tenant}" >/dev/null \
    || die "${tenant}.datastore-cleanup: DataStore/default inspection failed"
  etcd_prefix_state "${schema}" >/dev/null \
    || die "${tenant}.datastore-cleanup: exact etcd prefix inspection failed"
  etcd_user_state "${user}" >/dev/null \
    || die "${tenant}.datastore-cleanup: exact etcd user inspection failed"
  etcd_role_state "${schema}" >/dev/null \
    || die "${tenant}.datastore-cleanup: exact etcd role inspection failed"
}

final_tenant_tcp_state() {
  local tenant="$1"
  local output status
  set +e
  output="$(management_kubectl -n "$(tenant_namespace "${tenant}")" \
    get "$(tenant_tcp_ref "${tenant}")" --ignore-not-found -o name 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  if [[ -n "${output}" ]]; then
    printf 'present\n'
  else
    printf 'absent\n'
  fi
}

final_tenant_tcp_absent() {
  [[ "$(final_tenant_tcp_state "$1")" == absent ]]
}

force_delete_tenant_datastore_identity() {
  local tenant="$1"
  local schema user
  schema="$(tenant_schema "${tenant}")"
  user="$(tenant_datastore_user "${tenant}")"
  etcd_maintenance del "/${schema}/" --prefix >/dev/null \
    || die "${tenant}.datastore-cleanup: exact prefix deletion failed"
  local state
  state="$(etcd_user_state "${user}")" \
    || die "${tenant}.datastore-cleanup: exact user inspection failed"
  if [[ "${state}" == present ]]; then
    etcd_maintenance user delete "${user}" >/dev/null \
      || die "${tenant}.datastore-cleanup: exact user deletion failed"
  fi
  state="$(etcd_role_state "${schema}")" \
    || die "${tenant}.datastore-cleanup: exact role inspection failed"
  if [[ "${state}" == present ]]; then
    etcd_maintenance role delete "${schema}" >/dev/null \
      || die "${tenant}.datastore-cleanup: exact role deletion failed"
  fi
}

delete_final_tenant_control_plane() {
  local tenant="$1"
  local require_datastore_proof="${2:-true}"
  local namespace schema user credential_secret=""
  namespace="$(tenant_namespace "${tenant}")"
  schema="$(tenant_schema "${tenant}")"
  user="$(tenant_datastore_user "${tenant}")"
  if [[ "${require_datastore_proof}" == true ]]; then
    require_management_datastore_inspection "${tenant}"
  fi
  unpause_tenant_reconciliation "${tenant}"

  if management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    >/dev/null 2>&1; then
    credential_secret="$(management_kubectl -n "${namespace}" \
      get "$(tenant_tcp_ref "${tenant}")" \
      -o jsonpath='{.status.storage.config.secretName}')" \
      || die "${tenant}.cleanup: unable to inspect datastore credential Secret name"
    management_kubectl -n "${namespace}" delete "$(tenant_tcp_ref "${tenant}")" \
      --wait=false >/dev/null
    wait_for "${TENANT_DELETE_TIMEOUT}" "${tenant} TenantControlPlane deletion" \
      final_tenant_tcp_absent "${tenant}" \
      || die "${tenant}.cleanup: TenantControlPlane finalizers did not complete"
  fi

  if management_datastore_available; then
    force_delete_tenant_datastore_identity "${tenant}"
    if [[ -n "${credential_secret}" ]]; then
      local credential_state
      credential_state="$(management_namespaced_resource_state \
        "${namespace}" "secret/${credential_secret}")" \
        || die "${tenant}.cleanup: datastore credential Secret inspection failed"
      [[ "${credential_state}" == absent ]] \
        || die "${tenant}.cleanup: datastore credential Secret remains"
    fi
    wait_for_probe_state "${TENANT_DELETE_TIMEOUT}" absent \
      "${tenant}.cleanup: DataStore/default status.usedBy" \
      datastore_used_by_tenant_state "${tenant}" \
      || die "${tenant}.cleanup: DataStore/default status.usedBy cleanup proof failed"
    wait_for_probe_state "${TENANT_DELETE_TIMEOUT}" absent \
      "${tenant}.cleanup: exact etcd prefix" \
      etcd_prefix_state "${schema}" \
      || die "${tenant}.cleanup: exact etcd prefix cleanup proof failed"
    wait_for_probe_state "${TENANT_DELETE_TIMEOUT}" absent \
      "${tenant}.cleanup: exact etcd user" \
      etcd_user_state "${user}" \
      || die "${tenant}.cleanup: exact etcd user cleanup proof failed"
    wait_for_probe_state "${TENANT_DELETE_TIMEOUT}" absent \
      "${tenant}.cleanup: exact etcd role" \
      etcd_role_state "${schema}" \
      || die "${tenant}.cleanup: exact etcd role cleanup proof failed"
    [[ "$(management_kubectl get datastore default -o jsonpath='{.status.ready}')" == true ]] \
      || die "${tenant}.cleanup: shared DataStore/default became unhealthy"
  elif [[ "${require_datastore_proof}" == true ]]; then
    die "${tenant}.cleanup: shared datastore became unavailable; retaining namespace and runtime evidence for retry"
  fi

  management_kubectl delete namespace "${namespace}" \
    --ignore-not-found --wait=false >/dev/null
  wait_for "${TENANT_DELETE_TIMEOUT}" "${tenant} namespace deletion" \
    management_namespace_absent "${namespace}" \
    || die "${tenant}.cleanup: management namespace deletion did not finish"
  rm -f "$(tenant_kubeconfig "${tenant}")"
  rm -rf "$(tenant_runtime_dir "${tenant}")"
}

verify_final_tenant_cleanup_absent() {
  local tenant="$1"
  local namespace schema user claims
  namespace="$(tenant_namespace "${tenant}")"
  schema="$(tenant_schema "${tenant}")"
  user="$(tenant_datastore_user "${tenant}")"
  final_tenant_tcp_absent "${tenant}" \
    || die "${tenant}.cleanup-proof: TenantControlPlane is present or uninspectable"
  management_namespace_absent "${namespace}" \
    || die "${tenant}.cleanup-proof: namespace is present or uninspectable"
  require_probe_state absent "${tenant}.cleanup-proof: DataStore/default status.usedBy" \
    datastore_used_by_tenant_state "${tenant}"
  require_probe_state absent "${tenant}.cleanup-proof: exact etcd prefix" \
    etcd_prefix_state "${schema}"
  require_probe_state absent "${tenant}.cleanup-proof: exact etcd user" \
    etcd_user_state "${user}"
  require_probe_state absent "${tenant}.cleanup-proof: exact etcd role" \
    etcd_role_state "${schema}"
  [[ -z "$(docker ps -aq --filter "$(owned_docker_filter)" \
    --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}")" ]] \
    || die "${tenant}.cleanup-proof: worker container remains"
  [[ -z "$(docker volume ls -q --filter "$(owned_docker_filter)" \
    --filter "label=kamaji.cnpg-vcluster.io/tenant=${tenant}")" ]] \
    || die "${tenant}.cleanup-proof: worker volume remains"
  [[ ! -e "$(tenant_runtime_dir "${tenant}")" ]] \
    || die "${tenant}.cleanup-proof: runtime evidence remains"
  claims="$(services_claiming_vip "$(tenant_vip "${tenant}")")" \
    || die "${tenant}.cleanup-proof: VIP could not be inspected"
  [[ -z "${claims}" ]] \
    || die "${tenant}.cleanup-proof: VIP remains claimed by ${claims//$'\n'/,}"
}

final_tenant_exists() {
  local tenant="$1"
  management_kubectl -n "$(tenant_namespace "${tenant}")" \
    get "$(tenant_tcp_ref "${tenant}")" >/dev/null 2>&1
}

validate_final_tenant_ownership() {
  local tenant="$1"
  local namespace payload
  namespace="$(tenant_namespace "${tenant}")"
  payload="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" \
    -o json)" \
    || die "${tenant}.repair: TenantControlPlane is absent"
  TCP_JSON="${payload}" \
  EXPECTED_NAME="${tenant}" \
  EXPECTED_NAMESPACE="${namespace}" \
  EXPECTED_SCHEMA="$(tenant_schema "${tenant}")" \
  OWNERSHIP_LABEL="${OWNERSHIP_LABEL}" \
  LAB_PREFIX="${LAB_PREFIX}" \
  python3 -c '
import json,os
tcp=json.loads(os.environ["TCP_JSON"])
metadata=tcp.get("metadata",{})
assert metadata.get("name") == os.environ["EXPECTED_NAME"]
assert metadata.get("namespace") == os.environ["EXPECTED_NAMESPACE"]
assert metadata.get("labels",{}).get(os.environ["OWNERSHIP_LABEL"]) == os.environ["LAB_PREFIX"]
assert tcp.get("spec",{}).get("dataStoreSchema") == os.environ["EXPECTED_SCHEMA"]
' || die "${tenant}.repair: refusing to adopt a TCP without exact ownership and schema"
}

verify_initial_final_identities_free() {
  local tenant claims schema user
  for tenant in ${TENANT_NAMES}; do
    if final_tenant_exists "${tenant}"; then
      continue
    fi
    claims="$(services_claiming_vip "$(tenant_vip "${tenant}")")" \
      || die "final.pre-create: unable to inspect ${tenant} VIP"
    [[ -z "${claims}" ]] \
      || die "final.pre-create: ${tenant} VIP is claimed by ${claims//$'\n'/,}"
    schema="$(tenant_schema "${tenant}")"
    user="$(tenant_datastore_user "${tenant}")"
    require_probe_state absent "final.pre-create: ${tenant} datastore prefix" \
      etcd_prefix_state "${schema}"
    require_probe_state absent "final.pre-create: ${tenant} datastore user" \
      etcd_user_state "${user}"
    require_probe_state absent "final.pre-create: ${tenant} datastore role" \
      etcd_role_state "${schema}"
  done
}

verify_no_spike_residuals() {
  local claims
  ! management_kubectl -n "${SPIKE_NAMESPACE}" get "$(spike_tcp_ref)" \
    >/dev/null 2>&1 \
    || die "final.pre-create: spike TenantControlPlane remains"
  require_probe_state absent "final.pre-create: spike datastore prefix" \
    etcd_prefix_state "${SPIKE_SCHEMA}"
  require_probe_state absent "final.pre-create: spike datastore user" \
    etcd_user_state "${SPIKE_DATASTORE_USER}"
  require_probe_state absent "final.pre-create: spike datastore role" \
    etcd_role_state "${SPIKE_SCHEMA}"
  [[ ! -e "$(tenant_kubeconfig spike)" && ! -e "${SPIKE_RUNTIME_DIR}" ]] \
    || die "final.pre-create: spike runtime state remains"
  [[ -z "$(docker ps -aq --filter "$(owned_docker_filter)" \
      --filter 'label=kamaji.cnpg-vcluster.io/tenant=spike')" ]] \
    || die "final.pre-create: spike worker remains"
  [[ -z "$(docker volume ls -q --filter "$(owned_docker_filter)" \
      --filter 'label=kamaji.cnpg-vcluster.io/tenant=spike')" ]] \
    || die "final.pre-create: spike volume remains"
  claims="$(services_claiming_vip "${TENANT_A_VIP}")" \
    || die "final.pre-create: unable to inspect borrowed VIP"
  if final_tenant_exists tenant-a; then
    claims="$(grep -Fvx "${TENANT_A_NAMESPACE}/tenant-a" <<<"${claims}" || true)"
  fi
  if [[ -n "${claims}" ]]; then
    die "final.pre-create: borrowed VIP remains claimed by ${claims//$'\n'/,}"
  fi
}

datastore_used_by_spike_state() {
  local output status
  set +e
  output="$(management_kubectl get datastore default \
    -o jsonpath='{.status.usedBy[*]}' 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  if tr ' ' '\n' <<<"${output}" \
    | grep -Fxq "${SPIKE_NAMESPACE}/${SPIKE_NAME}"; then
    printf 'present\n'
  else
    printf 'absent\n'
  fi
}

management_namespaced_resource_state() {
  local namespace="$1"
  local resource="$2"
  local output status
  set +e
  output="$(management_kubectl -n "${namespace}" get "${resource}" \
    --ignore-not-found -o name 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  if [[ -n "${output}" ]]; then
    printf 'present\n'
  else
    printf 'absent\n'
  fi
}

datastore_used_by_spike() {
  [[ "$(datastore_used_by_spike_state)" == present ]]
}

etcd_maintenance_finished() {
  local pod="$1"
  local phase
  phase="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" \
    -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  [[ "${phase}" == Succeeded || "${phase}" == Failed ]]
}

etcd_maintenance_pod_absent() {
  local pod="$1"
  local output status
  set +e
  output="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" \
    --ignore-not-found -o name 2>&1)"
  status=$?
  set -e
  (( status == 0 )) && [[ -z "${output}" ]]
}

etcd_readonly() {
  local operation="$1"
  shift
  case "${operation}:$*" in
    get:*|user:get\ *|role:get\ *) ;;
    *) return 1 ;;
  esac
  local ca_certificate client_certificate client_key
  ca_certificate="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" \
    get secret kamaji-etcd-ca -o jsonpath='{.data.tls\.crt}' 2>/dev/null)" \
    || return 1
  client_certificate="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" \
    get secret kamaji-etcd-client-certs -o jsonpath='{.data.tls\.crt}' \
    2>/dev/null)" || return 1
  client_key="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" \
    get secret kamaji-etcd-client-certs -o jsonpath='{.data.tls\.key}' \
    2>/dev/null)" || return 1
  [[ -n "${ca_certificate}" && -n "${client_certificate}" \
    && -n "${client_key}" ]] || return 1

  {
    printf '%s\n' "${ca_certificate}"
    printf '%s\n' "${client_certificate}"
    printf '%s\n' "${client_key}"
  } | management_kubectl -n "${MANAGEMENT_NAMESPACE}" exec -i kamaji-etcd-0 \
    -- sh -c '
      set -eu
      cert_dir="$(mktemp -d)"
      trap '\''rm -rf "${cert_dir}"'\'' EXIT HUP INT TERM
      IFS= read -r ca_certificate
      IFS= read -r client_certificate
      IFS= read -r client_key
      printf "%s" "${ca_certificate}" | base64 -d >"${cert_dir}/ca.crt"
      printf "%s" "${client_certificate}" | base64 -d >"${cert_dir}/tls.crt"
      printf "%s" "${client_key}" | base64 -d >"${cert_dir}/tls.key"
      ETCDCTL_API=3 etcdctl \
        --endpoints=https://127.0.0.1:2379 \
        --cacert="${cert_dir}/ca.crt" \
        --cert="${cert_dir}/tls.crt" \
        --key="${cert_dir}/tls.key" \
        "$@"
    ' -- "${operation}" "$@"
}

etcd_readonly_prefix_state() {
  local output status
  set +e
  output="$(etcd_readonly get "/${1}/" --prefix --keys-only 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  [[ -z "${output}" ]] && printf 'absent\n' || printf 'present\n'
}

etcd_readonly_user_state() {
  local output status
  set +e
  output="$(etcd_readonly user get "$1" 2>&1)"
  status=$?
  set -e
  if (( status == 0 )); then
    printf 'present\n'
  elif grep -Eqi 'user .* does not exist|user name not found' <<<"${output}"; then
    printf 'absent\n'
  else
    printf 'inspection-failed\n'
    return 1
  fi
}

etcd_readonly_role_state() {
  local output status
  set +e
  output="$(etcd_readonly role get "$1" 2>&1)"
  status=$?
  set -e
  if (( status == 0 )); then
    printf 'present\n'
  elif grep -Eqi 'role .* does not exist|role name not found' <<<"${output}"; then
    printf 'absent\n'
  else
    printf 'inspection-failed\n'
    return 1
  fi
}

etcd_maintenance() {
  local operation="$1"
  shift
  local pod="${LAB_PREFIX}-etcd-maintenance"
  local manifest="${RUNTIME_DIR}/management/etcd-maintenance.yaml"
  local existing_label phase exit_code
  if [[ "${KAMAJI_TEST_ETCD_MAINTENANCE_FAILURE:-0}" == 1 ]]; then
    printf 'injected etcd maintenance failure\n' >&2
    return 1
  fi

  if management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" >/dev/null 2>&1; then
    existing_label="$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pod "${pod}" -o json \
      | OWNERSHIP_LABEL="${OWNERSHIP_LABEL}" python3 -c 'import json,os,sys; print(json.load(sys.stdin)["metadata"].get("labels",{}).get(os.environ["OWNERSHIP_LABEL"],""))')"
    [[ "${existing_label}" == "${LAB_PREFIX}" ]] \
      || die "spike.datastore-cleanup: refusing unowned maintenance pod ${pod}"
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete pod "${pod}" \
      --wait=false >/dev/null
    wait_for "${SPIKE_DELETE_TIMEOUT}" "old etcd maintenance pod deletion" \
      etcd_maintenance_pod_absent "${pod}" \
      || return 1
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
    --wait=false >/dev/null 2>&1 || true
  rm -f "${manifest}"
  [[ "${phase}" == Succeeded && "${exit_code}" == 0 ]]
}

etcd_prefix_state() {
  local output status
  set +e
  output="$(etcd_maintenance get "/${1}/" --prefix --keys-only 2>&1)"
  status=$?
  set -e
  if (( status != 0 )); then
    printf 'inspection-failed\n'
    return 1
  fi
  if [[ -n "${output}" ]]; then
    printf 'present\n'
  else
    printf 'absent\n'
  fi
}

etcd_user_state() {
  local output status
  set +e
  output="$(etcd_maintenance user get "$1" 2>&1)"
  status=$?
  set -e
  if (( status == 0 )); then
    printf 'present\n'
  elif grep -Eqi 'user .* does not exist|user name not found' <<<"${output}"; then
    printf 'absent\n'
  else
    printf 'inspection-failed\n'
    return 1
  fi
}

etcd_role_state() {
  local output status
  set +e
  output="$(etcd_maintenance role get "$1" 2>&1)"
  status=$?
  set -e
  if (( status == 0 )); then
    printf 'present\n'
  elif grep -Eqi 'role .* does not exist|role name not found' <<<"${output}"; then
    printf 'absent\n'
  else
    printf 'inspection-failed\n'
    return 1
  fi
}

etcd_prefix_exists() {
  [[ "$(etcd_prefix_state "$1")" == present ]]
}

etcd_user_exists() {
  [[ "$(etcd_user_state "$1")" == present ]]
}

etcd_role_exists() {
  [[ "$(etcd_role_state "$1")" == present ]]
}

force_delete_spike_datastore_identity() {
  local state
  etcd_maintenance del "/${SPIKE_SCHEMA}/" --prefix >/dev/null \
    || die "spike.datastore-cleanup: exact prefix deletion failed"
  state="$(etcd_user_state "${SPIKE_DATASTORE_USER}")" \
    || die "spike.datastore-cleanup: exact user inspection failed"
  if [[ "${state}" == present ]]; then
    etcd_maintenance user delete "${SPIKE_DATASTORE_USER}" >/dev/null \
      || die "spike.datastore-cleanup: exact user deletion failed"
  fi
  state="$(etcd_role_state "${SPIKE_SCHEMA}")" \
    || die "spike.datastore-cleanup: exact role inspection failed"
  if [[ "${state}" == present ]]; then
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
      -o jsonpath='{.status.storage.config.secretName}')" \
      || die "spike.cleanup: unable to inspect datastore credential Secret name"
    management_kubectl -n "${SPIKE_NAMESPACE}" delete "$(spike_tcp_ref)" \
      --wait=false >/dev/null
    if ! wait_for "${SPIKE_DELETE_TIMEOUT}" "spike TenantControlPlane deletion" \
      spike_tcp_absent; then
      die "spike.cleanup: TenantControlPlane finalizers did not complete"
    fi
  fi

  if management_datastore_available; then
    force_delete_spike_datastore_identity

    if [[ -n "${credential_secret}" ]]; then
      local credential_state
      credential_state="$(management_namespaced_resource_state \
        "${SPIKE_NAMESPACE}" "secret/${credential_secret}")" \
        || die "spike.cleanup: datastore credential Secret inspection failed"
      [[ "${credential_state}" == absent ]] \
        || die "spike.cleanup: datastore credential Secret remains"
    fi
    wait_for_probe_state "${SPIKE_DELETE_TIMEOUT}" absent \
      "spike.cleanup: DataStore/default status.usedBy" \
      datastore_used_by_spike_state \
      || die "spike.cleanup: DataStore/default status.usedBy cleanup proof failed"
    wait_for_probe_state "${SPIKE_DELETE_TIMEOUT}" absent \
      "spike.cleanup: exact etcd prefix" \
      etcd_prefix_state "${SPIKE_SCHEMA}" \
      || die "spike.cleanup: exact etcd prefix cleanup proof failed"
    wait_for_probe_state "${SPIKE_DELETE_TIMEOUT}" absent \
      "spike.cleanup: exact etcd user" \
      etcd_user_state "${SPIKE_DATASTORE_USER}" \
      || die "spike.cleanup: exact etcd user cleanup proof failed"
    wait_for_probe_state "${SPIKE_DELETE_TIMEOUT}" absent \
      "spike.cleanup: exact etcd role" \
      etcd_role_state "${SPIKE_SCHEMA}" \
      || die "spike.cleanup: exact etcd role cleanup proof failed"
    [[ "$(management_kubectl get datastore default -o jsonpath='{.status.ready}')" == true ]] \
      || die "spike.cleanup: shared DataStore/default became unhealthy"
  fi

  management_kubectl delete namespace "${SPIKE_NAMESPACE}" \
    --ignore-not-found --wait=false >/dev/null
  rm -f "$(tenant_kubeconfig spike)"
}
