#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tenants.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/addons.sh"

tenant="${1:-tenant-a}"
tenant_namespace "${tenant}" >/dev/null

replacement_kube_proxy_ready() {
  local pod_json
  pod_json="$(tenant_kubectl "${tenant}" -n kube-system get pods \
    -l k8s-app=kube-proxy -o json 2>/dev/null || printf '{"items":[]}')"
  POD_JSON="${pod_json}" OLD_UID="${old_uid}" NODE_NAME="${node_name}" \
    python3 -c '
import json, os
items=json.loads(os.environ["POD_JSON"]).get("items",[])
matches=[
    p for p in items
    if p.get("spec",{}).get("nodeName") == os.environ["NODE_NAME"]
    and p.get("metadata",{}).get("uid") != os.environ["OLD_UID"]
    and any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in p.get("status",{}).get("conditions",[])
    )
]
raise SystemExit(0 if len(matches) == 1 else 1)
'
}

require_exact_just
[[ -f "${MANAGEMENT_KUBECONFIG}" ]] \
  && management_kubectl get --raw=/readyz >/dev/null 2>&1 \
  || die "kube-proxy.restart: management API is unreachable"
final_tenant_exists "${tenant}" \
  || die "kube-proxy.restart: ${tenant} TenantControlPlane is absent"
[[ -f "$(tenant_kubeconfig "${tenant}")" ]] \
  && tenant_kubectl "${tenant}" get --raw=/readyz >/dev/null 2>&1 \
  || die "kube-proxy.restart: ${tenant} API is unreachable"
tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
  || die "kube-proxy.restart: ${tenant} is not in the expected paused remediation state"
final_tenant_daemonset_ready "${tenant}" kube-system kube-proxy \
  || die "kube-proxy.restart: ${tenant} kube-proxy is not initially Ready"

pod_json="$(tenant_kubectl "${tenant}" -n kube-system get pods \
  -l k8s-app=kube-proxy -o json)"
read -r pod_name old_uid node_name < <(
  POD_JSON="${pod_json}" python3 -c '
import json, os
items=sorted(
    json.loads(os.environ["POD_JSON"]).get("items",[]),
    key=lambda p: p["metadata"]["name"],
)
if not items:
    raise SystemExit("no kube-proxy pod found")
pod=items[0]
print(pod["metadata"]["name"], pod["metadata"]["uid"], pod["spec"]["nodeName"])
'
)

log "deleting ${tenant} kube-proxy pod on ${node_name}"
tenant_kubectl "${tenant}" -n kube-system delete pod "${pod_name}" \
  --wait=true --timeout="${TENANT_ADDON_TIMEOUT}" >/dev/null
wait_for "${TENANT_ADDON_TIMEOUT}" "${tenant} replacement kube-proxy readiness" \
  replacement_kube_proxy_ready \
  || die "kube-proxy.restart: replacement pod did not become Ready"

replacement_name="$(tenant_kubectl "${tenant}" -n kube-system get pods \
  -l k8s-app=kube-proxy -o json \
  | OLD_UID="${old_uid}" NODE_NAME="${node_name}" python3 -c '
import json, os, sys
for pod in json.load(sys.stdin).get("items",[]):
    if (pod.get("spec",{}).get("nodeName") == os.environ["NODE_NAME"]
        and pod.get("metadata",{}).get("uid") != os.environ["OLD_UID"]):
        print(pod["metadata"]["name"])
        break
else:
    raise SystemExit("replacement kube-proxy pod not found")
')"

final_tenant_daemonset_ready "${tenant}" kube-system kube-proxy \
  || die "kube-proxy.restart: DaemonSet did not return to ${WORKERS_PER_TENANT}/${WORKERS_PER_TENANT} Ready"
tenant_kube_proxy_steady_state_is_preserved "${tenant}" \
  || die "kube-proxy.restart: pause annotation or conntrack.maxPerCore remediation was lost"
restart_count="$(tenant_kubectl "${tenant}" -n kube-system get pod "${replacement_name}" \
  -o jsonpath='{.status.containerStatuses[0].restartCount}')"
[[ "${restart_count:-0}" -eq 0 ]] \
  || die "kube-proxy.restart: replacement pod restarted ${restart_count} time(s)"
if tenant_kubectl "${tenant}" -n kube-system logs "${replacement_name}" 2>&1 \
  | grep -Eq 'open /proc/sys/net/netfilter/nf_conntrack_max: permission denied|nf_conntrack_max.*permission denied'; then
  die "kube-proxy.restart: replacement pod hit the container permission crash"
fi

log "${tenant} replacement kube-proxy pod is Ready with conntrack.maxPerCore=${KUBE_PROXY_CONNTRACK_MAX_PER_CORE}"
