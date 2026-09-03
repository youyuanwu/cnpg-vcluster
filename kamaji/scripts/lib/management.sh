#!/usr/bin/env bash

set -Eeuo pipefail
MANAGEMENT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${MANAGEMENT_LIB_DIR}/common.sh"
# shellcheck disable=SC1091
source "${MANAGEMENT_LIB_DIR}/network.sh"

management_node_name() {
  printf '%s-control-plane\n' "${KIND_CLUSTER_NAME}"
}

kind_cluster_reported() {
  kind get clusters 2>/dev/null | grep -Fxq "${KIND_CLUSTER_NAME}"
}

management_container_exists() {
  docker container inspect "$(management_node_name)" >/dev/null 2>&1
}

management_state_present() {
  kind_cluster_reported || management_container_exists
}

cleanup_if_introduced() {
  local introduced="$1"
  shift
  (( introduced == 0 )) || "$@"
}

record_management_ownership() {
  local node id label
  node="$(management_node_name)"
  id="$(docker container inspect --format '{{.Id}}' "${node}")"
  label="$(docker container inspect --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}' "${node}")"
  [[ "${label}" == "${KIND_CLUSTER_NAME}" ]] \
    || die "management.ownership: kind node ${node} lacks the expected cluster label"
  {
    printf 'KIND_CLUSTER_NAME=%s\n' "${KIND_CLUSTER_NAME}"
    printf 'KIND_NODE_NAME=%s\n' "${node}"
    printf 'KIND_NODE_ID=%s\n' "${id}"
  } | write_secret_file "${MANAGEMENT_OWNERSHIP_FILE}"
}

validate_management_ownership() {
  [[ -f "${MANAGEMENT_OWNERSHIP_FILE}" ]] \
    || die "management.ownership-refusal: same-named cluster is not recorded as owned"
  local recorded_cluster recorded_name recorded_id actual_id actual_label
  recorded_cluster="$(sed -n 's/^KIND_CLUSTER_NAME=//p' "${MANAGEMENT_OWNERSHIP_FILE}")"
  recorded_name="$(sed -n 's/^KIND_NODE_NAME=//p' "${MANAGEMENT_OWNERSHIP_FILE}")"
  recorded_id="$(sed -n 's/^KIND_NODE_ID=//p' "${MANAGEMENT_OWNERSHIP_FILE}")"
  [[ "${recorded_cluster}" == "${KIND_CLUSTER_NAME}" && "${recorded_name}" == "$(management_node_name)" ]] \
    || die "management.ownership-refusal: runtime ownership record does not match ${KIND_CLUSTER_NAME}"
  actual_id="$(docker container inspect --format '{{.Id}}' "${recorded_name}" 2>/dev/null || true)"
  actual_label="$(docker container inspect --format '{{index .Config.Labels "io.x-k8s.kind.cluster"}}' "${recorded_name}" 2>/dev/null || true)"
  [[ -n "${actual_id}" && "${actual_id}" == "${recorded_id}" && "${actual_label}" == "${KIND_CLUSTER_NAME}" ]] \
    || die "management.ownership-refusal: live kind node identity does not match the ownership record"
}

ensure_owned_management_access() {
  local reported=0 container=0
  kind_cluster_reported && reported=1
  management_container_exists && container=1
  if (( reported == 0 && container == 0 )); then
    return 1
  fi
  (( reported == 1 && container == 1 )) \
    || die "management.ownership-refusal: partial same-named kind state is not safe to adopt or delete"
  validate_management_ownership
  if [[ ! -f "${MANAGEMENT_KUBECONFIG}" ]]; then
    kind export kubeconfig --name "${KIND_CLUSTER_NAME}" \
      --kubeconfig "${MANAGEMENT_KUBECONFIG}" >/dev/null
    chmod 0600 "${MANAGEMENT_KUBECONFIG}"
  fi
  management_kubectl get --raw=/readyz >/dev/null 2>&1 \
    || die "management.kubernetes: owned management API is unreachable"
}

