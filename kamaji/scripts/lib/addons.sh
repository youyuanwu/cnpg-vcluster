#!/usr/bin/env bash

set -Eeuo pipefail
ADDONS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${ADDONS_LIB_DIR}/common.sh"

tenant_daemonset_ready() {
  local namespace="$1"
  local name="$2"
  local desired ready
  desired="$(tenant_kubectl spike -n "${namespace}" get daemonset "${name}" \
    -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || true)"
  ready="$(tenant_kubectl spike -n "${namespace}" get daemonset "${name}" \
    -o jsonpath='{.status.numberReady}' 2>/dev/null || true)"
  [[ "${desired:-0}" -gt 0 && "${ready:-0}" -eq "${desired}" ]]
}

tenant_deployment_ready() {
  local namespace="$1"
  local name="$2"
  local desired ready
  desired="$(tenant_kubectl spike -n "${namespace}" get deployment "${name}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  ready="$(tenant_kubectl spike -n "${namespace}" get deployment "${name}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
  [[ "${desired:-0}" -gt 0 && "${ready:-0}" -eq "${desired}" ]]
}

spike_managed_addons_ready() {
  local status
  status="$(management_kubectl -n "${SPIKE_NAMESPACE}" get \
    "tenantcontrolplane/${SPIKE_NAME}" -o json)"
  STATUS_JSON="${status}" python3 -c '
import json, os
s=json.loads(os.environ["STATUS_JSON"]).get("status",{}).get("addons",{})
assert s.get("coreDNS",{}).get("enabled") is True
assert s.get("kubeProxy",{}).get("enabled") is True
k=s.get("konnectivity",{})
assert k.get("enabled") is True
assert k.get("agent",{}).get("name")
assert k.get("service",{}).get("name")
' \
    && tenant_deployment_ready kube-system coredns \
    && tenant_daemonset_ready kube-system kube-proxy \
    && tenant_daemonset_ready kube-system konnectivity-agent
}

spike_calico_ready() {
  tenant_daemonset_ready kube-system calico-node \
    && tenant_deployment_ready kube-system calico-kube-controllers
}

spike_addon_failure_summary() {
  local pods logs
  pods="$(tenant_kubectl spike -n kube-system get pods -o json 2>/dev/null \
    | python3 -c '
import json,sys
parts=[]
for pod in json.load(sys.stdin).get("items",[]):
    name=pod["metadata"]["name"]
    phase=pod.get("status",{}).get("phase","Unknown")
    ready=sum(1 for c in pod.get("status",{}).get("containerStatuses",[]) if c.get("ready"))
    total=len(pod.get("spec",{}).get("containers",[]))
    reasons=[]
    for c in pod.get("status",{}).get("containerStatuses",[]):
        state=c.get("state",{})
        if "waiting" in state:
            reasons.append(state["waiting"].get("reason","waiting"))
        elif "terminated" in state and state["terminated"].get("exitCode",0):
            reasons.append(state["terminated"].get("reason","terminated"))
    parts.append(f"{name}={phase}:{ready}/{total}" + (":"+",".join(reasons) if reasons else ""))
print("|".join(parts))
')"
  logs="$(tenant_kubectl spike -n kube-system logs \
    -l k8s-app=kube-proxy --tail=20 2>/dev/null \
    | grep -E '(^E[0-9]|Error|error|failed|permission denied)' \
    | tail -4 | tr '\n' '|' || true)"
  printf '%s%s\n' "${pods}" "$([[ -n "${logs}" ]] && printf '|kube-proxy-log=%s' "${logs}")"
}

spike_kube_proxy_procfs_blocked() {
  local pod_json container_id
  pod_json="$(tenant_kubectl spike -n kube-system get pods \
    -l k8s-app=kube-proxy -o json 2>/dev/null || printf '{"items":[]}')"
  if POD_JSON="${pod_json}" python3 -c '
import json,os
items=json.loads(os.environ["POD_JSON"]).get("items",[])
raise SystemExit(0 if any(
  any(c.get("ready") for c in p.get("status",{}).get("containerStatuses",[]))
  for p in items
) else 1)
'; then
    return 1
  fi
  container_id="$(POD_JSON="${pod_json}" python3 -c '
import json,os
items=json.loads(os.environ["POD_JSON"]).get("items",[])
statuses=items[0].get("status",{}).get("containerStatuses",[]) if items else []
value=statuses[0].get("containerID","") if statuses else ""
print(value.split("://",1)[-1] if value else "")
' 2>/dev/null || true)"
  [[ -n "${container_id}" ]] \
    && docker exec "${SPIKE_WORKER_NAME}" crictl logs "${container_id}" 2>&1 \
      | grep -Fq 'open /proc/sys/net/netfilter/nf_conntrack_max: permission denied'
}

