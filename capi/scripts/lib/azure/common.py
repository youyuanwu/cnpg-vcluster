from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Mapping, Sequence

from scripts.lib.config import (
    ConfigError,
    load_configuration,
    load_env_file,
    parse_duration,
    require,
)
from scripts.lib.files import (
    ensure_private_dir,
    private_file_exists,
    read_private_file,
    write_private_file,
)
from scripts.lib.locking import azure_lock, azure_lock_exists, e2e_lock, tools_lock
from scripts.lib.management import _prepare_kamaji_chart
from scripts.lib.process import run
from scripts.lib.redaction import redact, redact_value
from scripts.lib.tenant_runtime import (
    OperationJournal,
    TenantIdentity,
    TenantRuntime,
    foundation_sha256,
    recorded_tenant_names,
)
from scripts.lib.tenant_spec import (
    TenantSpec,
    require_non_overlapping_networks,
    validate_tenant_name,
)
from scripts.lib.tenant_status import TenantStatus
from scripts.lib.tenants import LIFECYCLE_MARKERS, lifecycle_markers, resource_lifecycle_markers


PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
FOUNDATION_INVENTORY_SCHEMA = 2
READY_EVIDENCE_MAX_AGE_SECONDS = 24 * 60 * 60
FOUNDATION_DEFAULT_KEYS = (
    "AZURE_AKS_KUBERNETES_VERSION",
    "AZURE_AKS_NODE_SKU",
    "AZURE_AKS_NODE_COUNT",
    "AZURE_VNET_CIDR",
    "AZURE_AKS_SUBNET_CIDR",
    "AZURE_TENANT_SUBNET_CIDR",
    "AZURE_AKS_POD_CIDR",
    "AZURE_AKS_SERVICE_CIDR",
    "AZURE_AKS_DNS_SERVICE_IP",
    "AZURE_CAPI_VERSION",
    "AZURE_CAPZ_VERSION",
    "AZURE_KAMAJI_CAPI_VERSION",
    "AZURE_KAMAJI_CHART_VERSION",
)
REQUIRED_PROVIDERS = (
    "Microsoft.Authorization",
    "Microsoft.Compute",
    "Microsoft.ContainerService",
    "Microsoft.ManagedIdentity",
    "Microsoft.Network",
)
CONTROLLER_DEPLOYMENTS = (
    ("capi-system", "capi-controller-manager"),
    ("capi-kubeadm-bootstrap-system", "capi-kubeadm-bootstrap-controller-manager"),
    ("capz-system", "capz-controller-manager"),
    ("capz-system", "azureserviceoperator-controller-manager"),
    ("kamaji-system", "kamaji"),
    ("kamaji-system", "capi-kamaji-controller-manager"),
)
CAPZ_EXTERNAL_CONTROL_PLANE_LABEL = "cnpg-vcluster-external-control-plane"
CAPZ_AZURECLUSTER_WEBHOOK = "default.azurecluster.infrastructure.cluster.x-k8s.io"
MANAGEMENT_RESOURCE_PLURALS = {
    "AzureClusterIdentity": "azureclusteridentities",
    "Cluster": "clusters",
    "ConfigMap": "configmaps",
    "Job": "jobs",
    "MachinePool": "machinepools",
    "Namespace": "namespaces",
}
KNOWN_AZURE_TENANT_TYPES = frozenset(
    {
        "microsoft.compute/virtualmachinescalesets",
        "microsoft.compute/virtualmachinescalesets/virtualmachines",
        "microsoft.network/networkinterfaces",
        "microsoft.network/natgateways",
        "microsoft.network/publicipaddresses",
    }
)
KNOWN_ASO_TENANT_KINDS = frozenset({"NatGateway", "PublicIPAddress"})
KNOWN_ASO_FOUNDATION_REFERENCE_OUTPUTS = {
    "ResourceGroup": "resourceGroupId",
    "VirtualNetwork": "vnetId",
    "VirtualNetworksSubnet": "tenantSubnetId",
}
KNOWN_CONTROLLER_MANAGEMENT_KINDS = frozenset(
    {
        "AzureCluster",
        "AzureMachinePool",
        "AzureMachinePoolMachine",
        "Certificate",
        "CertificateRequest",
        "Cluster",
        "Endpoints",
        "Issuer",
        "KamajiControlPlane",
        "KubeadmConfig",
        "Machine",
        "MachinePool",
        "MachineSet",
        "NatGateway",
        "PublicIPAddress",
        "ResourceGroup",
        "PodDisruptionBudget",
        "PersistentVolumeClaim",
        "Role",
        "RoleBinding",
        "Secret",
        "Service",
        "StatefulSet",
        "TenantControlPlane",
        "VirtualNetwork",
        "VirtualNetworksSubnet",
    }
)
KNOWN_ORCHESTRATION_MANAGEMENT_KINDS = frozenset(
    {
        "AzureClusterIdentity",
        "ConfigMap",
        "Deployment",
        "Job",
        "Namespace",
    }
)
KNOWN_NAMESPACE_CHILD_KINDS = frozenset(
    {
        "Endpoints",
        "EndpointSlice",
        "Event",
        "Lease",
        "Pod",
        "PodMetrics",
        "ReplicaSet",
        "ServiceAccount",
    }
)
REMOVED_TENANT_CONFIG_KEYS = frozenset(
    {
        "AZURE_TENANT_KUBERNETES_VERSION",
        "AZURE_TENANT_NODE_COUNT",
        "AZURE_TENANT_POD_CIDR",
        "AZURE_TENANT_SERVICE_CIDR",
        "AZURE_TENANT_DNS_SERVICE_IP",
    }
)


class AzureDeletionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        management: Mapping[str, object],
        azure: Mapping[str, object],
    ) -> None:
        super().__init__(message)
        self.management = dict(management)
        self.azure = dict(azure)


def load_azure_configuration(root: Path) -> dict[str, str]:
    defaults = load_env_file(root / "config" / "azure" / "defaults.env")
    local_path = root / "config" / "azure.local.env"
    details = local_path.lstat()
    if (
        local_path.is_symlink()
        or not local_path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise ConfigError("config/azure.local.env must be an owner-only regular file")
    local = load_env_file(local_path)
    duplicates = defaults.keys() & local.keys()
    if duplicates:
        raise ConfigError(f"duplicate Azure configuration keys: {sorted(duplicates)}")
    config = defaults | local
    legacy = sorted(REMOVED_TENANT_CONFIG_KEYS & config.keys())
    if legacy:
        raise ConfigError(
            "pre-cutover Azure tenant configuration is unsupported: "
            + ", ".join(legacy)
        )
    require(
        config,
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_LOCATION",
        "AZURE_PREFIX",
        "AZURE_AKS_KUBERNETES_VERSION",
        "AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION",
        "AZURE_AKS_NODE_SKU",
        "AZURE_TENANT_NODE_SKU",
        "AZURE_AKS_NODE_COUNT",
        "AZURE_VNET_CIDR",
        "AZURE_AKS_SUBNET_CIDR",
        "AZURE_TENANT_SUBNET_CIDR",
        "AZURE_AKS_POD_CIDR",
        "AZURE_AKS_SERVICE_CIDR",
        "AZURE_AKS_DNS_SERVICE_IP",
        "AZURE_CAPI_VERSION",
        "AZURE_CAPZ_VERSION",
        "AZURE_KAMAJI_CAPI_VERSION",
        "AZURE_KAMAJI_CHART_VERSION",
        "AZURE_CLOUD_PROVIDER_VERSION",
        "AZURE_CALICO_VERSION",
        "AZURE_DEPLOY_TIMEOUT",
        "AZURE_CONTROLLER_TIMEOUT",
        "AZURE_TENANT_TIMEOUT",
    )
    prefix = config["AZURE_PREFIX"]
    if not PREFIX_RE.fullmatch(prefix):
        raise ConfigError(
            "AZURE_PREFIX must start with a lowercase letter and contain "
            "2-20 lowercase letters, digits, or hyphens"
        )
    return config


def names(config: Mapping[str, str]) -> dict[str, str]:
    prefix = config["AZURE_PREFIX"]
    return {
        "deployment": f"{prefix}-foundation",
        "resourceGroup": f"{prefix}-rg",
        "aks": f"{prefix}-mgmt",
        "vnet": f"{prefix}-vnet",
        "identity": f"{prefix}-identity",
    }


def tenant_names(spec: TenantSpec) -> dict[str, str]:
    validate_tenant_name(spec.name)
    return {
        "namespace": spec.name,
        "cluster": spec.name,
        "azureClusterIdentity": f"{spec.name}-identity",
        "azureCluster": spec.name,
        "controlPlane": spec.name,
        "pool": f"{spec.name}-worker",
        "cloudValues": f"{spec.name}-azure-cloud-provider-values",
        "networkValues": f"{spec.name}-calico-values",
        "addonJob": f"{spec.name}-install-addons",
        "statusProbe": f"{spec.name}-status-probe",
    }


def _az(*arguments: str, timeout: int = 300, check: bool = True):
    return run(["az", *arguments], timeout=timeout, check=check)


def _json(command: Sequence[str], timeout: int = 300) -> object:
    result = run(command, timeout=timeout)
    return json.loads(result.stdout)


def _azure_id_equal(left: object, right: object) -> bool:
    return str(left or "").lower() == str(right or "").lower()


def _foundation_networks(config: Mapping[str, str]) -> dict[str, ipaddress.IPv4Network]:
    try:
        return {
            key: ipaddress.ip_network(config[key], strict=True)
            for key in (
                "AZURE_VNET_CIDR",
                "AZURE_AKS_SUBNET_CIDR",
                "AZURE_TENANT_SUBNET_CIDR",
                "AZURE_AKS_POD_CIDR",
                "AZURE_AKS_SERVICE_CIDR",
            )
        }
    except ValueError as exc:
        raise ConfigError(f"invalid Azure foundation network: {exc}") from exc


def _validate_foundation_networks(config: Mapping[str, str]) -> None:
    networks = _foundation_networks(config)
    vnet = networks["AZURE_VNET_CIDR"]
    for key in ("AZURE_AKS_SUBNET_CIDR", "AZURE_TENANT_SUBNET_CIDR"):
        if not networks[key].subnet_of(vnet):
            raise ConfigError(f"{key} must be contained by AZURE_VNET_CIDR")
    comparable = {
        key: value
        for key, value in networks.items()
        if key != "AZURE_VNET_CIDR"
    }
    items = list(comparable.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1 :]:
            if left.overlaps(right):
                raise ConfigError(
                    f"Azure network ranges overlap: {left_name}, {right_name}"
                )
    dns = ipaddress.ip_address(config["AZURE_AKS_DNS_SERVICE_IP"])
    if dns not in networks["AZURE_AKS_SERVICE_CIDR"]:
        raise ConfigError("AZURE_AKS_DNS_SERVICE_IP is outside the AKS service CIDR")


def _recorded_azure_specs(root: Path, *, excluding: str) -> tuple[TenantSpec, ...]:
    specs = []
    for tenant in recorded_tenant_names(root):
        if tenant == excluding:
            continue
        runtime = TenantRuntime(root, tenant)
        if runtime.identity_exists():
            specs.append(runtime.load_identity().specification)
        elif runtime.operation_exists():
            specs.append(TenantSpec.from_mapping(runtime.load_operation().specification))
    return tuple(specs)


def _validate_networks(
    config: Mapping[str, str],
    spec: TenantSpec | None = None,
    *,
    recorded_specs: Sequence[TenantSpec] = (),
) -> None:
    _validate_foundation_networks(config)
    if spec is None:
        return
    shared = _foundation_networks(config)
    conflicts = {
        "Azure VNet": shared["AZURE_VNET_CIDR"],
        "AKS subnet": shared["AZURE_AKS_SUBNET_CIDR"],
        "tenant node subnet": shared["AZURE_TENANT_SUBNET_CIDR"],
        "AKS Pod CIDR": shared["AZURE_AKS_POD_CIDR"],
        "AKS Service CIDR": shared["AZURE_AKS_SERVICE_CIDR"],
    }
    for existing in recorded_specs:
        conflicts[f"tenant {existing.name} Pod CIDR"] = existing.pod_network
        conflicts[f"tenant {existing.name} Service CIDR"] = existing.service_network
    require_non_overlapping_networks(spec, conflicts)


def _active_subscription(config: Mapping[str, str]) -> dict[str, object]:
    account = _json(["az", "account", "show", "--output", "json"])
    if account.get("id") != config["AZURE_SUBSCRIPTION_ID"]:
        raise RuntimeError("active Azure subscription does not match azure.local.env")
    if account.get("state") != "Enabled":
        raise RuntimeError("configured Azure subscription is not enabled")
    return account


def _sku_available(config: Mapping[str, str], sku: str) -> None:
    payload = _json(
        [
            "az",
            "vm",
            "list-skus",
            "--location",
            config["AZURE_LOCATION"],
            "--resource-type",
            "virtualMachines",
            "--size",
            sku,
            "--all",
            "--output",
            "json",
        ],
        timeout=600,
    )
    matching = [item for item in payload if item.get("name") == sku]
    if len(matching) != 1:
        raise RuntimeError(f"Azure VM SKU is unavailable: {sku}")
    if any(
        restriction.get("type") == "Location"
        for restriction in matching[0].get("restrictions", [])
    ):
        raise RuntimeError(f"Azure VM SKU is blocked in the region: {sku}")


def _reference_image_available(
    config: Mapping[str, str],
    kubernetes_version: str,
) -> None:
    result = _az(
        "sig",
        "image-version",
        "show-community",
        "--public-gallery-name",
        "ClusterAPI-f72ceb4f-5159-4c26-a0fe-2ea738f0d019",
        "--gallery-image-definition",
        "capi-ubun2-2404",
        "--gallery-image-version",
        kubernetes_version,
        "--location",
        config["AZURE_LOCATION"],
        "--output",
        "none",
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"CAPZ reference image {kubernetes_version} is unavailable in "
            f"{config['AZURE_LOCATION']}"
        )


def _foundation_defaults_checksum(
    root: Path,
    config: Mapping[str, str] | None = None,
) -> str:
    selected = (
        load_env_file(root / "config" / "azure" / "defaults.env")
        if config is None
        else config
    )
    payload = {key: selected[key] for key in FOUNDATION_DEFAULT_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
def _azure_runtime_path(root: Path) -> Path:
    return root / ".runtime" / "azure"


def _runtime_dir(root: Path) -> Path:
    path = _azure_runtime_path(root)
    ensure_private_dir(path)
    return path


def azure_tenant_runtime_path(root: Path, tenant: str) -> Path:
    validate_tenant_name(tenant)
    return _azure_runtime_path(root) / "tenants" / tenant


def _tenant_runtime_dir(root: Path, tenant: str) -> Path:
    path = azure_tenant_runtime_path(root, tenant)
    ensure_private_dir(path)
    return path


def _management_kubeconfig(root: Path) -> Path:
    path = _azure_runtime_path(root) / "management.kubeconfig"
    details = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("Azure management kubeconfig must be owner-only")
    return path


def _tenant_kubeconfig(root: Path, tenant: str) -> Path:
    path = azure_tenant_runtime_path(root, tenant) / "kubeconfig"
    details = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("Azure tenant kubeconfig must be owner-only")
    return path


def _kubectl(
    root: Path,
    *arguments: str,
    timeout: int = 300,
    check: bool = True,
    input_text: str | None = None,
):
    return run(
        [
            str(root / ".tools" / "bin" / "kubectl"),
            "--kubeconfig",
            str(_management_kubeconfig(root)),
            *arguments,
        ],
        timeout=timeout,
        check=check,
        input_text=input_text,
    )


def _tenant_kubectl(
    root: Path,
    tenant: str,
    *arguments: str,
    timeout: int = 300,
    check: bool = True,
):
    validate_tenant_name(tenant)
    probe = f"{tenant}-status-probe"
    return _kubectl(
        root,
        "-n",
        tenant,
        "exec",
        f"deployment/{probe}",
        "--",
        "kubectl",
        "--kubeconfig",
        "/tenant/value",
        *arguments,
        timeout=timeout,
        check=check,
    )


def _helm(root: Path, *arguments: str, timeout: int = 300, check: bool = True):
    return run(
        [
            str(root / ".tools" / "bin" / "helm"),
            "--kubeconfig",
            str(_management_kubeconfig(root)),
            *arguments,
        ],
        timeout=timeout,
        check=check,
    )

__all__ = [name for name in globals() if not name.startswith("__")]