delete_owned_kind_cluster() {
  local reported=0 container=0
  kind_cluster_reported && reported=1
  management_container_exists && container=1
  if (( reported == 0 && container == 0 )); then
    return 0
  fi
  (( reported == 1 && container == 1 )) \
    || die "management.ownership-refusal: partial same-named kind state is not safe to delete"
  validate_management_ownership
  kind delete cluster --name "${KIND_CLUSTER_NAME}" \
    --kubeconfig "${MANAGEMENT_KUBECONFIG}" >/dev/null
  ! kind_cluster_reported && ! management_container_exists \
    || die "management.cleanup: exact owned kind cluster remains"
}

cleanup_new_kind_cluster() {
  kind delete cluster --name "${KIND_CLUSTER_NAME}" >/dev/null 2>&1 || true
  rm -f "${MANAGEMENT_KUBECONFIG}" "${MANAGEMENT_OWNERSHIP_FILE}" \
    "${MANAGEMENT_NETWORK_FILE}" "${METALLB_RENDERED_MANIFEST}"
}

reconcile_kind_cluster() {
  require_command docker
  require_command kind
  ensure_runtime_layout
  local reported=0 container=0 created=0
  kind_cluster_reported && reported=1
  management_container_exists && container=1

  if (( reported == 1 || container == 1 )); then
    (( reported == 1 && container == 1 )) \
      || die "management.ownership-refusal: partial same-named kind state is not safe to adopt or delete"
    validate_management_ownership
    if [[ ! -f "${MANAGEMENT_KUBECONFIG}" ]]; then
      kind export kubeconfig --name "${KIND_CLUSTER_NAME}" \
        --kubeconfig "${MANAGEMENT_KUBECONFIG}" >/dev/null
      chmod 0600 "${MANAGEMENT_KUBECONFIG}"
    fi
  else
    rm -f "${MANAGEMENT_OWNERSHIP_FILE}" "${MANAGEMENT_NETWORK_FILE}" \
      "${METALLB_RENDERED_MANIFEST}" "${MANAGEMENT_KUBECONFIG}"
    if ! kind create cluster \
      --name "${KIND_CLUSTER_NAME}" \
      --config "${LAB_ROOT}/config/kind.yaml" \
      --kubeconfig "${MANAGEMENT_KUBECONFIG}" \
      --wait "${KIND_CREATE_TIMEOUT}"; then
      cleanup_new_kind_cluster
      die "management.kubernetes: kind failed to create Kubernetes ${KUBERNETES_VERSION}"
    fi
    chmod 0600 "${MANAGEMENT_KUBECONFIG}"
    record_management_ownership
    created=1
  fi

  if ! management_kubectl get --raw=/readyz >/dev/null 2>&1; then
    cleanup_if_introduced "${created}" cleanup_new_kind_cluster
    die "management.kubernetes: API readiness failed for the owned kind cluster"
  fi
  local server_version
  server_version="$(management_kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
  if [[ "${server_version}" != "${KUBERNETES_VERSION}" ]]; then
    cleanup_if_introduced "${created}" cleanup_new_kind_cluster
    die "management.kubernetes: expected ${KUBERNETES_VERSION}, found ${server_version}"
  fi
}

cleanup_cert_manager() {
  management_helm uninstall cert-manager --namespace cert-manager >/dev/null 2>&1 || true
  management_kubectl delete namespace cert-manager --ignore-not-found >/dev/null 2>&1 || true
}

reconcile_cert_manager() {
  local introduced=1
  management_helm status cert-manager --namespace cert-manager >/dev/null 2>&1 && introduced=0
  if ! management_helm upgrade --install cert-manager \
    "${INPUTS_DIR}/cert-manager-${CERT_MANAGER_VERSION}.tgz" \
    --namespace cert-manager \
    --create-namespace \
    --atomic \
    --wait \
    --timeout "${CERT_MANAGER_TIMEOUT}" \
    --set crds.enabled=true \
    --set resources.requests.cpu=100m \
    --set resources.requests.memory=128Mi \
    --set webhook.resources.requests.cpu=100m \
    --set webhook.resources.requests.memory=128Mi \
    --set cainjector.resources.requests.cpu=100m \
    --set cainjector.resources.requests.memory=128Mi; then
    cleanup_if_introduced "${introduced}" cleanup_cert_manager
    die "management.cert-manager: ${CERT_MANAGER_VERSION} installation or readiness failed"
  fi
  local deployment
  for deployment in cert-manager cert-manager-cainjector cert-manager-webhook; do
    if ! management_kubectl -n cert-manager rollout status "deployment/${deployment}" \
      --timeout="${CERT_MANAGER_TIMEOUT}" >/dev/null; then
      cleanup_if_introduced "${introduced}" cleanup_cert_manager
      die "management.cert-manager: deployment ${deployment} did not become ready"
    fi
  done
  if ! management_kubectl wait --for=condition=Established \
    crd/certificates.cert-manager.io crd/issuers.cert-manager.io \
    --timeout="${CERT_MANAGER_TIMEOUT}" >/dev/null; then
    cleanup_if_introduced "${introduced}" cleanup_cert_manager
    die "management.cert-manager: required CRDs did not become established"
  fi
  if [[ -z "$(management_kubectl -n cert-manager get endpoints cert-manager-webhook -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null)" ]]; then
    cleanup_if_introduced "${introduced}" cleanup_cert_manager
    die "management.cert-manager: webhook has no ready endpoint"
  fi
}