spike_network_failure_summary() {
  local evidence
  evidence="$(spike_addon_failure_summary 2>/dev/null || printf unavailable)"
  if spike_kube_proxy_procfs_blocked; then
    printf '%s\n' \
      "the host Linux kernel exposes net.netfilter.nf_conntrack_max read-only in the worker network namespace even to privileged kube-proxy; configure conntrack.maxPerCore: 0 before kube-proxy starts or use a kube-proxy-free dataplane; other pending add-on states are consequential, not independent failures: ${evidence}"
  else
    printf '%s\n' \
      "Calico, CoreDNS, kube-proxy, Konnectivity, DNS, or service routing failed: ${evidence}"
  fi
}

wait_spike_network_addons() {
  local deadline=$((SECONDS + $(seconds_from_duration "${TENANT_ADDON_TIMEOUT}")))
  until spike_managed_addons_ready && spike_calico_ready; do
    if spike_kube_proxy_procfs_blocked; then
      return 1
    fi
    if (( SECONDS >= deadline )); then
      warn "timed out waiting for tenant networking after ${TENANT_ADDON_TIMEOUT}"
      return 1
    fi
    sleep "${WAIT_POLL_INTERVAL}"
  done
}

spike_local_path_ready() {
  local defaults
  defaults="$(tenant_kubectl spike get storageclass -o json \
    | python3 -c 'import json,sys; print(sum(1 for i in json.load(sys.stdin).get("items",[]) if i.get("metadata",{}).get("annotations",{}).get("storageclass.kubernetes.io/is-default-class")=="true"))')"
  tenant_deployment_ready local-path-storage local-path-provisioner \
    && [[ "${defaults}" -eq 1 ]] \
    && [[ "$(tenant_kubectl spike get storageclass "${SPIKE_STORAGE_CLASS}" \
      -o jsonpath='{.provisioner}')" == rancher.io/local-path ]]
}

verify_spike_addon_images() {
  local calico_images local_path_image agent_image server_image
  calico_images="$(tenant_kubectl spike -n kube-system get daemonset/calico-node \
    deployment/calico-kube-controllers \
    -o jsonpath='{range .items[*].spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .items[*].spec.template.spec.containers[*]}{.image}{"\n"}{end}')"
  grep -Fxq "${CALICO_CNI_IMAGE}" <<<"${calico_images}" \
    && grep -Fxq "${CALICO_NODE_IMAGE}" <<<"${calico_images}" \
    && grep -Fxq "${CALICO_KUBE_CONTROLLERS_IMAGE}" <<<"${calico_images}" \
    || die "spike.addons: Calico images are not the selected digest pins"
  local_path_image="$(tenant_kubectl spike -n local-path-storage get deployment/local-path-provisioner \
    -o jsonpath='{.spec.template.spec.containers[0].image}')"
  [[ "${local_path_image}" == "${LOCAL_PATH_PROVISIONER_IMAGE}" ]] \
    || die "spike.addons: Local Path image is not the selected digest pin"
  agent_image="$(tenant_kubectl spike -n kube-system get daemonset/konnectivity-agent \
    -o jsonpath='{.spec.template.spec.containers[0].image}')"
  server_image="$(management_kubectl -n "${SPIKE_NAMESPACE}" get deployment "${SPIKE_NAME}" \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="konnectivity-server")].image}')"
  [[ "${agent_image}" == "${KONNECTIVITY_AGENT_IMAGE}" \
    && "${server_image}" == "${KONNECTIVITY_SERVER_IMAGE}" ]] \
    || die "spike.addons: Konnectivity images are not the selected digest pins"
}

