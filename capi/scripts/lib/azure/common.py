from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
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
from scripts.lib.tenant_spec import (
    TenantSpec,
    validate_tenant_name,
)


PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
CONTROLLER_REPOSITORY_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)
CONTROLLER_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
FOUNDATION_INVENTORY_SCHEMA = 3
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
    "AZURE_CNPG_VERSION",
    "AZURE_DISK_CSI_VERSION",
    "AZURE_CONTROLLER_REPOSITORY",
    "AZURE_CONTROLLER_TAG",
)
REQUIRED_PROVIDERS = (
    "Microsoft.Authorization",
    "Microsoft.Compute",
    "Microsoft.ContainerService",
    "Microsoft.ManagedIdentity",
    "Microsoft.Network",
)
PLATFORM_CONTROLLER_DEPLOYMENTS = (
    ("capi-system", "capi-controller-manager"),
    ("capi-kubeadm-bootstrap-system", "capi-kubeadm-bootstrap-controller-manager"),
    ("capz-system", "capz-controller-manager"),
    ("capz-system", "azureserviceoperator-controller-manager"),
    ("kamaji-system", "kamaji"),
    ("kamaji-system", "capi-kamaji-controller-manager"),
)
CONTROLLER_DEPLOYMENTS = PLATFORM_CONTROLLER_DEPLOYMENTS + (
    ("tenant-system", "tenant-controller"),
)
CAPZ_EXTERNAL_CONTROL_PLANE_LABEL = "cnpg-vcluster-external-control-plane"
CAPZ_AZURECLUSTER_WEBHOOK = "default.azurecluster.infrastructure.cluster.x-k8s.io"
REMOVED_TENANT_CONFIG_KEYS = frozenset(
    {
        "AZURE_TENANT_KUBERNETES_VERSION",
        "AZURE_TENANT_NODE_COUNT",
        "AZURE_TENANT_POD_CIDR",
        "AZURE_TENANT_SERVICE_CIDR",
        "AZURE_TENANT_DNS_SERVICE_IP",
    }
)

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
        "AZURE_TENANT_ALLOCATION_APPROVED_SHA256",
        "AZURE_CONTROLLER_REPOSITORY",
        "AZURE_CONTROLLER_TAG",
        "AZURE_ADMIN_REPOSITORY",
        "AZURE_ADMIN_TAG",
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
    for key in ("AZURE_CONTROLLER_REPOSITORY", "AZURE_ADMIN_REPOSITORY"):
        repository = config[key]
        if (
            len(repository) > 255
            or not CONTROLLER_REPOSITORY_RE.fullmatch(repository)
        ):
            raise ConfigError(
                f"{key} must be a lowercase OCI repository path"
            )
    for key in ("AZURE_CONTROLLER_TAG", "AZURE_ADMIN_TAG"):
        if not CONTROLLER_TAG_RE.fullmatch(config[key]):
            raise ConfigError(f"{key} must be a valid OCI tag")
    if not re.fullmatch(
        r"[0-9a-f]{64}", config["AZURE_TENANT_ALLOCATION_APPROVED_SHA256"]
    ):
        raise ConfigError(
            "AZURE_TENANT_ALLOCATION_APPROVED_SHA256 must be a lowercase SHA-256"
        )
    for key, expected in (
        ("AZURE_CNPG_VERSION", "1.30.0"),
        ("AZURE_DISK_CSI_VERSION", "v1.32.12"),
    ):
        if key in config and config[key] != expected:
            raise ConfigError(f"{key} must be {expected}")
    return config


def names(config: Mapping[str, str]) -> dict[str, str]:
    prefix = config["AZURE_PREFIX"]
    return {
        "deployment": f"{prefix}-foundation",
        "resourceGroup": f"{prefix}-rg",
        "aks": f"{prefix}-mgmt",
        "vnet": f"{prefix}-vnet",
        "identity": f"{prefix}-identity",
        "acr": f"{prefix.replace('-', '')}acr",
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
    payload = {key: selected[key] for key in FOUNDATION_DEFAULT_KEYS if key in selected}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
def _azure_runtime_path(root: Path) -> Path:
    return root / ".runtime" / "azure"


def _runtime_dir(root: Path) -> Path:
    path = _azure_runtime_path(root)
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