cleanup_metallb() {
  management_kubectl delete -f "${INPUTS_DIR}/metallb-native-${METALLB_VERSION}.yaml" \
    --ignore-not-found >/dev/null 2>&1 || true
}

apply_metallb_pool() {
  management_kubectl apply -f "${METALLB_RENDERED_MANIFEST}" >/dev/null 2>&1
}

reconcile_metallb() {
  local introduced=1
  management_kubectl get namespace metallb-system >/dev/null 2>&1 && introduced=0
  if ! management_kubectl apply \
    -f "${INPUTS_DIR}/metallb-native-${METALLB_VERSION}.yaml" >/dev/null; then
    cleanup_if_introduced "${introduced}" cleanup_metallb
    die "management.metallb: ${METALLB_VERSION} manifest application failed"
  fi
  if ! management_kubectl wait --for=condition=Established \
    crd/ipaddresspools.metallb.io crd/l2advertisements.metallb.io \
    --timeout="${METALLB_TIMEOUT}" >/dev/null \
    || ! management_kubectl -n metallb-system rollout status deployment/controller \
      --timeout="${METALLB_TIMEOUT}" >/dev/null \
    || ! management_kubectl -n metallb-system rollout status daemonset/speaker \
      --timeout="${METALLB_TIMEOUT}" >/dev/null; then
    cleanup_if_introduced "${introduced}" cleanup_metallb
    die "management.metallb: ${METALLB_VERSION} controller, speaker, or CRD readiness failed"
  fi
  if [[ -z "$(management_kubectl -n metallb-system get endpoints metallb-webhook-service -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null)" ]]; then
    cleanup_if_introduced "${introduced}" cleanup_metallb
    die "management.metallb: webhook has no ready endpoint"
  fi
  if ! wait_for "${METALLB_TIMEOUT}" "MetalLB admission webhook and VIP pool" \
    apply_metallb_pool; then
    if (( introduced == 1 )); then
      cleanup_if_introduced "${introduced}" cleanup_metallb
    else
      management_kubectl delete -f "${METALLB_RENDERED_MANIFEST}" --ignore-not-found >/dev/null 2>&1 || true
    fi
    die "management.metallb: deterministic VIP pool application failed"
  fi
}

cleanup_kamaji() {
  local claim
  management_helm uninstall kamaji --namespace "${MANAGEMENT_NAMESPACE}" >/dev/null 2>&1 || true
  while IFS= read -r claim; do
    [[ -n "${claim}" ]] || continue
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete "${claim}" \
      --ignore-not-found >/dev/null 2>&1 || true
  done < <(
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pvc -o name 2>/dev/null \
      | grep '^persistentvolumeclaim/data-kamaji-etcd-' || true
  )
  management_kubectl delete namespace "${MANAGEMENT_NAMESPACE}" --ignore-not-found >/dev/null 2>&1 || true
}