worker_endpoint_accessible() {
  docker exec "${SPIKE_WORKER_NAME}" bash -ec \
    "timeout 5 bash -c '</dev/tcp/${SPIKE_VIP}/6443' && timeout 5 bash -c '</dev/tcp/${SPIKE_VIP}/8132'"
}

configure_spike_cni_bootstrap_endpoint() {
  cat <<EOF | tenant_kubectl spike apply -f - >/dev/null
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubernetes-services-endpoint
  namespace: kube-system
data:
  KUBERNETES_SERVICE_HOST: "${SPIKE_VIP}"
  KUBERNETES_SERVICE_PORT: "6443"
  KUBERNETES_SERVICE_PORT_HTTPS: "6443"
EOF
}

run_network_smoke() {
  tenant_kubectl spike -n default delete pod spike-network-smoke \
    --ignore-not-found --wait=true --timeout="${TENANT_ADDON_TIMEOUT}" \
    >/dev/null 2>&1 || true
  tenant_kubectl spike -n default run spike-network-smoke \
    --image="${VERIFY_IMAGE}" \
    --restart=Never \
    --command -- sh -ec \
    "nslookup kubernetes.default.svc.${SPIKE_CLUSTER_DOMAIN} && wget --no-check-certificate -qO- https://kubernetes.default.svc.${SPIKE_CLUSTER_DOMAIN}/version >/dev/null" \
    >/dev/null
  if ! tenant_kubectl spike -n default wait --for=jsonpath='{.status.phase}'=Succeeded \
    pod/spike-network-smoke --timeout="${TENANT_ADDON_TIMEOUT}" >/dev/null; then
    tenant_kubectl spike -n default logs pod/spike-network-smoke >&2 || true
    return 1
  fi
  tenant_kubectl spike -n default delete pod spike-network-smoke \
    --wait=true --timeout="${TENANT_ADDON_TIMEOUT}" >/dev/null
}

install_spike_network_addons() {
  sha256_check "${CALICO_SPIKE_RENDER_SHA256}" "${LAB_ROOT}/manifests/addons/calico.yaml"
  configure_spike_cni_bootstrap_endpoint
  tenant_kubectl spike apply -f "${LAB_ROOT}/manifests/addons/calico.yaml" >/dev/null
  wait_spike_network_addons \
    && wait_for "${TENANT_ADDON_TIMEOUT}" "worker control-plane endpoint access" \
      worker_endpoint_accessible \
    && run_network_smoke
}

install_spike_storage_addon() {
  sha256_check "${LOCAL_PATH_SPIKE_RENDER_SHA256}" \
    "${LAB_ROOT}/manifests/addons/local-path.yaml"
  tenant_kubectl spike apply -f "${LAB_ROOT}/manifests/addons/local-path.yaml" >/dev/null
  wait_for "${TENANT_STORAGE_TIMEOUT}" "Local Path readiness" spike_local_path_ready
}

create_spike_storage_writer() {
  tenant_kubectl spike -n default delete pod "${SPIKE_SMOKE_POD}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  tenant_kubectl spike -n default delete pvc "${SPIKE_SMOKE_PVC}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  cat <<EOF | tenant_kubectl spike apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${SPIKE_SMOKE_PVC}
  namespace: default
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ${SPIKE_STORAGE_CLASS}
  resources:
    requests:
      storage: 64Mi
---
apiVersion: v1
kind: Pod
metadata:
  name: ${SPIKE_SMOKE_POD}
  namespace: default
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${SPIKE_WORKER_NAME}
  containers:
    - name: writer
      image: ${VERIFY_IMAGE}
      command: [sh, -ec]
      args:
        - printf '%s\n' '${SPIKE_PERSISTENCE_MARKER}' > /data/marker && sync
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ${SPIKE_SMOKE_PVC}
EOF
  if ! tenant_kubectl spike -n default wait \
    --for=jsonpath='{.status.phase}'=Succeeded \
    "pod/${SPIKE_SMOKE_POD}" --timeout="${TENANT_STORAGE_TIMEOUT}" >/dev/null; then
    tenant_kubectl spike -n default describe "pod/${SPIKE_SMOKE_POD}" >&2 || true
    tenant_kubectl spike -n local-path-storage logs \
      deployment/local-path-provisioner --tail=50 >&2 || true
    return 1
  fi
  [[ "$(tenant_kubectl spike -n default get pvc "${SPIKE_SMOKE_PVC}" \
    -o jsonpath='{.status.phase}')" == Bound ]]
}

