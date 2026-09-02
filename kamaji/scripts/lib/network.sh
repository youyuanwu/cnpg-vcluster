#!/usr/bin/env bash

set -Eeuo pipefail
NETWORK_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck disable=SC1091
source "${NETWORK_LIB_DIR}/common.sh"

kind_network_name() {
  printf 'kind\n'
}

write_management_network() {
  local inspect_json="$1"
  local selected
  selected="$(
    DOCKER_NETWORK_INSPECT="${inspect_json}" \
    EXCLUDED_CIDRS="${MANAGEMENT_POD_CIDR} ${MANAGEMENT_SERVICE_CIDR} ${TENANT_A_POD_CIDR} ${TENANT_A_SERVICE_CIDR} ${TENANT_B_POD_CIDR} ${TENANT_B_SERVICE_CIDR}" \
    python3 -c '
import ipaddress
import json
import os

payload = json.loads(os.environ["DOCKER_NETWORK_INSPECT"])[0]
subnets = [
    ipaddress.ip_network(item["Subnet"])
    for item in payload["IPAM"]["Config"]
    if item.get("Subnet") and ipaddress.ip_network(item["Subnet"]).version == 4
]
if len(subnets) != 1:
    raise SystemExit(f"expected one kind IPv4 subnet, found {len(subnets)}")
subnet = subnets[0]
excluded = [ipaddress.ip_network(value) for value in os.environ["EXCLUDED_CIDRS"].split()]
if any(subnet.overlaps(network) for network in excluded):
    raise SystemExit(f"kind Docker subnet {subnet} overlaps a configured cluster network")

used = set()
for item in payload["IPAM"]["Config"]:
    if item.get("Gateway"):
        used.add(ipaddress.ip_address(item["Gateway"]))
for container in payload.get("Containers", {}).values():
    address = container.get("IPv4Address", "").split("/")[0]
    if address:
        used.add(ipaddress.ip_address(address))

selected = []
candidate = int(subnet.broadcast_address) - 1
lower = int(subnet.network_address) + 1
while candidate >= lower and len(selected) < 2:
    address = ipaddress.ip_address(candidate)
    if address not in used:
        selected.append(address)
    candidate -= 1
if len(selected) != 2:
    raise SystemExit(f"kind Docker subnet {subnet} has fewer than two free IPv4 addresses")

print(f"DOCKER_NETWORK=kind")
print(f"DOCKER_SUBNET={subnet}")
print(f"TENANT_A_VIP={selected[0]}")
print(f"TENANT_B_VIP={selected[1]}")
'
  )" || die "network.vip-selection: unable to derive two free kind-network VIPs"
  printf '%s\n' "${selected}" | write_secret_file "${MANAGEMENT_NETWORK_FILE}"
}

load_management_network() {
  [[ -f "${MANAGEMENT_NETWORK_FILE}" ]] \
    || die "management network record is missing"
  # shellcheck disable=SC1090
  source "${MANAGEMENT_NETWORK_FILE}"
}

validate_management_network() {
  local inspect_json="$1"
  load_management_network
  DOCKER_NETWORK_INSPECT="${inspect_json}" \
  DOCKER_SUBNET="${DOCKER_SUBNET}" \
  TENANT_A_VIP="${TENANT_A_VIP}" \
  TENANT_B_VIP="${TENANT_B_VIP}" \
  EXCLUDED_CIDRS="${MANAGEMENT_POD_CIDR} ${MANAGEMENT_SERVICE_CIDR} ${TENANT_A_POD_CIDR} ${TENANT_A_SERVICE_CIDR} ${TENANT_B_POD_CIDR} ${TENANT_B_SERVICE_CIDR}" \
  python3 -c '
import ipaddress
import json
import os

payload = json.loads(os.environ["DOCKER_NETWORK_INSPECT"])[0]
actual = {
    ipaddress.ip_network(item["Subnet"])
    for item in payload["IPAM"]["Config"]
    if item.get("Subnet") and ipaddress.ip_network(item["Subnet"]).version == 4
}
subnet = ipaddress.ip_network(os.environ["DOCKER_SUBNET"])
if subnet not in actual:
    raise SystemExit(f"recorded kind subnet {subnet} is not current")
vips = [ipaddress.ip_address(os.environ["TENANT_A_VIP"]), ipaddress.ip_address(os.environ["TENANT_B_VIP"])]
if len(set(vips)) != 2 or any(address not in subnet for address in vips):
    raise SystemExit("recorded VIPs are not distinct addresses inside the kind subnet")
excluded = [ipaddress.ip_network(value) for value in os.environ["EXCLUDED_CIDRS"].split()]
if any(any(address in network for network in excluded) for address in vips):
    raise SystemExit("recorded VIP overlaps a configured cluster network")
used = set()
for item in payload["IPAM"]["Config"]:
    if item.get("Gateway"):
        used.add(ipaddress.ip_address(item["Gateway"]))
for container in payload.get("Containers", {}).values():
    address = container.get("IPv4Address", "").split("/")[0]
    if address:
        used.add(ipaddress.ip_address(address))
if any(address in used for address in vips):
    raise SystemExit("recorded VIP is assigned to a Docker endpoint")
'
}

render_metallb_pool() {
  load_management_network
  LAB_ROOT="${LAB_ROOT}" \
  METALLB_RENDERED_MANIFEST="${METALLB_RENDERED_MANIFEST}" \
  TENANT_A_VIP="${TENANT_A_VIP}" \
  TENANT_B_VIP="${TENANT_B_VIP}" \
    python3 -c '
import os
from pathlib import Path

source = Path(os.environ["LAB_ROOT"]) / "manifests/metallb/pool.yaml.tpl"
data = source.read_text(encoding="utf-8")
for name in ("TENANT_A_VIP", "TENANT_B_VIP"):
    data = data.replace("${" + name + "}", os.environ[name])
destination = Path(os.environ["METALLB_RENDERED_MANIFEST"])
destination.write_text(data, encoding="utf-8")
destination.chmod(0o600)
'
}

reconcile_management_network() {
  require_command docker
  require_command python3
  ensure_runtime_layout
  local network inspect_json
  network="$(kind_network_name)"
  docker network inspect "${network}" >/dev/null 2>&1 \
    || die "network.kind: Docker network ${network} does not exist after kind creation"
  inspect_json="$(docker network inspect "${network}")"
  if [[ -f "${MANAGEMENT_NETWORK_FILE}" ]]; then
    validate_management_network "${inspect_json}" \
      || die "network.vip-conflict: recorded management VIP assignment is no longer safe"
  else
    write_management_network "${inspect_json}"
    validate_management_network "${inspect_json}" \
      || die "network.vip-conflict: generated management VIP assignment is not safe"
  fi
  render_metallb_pool
}