destroy_kamaji_shared_resources() {
  management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete job kamaji-etcd-setup-2 \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  management_kubectl delete datastore default \
    --ignore-not-found --wait=true --timeout="${DATASTORE_TIMEOUT}" >/dev/null 2>&1 || true
  management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete \
    serviceaccount/kamaji-etcd \
    role.rbac.authorization.k8s.io/kamaji-etcd-gen-certs-role \
    rolebinding.rbac.authorization.k8s.io/kamaji-etcd-gen-certs-rolebinding \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  management_helm uninstall kamaji --namespace "${MANAGEMENT_NAMESPACE}" \
    >/dev/null 2>&1 || true
  if [[ -f "${KAMAJI_RENDERED_MANIFEST}" ]]; then
    management_kubectl delete -f "${KAMAJI_RENDERED_MANIFEST}" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  management_kubectl delete \
    crd/datastores.kamaji.clastix.io \
    crd/kubeconfiggenerators.kamaji.clastix.io \
    crd/tenantcontrolplanes.kamaji.clastix.io \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  management_kubectl delete namespace "${MANAGEMENT_NAMESPACE}" \
    --ignore-not-found --wait=true --timeout="${DATASTORE_TIMEOUT}" >/dev/null 2>&1 || true
}

destroy_metallb_shared_resources() {
  management_kubectl delete -f "${METALLB_RENDERED_MANIFEST}" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  cleanup_metallb
  management_kubectl delete namespace metallb-system \
    --ignore-not-found --wait=true --timeout="${METALLB_TIMEOUT}" >/dev/null 2>&1 || true
}

destroy_cert_manager_shared_resources() {
  cleanup_cert_manager
  local crd
  while IFS= read -r crd; do
    [[ -n "${crd}" ]] || continue
    management_kubectl delete "${crd}" --ignore-not-found --wait=false \
      >/dev/null 2>&1 || true
  done < <(
    management_kubectl get crd -o name 2>/dev/null \
      | grep -E '\.(cert-manager\.io|acme\.cert-manager\.io)$' || true
  )
}

render_kamaji_hook_manifests() {
  KAMAJI_RENDERED_MANIFEST="${KAMAJI_RENDERED_MANIFEST}" \
  KAMAJI_PRE_HOOKS_MANIFEST="${KAMAJI_PRE_HOOKS_MANIFEST}" \
  KAMAJI_POST_HOOKS_MANIFEST="${KAMAJI_POST_HOOKS_MANIFEST}" \
  python3 -c '
import os
import re
from pathlib import Path

documents = re.split(r"(?m)^---\s*$", Path(os.environ["KAMAJI_RENDERED_MANIFEST"]).read_text(encoding="utf-8"))
hooks = [document.strip() for document in documents if "helm.sh/hook" in document]
pre = [document for document in hooks if not re.search(r"(?m)^\s*name:\s*[\"\x27]?kamaji-etcd-setup-2[\"\x27]?\s*$", document)]
post = [document for document in hooks if re.search(r"(?m)^\s*name:\s*[\"\x27]?kamaji-etcd-setup-2[\"\x27]?\s*$", document)]
if not pre or len(post) != 1:
    raise SystemExit("unexpected locked Kamaji hook inventory")
for variable, selected in (
    ("KAMAJI_PRE_HOOKS_MANIFEST", pre),
    ("KAMAJI_POST_HOOKS_MANIFEST", post),
):
    path = Path(os.environ[variable])
    path.write_text("\n---\n".join(selected) + "\n", encoding="utf-8")
    path.chmod(0o600)
'
}

run_kamaji_post_install_hook() {
  management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete job kamaji-etcd-setup-2 \
    --ignore-not-found --wait=true --timeout="${DATASTORE_TIMEOUT}" \
    >/dev/null 2>&1 || true
  management_kubectl apply -f "${KAMAJI_POST_HOOKS_MANIFEST}" >/dev/null \
    || return 1
  if ! management_kubectl -n "${MANAGEMENT_NAMESPACE}" wait \
    --for=condition=Complete job/kamaji-etcd-setup-2 \
    --timeout="${DATASTORE_TIMEOUT}" >/dev/null; then
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" logs job/kamaji-etcd-setup-2 \
      --all-containers=true --tail=100 >&2 || true
    return 1
  fi
  local images
  images="$(
    management_kubectl -n "${MANAGEMENT_NAMESPACE}" get job kamaji-etcd-setup-2 \
      -o jsonpath='{range .spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}'
  )"
  grep -Fxq "${KAMAJI_KUBECTL_JOB_IMAGE}" <<<"${images}" \
    && grep -Fxq "${KAMAJI_ETCD_JOB_IMAGE}" <<<"${images}" \
    || return 1
  management_kubectl -n "${MANAGEMENT_NAMESPACE}" delete job kamaji-etcd-setup-2 \
    --wait=true --timeout="${DATASTORE_TIMEOUT}" >/dev/null
}