delete_spike_storage_writer_pod() {
  tenant_kubectl spike -n default delete pod "${SPIKE_SMOKE_POD}" \
    --ignore-not-found --wait=true --timeout="${TENANT_STORAGE_TIMEOUT}" >/dev/null
}

verify_spike_storage_reader() {
  tenant_kubectl spike -n default delete pod "${SPIKE_SMOKE_POD}" \
    --ignore-not-found --wait=true --timeout="${TENANT_STORAGE_TIMEOUT}" \
    >/dev/null 2>&1 || true
  cat <<EOF | tenant_kubectl spike apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${SPIKE_SMOKE_POD}
  namespace: default
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${SPIKE_WORKER_NAME}
  containers:
    - name: reader
      image: ${VERIFY_IMAGE}
      command: [sh, -ec]
      args:
        - test "\$(cat /data/marker)" = '${SPIKE_PERSISTENCE_MARKER}'
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ${SPIKE_SMOKE_PVC}
EOF
  if ! tenant_kubectl spike -n default wait --for=jsonpath='{.status.phase}'=Succeeded \
    "pod/${SPIKE_SMOKE_POD}" --timeout="${TENANT_STORAGE_TIMEOUT}" >/dev/null; then
    tenant_kubectl spike -n default logs "${SPIKE_SMOKE_POD}" >&2 || true
    return 1
  fi
}

delete_spike_storage_smoke() {
  if [[ -f "$(tenant_kubeconfig spike)" ]] \
    && tenant_kubectl spike get --raw=/readyz >/dev/null 2>&1; then
    tenant_kubectl spike -n default delete pod "${SPIKE_SMOKE_POD}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
    tenant_kubectl spike -n default delete pvc "${SPIKE_SMOKE_PVC}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
}

final_tenant_daemonset_ready() {
  local tenant="$1"
  local namespace="$2"
  local name="$3"
  local desired ready
  desired="$(tenant_kubectl "${tenant}" -n "${namespace}" get daemonset "${name}" \
    -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || true)"
  ready="$(tenant_kubectl "${tenant}" -n "${namespace}" get daemonset "${name}" \
    -o jsonpath='{.status.numberReady}' 2>/dev/null || true)"
  [[ "${desired:-0}" -eq "${WORKERS_PER_TENANT}" \
    && "${ready:-0}" -eq "${desired}" ]]
}

