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
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
  tenant_kubectl spike -n default run spike-network-smoke \
    --image="${VERIFY_IMAGE}" \
    --restart=Never \
    --command -- sh -ec \
    'nslookup kubernetes.default.svc && wget --no-check-certificate -qO- https://kubernetes.default.svc/version >/dev/null' \
    >/dev/null
  if ! tenant_kubectl spike -n default wait --for=jsonpath='{.status.phase}'=Succeeded \
    pod/spike-network-smoke --timeout="${TENANT_ADDON_TIMEOUT}" >/dev/null; then
    tenant_kubectl spike -n default logs pod/spike-network-smoke >&2 || true
    return 1
  fi
  tenant_kubectl spike -n default delete pod spike-network-smoke \
    --wait=true >/dev/null
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
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
  tenant_kubectl spike -n default delete pvc "${SPIKE_SMOKE_PVC}" \
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
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
  nodeName: ${SPIKE_WORKER_NAME}
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
  tenant_kubectl spike -n default wait --for=jsonpath='{.status.phase}'=Succeeded \
    "pod/${SPIKE_SMOKE_POD}" --timeout="${TENANT_STORAGE_TIMEOUT}" >/dev/null \
    && [[ "$(tenant_kubectl spike -n default get pvc "${SPIKE_SMOKE_PVC}" \
      -o jsonpath='{.status.phase}')" == Bound ]]
}

delete_spike_storage_writer_pod() {
  tenant_kubectl spike -n default delete pod "${SPIKE_SMOKE_POD}" \
    --ignore-not-found --wait=true >/dev/null
}

verify_spike_storage_reader() {
  tenant_kubectl spike -n default delete pod "${SPIKE_SMOKE_POD}" \
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
  cat <<EOF | tenant_kubectl spike apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${SPIKE_SMOKE_POD}
  namespace: default
spec:
  restartPolicy: Never
  nodeName: ${SPIKE_WORKER_NAME}
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
      --ignore-not-found --wait=true >/dev/null 2>&1 || true
    tenant_kubectl spike -n default delete pvc "${SPIKE_SMOKE_PVC}" \
      --ignore-not-found --wait=true >/dev/null 2>&1 || true
  fi
}