datastore_ready() {
  [[ "$(management_kubectl get datastore default \
    -o jsonpath='{.status.ready}' 2>/dev/null)" == "true" ]]
}

reconcile_kamaji() {
  local introduced=1
  management_helm status kamaji --namespace "${MANAGEMENT_NAMESPACE}" >/dev/null 2>&1 && introduced=0
  "${LAB_ROOT}/scripts/render-kamaji.sh" render
  render_kamaji_hook_manifests
  if ! management_kubectl get namespace "${MANAGEMENT_NAMESPACE}" >/dev/null 2>&1; then
    management_kubectl create namespace "${MANAGEMENT_NAMESPACE}" >/dev/null
  fi
  management_kubectl label namespace "${MANAGEMENT_NAMESPACE}" \
    "${OWNERSHIP_LABEL}=${LAB_PREFIX}" --overwrite >/dev/null
  if ! management_kubectl -n "${MANAGEMENT_NAMESPACE}" apply \
    -f "${KAMAJI_PRE_HOOKS_MANIFEST}" >/dev/null; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.kamaji: digest-pinned hook prerequisites failed"
  fi
  if ! management_helm upgrade --install kamaji "${KAMAJI_CHART_DIR}" \
    --namespace "${MANAGEMENT_NAMESPACE}" \
    --no-hooks \
    --atomic \
    --wait \
    --timeout "${KAMAJI_TIMEOUT}" \
    --values "${LAB_ROOT}/config/kamaji-values.yaml" \
    --post-renderer "${LAB_ROOT}/scripts/render-kamaji.sh"; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.kamaji: ${KAMAJI_VERSION} locked chart installation failed"
  fi
  "${LAB_ROOT}/scripts/render-kamaji.sh" validate
  if ! management_kubectl -n "${MANAGEMENT_NAMESPACE}" apply \
    -f "${KAMAJI_PRE_HOOKS_MANIFEST}" >/dev/null; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.kamaji: digest-pinned hook prerequisites were not retained"
  fi

  if ! management_kubectl wait --for=condition=Established \
    crd/datastores.kamaji.clastix.io \
    crd/kubeconfiggenerators.kamaji.clastix.io \
    crd/tenantcontrolplanes.kamaji.clastix.io \
    --timeout="${KAMAJI_TIMEOUT}" >/dev/null \
    || ! management_kubectl -n "${MANAGEMENT_NAMESPACE}" rollout status deployment/kamaji \
      --timeout="${KAMAJI_TIMEOUT}" >/dev/null; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.kamaji: controller or CRD readiness failed"
  fi
  if [[ -z "$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get endpoints kamaji-webhook-service -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null)" ]]; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.kamaji: webhook has no ready endpoint"
  fi
  if ! management_kubectl -n "${MANAGEMENT_NAMESPACE}" rollout status statefulset/kamaji-etcd \
    --timeout="${DATASTORE_TIMEOUT}" >/dev/null; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.datastore: three-member etcd StatefulSet did not become ready"
  fi
  if ! run_kamaji_post_install_hook; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.datastore: digest-pinned datastore setup hook failed"
  fi
  if ! wait_for "${DATASTORE_TIMEOUT}" "DataStore/default readiness" datastore_ready; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.datastore: DataStore/default did not become ready"
  fi
  if [[ "$(management_kubectl -n "${MANAGEMENT_NAMESPACE}" get pvc -o json \
      | python3 -c 'import json,sys; print(sum(1 for item in json.load(sys.stdin)["items"] if item["metadata"]["name"].startswith("data-kamaji-etcd-") and item.get("status", {}).get("phase") == "Bound"))')" -ne "${KAMAJI_ETCD_REPLICAS}" ]]; then
    cleanup_if_introduced "${introduced}" cleanup_kamaji
    die "management.datastore: expected ${KAMAJI_ETCD_REPLICAS} bound datastore PVCs"
  fi
}

reconcile_management_plane() {
  reconcile_kind_cluster
  reconcile_management_network
  reconcile_cert_manager
  reconcile_metallb
  reconcile_kamaji
}
