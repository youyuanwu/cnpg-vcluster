#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

requested_tenant="${1:-}"

printf '== Docker lab resources ==\n'
docker ps -a --filter "label=cnpg-vcluster.lab=${LAB_PREFIX}" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Networks}}' || true

if [[ -s "${HOST_KUBECONFIG}" ]]; then
  printf '\n== kind nodes ==\n'
  kubectl_host get nodes -o wide || true
  printf '\n== Platform and tenant control planes ==\n'
  kubectl_host get pods -A -o wide || true
fi

for tenant in ${TENANT_NAMES}; do
  [[ -z "${requested_tenant}" || "${requested_tenant}" == "${tenant}" ]] || continue
  printf '\n== %s API ==\n' "${tenant}"
  if [[ -s "$(tenant_kubeconfig "${tenant}")" ]]; then
    kubectl_tenant "${tenant}" get nodes -o wide || true
    kubectl_tenant "${tenant}" -n kube-system get pods -o wide || true
    kubectl_tenant "${tenant}" get storageclass,pv,pvc -A || true
    kubectl_tenant "${tenant}" get events -A --sort-by=.lastTimestamp | tail -40 || true
  fi

  for index in $(seq 1 "${WORKERS_PER_TENANT}"); do
    name="$(worker_name "${tenant}" "${index}")"
    docker container inspect "${name}" >/dev/null 2>&1 || continue
    printf '\n-- %s services --\n' "${name}"
    docker exec "${name}" systemctl --no-pager --full status \
      containerd kubelet vcluster-vpn 2>&1 | tail -80 || true
    printf '\n-- %s journal --\n' "${name}"
    docker exec "${name}" journalctl --no-pager -u vcluster-vpn -u kubelet -n 80 || true
  done
done