final_tenant_deployment_ready() {
  local tenant="$1"
  local namespace="$2"
  local name="$3"
  local desired ready
  desired="$(tenant_kubectl "${tenant}" -n "${namespace}" get deployment "${name}" \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  ready="$(tenant_kubectl "${tenant}" -n "${namespace}" get deployment "${name}" \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
  [[ "${desired:-0}" -gt 0 && "${ready:-0}" -eq "${desired}" ]]
}

render_tenant_addons() {
  local tenant="$1"
  local addon_dir calico local_path pod_cidr storage_path
  addon_dir="$(tenant_addon_dir "${tenant}")"
  calico="${addon_dir}/calico.yaml"
  local_path="${addon_dir}/local-path.yaml"
  pod_cidr="$(tenant_pod_cidr "${tenant}")"
  storage_path="$(tenant_storage_path "${tenant}")"
  mkdir -p -m 0700 "${addon_dir}"
  sha256_check "${CALICO_SPIKE_RENDER_SHA256}" \
    "${LAB_ROOT}/manifests/addons/calico.yaml"
  sha256_check "${LOCAL_PATH_SPIKE_RENDER_SHA256}" \
    "${LAB_ROOT}/manifests/addons/local-path.yaml"
  BASE_CALICO="${LAB_ROOT}/manifests/addons/calico.yaml" \
  BASE_LOCAL_PATH="${LAB_ROOT}/manifests/addons/local-path.yaml" \
  RENDERED_CALICO="${calico}" \
  RENDERED_LOCAL_PATH="${local_path}" \
  SPIKE_POD_CIDR="${SPIKE_POD_CIDR}" \
  TENANT_POD_CIDR="${pod_cidr}" \
  SPIKE_STORAGE_PATH="${SPIKE_STORAGE_PATH}" \
  TENANT_STORAGE_PATH="${storage_path}" \
  python3 -c '
import os
from pathlib import Path
calico=Path(os.environ["BASE_CALICO"]).read_text(encoding="utf-8")
if calico.count(os.environ["SPIKE_POD_CIDR"]) != 1:
    raise SystemExit("Calico base has no unique spike CIDR")
calico=calico.replace(os.environ["SPIKE_POD_CIDR"], os.environ["TENANT_POD_CIDR"], 1)
local_path=Path(os.environ["BASE_LOCAL_PATH"]).read_text(encoding="utf-8")
if local_path.count(os.environ["SPIKE_STORAGE_PATH"]) != 1:
    raise SystemExit("Local Path base has no unique spike path")
local_path=local_path.replace(
    os.environ["SPIKE_STORAGE_PATH"], os.environ["TENANT_STORAGE_PATH"], 1)
for destination, content in (
    (os.environ["RENDERED_CALICO"], calico),
    (os.environ["RENDERED_LOCAL_PATH"], local_path),
):
    path=Path(destination)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
'
}

configure_tenant_cni_bootstrap_endpoint() {
  local tenant="$1"
  cat <<EOF | tenant_kubectl "${tenant}" apply -f - >/dev/null
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubernetes-services-endpoint
  namespace: kube-system
data:
  KUBERNETES_SERVICE_HOST: "$(tenant_vip "${tenant}")"
  KUBERNETES_SERVICE_PORT: "6443"
  KUBERNETES_SERVICE_PORT_HTTPS: "6443"
EOF
}

final_tenant_managed_addons_ready() {
  local tenant="$1"
  local namespace status
  namespace="$(tenant_namespace "${tenant}")"
  status="$(management_kubectl -n "${namespace}" get "$(tenant_tcp_ref "${tenant}")" -o json)"
  STATUS_JSON="${status}" python3 -c '
import json, os
s=json.loads(os.environ["STATUS_JSON"]).get("status",{}).get("addons",{})
assert s.get("coreDNS",{}).get("enabled") is True
assert s.get("kubeProxy",{}).get("enabled") is True
k=s.get("konnectivity",{})
assert k.get("enabled") is True
assert k.get("agent",{}).get("name")
assert k.get("service",{}).get("name")
' \
    && tenant_kube_proxy_conntrack_is_zero "${tenant}" \
    && final_tenant_deployment_ready "${tenant}" kube-system coredns \
    && final_tenant_daemonset_ready "${tenant}" kube-system kube-proxy \
    && final_tenant_daemonset_ready "${tenant}" kube-system konnectivity-agent
}

final_tenant_calico_ready() {
  local tenant="$1"
  final_tenant_daemonset_ready "${tenant}" kube-system calico-node \
    && final_tenant_deployment_ready "${tenant}" kube-system calico-kube-controllers
}

final_tenant_network_ready() {
  local tenant="$1"
  tenant_workers_ready "${tenant}" \
    && final_tenant_managed_addons_ready "${tenant}" \
    && final_tenant_calico_ready "${tenant}"
}

wait_final_tenant_network() {
  local tenant="$1"
  wait_for "${TENANT_ADDON_TIMEOUT}" "${tenant} managed add-ons and Calico" \
    final_tenant_network_ready "${tenant}"
}

worker_tenant_endpoints_accessible() {
  local tenant="$1"
  local ordinal name vip
  vip="$(tenant_vip "${tenant}")"
  for ordinal in $(seq 1 "${WORKERS_PER_TENANT}"); do
    name="$(worker_name "${tenant}" "${ordinal}")"
    docker exec "${name}" bash -ec \
      "timeout 5 bash -c '</dev/tcp/${vip}/6443' && timeout 5 bash -c '</dev/tcp/${vip}/8132'" \
      || return 1
  done
}

run_final_tenant_network_smoke() {
  local tenant="$1"
  local domain pod
  domain="$(tenant_cluster_domain "${tenant}")"
  pod="${TENANT_NETWORK_SMOKE_POD}"
  tenant_kubectl "${tenant}" -n default delete pod "${pod}" \
    --ignore-not-found --wait=true --timeout="${TENANT_ADDON_TIMEOUT}" \
    >/dev/null 2>&1 || true
  tenant_kubectl "${tenant}" -n default run "${pod}" \
    --image="${VERIFY_IMAGE}" \
    --restart=Never \
    --command -- sh -ec \
    "nslookup kubernetes.default.svc.${domain} && wget --no-check-certificate -qO- https://kubernetes.default.svc.${domain}/version >/dev/null" \
    >/dev/null
  if ! tenant_kubectl "${tenant}" -n default wait \
    --for=jsonpath='{.status.phase}'=Succeeded "pod/${pod}" \
    --timeout="${TENANT_ADDON_TIMEOUT}" >/dev/null; then
    tenant_kubectl "${tenant}" -n default logs "pod/${pod}" >&2 || true
    return 1
  fi
  tenant_kubectl "${tenant}" -n default delete pod "${pod}" \
    --wait=true --timeout="${TENANT_ADDON_TIMEOUT}" >/dev/null
}

verify_final_tenant_addon_images() {
  local tenant="$1"
  local namespace calico_images local_path_image agent_image server_image
  namespace="$(tenant_namespace "${tenant}")"
  calico_images="$(tenant_kubectl "${tenant}" -n kube-system get \
    daemonset/calico-node deployment/calico-kube-controllers \
    -o jsonpath='{range .items[*].spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .items[*].spec.template.spec.containers[*]}{.image}{"\n"}{end}')"
  grep -Fxq "${CALICO_CNI_IMAGE}" <<<"${calico_images}" \
    && grep -Fxq "${CALICO_NODE_IMAGE}" <<<"${calico_images}" \
    && grep -Fxq "${CALICO_KUBE_CONTROLLERS_IMAGE}" <<<"${calico_images}" \
    || die "${tenant}.addons: Calico images are not digest pinned"
  local_path_image="$(tenant_kubectl "${tenant}" -n local-path-storage \
    get deployment/local-path-provisioner \
    -o jsonpath='{.spec.template.spec.containers[0].image}')"
  [[ "${local_path_image}" == "${LOCAL_PATH_PROVISIONER_IMAGE}" ]] \
    || die "${tenant}.addons: Local Path image is not digest pinned"
  agent_image="$(tenant_kubectl "${tenant}" -n kube-system \
    get daemonset/konnectivity-agent \
    -o jsonpath='{.spec.template.spec.containers[0].image}')"
  server_image="$(management_kubectl -n "${namespace}" get deployment "${tenant}" \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="konnectivity-server")].image}')"
  [[ "${agent_image}" == "${KONNECTIVITY_AGENT_IMAGE}" \
    && "${server_image}" == "${KONNECTIVITY_SERVER_IMAGE}" ]] \
    || die "${tenant}.addons: Konnectivity images are not digest pinned"
}

final_tenant_local_path_ready() {
  local tenant="$1"
  local defaults
  defaults="$(tenant_kubectl "${tenant}" get storageclass -o json \
    | python3 -c 'import json,sys; print(sum(1 for i in json.load(sys.stdin).get("items",[]) if i.get("metadata",{}).get("annotations",{}).get("storageclass.kubernetes.io/is-default-class")=="true"))')"
  final_tenant_deployment_ready "${tenant}" local-path-storage local-path-provisioner \
    && [[ "${defaults}" -eq 1 ]] \
    && [[ "$(tenant_kubectl "${tenant}" get storageclass "${TENANT_STORAGE_CLASS}" \
      -o jsonpath='{.provisioner}')" == rancher.io/local-path ]]
}

create_final_tenant_storage_smoke() {
  local tenant="$1"
  tenant_kubectl "${tenant}" -n default delete pod "${TENANT_SMOKE_POD}" \
    --ignore-not-found --wait=true --timeout="${TENANT_STORAGE_TIMEOUT}" \
    >/dev/null 2>&1 || true
  if tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
    >/dev/null 2>&1; then
    [[ "$(tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
      -o jsonpath='{.status.phase}')" == Bound ]] && return 0
    tenant_kubectl "${tenant}" -n default delete pvc "${TENANT_SMOKE_PVC}" \
      --wait=true --timeout="${TENANT_STORAGE_TIMEOUT}" >/dev/null
  fi
  cat <<EOF | tenant_kubectl "${tenant}" apply -f - >/dev/null
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${TENANT_SMOKE_PVC}
  namespace: default
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
    kamaji.cnpg-vcluster.io/tenant: ${tenant}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ${TENANT_STORAGE_CLASS}
  resources:
    requests:
      storage: 64Mi
---
apiVersion: v1
kind: Pod
metadata:
  name: ${TENANT_SMOKE_POD}
  namespace: default
spec:
  restartPolicy: Never
  containers:
    - name: writer
      image: ${VERIFY_IMAGE}
      command: [sh, -ec]
      args:
        - printf '%s\n' '${TENANT_SMOKE_MARKER}-${tenant}' > /data/marker && sync
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ${TENANT_SMOKE_PVC}
EOF
  tenant_kubectl "${tenant}" -n default wait \
    --for=jsonpath='{.status.phase}'=Succeeded "pod/${TENANT_SMOKE_POD}" \
    --timeout="${TENANT_STORAGE_TIMEOUT}" >/dev/null \
    && [[ "$(tenant_kubectl "${tenant}" -n default get pvc "${TENANT_SMOKE_PVC}" \
      -o jsonpath='{.status.phase}')" == Bound ]]
}

install_final_tenant_addons() {
  local tenant="$1"
  if [[ "${KAMAJI_TEST_INJECT_ADDON_FAILURE:-}" == "${tenant}" ]]; then
    warn "${tenant}.addons: injected ordinary add-on failure"
    return 1
  fi
  render_tenant_addons "${tenant}"
  configure_tenant_cni_bootstrap_endpoint "${tenant}"
  tenant_kubectl "${tenant}" apply \
    -f "$(tenant_addon_dir "${tenant}")/calico.yaml" >/dev/null
  wait_final_tenant_network "${tenant}" \
    && wait_for "${TENANT_ADDON_TIMEOUT}" "${tenant} worker endpoint reachability" \
      worker_tenant_endpoints_accessible "${tenant}" \
    && run_final_tenant_network_smoke "${tenant}" \
    || return 1
  tenant_kubectl "${tenant}" apply \
    -f "$(tenant_addon_dir "${tenant}")/local-path.yaml" >/dev/null
  wait_for "${TENANT_STORAGE_TIMEOUT}" "${tenant} Local Path readiness" \
    final_tenant_local_path_ready "${tenant}" \
    && create_final_tenant_storage_smoke "${tenant}" \
    || return 1
  verify_final_tenant_addon_images "${tenant}"
}

final_tenant_kube_proxy_procfs_blocked() {
  local tenant="$1"
  local pod_json entry node container_id
  pod_json="$(tenant_kubectl "${tenant}" -n kube-system get pods \
    -l k8s-app=kube-proxy -o json 2>/dev/null || printf '{"items":[]}')"
  while IFS=$'\t' read -r node container_id; do
    [[ -n "${node}" && -n "${container_id}" ]] || continue
    docker exec "${node}" crictl logs "${container_id}" 2>&1 \
      | grep -Fq 'open /proc/sys/net/netfilter/nf_conntrack_max: permission denied' \
      && return 0
  done < <(
    POD_JSON="${pod_json}" python3 -c '
import json,os
for pod in json.loads(os.environ["POD_JSON"]).get("items",[]):
    node=pod.get("spec",{}).get("nodeName","")
    for status in pod.get("status",{}).get("containerStatuses",[]):
        value=status.get("containerID","")
        if value:
            print(node + "\t" + value.split("://",1)[-1])
'
  )
  return 1
}

delete_final_tenant_smoke() {
  local tenant="$1"
  if [[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
    && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1; then
    tenant_kubectl "${tenant}" -n default delete pod "${TENANT_SMOKE_POD}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
    tenant_kubectl "${tenant}" -n default delete pvc "${TENANT_SMOKE_PVC}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
}
