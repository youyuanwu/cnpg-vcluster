#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
from scripts.lib.locking import e2e_lock, profile_lock, profile_lock_exists, tools_lock
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
    for tenant in recorded_tenant_names(root, "azure"):
        if tenant == excluding:
            continue
        runtime = TenantRuntime(root, "azure", tenant)
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


def preflight(
    root: Path,
    config: Mapping[str, str],
    *,
    emit: bool = True,
) -> dict[str, object]:
    _validate_foundation_networks(config)
    account = _active_subscription(config)
    for provider in REQUIRED_PROVIDERS:
        state = _az(
            "provider",
            "show",
            "--namespace",
            provider,
            "--query",
            "registrationState",
            "-o",
            "tsv",
        ).stdout.strip()
        if state != "Registered":
            raise RuntimeError(f"Azure resource provider is not registered: {provider}")
    _sku_available(config, config["AZURE_AKS_NODE_SKU"])
    _sku_available(config, config["AZURE_TENANT_NODE_SKU"])
    _reference_image_available(
        config,
        config["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
    )
    run(
        [
            "az",
            "bicep",
            "build",
            "--file",
            str(root / "infra" / "azure" / "main.bicep"),
            "--stdout",
        ],
        timeout=120,
    )
    result = {
        "subscriptionId": str(account["id"]),
        "location": config["AZURE_LOCATION"],
        "prefix": config["AZURE_PREFIX"],
        "names": names(config),
        "foundationDefaultsSha256": _foundation_defaults_checksum(root, config),
    }
    if emit:
        print(
            json.dumps(
                {
                    "location": result["location"],
                    "prefix": result["prefix"],
                    "names": result["names"],
                    "foundationDefaultsSha256": result[
                        "foundationDefaultsSha256"
                    ],
                },
                sort_keys=True,
            )
        )
    return result


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


def _deployment_parameters(config: Mapping[str, str]) -> list[str]:
    return [
        f"prefix={config['AZURE_PREFIX']}",
        f"location={config['AZURE_LOCATION']}",
        f"aksKubernetesVersion={config['AZURE_AKS_KUBERNETES_VERSION']}",
        f"aksNodeSku={config['AZURE_AKS_NODE_SKU']}",
        f"aksNodeCount={config['AZURE_AKS_NODE_COUNT']}",
        f"vnetCidr={config['AZURE_VNET_CIDR']}",
        f"aksSubnetCidr={config['AZURE_AKS_SUBNET_CIDR']}",
        f"tenantSubnetCidr={config['AZURE_TENANT_SUBNET_CIDR']}",
        f"aksPodCidr={config['AZURE_AKS_POD_CIDR']}",
        f"aksServiceCidr={config['AZURE_AKS_SERVICE_CIDR']}",
        f"aksDnsServiceIP={config['AZURE_AKS_DNS_SERVICE_IP']}",
    ]


def _write_inventory(root: Path, payload: Mapping[str, object]) -> None:
    write_private_file(
        _runtime_dir(root) / "resources.json",
        json.dumps(dict(payload), sort_keys=True) + "\n",
    )


def create_foundation(root: Path, config: Mapping[str, str]) -> dict[str, object]:
    expected = preflight(root, config, emit=False)
    inventory_path = _azure_runtime_path(root) / "resources.json"
    if os.path.lexists(inventory_path):
        load_inventory(root, config)
    deployment = names(config)["deployment"]
    payload = _json(
        [
            "az",
            "deployment",
            "sub",
            "create",
            "--name",
            deployment,
            "--location",
            config["AZURE_LOCATION"],
            "--template-file",
            str(root / "infra" / "azure" / "main.bicep"),
            "--parameters",
            *_deployment_parameters(config),
            "--output",
            "json",
        ],
        timeout=parse_duration(config["AZURE_DEPLOY_TIMEOUT"]),
    )
    outputs = {
        key: value["value"]
        for key, value in payload["properties"]["outputs"].items()
    }
    record = {
        "schema": FOUNDATION_INVENTORY_SCHEMA,
        **expected,
        "deploymentId": payload["id"],
        "deploymentName": deployment,
        "outputs": outputs,
        "controllers": {},
    }
    _write_inventory(root, record)
    kubeconfig = _runtime_dir(root) / "management.kubeconfig"
    _az(
        "aks",
        "get-credentials",
        "--resource-group",
        outputs["resourceGroupName"],
        "--name",
        outputs["aksName"],
        "--file",
        str(kubeconfig),
        "--overwrite-existing",
        timeout=120,
    )
    kubeconfig.chmod(0o600)
    print(f"Azure management foundation is ready: {outputs['aksName']}")
    return record


def _patch_capz_identity(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> None:
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    client_id = outputs["identityClientId"]
    tenant_id = outputs["tenantId"]
    for service_account in ("capz-manager", "azureserviceoperator-default"):
        _kubectl(
            root,
            "-n",
            "capz-system",
            "annotate",
            "serviceaccount",
            service_account,
            f"azure.workload.identity/client-id={client_id}",
            "--overwrite",
        )
    patch = {
        "stringData": {
            "AZURE_CLIENT_ID": client_id,
            "AZURE_SUBSCRIPTION_ID": config["AZURE_SUBSCRIPTION_ID"],
            "AZURE_TENANT_ID": tenant_id,
            "USE_WORKLOAD_IDENTITY_AUTH": "true",
        }
    }
    _kubectl(
        root,
        "-n",
        "capz-system",
        "patch",
        "secret/aso-controller-settings",
        "--type=merge",
        "-p",
        json.dumps(patch, separators=(",", ":")),
    )
    for deployment in (
        "azureserviceoperator-controller-manager",
        "capz-controller-manager",
    ):
        _kubectl(
            root,
            "-n",
            "capz-system",
            "rollout",
            "restart",
            f"deployment/{deployment}",
        )


def _install_capi_capz(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> None:
    environment = {
        **os.environ,
        "AZURE_SUBSCRIPTION_ID_B64": base64.b64encode(
            config["AZURE_SUBSCRIPTION_ID"].encode()
        ).decode(),
        "EXP_MACHINE_POOL": "true",
    }
    run(
        [
            str(root / ".tools" / "bin" / "clusterctl"),
            "init",
            "--kubeconfig",
            str(_management_kubeconfig(root)),
            "--core",
            f"cluster-api:{config['AZURE_CAPI_VERSION']}",
            "--bootstrap",
            f"kubeadm:{config['AZURE_CAPI_VERSION']}",
            "--control-plane",
            f"kubeadm:{config['AZURE_CAPI_VERSION']}",
            "--infrastructure",
            f"azure:{config['AZURE_CAPZ_VERSION']}",
            "--wait-providers",
        ],
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]),
        env=environment,
    )
    _patch_capz_identity(root, config, inventory)
    _configure_capz_external_control_plane_webhook(root)
    for deployment in (
        "azureserviceoperator-controller-manager",
        "capz-controller-manager",
    ):
        _kubectl(
            root,
            "-n",
            "capz-system",
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
            timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]) + 60,
        )


def _capz_external_control_plane_webhook_ready(
    payload: Mapping[str, object],
) -> bool:
    webhooks = payload.get("webhooks")
    if not isinstance(webhooks, list):
        return False
    webhook = next(
        (
            item
            for item in webhooks
            if isinstance(item, dict)
            and item.get("name") == CAPZ_AZURECLUSTER_WEBHOOK
        ),
        None,
    )
    if not isinstance(webhook, dict):
        return False
    selector = webhook.get("objectSelector")
    return selector == {
        "matchExpressions": [
            {
                "key": CAPZ_EXTERNAL_CONTROL_PLANE_LABEL,
                "operator": "NotIn",
                "values": ["true"],
            }
        ]
    }


def _configure_capz_external_control_plane_webhook(root: Path) -> None:
    resource = (
        "mutatingwebhookconfiguration/"
        "capz-mutating-webhook-configuration"
    )
    payload = _get_management_resource(root, None, resource)
    if payload is None:
        raise RuntimeError("CAPZ mutating webhook configuration is absent")
    if _capz_external_control_plane_webhook_ready(payload):
        return
    webhooks = payload.get("webhooks")
    if not isinstance(webhooks, list):
        raise RuntimeError("CAPZ mutating webhook configuration is invalid")
    index = next(
        (
            position
            for position, item in enumerate(webhooks)
            if isinstance(item, dict)
            and item.get("name") == CAPZ_AZURECLUSTER_WEBHOOK
        ),
        None,
    )
    if index is None:
        raise RuntimeError("CAPZ AzureCluster mutating webhook is absent")
    webhook = webhooks[index]
    selector = webhook.get("objectSelector")
    if selector not in (None, {}):
        raise RuntimeError("CAPZ external control-plane webhook selector changed")
    selector = {
        "matchExpressions": [
            {
                "key": CAPZ_EXTERNAL_CONTROL_PLANE_LABEL,
                "operator": "NotIn",
                "values": ["true"],
            }
        ]
    }
    _kubectl(
        root,
        "patch",
        resource,
        "--type=json",
        "-p",
        json.dumps(
            [
                {
                    "op": "test",
                    "path": f"/webhooks/{index}/name",
                    "value": CAPZ_AZURECLUSTER_WEBHOOK,
                },
                {
                    "op": "replace",
                    "path": f"/webhooks/{index}/objectSelector",
                    "value": selector,
                },
            ],
            separators=(",", ":"),
        ),
    )
    updated = _get_management_resource(root, None, resource)
    if updated is None or not _capz_external_control_plane_webhook_ready(updated):
        raise RuntimeError(
            "CAPZ external control-plane webhook selector was not retained"
        )


def _install_kamaji(root: Path, config: Mapping[str, str]) -> None:
    chart = _prepare_kamaji_chart(root, load_configuration(root))
    for crd in sorted((chart / "crds").glob("*.yaml")):
        _kubectl(
            root,
            "apply",
            "--server-side",
            "--force-conflicts",
            "--field-manager=cnpg-vcluster-azure",
            "-f",
            str(crd),
            timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]),
        )
    _helm(
        root,
        "upgrade",
        "--install",
        "kamaji",
        str(chart),
        "--namespace",
        "kamaji-system",
        "--create-namespace",
        "--values",
        str(root / "manifests" / "management" / "kamaji-values.yaml"),
        "--set",
        "kamaji-etcd.persistentVolumeClaim.storageClassName=default",
        "--post-renderer",
        str(root / "scripts" / "post_renderer.py"),
        "--atomic",
        "--wait",
        f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]) + 60,
    )


def _install_kamaji_provider(root: Path, config: Mapping[str, str]) -> None:
    version = config["AZURE_KAMAJI_CAPI_VERSION"]
    url = (
        "https://github.com/clastix/"
        "cluster-api-control-plane-provider-kamaji/releases/download/"
        f"{version}/control-plane-components.yaml"
    )
    with urllib.request.urlopen(url, timeout=120) as response:
        manifest = response.read().decode("utf-8")
    replacements = {
        "${CACPPK_DYNAMIC_INFRASTRUCTURE_CLUSTER_PATCH:=false}": "false",
        "${CACPPK_EXTERNAL_CLUSTER_REFERENCE:=false}": "false",
        "${CACPPK_EXTERNAL_CLUSTER_REFERENCE_CROSS_NAMESPACE:=false}": "false",
        "${CACPPK_SKIP_INFRA_CLUSTER_PATCH:=false}": "false",
        "${CACPPK_INFRASTRUCTURE_CLUSTERS:= }": "",
    }
    for source, destination in replacements.items():
        if manifest.count(source) != 1:
            raise RuntimeError(f"unexpected Kamaji provider variable count: {source}")
        manifest = manifest.replace(source, destination)
    if "${" in manifest:
        raise RuntimeError("Kamaji provider manifest has unresolved variables")
    path = _runtime_dir(root) / "kamaji-provider.yaml"
    write_private_file(path, manifest)
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-azure",
        "-f",
        str(path),
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]),
    )
    _kubectl(
        root,
        "-n",
        "kamaji-system",
        "rollout",
        "status",
        "deployment/capi-kamaji-controller-manager",
        f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]) + 60,
    )


def _controller_identities(root: Path) -> dict[str, str]:
    identities = {}
    for namespace, deployment in CONTROLLER_DEPLOYMENTS:
        payload = _get_management_resource(
            root,
            namespace,
            f"deployment/{deployment}",
        )
        if payload is None:
            raise RuntimeError(f"Azure management controller is absent: {deployment}")
        uid = payload.get("metadata", {}).get("uid")
        if not isinstance(uid, str) or not uid:
            raise RuntimeError(f"Azure management controller UID is absent: {deployment}")
        identities[f"{namespace}/{deployment}"] = uid
    return identities


def create_management(root: Path, config: Mapping[str, str]) -> None:
    preflight(root, config, emit=False)
    inventory = load_inventory(root, config)
    _kubectl(root, "get", "--raw=/readyz")
    _install_capi_capz(root, config, inventory)
    _install_kamaji(root, config)
    _install_kamaji_provider(root, config)
    for namespace, deployment in CONTROLLER_DEPLOYMENTS:
        _kubectl(
            root,
            "-n",
            namespace,
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
            timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]) + 60,
        )
    updated = dict(inventory)
    updated["controllers"] = _controller_identities(root)
    _write_inventory(root, updated)
    print("Azure management controllers are ready")


def load_inventory(
    root: Path,
    config: Mapping[str, str],
) -> dict[str, object]:
    path = _azure_runtime_path(root) / "resources.json"
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("Azure foundation inventory is absent") from exc
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("Azure resource inventory must be an owner-only regular file")
    try:
        payload = json.loads(read_private_file(path).decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Azure foundation inventory is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Azure foundation inventory must be an object")
    if payload.get("schema") != FOUNDATION_INVENTORY_SCHEMA:
        raise RuntimeError(
            "unsupported pre-cutover Azure foundation inventory; "
            "clean foundation redeploy required"
        )
    expected = {
        "subscriptionId": config["AZURE_SUBSCRIPTION_ID"],
        "location": config["AZURE_LOCATION"],
        "prefix": config["AZURE_PREFIX"],
        "foundationDefaultsSha256": _foundation_defaults_checksum(root, config),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            if key == "foundationDefaultsSha256":
                raise RuntimeError(
                    "Azure foundation inventory checksum changed; "
                    "clean foundation redeploy required"
                )
            raise RuntimeError(f"Azure foundation inventory does not match {key}")
    outputs = payload.get("outputs")
    controllers = payload.get("controllers")
    if not isinstance(outputs, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in outputs.items()
    ):
        raise RuntimeError("Azure foundation inventory outputs are invalid")
    if not isinstance(controllers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in controllers.items()
    ):
        raise RuntimeError("Azure foundation controller inventory is invalid")
    return payload


def _foundation_identity(inventory: Mapping[str, object]) -> dict[str, str]:
    outputs = inventory["outputs"]
    controllers = inventory["controllers"]
    assert isinstance(outputs, dict)
    assert isinstance(controllers, dict)
    required_outputs = (
        "resourceGroupId",
        "aksId",
        "aksNodeResourceGroup",
        "aksOidcIssuer",
        "vnetId",
        "aksSubnetId",
        "tenantSubnetId",
        "identityId",
        "roleAssignmentId",
        "aksRoleAssignmentId",
        "capzFederationId",
        "asoFederationId",
    )
    missing = [key for key in required_outputs if not outputs.get(key)]
    if missing:
        raise RuntimeError(
            "Azure foundation inventory is incomplete: " + ", ".join(missing)
        )
    if set(controllers) != {
        f"{namespace}/{deployment}"
        for namespace, deployment in CONTROLLER_DEPLOYMENTS
    }:
        raise RuntimeError("Azure management controller inventory is incomplete")
    return {
        "foundationDefaultsSha256": str(inventory["foundationDefaultsSha256"]),
        **{key: str(outputs[key]) for key in required_outputs},
        **{
            f"controller:{key}": str(value)
            for key, value in sorted(controllers.items())
        },
    }


def _get_management_resource(
    root: Path,
    namespace: str | None,
    resource: str,
) -> dict[str, object] | None:
    arguments = []
    if namespace is not None:
        arguments.extend(("-n", namespace))
    response = _kubectl(
        root,
        *arguments,
        "get",
        resource,
        "-o",
        "json",
        check=False,
    )
    if response.returncode != 0:
        if re.search(
            r"Error from server \(NotFound\):",
            response.stderr,
            re.IGNORECASE,
        ):
            return None
        raise RuntimeError(
            f"Azure management resource inspection failed for {resource}: "
            f"{response.stderr}"
        )
    payload = json.loads(response.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid Azure management resource: {resource}")
    return payload


def _deployment_ready(payload: Mapping[str, object]) -> bool:
    spec = payload.get("spec")
    status = payload.get("status")
    if not isinstance(spec, dict) or not isinstance(status, dict):
        return False
    requested = spec.get("replicas", 1)
    return (
        isinstance(requested, int)
        and status.get("availableReplicas", 0) >= requested
        and status.get("updatedReplicas", 0) >= requested
    )


def _inspect_foundation(
    root: Path,
    config: Mapping[str, str],
    *,
    require_healthy: bool,
) -> tuple[dict[str, str], bool, tuple[str, ...]]:
    _validate_foundation_networks(config)
    _active_subscription(config)
    inventory = load_inventory(root, config)
    identity = _foundation_identity(inventory)
    outputs = inventory["outputs"]
    controllers = inventory["controllers"]
    assert isinstance(outputs, dict)
    assert isinstance(controllers, dict)
    blockers = []
    aks = _az(
        "aks",
        "show",
        "--resource-group",
        str(outputs["resourceGroupName"]),
        "--name",
        str(outputs["aksName"]),
        "--query",
        (
            "{id:id,provisioningState:provisioningState,"
            "powerState:powerState.code,kubernetesVersion:kubernetesVersion,"
            "nodeResourceGroup:nodeResourceGroup,"
            "oidcIssuer:oidcIssuerProfile.issuerUrl}"
        ),
        "--output",
        "json",
        check=False,
    )
    if aks.returncode != 0:
        blockers.append("recorded AKS management cluster is absent")
    else:
        payload = json.loads(aks.stdout)
        if not _azure_id_equal(payload.get("id"), outputs["aksId"]):
            blockers.append("AKS management identity changed")
        if payload.get("provisioningState") != "Succeeded":
            blockers.append("AKS provisioning is not Succeeded")
        if payload.get("powerState") != "Running":
            blockers.append("AKS power state is not Running")
        if payload.get("kubernetesVersion") != config["AZURE_AKS_KUBERNETES_VERSION"]:
            blockers.append("AKS Kubernetes version changed")
        if payload.get("nodeResourceGroup") != outputs["aksNodeResourceGroup"]:
            blockers.append("AKS managed node resource group changed")
        if payload.get("oidcIssuer") != outputs["aksOidcIssuer"]:
            blockers.append("AKS OIDC issuer changed")
    azure_identity_checks = (
        (
            "resource group",
            ("group", "show", "--name", str(outputs["resourceGroupName"])),
            outputs["resourceGroupId"],
        ),
        (
            "virtual network",
            (
                "network",
                "vnet",
                "show",
                "--resource-group",
                str(outputs["resourceGroupName"]),
                "--name",
                str(outputs["vnetName"]),
            ),
            outputs["vnetId"],
        ),
        (
            "AKS subnet",
            (
                "network",
                "vnet",
                "subnet",
                "show",
                "--resource-group",
                str(outputs["resourceGroupName"]),
                "--vnet-name",
                str(outputs["vnetName"]),
                "--name",
                str(outputs["aksSubnetName"]),
            ),
            outputs["aksSubnetId"],
        ),
        (
            "tenant subnet",
            (
                "network",
                "vnet",
                "subnet",
                "show",
                "--resource-group",
                str(outputs["resourceGroupName"]),
                "--vnet-name",
                str(outputs["vnetName"]),
                "--name",
                str(outputs["tenantSubnetName"]),
            ),
            outputs["tenantSubnetId"],
        ),
        (
            "management identity",
            (
                "identity",
                "show",
                "--resource-group",
                str(outputs["resourceGroupName"]),
                "--name",
                str(outputs["identityName"]),
            ),
            outputs["identityId"],
        ),
        (
            "management identity role assignment",
            ("resource", "show", "--ids", str(outputs["roleAssignmentId"])),
            outputs["roleAssignmentId"],
        ),
        (
            "AKS role assignment",
            ("resource", "show", "--ids", str(outputs["aksRoleAssignmentId"])),
            outputs["aksRoleAssignmentId"],
        ),
        (
            "CAPZ federated credential",
            ("resource", "show", "--ids", str(outputs["capzFederationId"])),
            outputs["capzFederationId"],
        ),
        (
            "ASO federated credential",
            ("resource", "show", "--ids", str(outputs["asoFederationId"])),
            outputs["asoFederationId"],
        ),
    )
    for description, arguments, expected_id in azure_identity_checks:
        response = _az(
            *arguments,
            "--query",
            "id",
            "--output",
            "tsv",
            check=False,
        )
        if response.returncode != 0:
            blockers.append(f"recorded Azure {description} is absent")
        elif response.stdout.strip().lower() != str(expected_id).lower():
            blockers.append(f"Azure {description} identity changed")
    try:
        readyz = _kubectl(root, "get", "--raw=/readyz", check=False)
    except (OSError, RuntimeError):
        blockers.append("Azure management kubeconfig is unavailable")
    else:
        if readyz.returncode != 0:
            blockers.append("Azure management API is not ready")
        for namespace, deployment in CONTROLLER_DEPLOYMENTS:
            observed = _get_management_resource(
                root,
                namespace,
                f"deployment/{deployment}",
            )
            if observed is None:
                blockers.append(f"management controller is absent: {deployment}")
                continue
            uid = observed.get("metadata", {}).get("uid")
            if uid != controllers.get(f"{namespace}/{deployment}"):
                blockers.append(f"management controller identity changed: {deployment}")
            if not _deployment_ready(observed):
                blockers.append(f"management controller is unavailable: {deployment}")
        webhook = _get_management_resource(
            root,
            None,
            "mutatingwebhookconfiguration/capz-mutating-webhook-configuration",
        )
        if (
            webhook is None
            or not _capz_external_control_plane_webhook_ready(webhook)
        ):
            blockers.append(
                "CAPZ external control-plane webhook selector is unavailable"
            )
    healthy = not blockers
    if require_healthy and not healthy:
        raise RuntimeError("Azure management foundation is unhealthy: " + "; ".join(blockers))
    return identity, healthy, tuple(blockers)


def foundation_status(root: Path, config: Mapping[str, str]) -> int:
    try:
        _, healthy, blockers = _inspect_foundation(
            root,
            config,
            require_healthy=False,
        )
    except BaseException as exc:
        result = {
            "schema": 1,
            "foundation": "unhealthy",
            "healthy": False,
            "blockers": (str(exc),),
        }
        result["blockers"] = tuple(redact(str(item)) for item in result["blockers"])
        print(json.dumps(result, sort_keys=True))
        return 1
    result = {
        "schema": 1,
        "foundation": "healthy" if healthy else "unhealthy",
        "healthy": healthy,
        "blockers": blockers,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if healthy else 1


def _marker_annotations(markers: Mapping[str, str]) -> dict[str, str]:
    return {LIFECYCLE_MARKERS[key]: value for key, value in markers.items()}


def _metadata(
    name: str,
    spec: TenantSpec,
    markers: Mapping[str, str],
    *,
    namespace: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "name": name,
        "labels": {
            "cnpg-vcluster-experiment": "azure-capi",
            "cnpg-vcluster-tenant": spec.name,
            "cnpg-vcluster-profile": "azure",
        },
        "annotations": _marker_annotations(markers),
    }
    if namespace is not None:
        metadata["namespace"] = namespace
    return metadata


def _external_azure_cluster_metadata(
    name: str,
    spec: TenantSpec,
    markers: Mapping[str, str],
    *,
    namespace: str,
) -> dict[str, object]:
    metadata = _metadata(name, spec, markers, namespace=namespace)
    labels = metadata["labels"]
    assert isinstance(labels, dict)
    labels[CAPZ_EXTERNAL_CONTROL_PLANE_LABEL] = "true"
    return metadata


def _azure_tags(markers: Mapping[str, str]) -> dict[str, str]:
    return {
        "cnpg-vcluster-tenant": markers["tenant"],
        "cnpg-vcluster-profile": markers["profile"],
        "cnpg-vcluster-spec-sha256": markers["specificationSha256"],
        "cnpg-vcluster-foundation-sha256": markers["foundationSha256"],
        "cnpg-vcluster-operation-id": markers["operationId"],
    }


def _azure_tags_match(
    tags: object,
    expected: Mapping[str, str],
) -> bool:
    return isinstance(tags, dict) and all(
        tags.get(key) == value for key, value in expected.items()
    )


def _write_manifest(
    root: Path,
    spec: TenantSpec,
    name: str,
    items: Sequence[Mapping[str, object]],
) -> Path:
    path = _tenant_runtime_dir(root, spec.name) / f"{name}.json"
    write_private_file(
        path,
        json.dumps(
            {"apiVersion": "v1", "kind": "List", "items": list(items)},
            sort_keys=True,
        )
        + "\n",
    )
    return path


def _render_tenant_control_plane(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
    spec: TenantSpec,
    journal: OperationJournal,
) -> Path:
    selected = tenant_names(spec)
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    markers = lifecycle_markers(spec, journal)
    namespace = spec.namespace
    items = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": _metadata(namespace, spec, markers),
        },
        {
            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
            "kind": "AzureClusterIdentity",
            "metadata": _metadata(
                selected["azureClusterIdentity"],
                spec,
                markers,
                namespace=namespace,
            ),
            "spec": {
                "type": "WorkloadIdentity",
                "tenantID": outputs["tenantId"],
                "clientID": outputs["identityClientId"],
                "allowedNamespaces": {"list": [namespace]},
            },
        },
        {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "Cluster",
            "metadata": _metadata(
                selected["cluster"],
                spec,
                markers,
                namespace=namespace,
            ),
            "spec": {
                "clusterNetwork": {
                    "apiServerPort": 6443,
                    "pods": {"cidrBlocks": [str(spec.pod_network)]},
                    "services": {"cidrBlocks": [str(spec.service_network)]},
                    "serviceDomain": spec.cluster_domain,
                },
                "controlPlaneRef": {
                    "apiVersion": "controlplane.cluster.x-k8s.io/v1alpha1",
                    "kind": "KamajiControlPlane",
                    "name": selected["controlPlane"],
                },
                "infrastructureRef": {
                    "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                    "kind": "AzureCluster",
                    "name": selected["azureCluster"],
                },
            },
        },
        {
            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
            "kind": "AzureCluster",
            "metadata": _external_azure_cluster_metadata(
                selected["azureCluster"],
                spec,
                markers,
                namespace=namespace,
            ),
            "spec": {
                "subscriptionID": config["AZURE_SUBSCRIPTION_ID"],
                "location": config["AZURE_LOCATION"],
                "resourceGroup": outputs["resourceGroupName"],
                "controlPlaneEnabled": False,
                "identityRef": {
                    "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                    "kind": "AzureClusterIdentity",
                    "name": selected["azureClusterIdentity"],
                },
                "networkSpec": {
                    "apiServerLB": {"type": "Public"},
                    "vnet": {
                        "name": outputs["vnetName"],
                        "resourceGroup": outputs["resourceGroupName"],
                    },
                    "subnets": [
                        {"name": outputs["tenantSubnetName"], "role": "node"}
                    ],
                },
                "additionalTags": _azure_tags(markers),
            },
        },
        {
            "apiVersion": "controlplane.cluster.x-k8s.io/v1alpha1",
            "kind": "KamajiControlPlane",
            "metadata": _metadata(
                selected["controlPlane"],
                spec,
                markers,
                namespace=namespace,
            ),
            "spec": {
                "version": spec.kubernetes_version,
                "replicas": 1,
                "dataStoreName": "default",
                "controllerManager": {
                    "extraArgs": [
                        "--cloud-provider=external",
                        f"--cluster-name={spec.name}",
                        "--allocate-node-cidrs=false",
                    ]
                },
                "network": {
                    "serviceType": "LoadBalancer",
                    "serviceAnnotations": {
                        "service.beta.kubernetes.io/azure-load-balancer-internal": "true"
                    },
                    "certSANs": [],
                    "dnsServiceIPs": [spec.dns_service_ip],
                },
                "addons": {
                    "coreDNS": {"dnsServiceIPs": [spec.dns_service_ip]},
                    "kubeProxy": {},
                    "konnectivity": {
                        "server": {"port": 8132},
                        "agent": {
                            "mode": "DaemonSet",
                            "hostNetwork": True,
                            "tolerations": [
                                {
                                    "key": "node.kubernetes.io/not-ready",
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                },
                                {
                                    "key": "node.kubernetes.io/not-ready",
                                    "operator": "Exists",
                                    "effect": "NoExecute",
                                },
                                {
                                    "key": "node.cloudprovider.kubernetes.io/uninitialized",
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                },
                            ],
                        },
                    },
                },
            },
        },
    ]
    return _write_manifest(root, spec, "control-plane", items)


def _render_worker_pool(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
    spec: TenantSpec,
    journal: OperationJournal,
) -> Path:
    selected = tenant_names(spec)
    pool = selected["pool"]
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    markers = lifecycle_markers(spec, journal)
    identity_provider_id = (
        "azure:///subscriptions/"
        f"{config['AZURE_SUBSCRIPTION_ID']}/resourceGroups/"
        f"{outputs['resourceGroupName']}/providers/Microsoft.ManagedIdentity/"
        f"userAssignedIdentities/{outputs['identityName']}"
    )
    items = [
        {
            "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
            "kind": "KubeadmConfig",
            "metadata": _metadata(
                pool,
                spec,
                markers,
                namespace=spec.namespace,
            ),
            "spec": {
                "files": [
                    {
                        "contentFrom": {
                            "secret": {
                                "name": f"{pool}-azure-json",
                                "key": "worker-node-azure.json",
                            }
                        },
                        "owner": "root:root",
                        "path": "/etc/kubernetes/azure.json",
                        "permissions": "0644",
                    }
                ],
                "joinConfiguration": {
                    "nodeRegistration": {
                        "name": '{{ ds.meta_data["local_hostname"] }}',
                        "kubeletExtraArgs": {
                            "cloud-provider": "external",
                            "feature-gates": "KubeletCrashLoopBackOffMax=true",
                        },
                    }
                },
            },
        },
        {
            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
            "kind": "AzureMachinePool",
            "metadata": _metadata(
                pool,
                spec,
                markers,
                namespace=spec.namespace,
            ),
            "spec": {
                "location": config["AZURE_LOCATION"],
                "orchestrationMode": "Uniform",
                "platformFaultDomainCount": 1,
                "identity": "UserAssigned",
                "userAssignedIdentities": [{"providerID": identity_provider_id}],
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {
                        "maxSurge": 1,
                        "maxUnavailable": 0,
                        "deletePolicy": "Oldest",
                    },
                },
                "template": {
                    "vmSize": config["AZURE_TENANT_NODE_SKU"],
                    "networkInterfaces": [
                        {"subnetName": outputs["tenantSubnetName"]}
                    ],
                    "osDisk": {
                        "diskSizeGB": 30,
                        "osType": "Linux",
                        "managedDisk": {"storageAccountType": "StandardSSD_LRS"},
                    },
                    "image": {
                        "computeGallery": {
                            "gallery": "ClusterAPI-f72ceb4f-5159-4c26-a0fe-2ea738f0d019",
                            "name": "capi-ubun2-2404",
                            "version": spec.kubernetes_version,
                        }
                    },
                    "sshPublicKey": "",
                },
                "additionalTags": _azure_tags(markers),
            },
        },
        {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "MachinePool",
            "metadata": _metadata(
                pool,
                spec,
                markers,
                namespace=spec.namespace,
            ),
            "spec": {
                "clusterName": spec.name,
                "replicas": spec.workers,
                "template": {
                    "metadata": {
                        "labels": {
                            "cnpg-vcluster-experiment": "azure-capi",
                            "cnpg-vcluster-tenant": spec.name,
                            "cnpg-vcluster-profile": "azure",
                        },
                        "annotations": _marker_annotations(markers),
                    },
                    "spec": {
                        "clusterName": spec.name,
                        "version": f"v{spec.kubernetes_version}",
                        "nodeDrainTimeout": "2m",
                        "bootstrap": {
                            "configRef": {
                                "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                                "kind": "KubeadmConfig",
                                "name": pool,
                            }
                        },
                        "infrastructureRef": {
                            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                            "kind": "AzureMachinePool",
                            "name": pool,
                        },
                    },
                },
            },
        },
    ]
    return _write_manifest(root, spec, "worker", items)


def _render_addon_job(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
    journal: OperationJournal,
) -> Path:
    selected = tenant_names(spec)
    markers = lifecycle_markers(spec, journal)
    cloud_values = {
        "infra": {"clusterName": spec.name},
        "cloudControllerManager": {
            "allocateNodeCidrs": "false",
            "clusterCIDR": str(spec.pod_network),
            "configureCloudRoutes": "false",
            "nodeSelector": None,
            "replicas": 1,
            "tolerations": [{"operator": "Exists"}],
        },
        "cloudNodeManager": {"cloudConfig": "/etc/kubernetes/azure.json"},
    }
    calico_values = {
        "installation": {
            "cni": {"type": "Calico", "ipam": {"type": "Calico"}},
            "calicoNetwork": {
                "bgp": "Disabled",
                "mtu": 1350,
                "ipPools": [
                    {"cidr": str(spec.pod_network), "encapsulation": "VXLAN"}
                ],
            },
        },
        "serviceCIDRs": [str(spec.service_network)],
        "tolerations": [{"operator": "Exists"}],
    }
    cloud_yaml = json.dumps(cloud_values, sort_keys=True)
    calico_yaml = json.dumps(calico_values, sort_keys=True)
    job = selected["addonJob"]
    items = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": _metadata(
                selected["cloudValues"],
                spec,
                markers,
                namespace=spec.namespace,
            ),
            "data": {"values.yaml": cloud_yaml},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": _metadata(
                selected["networkValues"],
                spec,
                markers,
                namespace=spec.namespace,
            ),
            "data": {"values.yaml": calico_yaml},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": _metadata(
                selected["statusProbe"],
                spec,
                markers,
                namespace=spec.namespace,
            ),
            "spec": {
                "replicas": 1,
                "selector": {
                    "matchLabels": {
                        "cnpg-vcluster-status-probe": spec.name,
                    }
                },
                "template": {
                    "metadata": {
                        "labels": {
                            "cnpg-vcluster-status-probe": spec.name,
                            "cnpg-vcluster-tenant": spec.name,
                            "cnpg-vcluster-profile": "azure",
                        },
                        "annotations": _marker_annotations(markers),
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "kubectl",
                                "image": (
                                    "registry.k8s.io/kubectl:"
                                    f"v{spec.kubernetes_version}"
                                ),
                                "command": ["kubectl"],
                                "args": [
                                    "proxy",
                                    "--kubeconfig=/tenant/value",
                                    "--address=127.0.0.1",
                                    "--accept-hosts=^localhost$",
                                ],
                                "readinessProbe": {
                                    "exec": {
                                        "command": [
                                            "kubectl",
                                            "--kubeconfig=/tenant/value",
                                            "get",
                                            "--raw=/readyz",
                                        ]
                                    },
                                    "periodSeconds": 10,
                                },
                                "volumeMounts": [
                                    {
                                        "name": "tenant-kubeconfig",
                                        "mountPath": "/tenant",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "tenant-kubeconfig",
                                "secret": {"secretName": f"{spec.name}-kubeconfig"},
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": _metadata(
                job,
                spec,
                markers,
                namespace=spec.namespace,
            ),
            "spec": {
                "backoffLimit": 1,
                "template": {
                    "metadata": {
                        "labels": {
                            "cnpg-vcluster-experiment": "azure-capi",
                            "cnpg-vcluster-tenant": spec.name,
                            "cnpg-vcluster-profile": "azure",
                        },
                        "annotations": _marker_annotations(markers),
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "helm",
                                "image": "alpine/helm:3.19.0",
                                "command": ["sh", "-ec"],
                                "args": [
                                    (
                                        "helm repo add cloud-provider-azure "
                                        "https://raw.githubusercontent.com/kubernetes-sigs/"
                                        "cloud-provider-azure/master/helm/repo\n"
                                        "helm repo add projectcalico "
                                        "https://docs.tigera.io/calico/charts\n"
                                        "helm upgrade --install cloud-provider-azure "
                                        "cloud-provider-azure/cloud-provider-azure "
                                        "--kubeconfig /tenant/value "
                                        f"--version {config['AZURE_CLOUD_PROVIDER_VERSION'].removeprefix('v')} "
                                        "--namespace kube-system "
                                        "--values /values/cloud-provider.yaml "
                                        "--wait --timeout 10m\n"
                                        "helm upgrade --install calico-crds "
                                        "projectcalico/crd.projectcalico.org.v1 "
                                        "--kubeconfig /tenant/value "
                                        f"--version {config['AZURE_CALICO_VERSION']} "
                                        "--namespace tigera-operator --create-namespace "
                                        "--wait --timeout 5m\n"
                                        "helm upgrade --install calico "
                                        "projectcalico/tigera-operator "
                                        "--kubeconfig /tenant/value "
                                        f"--version {config['AZURE_CALICO_VERSION']} "
                                        "--namespace tigera-operator --create-namespace "
                                        "--values /values/calico.yaml "
                                        "--wait --timeout 10m"
                                    )
                                ],
                                "volumeMounts": [
                                    {
                                        "name": "tenant-kubeconfig",
                                        "mountPath": "/tenant",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "cloud-values",
                                        "mountPath": "/values/cloud-provider.yaml",
                                        "subPath": "values.yaml",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "calico-values",
                                        "mountPath": "/values/calico.yaml",
                                        "subPath": "values.yaml",
                                        "readOnly": True,
                                    },
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "tenant-kubeconfig",
                                "secret": {"secretName": f"{spec.name}-kubeconfig"},
                            },
                            {
                                "name": "cloud-values",
                                "configMap": {"name": selected["cloudValues"]},
                            },
                            {
                                "name": "calico-values",
                                "configMap": {"name": selected["networkValues"]},
                            },
                        ],
                    },
                },
            },
        },
    ]
    return _write_manifest(root, spec, "addons", items)


RESOURCE_IDENTITY_KEYS = {
    "Namespace": "namespaceUid",
    "AzureClusterIdentity": "azureClusterIdentityUid",
    "Cluster": "clusterUid",
    "AzureCluster": "azureClusterUid",
    "KamajiControlPlane": "kamajiControlPlaneUid",
    "KubeadmConfig": "kubeadmConfigUid",
    "MachinePool": "machinePoolUid",
    "AzureMachinePool": "azureMachinePoolUid",
    "ConfigMap": {
        "cloud": "cloudValuesConfigMapUid",
        "network": "networkValuesConfigMapUid",
    },
    "Deployment": "statusProbeDeploymentUid",
    "Job": "addonJobUid",
}


def _resource_ref(item: Mapping[str, object]) -> tuple[str | None, str]:
    metadata = item["metadata"]
    assert isinstance(metadata, dict)
    namespace = metadata.get("namespace")
    return (
        str(namespace) if isinstance(namespace, str) else None,
        f"{str(item['kind']).lower()}/{metadata['name']}",
    )


def _identity_key(item: Mapping[str, object], spec: TenantSpec) -> str:
    kind = str(item["kind"])
    value = RESOURCE_IDENTITY_KEYS[kind]
    if isinstance(value, dict):
        name = str(item["metadata"]["name"])
        return value["cloud" if name == tenant_names(spec)["cloudValues"] else "network"]
    return value


def _require_markers(
    payload: Mapping[str, object],
    expected: Mapping[str, str],
    description: str,
) -> None:
    if resource_lifecycle_markers(payload) != dict(expected):
        raise RuntimeError(f"foreign Azure tenant lifecycle markers: {description}")


def _reconcile_manifest(
    root: Path,
    spec: TenantSpec,
    runtime: TenantRuntime,
    journal: OperationJournal,
    path: Path,
    *,
    phase: str,
) -> OperationJournal:
    manifest = json.loads(read_private_file(path).decode())
    items = manifest.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"invalid Azure tenant manifest: {path.name}")
    current = runtime.load_operation()
    expected = lifecycle_markers(spec, current)
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError(f"invalid Azure tenant manifest item: {path.name}")
        namespace, resource = _resource_ref(item)
        identity_key = _identity_key(item, spec)
        recorded_uid = current.observed.get(identity_key)
        existing = _get_management_resource(root, namespace, resource)
        if existing is not None:
            _require_markers(existing, expected, resource)
            existing_uid = existing.get("metadata", {}).get("uid")
            if not isinstance(existing_uid, str) or not existing_uid:
                raise RuntimeError(
                    f"Azure tenant resource UID is absent: {resource}"
                )
            if recorded_uid is not None and recorded_uid != existing_uid:
                raise RuntimeError(
                    f"Azure tenant resource identity changed: {resource}"
                )
            if recorded_uid is None:
                current = runtime.recover_observed_identity(
                    current,
                    resource=identity_key,
                    identifier=existing_uid,
                    markers=resource_lifecycle_markers(existing),
                    phase=phase,
                )
                recorded_uid = existing_uid
        elif recorded_uid is not None:
            raise RuntimeError(
                f"recorded Azure tenant resource is absent: {resource}"
            )
        _kubectl(
            root,
            "apply",
            "--server-side",
            "--field-manager=cnpg-vcluster-azure",
            "-f",
            "-",
            input_text=json.dumps(item),
        )
        observed = _get_management_resource(root, namespace, resource)
        if observed is None:
            raise RuntimeError(f"Azure tenant resource disappeared after apply: {resource}")
        _require_markers(observed, expected, resource)
        uid = observed.get("metadata", {}).get("uid")
        if not isinstance(uid, str) or not uid:
            raise RuntimeError(f"Azure tenant resource UID is absent: {resource}")
        if recorded_uid is not None and uid != recorded_uid:
            raise RuntimeError(
                f"Azure tenant resource identity changed after apply: {resource}"
            )
        if recorded_uid is None:
            current = runtime.update_operation(
                current,
                phase=phase,
                observed={identity_key: uid},
            )
    return current


def _wait_tenant_endpoint(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
) -> dict[str, object]:
    deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    patched_endpoint: dict[str, object] | None = None
    while time.monotonic() < deadline:
        infrastructure = _get_management_resource(
            root,
            spec.namespace,
            f"azurecluster/{spec.name}",
        )
        if infrastructure is not None and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in infrastructure.get("status", {}).get("conditions", [])
        ):
            legacy_cluster = _get_management_resource(
                root,
                spec.namespace,
                f"cluster/{spec.name}",
            )
            if (
                legacy_cluster is not None
                and legacy_cluster.get("status", {}).get("infrastructureReady") is not True
            ):
                _kubectl(
                    root,
                    "-n",
                    spec.namespace,
                    "patch",
                    f"clusters.v1beta1.cluster.x-k8s.io/{spec.name}",
                    "--subresource=status",
                    "--type=merge",
                    "-p",
                    '{"status":{"infrastructureReady":true}}',
                )
        payload = _get_management_resource(
            root,
            spec.namespace,
            f"kamajicontrolplane/{spec.name}",
        )
        if payload is not None:
            endpoint = payload.get("spec", {}).get("controlPlaneEndpoint", {})
            if (
                isinstance(endpoint, dict)
                and endpoint.get("host")
                and endpoint.get("port")
                and endpoint != patched_endpoint
            ):
                _kubectl(
                    root,
                    "-n",
                    spec.namespace,
                    "patch",
                    f"cluster/{spec.name}",
                    "--type=merge",
                    "-p",
                    json.dumps(
                        {"spec": {"controlPlaneEndpoint": endpoint}},
                        separators=(",", ":"),
                    ),
                )
                patched_endpoint = endpoint
            if (
                payload.get("status", {}).get("ready") is True
                and isinstance(endpoint, dict)
                and endpoint.get("host")
                and endpoint.get("port")
            ):
                return payload
        time.sleep(5)
    raise RuntimeError("Kamaji tenant control plane did not become ready")


def _retain_external_control_plane_lb(
    root: Path,
    spec: TenantSpec,
    journal: OperationJournal,
) -> None:
    selected = tenant_names(spec)
    resource = f"azurecluster/{selected['azureCluster']}"
    payload = _get_management_resource(root, spec.namespace, resource)
    if payload is None:
        raise RuntimeError("AzureCluster is absent after reconciliation")
    _require_markers(payload, _expected_tenant_markers(spec, journal), resource)
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("uid") != journal.observed.get("azureClusterUid"):
        raise RuntimeError("AzureCluster identity changed before webhook bypass")
    labels = metadata.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    if labels.get(CAPZ_EXTERNAL_CONTROL_PLANE_LABEL) != "true":
        _kubectl(
            root,
            "-n",
            spec.namespace,
            "patch",
            resource,
            "--type=merge",
            "--field-manager=cnpg-vcluster-azure",
            "-p",
            json.dumps(
                {
                    "metadata": {
                        "labels": {
                            CAPZ_EXTERNAL_CONTROL_PLANE_LABEL: "true",
                        }
                    },
                },
                separators=(",", ":"),
            ),
        )
    labeled = _get_management_resource(root, spec.namespace, resource)
    if labeled is None:
        raise RuntimeError("AzureCluster is absent after webhook bypass")
    labeled_metadata = labeled.get("metadata")
    labeled_metadata = (
        labeled_metadata if isinstance(labeled_metadata, dict) else {}
    )
    if (
        labeled_metadata.get("uid") != journal.observed.get("azureClusterUid")
        or labeled_metadata.get("labels", {}).get(
            CAPZ_EXTERNAL_CONTROL_PLANE_LABEL
        )
        != "true"
    ):
        raise RuntimeError("CAPZ external control-plane label was not retained")
    labeled_spec = labeled.get("spec")
    labeled_spec = labeled_spec if isinstance(labeled_spec, dict) else {}
    labeled_network = labeled_spec.get("networkSpec")
    labeled_network = (
        labeled_network if isinstance(labeled_network, dict) else {}
    )
    labeled_lb = labeled_network.get("apiServerLB")
    if not isinstance(labeled_lb, dict) or labeled_lb.get("type") != "Public":
        _kubectl(
            root,
            "-n",
            spec.namespace,
            "patch",
            resource,
            "--type=merge",
            "--field-manager=cnpg-vcluster-azure",
            "-p",
            json.dumps(
                {
                    "spec": {
                        "networkSpec": {
                            "apiServerLB": {"type": "Public"},
                        }
                    },
                },
                separators=(",", ":"),
            ),
        )
    updated = _get_management_resource(root, spec.namespace, resource)
    if updated is None:
        raise RuntimeError("AzureCluster is absent after load balancer retention")
    updated_metadata = updated.get("metadata")
    updated_metadata = (
        updated_metadata if isinstance(updated_metadata, dict) else {}
    )
    updated_spec = updated.get("spec")
    updated_spec = updated_spec if isinstance(updated_spec, dict) else {}
    network_spec = updated_spec.get("networkSpec")
    network_spec = network_spec if isinstance(network_spec, dict) else {}
    api_server_lb = network_spec.get("apiServerLB")
    if (
        updated_metadata.get("uid") != journal.observed.get("azureClusterUid")
        or updated_metadata.get("labels", {}).get(
            CAPZ_EXTERNAL_CONTROL_PLANE_LABEL
        )
        != "true"
        or not isinstance(api_server_lb, dict)
        or api_server_lb.get("type") != "Public"
    ):
        raise RuntimeError(
            "CAPZ external control-plane load balancer placeholder was not retained"
        )


def _capture_tenant_kubeconfig(
    root: Path,
    spec: TenantSpec,
    runtime: TenantRuntime,
    journal: OperationJournal,
) -> OperationJournal:
    secret = _get_management_resource(
        root,
        spec.namespace,
        f"secret/{spec.name}-kubeconfig",
    )
    if secret is None:
        raise RuntimeError("Azure tenant kubeconfig Secret is absent")
    uid = secret.get("metadata", {}).get("uid")
    owners = [
        owner
        for owner in secret.get("metadata", {}).get("ownerReferences") or []
        if owner.get("controller") is True
    ]
    allowed_owner_uids = {
        journal.observed.get("clusterUid"),
        journal.observed.get("kamajiControlPlaneUid"),
    } - {None}
    encoded = secret.get("data", {}).get("value")
    if (
        not isinstance(uid, str)
        or not uid
        or len(owners) != 1
        or owners[0].get("uid") not in allowed_owner_uids
        or not isinstance(encoded, str)
        or not encoded
    ):
        raise RuntimeError("Azure tenant kubeconfig Secret is incomplete")
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RuntimeError("Azure tenant kubeconfig Secret is invalid") from exc
    path = _tenant_runtime_dir(root, spec.name) / "kubeconfig"
    write_private_file(path, content)
    return runtime.update_operation(
        journal,
        phase="control-plane-ready",
        observed={
            "tenantKubeconfigSecretUid": uid,
            "tenantKubeconfigSha256": hashlib.sha256(content).hexdigest(),
        },
    )


def _wait_worker_registered(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    pool = tenant_names(spec)["pool"]
    deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    while time.monotonic() < deadline:
        payload = _get_management_resource(
            root,
            spec.namespace,
            f"machinepool/{pool}",
        )
        instances = _az(
            "vmss",
            "list-instances",
            "--resource-group",
            names(config)["resourceGroup"],
            "--name",
            pool,
            "--query",
            "[].{id:id,instanceId:instanceId}",
            "--output",
            "json",
            check=False,
        )
        if payload is not None and instances.returncode == 0:
            items = json.loads(instances.stdout)
            if isinstance(items, list) and len(items) == spec.workers:
                return payload, items
        time.sleep(10)
    raise RuntimeError("Azure VMSS workers did not register with the tenant API")


def _capture_vmss_identities(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
    runtime: TenantRuntime,
    journal: OperationJournal,
) -> OperationJournal:
    pool = tenant_names(spec)["pool"]
    group = names(config)["resourceGroup"]
    vmss = _az(
        "vmss",
        "show",
        "--resource-group",
        group,
        "--name",
        pool,
        "--query",
        "{id:id,tags:tags}",
        "--output",
        "json",
        check=False,
    )
    if vmss.returncode != 0:
        raise RuntimeError("Azure tenant VMSS is absent")
    payload = json.loads(vmss.stdout)
    expected = _azure_tags(lifecycle_markers(spec, journal))
    if not _azure_tags_match(payload.get("tags"), expected):
        raise RuntimeError("foreign Azure tenant VMSS markers")
    vmss_id = payload.get("id")
    if not isinstance(vmss_id, str) or not vmss_id:
        raise RuntimeError("Azure tenant VMSS identity is absent")
    instances = _json(
        [
            "az",
            "vmss",
            "list-instances",
            "--resource-group",
            group,
            "--name",
            pool,
            "--query",
            "[].{id:id,instanceId:instanceId}",
            "--output",
            "json",
        ]
    )
    if not isinstance(instances, list) or len(instances) != spec.workers:
        raise RuntimeError("Azure tenant VMSS instance count does not match specification")
    identities = sorted(
        str(item.get("id"))
        for item in instances
        if isinstance(item, dict) and item.get("id") and item.get("instanceId") is not None
    )
    if len(identities) != spec.workers:
        raise RuntimeError("Azure tenant VMSS instance identities are incomplete")
    return runtime.update_operation(
        journal,
        phase="workers-registered",
        observed={
            "vmssId": vmss_id,
            "vmssInstanceIds": json.dumps(identities, separators=(",", ":")),
        },
    )


def _wait_addon_job(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
) -> None:
    selected = tenant_names(spec)
    _kubectl(
        root,
        "-n",
        spec.namespace,
        "rollout",
        "status",
        f"deployment/{selected['statusProbe']}",
        f"--timeout={config['AZURE_TENANT_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"]) + 60,
    )
    job = selected["addonJob"]
    result = _kubectl(
        root,
        "-n",
        spec.namespace,
        "wait",
        "--for=condition=Complete",
        f"job/{job}",
        f"--timeout={config['AZURE_TENANT_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"]) + 60,
        check=False,
    )
    if result.returncode != 0:
        logs = _kubectl(
            root,
            "-n",
            spec.namespace,
            "logs",
            f"job/{job}",
            "--tail=200",
            check=False,
        )
        raise RuntimeError(f"tenant add-on installation failed: {logs.stdout}{logs.stderr}")


def _condition_true(payload: Mapping[str, object], condition_type: str) -> bool:
    return any(
        condition.get("type") == condition_type and condition.get("status") == "True"
        for condition in payload.get("status", {}).get("conditions", [])
        if isinstance(condition, dict)
    )


def _workload_ready(payload: Mapping[str, object], *, daemonset: bool = False) -> bool:
    status = payload.get("status")
    if not isinstance(status, dict):
        return False
    if daemonset:
        desired = status.get("desiredNumberScheduled")
        return (
            isinstance(desired, int)
            and desired > 0
            and status.get("numberReady") == desired
            and status.get("updatedNumberScheduled") == desired
        )
    spec = payload.get("spec")
    requested = spec.get("replicas", 1) if isinstance(spec, dict) else 1
    return (
        isinstance(requested, int)
        and requested > 0
        and status.get("availableReplicas") == requested
        and status.get("updatedReplicas") == requested
    )


def _collect_ready_observations(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
) -> tuple[dict[str, object], tuple[str, ...]]:
    selected = tenant_names(spec)
    blockers = []
    cluster = _get_management_resource(
        root, spec.namespace, f"cluster/{selected['cluster']}"
    )
    control_plane = _get_management_resource(
        root,
        spec.namespace,
        f"kamajicontrolplane/{selected['controlPlane']}",
    )
    pool = _get_management_resource(
        root, spec.namespace, f"machinepool/{selected['pool']}"
    )
    if cluster is None or not (
        cluster.get("status", {}).get("controlPlaneReady") is True
        or _condition_true(cluster, "ControlPlaneAvailable")
    ):
        blockers.append("tenant control plane is unavailable")
    if control_plane is None or control_plane.get("status", {}).get("ready") is not True:
        blockers.append("Kamaji control plane is not Ready")
    node_refs = [] if pool is None else pool.get("status", {}).get("nodeRefs", [])
    ready_replicas = None if pool is None else pool.get("status", {}).get("readyReplicas")
    if (
        not isinstance(node_refs, list)
        or len(node_refs) != spec.workers
        or ready_replicas != spec.workers
    ):
        blockers.append("MachinePool Ready replicas do not match specification")
    node_response = _tenant_kubectl(
        root,
        spec.name,
        "get",
        "nodes",
        "-o",
        "json",
        check=False,
    )
    nodes = []
    if node_response.returncode == 0:
        payload = json.loads(node_response.stdout)
        if isinstance(payload.get("items"), list):
            nodes = payload["items"]
    node_names = {
        item.get("metadata", {}).get("name")
        for item in nodes
        if isinstance(item, dict)
    }
    expected_node_names = {
        item.get("name")
        for item in node_refs
        if isinstance(item, dict)
    }
    if (
        len(nodes) != spec.workers
        or node_names != expected_node_names
    ):
        blockers.append("tenant Nodes do not match MachinePool nodeRefs")
    tenant_subnet = _foundation_networks(config)["AZURE_TENANT_SUBNET_CIDR"]
    node_identities = []
    for node in nodes:
        metadata = node.get("metadata", {})
        status = node.get("status", {})
        spec_payload = node.get("spec", {})
        provider_id = spec_payload.get("providerID")
        addresses = status.get("addresses", [])
        internal_ips = [
            address.get("address")
            for address in addresses
            if isinstance(address, dict) and address.get("type") == "InternalIP"
        ]
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions", [])
            if isinstance(condition, dict)
        )
        if not isinstance(provider_id, str) or not provider_id.lower().startswith("azure://"):
            blockers.append(f"Node cloud provider identity is absent: {metadata.get('name')}")
        if len(internal_ips) != 1:
            blockers.append(f"Node InternalIP is invalid: {metadata.get('name')}")
        else:
            try:
                if ipaddress.ip_address(internal_ips[0]) not in tenant_subnet:
                    blockers.append(
                        f"Node InternalIP is outside the tenant subnet: {metadata.get('name')}"
                    )
            except ValueError:
                blockers.append(f"Node InternalIP is invalid: {metadata.get('name')}")
        if not ready:
            blockers.append(f"Node is not Ready: {metadata.get('name')}")
        node_identities.append(
            {
                "name": metadata.get("name"),
                "uid": metadata.get("uid"),
                "providerID": provider_id,
                "internalIP": internal_ips[0] if len(internal_ips) == 1 else None,
            }
        )
    workloads = (
        ("cloudController", "kube-system", "deployment/cloud-controller-manager", False),
        ("cloudNode", "kube-system", "daemonset/cloud-node-manager", True),
        ("calicoNode", "calico-system", "daemonset/calico-node", True),
        (
            "calicoControllers",
            "calico-system",
            "deployment/calico-kube-controllers",
            False,
        ),
    )
    component_status: dict[str, bool] = {}
    component_identities: dict[str, str | None] = {}
    for key, namespace, resource, daemonset in workloads:
        response = _tenant_kubectl(
            root,
            spec.name,
            "-n",
            namespace,
            "get",
            resource,
            "-o",
            "json",
            check=False,
        )
        ready = False
        if response.returncode == 0:
            workload_payload = json.loads(response.stdout)
            ready = _workload_ready(workload_payload, daemonset=daemonset)
            uid = workload_payload.get("metadata", {}).get("uid")
            component_identities[key] = uid if isinstance(uid, str) else None
        else:
            component_identities[key] = None
        component_status[key] = ready
        if not ready:
            blockers.append(f"tenant component is not Ready: {key}")
        if not component_identities[key]:
            blockers.append(f"tenant component identity is absent: {key}")
    observations = {
        "controlPlaneAvailable": cluster is not None
        and (
            cluster.get("status", {}).get("controlPlaneReady") is True
            or _condition_true(cluster, "ControlPlaneAvailable")
        ),
        "kamajiReady": control_plane is not None
        and control_plane.get("status", {}).get("ready") is True,
        "requestedWorkers": spec.workers,
        "readyReplicas": ready_replicas,
        "nodeRefs": sorted(name for name in expected_node_names if isinstance(name, str)),
        "nodes": sorted(node_identities, key=lambda item: str(item["name"])),
        "componentIdentities": component_identities,
        **component_status,
    }
    return observations, tuple(blockers)


def _wait_ready_observations(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
) -> dict[str, object]:
    deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    last_blockers: tuple[str, ...] = ("tenant readiness has not been observed",)
    while time.monotonic() < deadline:
        observations, blockers = _collect_ready_observations(root, config, spec)
        if not blockers:
            return observations
        last_blockers = blockers
        time.sleep(10)
    raise RuntimeError(
        "Azure tenant is not Ready: " + "; ".join(last_blockers)
    )


def _tenant_spec_blockers(
    spec: TenantSpec,
    selected: Mapping[str, str],
    payloads: Mapping[str, Mapping[str, object]],
    config: Mapping[str, str],
) -> tuple[str, ...]:
    blockers = []
    cluster_spec = payloads.get("clusterUid", {}).get("spec", {})
    cluster_network = (
        cluster_spec.get("clusterNetwork")
        if isinstance(cluster_spec, dict)
        else {}
    )
    if not isinstance(cluster_network, dict):
        cluster_network = {}
    if cluster_network.get("pods", {}).get("cidrBlocks") != [str(spec.pod_network)]:
        blockers.append("tenant Cluster Pod CIDR changed")
    if cluster_network.get("services", {}).get("cidrBlocks") != [
        str(spec.service_network)
    ]:
        blockers.append("tenant Cluster Service CIDR changed")
    if cluster_network.get("serviceDomain") != spec.cluster_domain:
        blockers.append("tenant Cluster service domain changed")
    control_plane_spec = payloads.get("kamajiControlPlaneUid", {}).get("spec", {})
    if (
        not isinstance(control_plane_spec, dict)
        or str(control_plane_spec.get("version", "")).removeprefix("v")
        != spec.kubernetes_version
    ):
        blockers.append("tenant control-plane Kubernetes version changed")
    machine_pool_spec = payloads.get("machinePoolUid", {}).get("spec", {})
    if (
        not isinstance(machine_pool_spec, dict)
        or machine_pool_spec.get("replicas") != spec.workers
        or machine_pool_spec.get("clusterName") != spec.name
    ):
        blockers.append("tenant MachinePool specification changed")
    azure_pool_spec = payloads.get("azureMachinePoolUid", {}).get("spec", {})
    template = (
        azure_pool_spec.get("template")
        if isinstance(azure_pool_spec, dict)
        else {}
    )
    if not isinstance(template, dict):
        template = {}
    interfaces = template.get("networkInterfaces", [])
    interface_ready = (
        isinstance(interfaces, list)
        and len(interfaces) == 1
        and isinstance(interfaces[0], dict)
        and interfaces[0].get("subnetName") == "tenant"
        and interfaces[0].get("privateIPConfigs", 1) == 1
    )
    image = template.get("image", {})
    gallery = image.get("computeGallery", {}) if isinstance(image, dict) else {}
    if (
        template.get("vmSize") != config["AZURE_TENANT_NODE_SKU"]
        or not interface_ready
        or not isinstance(gallery, dict)
        or gallery.get("version") != spec.kubernetes_version
    ):
        blockers.append(f"tenant AzureMachinePool specification changed: {selected['pool']}")
    return tuple(blockers)


def _expected_tenant_markers(
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
) -> dict[str, str]:
    marker_operation_id = identity.observed.get("markerOperationId")
    if not marker_operation_id:
        raise RuntimeError("Azure tenant marker operation identity is absent")
    return {
        "tenant": spec.name,
        "profile": "azure",
        "specificationSha256": spec.sha256(),
        "foundationSha256": foundation_sha256(identity.foundation_identity),
        "operationId": marker_operation_id,
    }


def _management_resource_specs(
    spec: TenantSpec,
) -> tuple[tuple[str, str | None, str, str], ...]:
    selected = tenant_names(spec)
    return (
        ("namespaceUid", None, "Namespace", spec.namespace),
        (
            "azureClusterIdentityUid",
            spec.namespace,
            "AzureClusterIdentity",
            selected["azureClusterIdentity"],
        ),
        ("clusterUid", spec.namespace, "Cluster", selected["cluster"]),
        (
            "azureClusterUid",
            spec.namespace,
            "AzureCluster",
            selected["azureCluster"],
        ),
        (
            "kamajiControlPlaneUid",
            spec.namespace,
            "KamajiControlPlane",
            selected["controlPlane"],
        ),
        (
            "kubeadmConfigUid",
            spec.namespace,
            "KubeadmConfig",
            selected["pool"],
        ),
        (
            "azureMachinePoolUid",
            spec.namespace,
            "AzureMachinePool",
            selected["pool"],
        ),
        (
            "machinePoolUid",
            spec.namespace,
            "MachinePool",
            selected["pool"],
        ),
        (
            "cloudValuesConfigMapUid",
            spec.namespace,
            "ConfigMap",
            selected["cloudValues"],
        ),
        (
            "networkValuesConfigMapUid",
            spec.namespace,
            "ConfigMap",
            selected["networkValues"],
        ),
        (
            "statusProbeDeploymentUid",
            spec.namespace,
            "Deployment",
            selected["statusProbe"],
        ),
        ("addonJobUid", spec.namespace, "Job", selected["addonJob"]),
    )


def _management_resource_name(kind: str, name: str) -> str:
    return f"{kind.lower()}/{name}"


def _management_object_summary(payload: Mapping[str, object]) -> dict[str, object]:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    owner_references = metadata.get("ownerReferences")
    owner_references = owner_references if isinstance(owner_references, list) else []
    return {
        "apiVersion": str(payload.get("apiVersion", "")),
        "kind": str(payload.get("kind", "")),
        "name": str(metadata.get("name", "")),
        "uid": str(metadata.get("uid", "")),
        "resourceVersion": str(metadata.get("resourceVersion", "")),
        "ownerUids": sorted(
            str(reference["uid"])
            for reference in owner_references
            if isinstance(reference, dict)
            and isinstance(reference.get("uid"), str)
            and reference["uid"]
        ),
        "finalizers": sorted(
            str(value)
            for value in metadata.get("finalizers", [])
            if isinstance(value, str)
        ),
        "deletionTimestamp": metadata.get("deletionTimestamp"),
    }


def _list_namespaced_management_objects(
    root: Path,
    namespace: str,
) -> list[dict[str, object]]:
    response = _kubectl(
        root,
        "api-resources",
        "--namespaced=true",
        "--verbs=list",
        "-o",
        "name",
        check=False,
    )
    if response.returncode != 0:
        raise RuntimeError(
            "Azure management API discovery failed: " + response.stderr
        )
    resource_types = sorted(set(response.stdout.split()))
    if not resource_types:
        raise RuntimeError("Azure management API discovery returned no resources")
    objects: list[dict[str, object]] = []
    for resource_type in resource_types:
        listed = _kubectl(
            root,
            "-n",
            namespace,
            "get",
            resource_type,
            "-o",
            "json",
            check=False,
        )
        if listed.returncode != 0:
            raise RuntimeError(
                f"Azure management inventory failed for {resource_type}: "
                f"{listed.stderr}"
            )
        try:
            payload = json.loads(listed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Azure management inventory is invalid for {resource_type}"
            ) from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RuntimeError(
                f"Azure management inventory is invalid for {resource_type}"
            )
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"Azure management inventory contains an invalid {resource_type}"
                )
            objects.append(item)
    return objects


def _classify_management_owned_resources(
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    namespace: Mapping[str, object] | None,
    objects: Sequence[Mapping[str, object]],
    *,
    require_complete: bool,
    verified_uids: Sequence[str] = (),
) -> dict[str, object]:
    markers = _expected_tenant_markers(spec, identity)
    expected = {
        (kind, name): (key, namespace_name)
        for key, namespace_name, kind, name in _management_resource_specs(spec)
    }
    records: list[dict[str, object]] = []
    if namespace is not None:
        records.append(_management_object_summary(namespace))
    records.extend(_management_object_summary(item) for item in objects)
    by_identity = {
        (str(item["kind"]), str(item["name"])): item
        for item in records
        if item["kind"] and item["name"]
    }
    unknown = []
    classified: dict[tuple[str, str], str] = {}
    verified_uid_set = set(verified_uids)
    owned_uids = {
        uid
        for key, uid in identity.observed.items()
        if key.endswith("Uid") and isinstance(uid, str) and uid
    }
    for resource_identity, (key, _) in expected.items():
        item = by_identity.get(resource_identity)
        recorded_uid = identity.observed.get(key)
        if item is None:
            if require_complete:
                unknown.append(
                    {
                        "kind": resource_identity[0],
                        "name": resource_identity[1],
                        "reason": "recorded management resource is absent",
                    }
                )
            continue
        if not recorded_uid:
            unknown.append(
                {
                    "kind": item["kind"],
                    "name": item["name"],
                    "reason": "recorded management UID is absent",
                }
            )
            continue
        if item["uid"] != recorded_uid:
            unknown.append(
                {
                    "kind": item["kind"],
                    "name": item["name"],
                    "reason": "management UID changed",
                }
            )
            continue
        payload = namespace if resource_identity[0] == "Namespace" else next(
            (
                value
                for value in objects
                if value.get("kind") == resource_identity[0]
                and value.get("metadata", {}).get("name") == resource_identity[1]
            ),
            None,
        )
        if payload is None:
            raise RuntimeError("Azure management inventory changed during classification")
        _require_markers(
            payload,
            markers,
            _management_resource_name(resource_identity[0], resource_identity[1]),
        )
        classification = (
            "orchestration"
            if resource_identity[0] in KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
            else "controller"
        )
        classified[resource_identity] = classification
        owned_uids.add(str(item["uid"]))
    changed = True
    while changed:
        changed = False
        for item in records:
            identity_key = (str(item["kind"]), str(item["name"]))
            if identity_key in classified:
                continue
            owner_uids = set(item["ownerUids"])
            if owner_uids & owned_uids:
                kind = str(item["kind"])
                if kind in (
                    KNOWN_CONTROLLER_MANAGEMENT_KINDS
                    | KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
                    | KNOWN_NAMESPACE_CHILD_KINDS
                ):
                    classified[identity_key] = (
                        "namespace-child"
                        if kind in KNOWN_NAMESPACE_CHILD_KINDS
                        else "controller"
                    )
                    if item["uid"]:
                        owned_uids.add(str(item["uid"]))
                    changed = True
    for item in records:
        identity_key = (str(item["kind"]), str(item["name"]))
        if identity_key in classified:
            continue
        kind = str(item["kind"])
        name = str(item["name"])
        payload = namespace if kind == "Namespace" else next(
            (
                value
                for value in objects
                if value.get("kind") == kind
                and value.get("metadata", {}).get("name") == name
            ),
            None,
        )
        observed_markers = (
            resource_lifecycle_markers(payload)
            if isinstance(payload, Mapping)
            else {}
        )
        resource_spec = (
            payload.get("spec")
            if isinstance(payload, Mapping)
            else None
        )
        resource_tags = (
            resource_spec.get("tags")
            if isinstance(resource_spec, Mapping)
            else None
        )
        expected_tags = _azure_tags(markers)
        has_azure_tags = isinstance(resource_tags, dict) and any(
            key in resource_tags for key in expected_tags
        )
        exact_azure_tags = _azure_tags_match(resource_tags, expected_tags)
        if any(observed_markers.values()) or has_azure_tags:
            if observed_markers != markers and not exact_azure_tags:
                reason = "foreign lifecycle markers"
            elif kind not in (
                KNOWN_CONTROLLER_MANAGEMENT_KINDS
                | KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
            ):
                reason = "unknown marked management kind"
            else:
                classified[identity_key] = (
                    "orchestration"
                    if kind in KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
                    else "controller"
                )
                continue
        elif item["uid"] in verified_uid_set and kind in (
            KNOWN_CONTROLLER_MANAGEMENT_KINDS
            | KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
            | KNOWN_NAMESPACE_CHILD_KINDS
        ):
            classified[identity_key] = (
                "namespace-child"
                if kind in KNOWN_NAMESPACE_CHILD_KINDS
                else (
                    "orchestration"
                    if kind in KNOWN_ORCHESTRATION_MANAGEMENT_KINDS
                    else "controller"
                )
            )
            continue
        elif kind in KNOWN_NAMESPACE_CHILD_KINDS or (
            kind == "ConfigMap" and name == "kube-root-ca.crt"
        ) or (
            kind == "Secret" and name.startswith("default-token-")
        ):
            classified[identity_key] = "namespace-child"
            continue
        else:
            reason = "unclassifiable namespace resource"
        unknown.append({"kind": kind, "name": name, "reason": reason})
    result = {
        "controller": sorted(
            (
                item
                for item in records
                if classified.get((str(item["kind"]), str(item["name"])))
                == "controller"
            ),
            key=lambda item: (str(item["kind"]), str(item["name"])),
        ),
        "orchestration": sorted(
            (
                item
                for item in records
                if classified.get((str(item["kind"]), str(item["name"])))
                == "orchestration"
            ),
            key=lambda item: (str(item["kind"]), str(item["name"])),
        ),
        "namespaceChildren": sorted(
            (
                item
                for item in records
                if classified.get((str(item["kind"]), str(item["name"])))
                == "namespace-child"
            ),
            key=lambda item: (str(item["kind"]), str(item["name"])),
        ),
        "unknown": sorted(
            unknown,
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
    }
    if result["unknown"]:
        raise RuntimeError(
            "Azure tenant management ownership is unknown: "
            + json.dumps(result["unknown"], sort_keys=True)
        )
    return result


def discover_management_owned_resources(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    require_complete: bool,
    verified_uids: Sequence[str] = (),
) -> dict[str, object]:
    namespace = _get_management_resource(root, None, f"namespace/{spec.namespace}")
    if namespace is None:
        if require_complete:
            raise RuntimeError("Azure tenant Namespace is absent")
        return {
            "controller": [],
            "orchestration": [],
            "namespaceChildren": [],
            "unknown": [],
        }
    objects = _list_namespaced_management_objects(root, spec.namespace)
    return _classify_management_owned_resources(
        spec,
        identity,
        namespace,
        objects,
        require_complete=require_complete,
        verified_uids=verified_uids,
    )


def classify_azure_owned_resources(
    resources: Sequence[Mapping[str, object]],
    expected_markers: Mapping[str, str],
    *,
    parent_ids: Sequence[str] = (),
    aso_objects: Sequence[Mapping[str, object]] = (),
    parent_uids: Sequence[str] = (),
    verified_ids: Sequence[str] = (),
) -> dict[str, object]:
    expected_tags = _azure_tags(expected_markers)
    normalized_parents = tuple(parent.rstrip("/").lower() for parent in parent_ids)
    normalized_verified = {identifier.lower() for identifier in verified_ids}
    owned = []
    unknown = []
    for resource in resources:
        identifier = resource.get("id")
        resource_type = str(resource.get("type", "")).lower()
        tags = resource.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        marked = any(key in tags for key in expected_tags)
        exact = _azure_tags_match(tags, expected_tags)
        child = isinstance(identifier, str) and any(
            identifier.lower().startswith(parent + "/")
            for parent in normalized_parents
        )
        verified = isinstance(identifier, str) and identifier.lower() in normalized_verified
        if not (marked or exact or child or verified):
            continue
        if not isinstance(identifier, str) or not identifier:
            unknown.append({"kind": "AzureResource", "reason": "missing id"})
        elif resource_type not in KNOWN_AZURE_TENANT_TYPES:
            unknown.append(
                {"kind": "AzureResource", "id": identifier, "type": resource_type}
            )
        elif marked and not exact:
            unknown.append(
                {"kind": "AzureResource", "id": identifier, "reason": "foreign markers"}
            )
        else:
            owned.append({"id": identifier, "type": resource_type})
    parent_uid_set = set(parent_uids)
    aso_owned = []
    for payload in aso_objects:
        kind = payload.get("kind")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            unknown.append({"kind": str(kind), "reason": "missing metadata"})
            continue
        owner_uids = {
            reference.get("uid")
            for reference in metadata.get("ownerReferences", [])
            if isinstance(reference, dict)
        }
        marker_values = resource_lifecycle_markers(payload)
        resource_spec = payload.get("spec")
        resource_spec = resource_spec if isinstance(resource_spec, dict) else {}
        exact = (
            marker_values == dict(expected_markers)
            or _azure_tags_match(resource_spec.get("tags"), expected_tags)
        )
        parent_owned = bool(owner_uids & parent_uid_set)
        if not (exact or parent_owned):
            unknown.append(
                {
                    "kind": str(kind),
                    "name": str(metadata.get("name", "")),
                    "reason": "foreign ASO ownership",
                }
            )
            continue
        if kind not in KNOWN_ASO_TENANT_KINDS:
            unknown.append(
                {
                    "kind": str(kind),
                    "name": str(metadata.get("name", "")),
                    "reason": "unknown ASO ownership",
                }
            )
        else:
            status = payload.get("status")
            resource_id = status.get("id") if isinstance(status, dict) else None
            if not isinstance(resource_id, str) or not resource_id:
                unknown.append(
                    {
                        "kind": str(kind),
                        "name": str(metadata.get("name", "")),
                        "reason": "missing Azure resource id",
                    }
                )
                continue
            aso_owned.append(
                {
                    "kind": str(kind),
                    "name": str(metadata.get("name", "")),
                    "uid": str(metadata.get("uid", "")),
                    "azureResourceId": resource_id,
                }
            )
    unique_owned = {str(item["id"]).lower(): item for item in owned}
    result = {
        "azure": sorted(unique_owned.values(), key=lambda item: str(item["id"])),
        "aso": sorted(aso_owned, key=lambda item: (item["kind"], item["name"])),
        "unknown": sorted(unknown, key=lambda item: json.dumps(item, sort_keys=True)),
    }
    if result["unknown"]:
        raise RuntimeError(
            "Azure tenant resource ownership is unknown: "
            + json.dumps(result["unknown"], sort_keys=True)
        )
    return result


def discover_azure_owned_resources(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    require_parents: bool = True,
    require_azure_resources: bool = True,
    verified_resource_ids: Sequence[str] = (),
) -> dict[str, object]:
    markers = _expected_tenant_markers(spec, identity)
    selected = tenant_names(spec)
    for key, resource in (
        ("azureClusterUid", f"azurecluster/{selected['azureCluster']}"),
        ("azureMachinePoolUid", f"azuremachinepool/{selected['pool']}"),
    ):
        parent = _get_management_resource(root, spec.namespace, resource)
        if parent is None:
            if require_parents:
                raise RuntimeError(f"Azure tenant discovery parent is absent: {resource}")
            continue
        _require_markers(parent, markers, resource)
        if parent.get("metadata", {}).get("uid") != identity.observed.get(key):
            raise RuntimeError(f"Azure tenant discovery parent identity changed: {resource}")
    inventory = load_inventory(root, config)
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    resources = _json(
        [
            "az",
            "resource",
            "list",
            "--resource-group",
            str(outputs["resourceGroupName"]),
            "--output",
            "json",
        ]
    )
    if not isinstance(resources, list):
        raise RuntimeError("Azure tenant resource discovery returned invalid resources")
    aso_objects: list[Mapping[str, object]] = []
    parent_ids = [
        value
        for key, value in identity.observed.items()
        if key == "vmssId"
    ]
    verified_ids = list(verified_resource_ids)
    serialized_recorded = identity.observed.get("azureResources")
    if serialized_recorded:
        try:
            recorded_payload = json.loads(serialized_recorded)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "recorded Azure tenant resource inventory is invalid"
            ) from exc
        recorded_azure = (
            recorded_payload.get("azure")
            if isinstance(recorded_payload, dict)
            else None
        )
        if not isinstance(recorded_azure, list):
            raise RuntimeError(
                "recorded Azure tenant resource inventory is invalid"
            )
        for item in recorded_azure:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise RuntimeError(
                    "recorded Azure tenant resource inventory is invalid"
                )
            verified_ids.append(item["id"])
    current_resource_ids = {
        str(resource.get("id", "")).lower()
        for resource in resources
        if isinstance(resource, dict)
    }
    if parent_ids and any(
        parent.lower() in current_resource_ids for parent in parent_ids
    ):
        instance_response = _az(
            "vmss",
            "list-instances",
            "--resource-group",
            str(outputs["resourceGroupName"]),
            "--name",
            tenant_names(spec)["pool"],
            "--query",
            "[].{id:id,type:type,tags:tags}",
            "--output",
            "json",
            check=False,
        )
        if instance_response.returncode != 0:
            raise RuntimeError("Azure tenant VMSS instance discovery failed")
        instances = json.loads(instance_response.stdout)
        if not isinstance(instances, list):
            raise RuntimeError(
                "Azure tenant VMSS instance discovery returned invalid resources"
            )
        for instance in instances:
            if not isinstance(instance, dict) or not isinstance(
                instance.get("id"), str
            ):
                raise RuntimeError(
                    "Azure tenant VMSS instance discovery returned an invalid instance"
                )
            if not instance.get("type"):
                instance["type"] = (
                    "Microsoft.Compute/virtualMachineScaleSets/virtualMachines"
                )
            resources.append(instance)
            verified_ids.append(instance["id"])
        nic_response = _az(
            "vmss",
            "nic",
            "list",
            "--resource-group",
            str(outputs["resourceGroupName"]),
            "--vmss-name",
            selected["pool"],
            "--query",
            "[].{id:id,type:type,tags:tags}",
            "--output",
            "json",
            check=False,
        )
        if nic_response.returncode != 0:
            raise RuntimeError("Azure tenant VMSS NIC discovery failed")
        nics = json.loads(nic_response.stdout)
        if not isinstance(nics, list):
            raise RuntimeError("Azure tenant VMSS NIC discovery returned invalid resources")
        for nic in nics:
            if not isinstance(nic, dict) or not isinstance(nic.get("id"), str):
                raise RuntimeError("Azure tenant VMSS NIC discovery returned an invalid NIC")
            if not nic.get("type"):
                nic["type"] = "Microsoft.Network/networkInterfaces"
            resources.append(nic)
            verified_ids.append(nic["id"])
    parent_uids = [
        value
        for key, value in identity.observed.items()
        if key in {"azureClusterUid", "azureMachinePoolUid"}
    ]
    namespace = _get_management_resource(root, None, f"namespace/{spec.namespace}")
    if namespace is None:
        if require_parents:
            raise RuntimeError("Azure tenant Namespace is absent during discovery")
        return classify_azure_owned_resources(
            resources,
            markers,
            parent_ids=parent_ids,
            parent_uids=parent_uids,
            verified_ids=verified_ids,
        )
    aso_resource_types = set()
    for group in (
        "network.azure.com",
        "compute.azure.com",
        "resources.azure.com",
    ):
        response = _kubectl(
            root,
            "api-resources",
            "--api-group",
            group,
            "--namespaced=true",
            "-o",
            "name",
            check=False,
        )
        if response.returncode != 0:
            raise RuntimeError(
                f"Azure Service Operator API discovery failed for {group}"
            )
        aso_resource_types.update(response.stdout.split())
    for resource_name in sorted(aso_resource_types):
        aso_response = _kubectl(
            root,
            "-n",
            spec.namespace,
            "get",
            resource_name,
            "-o",
            "json",
            check=False,
        )
        if aso_response.returncode != 0:
            raise RuntimeError(
                f"Azure Service Operator discovery failed for {resource_name}"
            )
        payload = json.loads(aso_response.stdout)
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("Azure Service Operator discovery returned invalid objects")
        for item in items:
            if isinstance(item, dict):
                metadata = item.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                status = item.get("status")
                resource_id = status.get("id") if isinstance(status, dict) else None
                kind = item.get("kind")
                if kind in KNOWN_ASO_FOUNDATION_REFERENCE_OUTPUTS:
                    expected_id = outputs[
                        KNOWN_ASO_FOUNDATION_REFERENCE_OUTPUTS[str(kind)]
                    ]
                    annotations = metadata.get("annotations")
                    annotations = (
                        annotations if isinstance(annotations, dict) else {}
                    )
                    if (
                        not isinstance(resource_id, str)
                        or not _azure_id_equal(resource_id, expected_id)
                        or annotations.get(
                            "serviceoperator.azure.com/reconcile-policy"
                        )
                        != "skip"
                    ):
                        raise RuntimeError(
                            f"Azure Service Operator foundation reference changed: {kind}"
                        )
                    continue
                aso_objects.append(item)
                owner_uids = {
                    reference.get("uid")
                    for reference in metadata.get("ownerReferences", [])
                    if isinstance(reference, dict)
                }
                resource_spec = item.get("spec")
                resource_spec = (
                    resource_spec if isinstance(resource_spec, dict) else {}
                )
                owned = (
                    resource_lifecycle_markers(item) == markers
                    or _azure_tags_match(
                        resource_spec.get("tags"),
                        _azure_tags(markers),
                    )
                    or bool(owner_uids & set(parent_uids))
                )
                if owned and isinstance(resource_id, str) and resource_id:
                    verified_ids.append(resource_id)
                    resource_response = _az(
                        "resource",
                        "show",
                        "--ids",
                        resource_id,
                        "--output",
                        "json",
                        check=False,
                    )
                    if resource_response.returncode != 0:
                        if require_azure_resources:
                            raise RuntimeError(
                                "recorded Azure Service Operator resource is absent"
                            )
                        continue
                    resource_payload = json.loads(resource_response.stdout)
                    if not isinstance(resource_payload, dict):
                        raise RuntimeError(
                            "Azure Service Operator resource discovery is invalid"
                        )
                    resources.append(resource_payload)
    return classify_azure_owned_resources(
        resources,
        markers,
        parent_ids=parent_ids,
        aso_objects=aso_objects,
        parent_uids=parent_uids,
        verified_ids=verified_ids,
    )


def _merge_owned_discoveries(
    discoveries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    azure: dict[str, dict[str, object]] = {}
    aso: dict[tuple[str, str], dict[str, object]] = {}
    for discovery in discoveries:
        unknown = discovery.get("unknown")
        if unknown:
            raise RuntimeError(
                "Azure tenant resource discovery is incomplete: "
                + json.dumps(unknown, sort_keys=True)
            )
        azure_items = discovery.get("azure")
        aso_items = discovery.get("aso")
        if not isinstance(azure_items, list) or not isinstance(aso_items, list):
            raise RuntimeError("Azure tenant resource discovery is invalid")
        for item in azure_items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise RuntimeError("Azure tenant resource discovery is invalid")
            azure[item["id"].lower()] = dict(item)
        for item in aso_items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("kind"), str)
                or not isinstance(item.get("uid"), str)
            ):
                raise RuntimeError("Azure tenant ASO discovery is invalid")
            aso[(item["kind"], item["uid"])] = dict(item)
    return {
        "azure": sorted(azure.values(), key=lambda item: str(item["id"])),
        "aso": sorted(
            aso.values(),
            key=lambda item: (str(item["kind"]), str(item.get("name", ""))),
        ),
        "unknown": [],
    }


def _merge_management_discoveries(
    discoveries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {
        "controller": [],
        "orchestration": [],
        "namespaceChildren": [],
        "unknown": [],
    }
    for category in ("controller", "orchestration", "namespaceChildren"):
        merged = {}
        for discovery in discoveries:
            unknown = discovery.get("unknown")
            if unknown:
                raise RuntimeError(
                    "Azure tenant management discovery is incomplete: "
                    + json.dumps(unknown, sort_keys=True)
                )
            items = discovery.get(category)
            if not isinstance(items, list):
                raise RuntimeError("Azure tenant management discovery is invalid")
            for item in items:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("kind"), str)
                    or not isinstance(item.get("name"), str)
                ):
                    raise RuntimeError("Azure tenant management discovery is invalid")
                key = (
                    item["kind"],
                    item["name"],
                    str(item.get("uid", "")),
                )
                merged[key] = dict(item)
        result[category] = sorted(
            merged.values(),
            key=lambda item: (
                str(item["kind"]),
                str(item["name"]),
                str(item.get("uid", "")),
            ),
        )
    return result


def _recorded_owned_resources(identity: TenantIdentity) -> dict[str, object]:
    serialized = identity.observed.get("azureResources")
    if not serialized:
        raise RuntimeError("recorded Azure tenant resource inventory is absent")
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise RuntimeError("recorded Azure tenant resource inventory is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("recorded Azure tenant resource inventory is invalid")
    return _merge_owned_discoveries((payload,))


def _journal_discovery(
    journal: OperationJournal,
    key: str,
) -> dict[str, object] | None:
    serialized = journal.observed.get(key)
    if serialized is None:
        return None
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"recorded Azure deletion discovery is invalid: {key}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"recorded Azure deletion discovery is invalid: {key}"
        )
    return payload


def _require_recorded_resources_present(
    recorded: Mapping[str, object],
    discovered: Mapping[str, object],
) -> None:
    recorded_azure = {
        str(item["id"]).lower()
        for item in recorded["azure"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    discovered_azure = {
        str(item["id"]).lower()
        for item in discovered["azure"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    recorded_aso = {
        (str(item.get("kind")), str(item.get("uid")))
        for item in recorded["aso"]
        if isinstance(item, dict)
    }
    discovered_aso = {
        (str(item.get("kind")), str(item.get("uid")))
        for item in discovered["aso"]
        if isinstance(item, dict)
    }
    missing = sorted(recorded_azure - discovered_azure)
    missing_aso = sorted(recorded_aso - discovered_aso)
    if missing or missing_aso:
        raise RuntimeError(
            "recorded Azure tenant resource inventory changed before deletion: "
            + json.dumps(
                {"azure": missing, "aso": missing_aso},
                sort_keys=True,
            )
        )


def _discover_owned_repeatedly(
    root: Path,
    config: Mapping[str, str],
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    passes: int,
    require_parents: bool,
    require_azure_resources: bool,
    verified_resource_ids: Sequence[str] = (),
) -> dict[str, object]:
    if passes < 2:
        raise RuntimeError("Azure tenant discovery must be repeated")
    discoveries = []
    verified_ids = list(verified_resource_ids)
    for _ in range(passes):
        discovery = discover_azure_owned_resources(
            root,
            config,
            spec,
            identity,
            require_parents=require_parents,
            require_azure_resources=require_azure_resources,
            verified_resource_ids=verified_ids,
        )
        discoveries.append(discovery)
        verified_ids.extend(
            str(item["id"])
            for item in discovery["azure"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    return _merge_owned_discoveries(discoveries)


def _exact_delete_management_resource(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    namespace: str | None,
    resource: str,
    uid_key: str,
    cascade: str,
) -> bool:
    payload = _get_management_resource(root, namespace, resource)
    if payload is None:
        return False
    _require_markers(payload, _expected_tenant_markers(spec, identity), resource)
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    if uid != identity.observed.get(uid_key):
        raise RuntimeError(f"Azure tenant management UID changed: {resource}")
    if not isinstance(resource_version, str) or not resource_version:
        raise RuntimeError(
            f"Azure tenant management resourceVersion is absent: {resource}"
        )
    api_version = payload.get("apiVersion")
    kind = payload.get("kind")
    name = metadata.get("name")
    plural = MANAGEMENT_RESOURCE_PLURALS.get(str(kind))
    if (
        not isinstance(api_version, str)
        or not api_version
        or not isinstance(name, str)
        or not name
        or plural is None
    ):
        raise RuntimeError(
            f"Azure tenant management API identity is invalid: {resource}"
        )
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        base = (
            "/apis/"
            + urllib.parse.quote(group, safe=".")
            + "/"
            + urllib.parse.quote(version, safe="")
        )
    else:
        base = "/api/" + urllib.parse.quote(api_version, safe="")
    if namespace is None:
        path = (
            f"{base}/{plural}/"
            + urllib.parse.quote(name, safe="")
        )
    else:
        path = (
            f"{base}/namespaces/"
            + urllib.parse.quote(namespace, safe="")
            + f"/{plural}/"
            + urllib.parse.quote(name, safe="")
        )
    propagation = {
        "background": "Background",
        "foreground": "Foreground",
        "orphan": "Orphan",
    }.get(cascade)
    if propagation is None:
        raise RuntimeError(f"unsupported Kubernetes deletion propagation: {cascade}")
    _kubectl(
        root,
        "delete",
        f"--raw={path}",
        "-f",
        "-",
        input_text=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": propagation,
                "preconditions": {
                    "uid": uid,
                    "resourceVersion": resource_version,
                },
            },
            separators=(",", ":"),
        )
        + "\n",
    )
    return True


def _enable_capz_external_control_plane_delete(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
) -> None:
    resource = f"azurecluster/{tenant_names(spec)['azureCluster']}"
    deadline = time.monotonic() + 120
    while True:
        payload = _get_management_resource(root, spec.namespace, resource)
        if payload is None:
            return
        _require_markers(payload, _expected_tenant_markers(spec, identity), resource)
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("uid") != identity.observed.get("azureClusterUid"):
            raise RuntimeError(f"Azure tenant management UID changed: {resource}")
        if metadata.get("deletionTimestamp"):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "AzureCluster deletion did not start before the CAPZ workaround"
            )
        time.sleep(2)

    azure_cluster_spec = payload.get("spec")
    azure_cluster_spec = (
        azure_cluster_spec if isinstance(azure_cluster_spec, dict) else {}
    )
    network_spec = azure_cluster_spec.get("networkSpec")
    network_spec = network_spec if isinstance(network_spec, dict) else {}
    api_server_lb = network_spec.get("apiServerLB")
    labels = metadata.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    if azure_cluster_spec.get("controlPlaneEnabled") is not False:
        raise RuntimeError(
            "CAPZ external control-plane ownership changed during deletion"
        )
    if labels.get(CAPZ_EXTERNAL_CONTROL_PLANE_LABEL) != "true":
        raise RuntimeError(
            "CAPZ external control-plane label changed during deletion"
        )
    if isinstance(api_server_lb, dict) and api_server_lb.get("type") == "Public":
        return
    _kubectl(
        root,
        "-n",
        spec.namespace,
        "patch",
        resource,
        "--type=merge",
        "--field-manager=cnpg-vcluster-azure",
        "-p",
        json.dumps(
            {
                "spec": {
                    "networkSpec": {
                        "apiServerLB": {"type": "Public"},
                    }
                }
            },
            separators=(",", ":"),
        ),
    )
    updated = _get_management_resource(root, spec.namespace, resource)
    if updated is None:
        return
    updated_metadata = updated.get("metadata")
    updated_metadata = (
        updated_metadata if isinstance(updated_metadata, dict) else {}
    )
    updated_spec = updated.get("spec")
    updated_spec = updated_spec if isinstance(updated_spec, dict) else {}
    network_spec = updated_spec.get("networkSpec")
    network_spec = network_spec if isinstance(network_spec, dict) else {}
    if (
        updated_metadata.get("uid") != identity.observed.get("azureClusterUid")
        or not updated_metadata.get("deletionTimestamp")
        or updated_spec.get("controlPlaneEnabled") is not False
        or updated_metadata.get("labels", {}).get(
            CAPZ_EXTERNAL_CONTROL_PLANE_LABEL
        )
        != "true"
        or not isinstance(network_spec.get("apiServerLB"), dict)
        or network_spec["apiServerLB"].get("type") != "Public"
    ):
        raise RuntimeError(
            "CAPZ external control-plane deletion workaround was not retained"
        )


def _owned_tenant_machines(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
) -> list[Mapping[str, object]]:
    response = _kubectl(
        root,
        "-n",
        spec.namespace,
        "get",
        "machines",
        "-l",
        f"cluster.x-k8s.io/cluster-name={spec.name}",
        "-o",
        "json",
    )
    payload = json.loads(response.stdout)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Azure tenant Machine discovery returned invalid objects")
    expected_markers = _expected_tenant_markers(spec, identity)
    expected_pool_uid = identity.observed.get("machinePoolUid")
    result = []
    for machine in items:
        if not isinstance(machine, dict):
            raise RuntimeError("Azure tenant Machine discovery returned an invalid object")
        metadata = machine.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        name = metadata.get("name")
        uid = metadata.get("uid")
        annotations = metadata.get("annotations")
        owner_uids = {
            reference.get("uid")
            for reference in metadata.get("ownerReferences", [])
            if isinstance(reference, dict)
        }
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(uid, str)
            or not uid
            or not isinstance(annotations, dict)
            or expected_pool_uid not in owner_uids
        ):
            raise RuntimeError("Azure tenant Machine ownership is invalid")
        _require_markers(machine, expected_markers, f"machine/{name}")
        result.append(machine)
    return result


def _exclude_tenant_machines_from_drain(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
) -> None:
    for machine in _owned_tenant_machines(root, spec, identity):
        metadata = machine["metadata"]
        assert isinstance(metadata, dict)
        name = metadata["name"]
        uid = metadata["uid"]
        _kubectl(
            root,
            "-n",
            spec.namespace,
            "patch",
            f"machine/{name}",
            "--type=json",
            "-p",
            json.dumps(
                [
                    {
                        "op": "test",
                        "path": "/metadata/uid",
                        "value": uid,
                    },
                    {
                        "op": "add",
                        "path": (
                            "/metadata/annotations/"
                            "machine.cluster.x-k8s.io~1exclude-node-draining"
                        ),
                        "value": "true",
                    },
                ],
                separators=(",", ":"),
            ),
        )


def _deletion_diagnostics(
    root: Path,
    spec: TenantSpec,
    identity: TenantIdentity | OperationJournal,
    *,
    management: Mapping[str, object] | None,
    azure: Mapping[str, object] | None,
    error: BaseException | str,
) -> dict[str, object]:
    selected = tenant_names(spec)
    conditions = []
    for resource in (
        f"cluster/{selected['cluster']}",
        f"kamajicontrolplane/{selected['controlPlane']}",
        f"azurecluster/{selected['azureCluster']}",
        f"kubeadmconfig/{selected['pool']}",
        f"machinepool/{selected['pool']}",
        f"azuremachinepool/{selected['pool']}",
    ):
        try:
            payload = _get_management_resource(root, spec.namespace, resource)
        except BaseException as exc:
            conditions.append(
                {"resource": resource, "inspectionError": redact(str(exc))}
            )
            continue
        if payload is None:
            continue
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        status = payload.get("status")
        status = status if isinstance(status, dict) else {}
        conditions.append(
            {
                "resource": resource,
                "uid": metadata.get("uid"),
                "deletionTimestamp": metadata.get("deletionTimestamp"),
                "finalizers": metadata.get("finalizers", []),
                "conditions": status.get("conditions", []),
                "failureReason": status.get("failureReason"),
                "failureMessage": status.get("failureMessage"),
            }
        )
    events = []
    try:
        response = _kubectl(
            root,
            "-n",
            spec.namespace,
            "get",
            "events",
            "-o",
            "json",
            check=False,
        )
        if response.returncode == 0:
            payload = json.loads(response.stdout)
            for item in payload.get("items", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    continue
                involved = item.get("involvedObject")
                involved = involved if isinstance(involved, dict) else {}
                events.append(
                    {
                        "type": item.get("type"),
                        "reason": item.get("reason"),
                        "message": item.get("message"),
                        "kind": involved.get("kind"),
                        "name": involved.get("name"),
                    }
                )
    except BaseException as exc:
        events.append({"inspectionError": redact(str(exc))})
    return {
        "schema": 1,
        "tenant": spec.name,
        "specificationSha256": spec.sha256(),
        "foundationSha256": foundation_sha256(identity.foundation_identity),
        "error": str(error),
        "management": management or {},
        "azure": azure or {},
        "conditions": conditions,
        "events": events[-100:],
    }


def _write_deletion_diagnostics(
    runtime: TenantRuntime,
    journal: OperationJournal,
    payload: Mapping[str, object],
) -> Path:
    path = runtime.paths.evidence / f"delete-diagnostics-{journal.operation_id}.json"
    write_private_file(
        path,
        json.dumps(redact_value(dict(payload)), sort_keys=True) + "\n",
    )
    return path


def _remove_private_tree(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if (
        path.is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError(f"private runtime directory is unsafe: {path}")
    for child in path.iterdir():
        details = child.lstat()
        if stat.S_ISDIR(details.st_mode):
            _remove_private_tree(child)
        elif (
            stat.S_ISREG(details.st_mode)
            and details.st_uid == os.getuid()
            and not details.st_mode & 0o077
        ):
            child.unlink()
        else:
            raise RuntimeError(f"private runtime artifact is unsafe: {child}")
    path.rmdir()


def _tenant_tagged_azure_resources(
    root: Path,
    config: Mapping[str, str],
    tenant: str,
) -> list[dict[str, object]]:
    del root
    resources = _json(
        [
            "az",
            "resource",
            "list",
            "--resource-group",
            names(config)["resourceGroup"],
            "--output",
            "json",
        ]
    )
    if not isinstance(resources, list):
        raise RuntimeError("Azure tenant absence discovery returned invalid resources")
    residues = []
    for item in resources:
        if not isinstance(item, dict):
            raise RuntimeError("Azure tenant absence discovery returned invalid resources")
        tags = item.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        if tags.get("cnpg-vcluster-tenant") != tenant:
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError("Azure tenant residue has no resource ID")
        residues.append(
            {
                "id": identifier,
                "type": str(item.get("type", "")).lower(),
            }
        )
    return sorted(residues, key=lambda item: str(item["id"]))


def _operation_failed(runtime: TenantRuntime, journal: OperationJournal) -> bool:
    path = runtime.paths.evidence / f"{journal.operation}-{journal.operation_id}.json"
    if not private_file_exists(path):
        return False
    payload = json.loads(read_private_file(path).decode())
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise RuntimeError("Azure tenant timing evidence is invalid")
    return any(
        isinstance(record, dict) and record.get("status") == "failed"
        for record in records
    )


class AzureTenantAdapter:
    def __init__(
        self,
        *,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self._delete_snapshots: dict[str, dict[str, object]] = {}

    @staticmethod
    def _config(root: Path) -> dict[str, str]:
        return load_azure_configuration(root)

    def foundation_identity(
        self,
        root: Path,
        spec: TenantSpec,
    ) -> Mapping[str, str]:
        config = self._config(root)
        if spec.kubernetes_version != config[
            "AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"
        ].removeprefix("v"):
            raise RuntimeError("unsupported Azure tenant Kubernetes version")
        _validate_networks(
            config,
            spec,
            recorded_specs=_recorded_azure_specs(root, excluding=spec.name),
        )
        identity, _, _ = _inspect_foundation(root, config, require_healthy=True)
        _sku_available(config, config["AZURE_TENANT_NODE_SKU"])
        _reference_image_available(config, spec.kubernetes_version)
        return identity

    @staticmethod
    def intended_resources(spec: TenantSpec) -> Sequence[str]:
        selected = tenant_names(spec)
        return (
            f"Namespace/{spec.namespace}",
            f"AzureClusterIdentity/{spec.namespace}/{selected['azureClusterIdentity']}",
            f"Cluster/{spec.namespace}/{selected['cluster']}",
            f"AzureCluster/{spec.namespace}/{selected['azureCluster']}",
            f"KamajiControlPlane/{spec.namespace}/{selected['controlPlane']}",
            f"KubeadmConfig/{spec.namespace}/{selected['pool']}",
            f"AzureMachinePool/{spec.namespace}/{selected['pool']}",
            f"MachinePool/{spec.namespace}/{selected['pool']}",
            f"VirtualMachineScaleSet/{selected['pool']}",
            f"ConfigMap/{spec.namespace}/{selected['cloudValues']}",
            f"ConfigMap/{spec.namespace}/{selected['networkValues']}",
            f"Deployment/{spec.namespace}/{selected['statusProbe']}",
            f"Job/{spec.namespace}/{selected['addonJob']}",
            f"Credential/{spec.name}",
        )

    def create(
        self,
        root: Path,
        spec: TenantSpec,
        runtime: TenantRuntime,
        journal: OperationJournal,
        timings,
    ) -> Mapping[str, str]:
        config = self._config(root)
        inventory = load_inventory(root, config)
        current = runtime.load_operation()
        marker_operation_id = current.observed.get(
            "markerOperationId",
            current.operation_id,
        )
        current = runtime.update_operation(
            current,
            phase="markers-recorded",
            observed={"markerOperationId": marker_operation_id},
        )
        with timings.phase("control-plane"):
            manifest = _render_tenant_control_plane(
                root,
                config,
                inventory,
                spec,
                current,
            )
            current = _reconcile_manifest(
                root,
                spec,
                runtime,
                current,
                manifest,
                phase="control-plane-resources",
            )
            _retain_external_control_plane_lb(root, spec, current)
            control_plane = _wait_tenant_endpoint(root, config, spec)
            endpoint = control_plane["spec"]["controlPlaneEndpoint"]
            endpoint_text = f"{endpoint['host']}:{endpoint['port']}"
            write_private_file(
                _tenant_runtime_dir(root, spec.name) / "endpoint.json",
                json.dumps(endpoint, sort_keys=True) + "\n",
            )
            current = runtime.update_operation(
                current,
                phase="control-plane-endpoint",
                observed={"endpoint": endpoint_text},
            )
            current = _capture_tenant_kubeconfig(
                root,
                spec,
                runtime,
                current,
            )
        with timings.phase("workers"):
            manifest = _render_worker_pool(
                root,
                config,
                inventory,
                spec,
                current,
            )
            current = _reconcile_manifest(
                root,
                spec,
                runtime,
                current,
                manifest,
                phase="worker-resources",
            )
            _wait_worker_registered(root, config, spec)
            current = _capture_vmss_identities(
                root,
                config,
                spec,
                runtime,
                current,
            )
        with timings.phase("add-ons"):
            manifest = _render_addon_job(root, config, spec, current)
            current = _reconcile_manifest(
                root,
                spec,
                runtime,
                current,
                manifest,
                phase="addon-resources",
            )
            _wait_addon_job(root, config, spec)
            observations = _wait_ready_observations(root, config, spec)
            component_identities = observations["componentIdentities"]
            if not isinstance(component_identities, dict) or not all(
                isinstance(value, str) and value
                for value in component_identities.values()
            ):
                raise RuntimeError("Azure tenant add-on identities are incomplete")
            current = runtime.update_operation(
                current,
                phase="addons-ready",
                observed={
                    "nodeIdentities": json.dumps(
                        observations["nodes"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    **{
                        f"{key}Uid": value
                        for key, value in component_identities.items()
                    },
                },
            )
            discovery = discover_azure_owned_resources(
                root,
                config,
                spec,
                current,
            )
            current = runtime.update_operation(
                current,
                phase="ready",
                observed={
                    "azureResources": json.dumps(
                        discovery,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                },
            )
        runtime.write_ready_evidence(
            {
                "schema": 1,
                "profile": "azure",
                "tenant": spec.name,
                "specificationSha256": spec.sha256(),
                "foundationIdentity": dict(current.foundation_identity),
                "observed": dict(current.observed),
                "verifiedAt": self.clock(),
                "ready": observations,
            }
        )
        print(f"Azure tenant is Ready: {spec.name}")
        return dict(current.observed)

    def _inspect_absence(
        self,
        root: Path,
        config: Mapping[str, str],
        tenant: str,
        *,
        foundation_healthy: bool,
    ) -> TenantStatus:
        namespace = _get_management_resource(root, None, f"namespace/{tenant}")
        management_residue = (
            [
                _management_object_summary(item)
                for item in _list_namespaced_management_objects(root, tenant)
            ]
            if namespace is not None
            else []
        )
        resources = _tenant_tagged_azure_resources(root, config, tenant)
        tenant_runtime = azure_tenant_runtime_path(root, tenant)
        runtime_residue = (
            sorted(
                str(path.relative_to(tenant_runtime))
                for path in tenant_runtime.rglob("*")
            )
            if tenant_runtime.exists()
            else []
        )
        if (
            namespace is not None
            or management_residue
            or resources
            or runtime_residue
        ):
            return TenantStatus(
                profile="azure",
                tenant=tenant,
                classification="ownership-invalid",
                foundation_healthy=foundation_healthy,
                components={
                    "namespacePresent": namespace is not None,
                    "managementResidue": management_residue,
                    "azureResources": resources,
                    "runtimeResidue": runtime_residue,
                },
                blockers=(
                    "Azure tenant resources exist without an authoritative identity",
                ),
            )
        return TenantStatus(
            profile="azure",
            tenant=tenant,
            classification="absent" if foundation_healthy else "degraded",
            foundation_healthy=foundation_healthy,
            components={"inspected": True},
            blockers=() if foundation_healthy else ("Azure foundation is unhealthy",),
        )

    def status(self, root: Path, tenant: str) -> TenantStatus:
        config = self._config(root)
        runtime = TenantRuntime(root, "azure", tenant)
        identity = runtime.load_identity() if runtime.identity_exists() else None
        operation = runtime.load_operation() if runtime.operation_exists() else None
        try:
            foundation, healthy, foundation_blockers = _inspect_foundation(
                root,
                config,
                require_healthy=False,
            )
        except BaseException as exc:
            if identity is None and operation is None:
                return TenantStatus(
                    profile="azure",
                    tenant=tenant,
                    classification="degraded",
                    foundation_healthy=False,
                    blockers=(str(exc),),
                )
            raise
        if identity is None and operation is None:
            try:
                return self._inspect_absence(
                    root,
                    config,
                    tenant,
                    foundation_healthy=healthy,
                )
            except BaseException as exc:
                return TenantStatus(
                    profile="azure",
                    tenant=tenant,
                    classification="ownership-invalid",
                    foundation_healthy=healthy,
                    blockers=(str(exc),),
                )
        binding = (
            identity.foundation_identity
            if identity is not None
            else operation.foundation_identity
        )
        if dict(binding) != foundation:
            raise RuntimeError("Azure tenant foundation binding changed")
        if operation is not None:
            classification = (
                "failed"
                if _operation_failed(runtime, operation)
                else ("deleting" if operation.operation == "delete" else "progressing")
            )
            return TenantStatus(
                profile="azure",
                tenant=tenant,
                classification=classification,
                foundation_healthy=healthy,
                components={
                    "operation": operation.operation,
                    "phase": operation.phase,
                },
                blockers=(
                    ("tenant lifecycle operation failed",)
                    if classification == "failed"
                    else foundation_blockers
                ),
            )
        assert identity is not None
        spec = identity.specification
        selected = tenant_names(spec)
        expected_markers = {
            "tenant": spec.name,
            "profile": "azure",
            "specificationSha256": spec.sha256(),
            "foundationSha256": foundation_sha256(identity.foundation_identity),
            "operationId": identity.observed.get("markerOperationId", ""),
        }
        resources = (
            ("namespaceUid", None, f"namespace/{spec.namespace}"),
            (
                "azureClusterIdentityUid",
                spec.namespace,
                f"azureclusteridentity/{selected['azureClusterIdentity']}",
            ),
            ("clusterUid", spec.namespace, f"cluster/{selected['cluster']}"),
            (
                "azureClusterUid",
                spec.namespace,
                f"azurecluster/{selected['azureCluster']}",
            ),
            (
                "kamajiControlPlaneUid",
                spec.namespace,
                f"kamajicontrolplane/{selected['controlPlane']}",
            ),
            (
                "kubeadmConfigUid",
                spec.namespace,
                f"kubeadmconfig/{selected['pool']}",
            ),
            (
                "azureMachinePoolUid",
                spec.namespace,
                f"azuremachinepool/{selected['pool']}",
            ),
            (
                "machinePoolUid",
                spec.namespace,
                f"machinepool/{selected['pool']}",
            ),
            (
                "cloudValuesConfigMapUid",
                spec.namespace,
                f"configmap/{selected['cloudValues']}",
            ),
            (
                "networkValuesConfigMapUid",
                spec.namespace,
                f"configmap/{selected['networkValues']}",
            ),
            (
                "addonJobUid",
                spec.namespace,
                f"job/{selected['addonJob']}",
            ),
            (
                "statusProbeDeploymentUid",
                spec.namespace,
                f"deployment/{selected['statusProbe']}",
            ),
        )
        blockers = list(foundation_blockers)
        observed_payloads: dict[str, dict[str, object]] = {}
        for key, namespace, resource in resources:
            payload = _get_management_resource(root, namespace, resource)
            if payload is None:
                blockers.append(f"tenant resource is absent: {resource}")
                continue
            observed_payloads[key] = payload
            _require_markers(payload, expected_markers, resource)
            if payload.get("metadata", {}).get("uid") != identity.observed.get(key):
                blockers.append(f"tenant resource identity changed: {resource}")
        blockers.extend(
            _tenant_spec_blockers(spec, selected, observed_payloads, config)
        )
        secret = _get_management_resource(
            root,
            spec.namespace,
            f"secret/{spec.name}-kubeconfig",
        )
        if (
            secret is None
            or secret.get("metadata", {}).get("uid")
            != identity.observed.get("tenantKubeconfigSecretUid")
        ):
            blockers.append("tenant kubeconfig Secret identity changed")
        elif isinstance(secret.get("data", {}).get("value"), str):
            try:
                secret_content = base64.b64decode(
                    secret["data"]["value"],
                    validate=True,
                )
            except ValueError:
                blockers.append("tenant kubeconfig Secret content is invalid")
            else:
                if hashlib.sha256(secret_content).hexdigest() != identity.observed.get(
                    "tenantKubeconfigSha256"
                ):
                    blockers.append("tenant kubeconfig Secret content changed")
        control_plane_payload = observed_payloads.get("kamajiControlPlaneUid", {})
        endpoint = control_plane_payload.get("spec", {}).get("controlPlaneEndpoint", {})
        if (
            not isinstance(endpoint, dict)
            or f"{endpoint.get('host')}:{endpoint.get('port')}"
            != identity.observed.get("endpoint")
        ):
            blockers.append("tenant control-plane endpoint identity changed")
        kubeconfig = _tenant_kubeconfig(root, tenant)
        if hashlib.sha256(read_private_file(kubeconfig)).hexdigest() != identity.observed.get(
            "tenantKubeconfigSha256"
        ):
            blockers.append("tenant kubeconfig identity changed")
        observations, ready_blockers = _collect_ready_observations(root, config, spec)
        blockers.extend(ready_blockers)
        if json.dumps(
            observations["nodes"],
            sort_keys=True,
            separators=(",", ":"),
        ) != identity.observed.get("nodeIdentities"):
            blockers.append("tenant Node identities changed")
        component_identities = observations["componentIdentities"]
        if isinstance(component_identities, dict):
            for key, value in component_identities.items():
                if value != identity.observed.get(f"{key}Uid"):
                    blockers.append(f"tenant add-on identity changed: {key}")
        discovery = discover_azure_owned_resources(root, config, spec, identity)
        if json.dumps(discovery, sort_keys=True, separators=(",", ":")) != identity.observed.get(
            "azureResources"
        ):
            blockers.append("Azure tenant owned-resource inventory changed")
        evidence = runtime.load_ready_evidence()
        if (
            evidence.get("profile") != "azure"
            or evidence.get("tenant") != tenant
            or evidence.get("specificationSha256") != spec.sha256()
            or evidence.get("foundationIdentity") != dict(identity.foundation_identity)
            or evidence.get("observed") != dict(identity.observed)
            or evidence.get("ready") != observations
            or not isinstance(evidence.get("verifiedAt"), (int, float))
            or self.clock() < evidence["verifiedAt"]
            or self.clock() - evidence["verifiedAt"] > READY_EVIDENCE_MAX_AGE_SECONDS
        ):
            blockers.append("Azure Ready evidence does not match current identities")
        return TenantStatus(
            profile="azure",
            tenant=tenant,
            classification="ready" if healthy and not blockers else "degraded",
            foundation_healthy=healthy,
            components={
                "requestedWorkers": spec.workers,
                "readyReplicas": observations["readyReplicas"],
                "nodes": observations["nodes"],
                "controlPlaneAvailable": observations["controlPlaneAvailable"],
                "cloudControllerReady": observations["cloudController"],
                "cloudNodeReady": observations["cloudNode"],
                "tenantNetworkReady": (
                    observations["calicoNode"]
                    and observations["calicoControllers"]
                ),
            },
            blockers=tuple(blockers),
        )

    def authoritative_absence(self, root: Path, tenant: str) -> TenantStatus:
        config = self._config(root)
        _, healthy, _ = _inspect_foundation(root, config, require_healthy=False)
        return self._inspect_absence(
            root,
            config,
            tenant,
            foundation_healthy=healthy,
        )

    def validate_delete(
        self,
        root: Path,
        spec: TenantSpec,
        identity: TenantIdentity,
    ) -> None:
        if (
            identity.profile != "azure"
            or identity.tenant != spec.name
            or identity.specification_sha256 != spec.sha256()
            or identity.specification.to_mapping() != spec.to_mapping()
        ):
            raise RuntimeError("Azure tenant deletion specification binding changed")
        config = self._config(root)
        _active_subscription(config)
        foundation, healthy, blockers = _inspect_foundation(
            root,
            config,
            require_healthy=False,
        )
        if not healthy:
            raise RuntimeError(
                "Azure management foundation is unhealthy: " + "; ".join(blockers)
            )
        if foundation != dict(identity.foundation_identity):
            raise RuntimeError("Azure tenant foundation binding changed")
        runtime = TenantRuntime(root, "azure", spec.name)
        pending = runtime.load_operation() if runtime.operation_exists() else None
        if pending is not None and (
            pending.operation != "delete"
            or pending.specification_sha256 != spec.sha256()
            or dict(pending.foundation_identity) != foundation
        ):
            raise RuntimeError("conflicting Azure tenant lifecycle operation exists")
        require_complete = pending is None
        recorded_management = []
        recorded_azure = []
        if pending is not None:
            for key in (
                "deleteManagementBefore",
                "deleteManagementDiscovered",
            ):
                discovery = _journal_discovery(pending, key)
                if discovery is not None:
                    recorded_management.append(discovery)
            for key in (
                "deleteAzureBefore",
                "deleteAzureDiscovered",
                "deleteAzureFinalDiscovery",
            ):
                discovery = _journal_discovery(pending, key)
                if discovery is not None:
                    recorded_azure.append(discovery)
        management_history = _merge_management_discoveries(recorded_management)
        management_current = discover_management_owned_resources(
            root,
            spec,
            identity,
            require_complete=require_complete,
            verified_uids=tuple(
                str(item["uid"])
                for category in (
                    "controller",
                    "orchestration",
                    "namespaceChildren",
                )
                for item in management_history[category]
                if isinstance(item, dict)
                and isinstance(item.get("uid"), str)
                and item["uid"]
            ),
        )
        management = _merge_management_discoveries(
            (management_history, management_current)
        )
        if require_complete:
            kubeconfig_secret = next(
                (
                    item
                    for item in management["controller"]
                    if item["kind"] == "Secret"
                    and item["name"] == f"{spec.name}-kubeconfig"
                ),
                None,
            )
            if (
                kubeconfig_secret is None
                or kubeconfig_secret["uid"]
                != identity.observed.get("tenantKubeconfigSecretUid")
            ):
                raise RuntimeError("Azure tenant kubeconfig Secret identity changed")
        azure_history = _merge_owned_discoveries(recorded_azure)
        azure_current = _discover_owned_repeatedly(
            root,
            config,
            spec,
            identity,
            passes=2,
            require_parents=require_complete,
            require_azure_resources=require_complete,
            verified_resource_ids=tuple(
                str(item["id"])
                for item in azure_history["azure"]
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
            ),
        )
        azure = _merge_owned_discoveries((azure_history, azure_current))
        if require_complete:
            _require_recorded_resources_present(
                _recorded_owned_resources(identity),
                azure,
            )
        self._delete_snapshots[spec.name] = {
            "foundation": foundation,
            "foundationHealth": {
                "healthy": healthy,
                "blockers": list(blockers),
            },
            "management": management,
            "azure": azure,
        }

    def _wait_for_controller_cleanup(
        self,
        root: Path,
        config: Mapping[str, str],
        spec: TenantSpec,
        identity: TenantIdentity,
        initial_management: Mapping[str, object],
        initial_azure: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        deadline = self.monotonic() + parse_duration(
            config["AZURE_TENANT_TIMEOUT"]
        )
        accumulated = _merge_owned_discoveries((initial_azure,))
        management_history = _merge_management_discoveries(
            (initial_management,)
        )
        last_management: dict[str, object] = {}
        while True:
            try:
                last_management = discover_management_owned_resources(
                    root,
                    spec,
                    identity,
                    require_complete=False,
                    verified_uids=tuple(
                        str(item["uid"])
                        for category in (
                            "controller",
                            "orchestration",
                            "namespaceChildren",
                        )
                        for item in management_history[category]
                        if isinstance(item, dict)
                        and isinstance(item.get("uid"), str)
                        and item["uid"]
                    ),
                )
                management_history = _merge_management_discoveries(
                    (management_history, last_management)
                )
                current = _discover_owned_repeatedly(
                    root,
                    config,
                    spec,
                    identity,
                    passes=2,
                    require_parents=False,
                    require_azure_resources=False,
                    verified_resource_ids=tuple(
                        str(item["id"])
                        for item in accumulated["azure"]
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                    ),
                )
            except BaseException as exc:
                raise AzureDeletionError(
                    "Azure tenant controller cleanup discovery failed: "
                    + str(exc),
                    management=management_history,
                    azure=accumulated,
                ) from exc
            accumulated = _merge_owned_discoveries((accumulated, current))
            controller = last_management["controller"]
            azure_remaining = current["azure"]
            aso_remaining = current["aso"]
            if not controller and not azure_remaining and not aso_remaining:
                return management_history, accumulated
            if self.monotonic() >= deadline:
                blockers = {
                    "management": [
                        f"{item['kind']}/{item['name']}"
                        for item in controller
                    ],
                    "azureResourceIds": [
                        item["id"] for item in azure_remaining
                    ],
                    "aso": [
                        f"{item['kind']}/{item['name']}"
                        for item in aso_remaining
                    ],
                }
                raise AzureDeletionError(
                    "Azure tenant controller cleanup timed out: "
                    + json.dumps(blockers, sort_keys=True),
                    management=management_history,
                    azure=accumulated,
                )
            self.sleep(min(10, max(0, deadline - self.monotonic())))

    def _wait_for_worker_cleanup(
        self,
        root: Path,
        config: Mapping[str, str],
        spec: TenantSpec,
        identity: TenantIdentity,
    ) -> None:
        deadline = self.monotonic() + parse_duration(
            config["AZURE_TENANT_TIMEOUT"]
        )
        selected = tenant_names(spec)
        inventory = load_inventory(root, config)
        outputs = inventory.get("outputs")
        if not isinstance(outputs, dict):
            raise RuntimeError("Azure foundation inventory outputs are invalid")
        resource_group = outputs.get("resourceGroupName")
        vmss_id = identity.observed.get("vmssId")
        if (
            not isinstance(resource_group, str)
            or not resource_group
            or not isinstance(vmss_id, str)
            or not vmss_id
        ):
            raise RuntimeError("Azure tenant VMSS identity is absent")
        resources = (
            (
                f"machinepool/{selected['pool']}",
                "machinePoolUid",
            ),
            (
                f"azuremachinepool/{selected['pool']}",
                "azureMachinePoolUid",
            ),
        )
        while True:
            remaining = []
            for resource, uid_key in resources:
                payload = _get_management_resource(
                    root,
                    spec.namespace,
                    resource,
                )
                if payload is None:
                    continue
                _require_markers(
                    payload,
                    _expected_tenant_markers(spec, identity),
                    resource,
                )
                metadata = payload.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                if metadata.get("uid") != identity.observed.get(uid_key):
                    raise RuntimeError(
                        f"Azure tenant management UID changed: {resource}"
                    )
                remaining.append(resource)
            remaining.extend(
                f"machine/{machine['metadata']['name']}"
                for machine in _owned_tenant_machines(root, spec, identity)
            )
            vmss_response = _az(
                "vmss",
                "list",
                "--resource-group",
                resource_group,
                "--query",
                "[].id",
                "--output",
                "json",
                check=False,
            )
            if vmss_response.returncode != 0:
                raise RuntimeError("Azure tenant VMSS absence check failed")
            try:
                vmss_ids = json.loads(vmss_response.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "Azure tenant VMSS absence check returned invalid data"
                ) from exc
            if not isinstance(vmss_ids, list) or not all(
                isinstance(item, str) for item in vmss_ids
            ):
                raise RuntimeError(
                    "Azure tenant VMSS absence check returned invalid data"
                )
            if any(_azure_id_equal(item, vmss_id) for item in vmss_ids):
                remaining.append(f"vmss/{selected['pool']}")
            if not remaining:
                return
            if self.monotonic() >= deadline:
                raise RuntimeError(
                    "Azure tenant worker cleanup timed out: "
                    + json.dumps(remaining, sort_keys=True)
                )
            self.sleep(min(10, max(0, deadline - self.monotonic())))

    def _wait_for_tenant_absence(
        self,
        root: Path,
        config: Mapping[str, str],
        spec: TenantSpec,
        identity: TenantIdentity,
        initial_azure: Mapping[str, object],
    ) -> dict[str, object]:
        deadline = self.monotonic() + parse_duration(
            config["AZURE_TENANT_TIMEOUT"]
        )
        accumulated = _merge_owned_discoveries((initial_azure,))
        while True:
            namespace = _get_management_resource(
                root,
                None,
                f"namespace/{spec.namespace}",
            )
            current = _discover_owned_repeatedly(
                root,
                config,
                spec,
                identity,
                passes=2,
                require_parents=False,
                require_azure_resources=False,
                verified_resource_ids=tuple(
                    str(item["id"])
                    for item in accumulated["azure"]
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                ),
            )
            accumulated = _merge_owned_discoveries((accumulated, current))
            if (
                namespace is None
                and not current["azure"]
                and not current["aso"]
            ):
                return accumulated
            if self.monotonic() >= deadline:
                raise RuntimeError(
                    "Azure tenant final absence timed out: "
                    + json.dumps(
                        {
                            "namespacePresent": namespace is not None,
                            "azureResourceIds": [
                                item["id"] for item in current["azure"]
                            ],
                            "aso": [
                                f"{item['kind']}/{item['name']}"
                                for item in current["aso"]
                            ],
                        },
                        sort_keys=True,
                    )
                )
            self.sleep(min(10, max(0, deadline - self.monotonic())))

    def delete(
        self,
        root: Path,
        spec: TenantSpec,
        identity: TenantIdentity,
        runtime: TenantRuntime,
        journal: OperationJournal,
        timings,
    ) -> None:
        config = self._config(root)
        snapshot = self._delete_snapshots.pop(spec.name, None)
        if snapshot is None:
            self.validate_delete(root, spec, identity)
            snapshot = self._delete_snapshots.pop(spec.name)
        current = runtime.load_operation()
        management = snapshot["management"]
        azure = snapshot["azure"]
        foundation_before = snapshot["foundation"]
        foundation_health_before = snapshot.get(
            "foundationHealth",
            {"healthy": True, "blockers": []},
        )
        try:
            initial_records = {
                "deleteFoundationBefore": json.dumps(
                    foundation_before,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "deleteFoundationHealthBefore": json.dumps(
                    foundation_health_before,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "deleteManagementBefore": json.dumps(
                    management,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "deleteAzureBefore": json.dumps(
                    azure,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            current = runtime.update_operation(
                current,
                phase="delete-inventory-recorded",
                observed={
                    key: value
                    for key, value in initial_records.items()
                    if key not in current.observed
                },
            )
            with timings.phase("worker-deletion"):
                _exclude_tenant_machines_from_drain(
                    root,
                    spec,
                    identity,
                )
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=spec.namespace,
                    resource=f"machinepool/{tenant_names(spec)['pool']}",
                    uid_key="machinePoolUid",
                    cascade="foreground",
                )
                self._wait_for_worker_cleanup(
                    root,
                    config,
                    spec,
                    identity,
                )
                current = runtime.update_operation(
                    current,
                    phase="worker-resources-absent",
                )
            with timings.phase("deletion"):
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=spec.namespace,
                    resource=f"cluster/{spec.name}",
                    uid_key="clusterUid",
                    cascade="foreground",
                )
                _enable_capz_external_control_plane_delete(
                    root,
                    spec,
                    identity,
                )
                current = runtime.update_operation(
                    current,
                    phase="cluster-deletion-requested",
                )
            with timings.phase("controller-cleanup"):
                management, discovered = self._wait_for_controller_cleanup(
                    root,
                    config,
                    spec,
                    identity,
                    management,
                    azure,
                )
                azure = discovered
                current = runtime.update_operation(
                    current,
                    phase="controller-resources-absent",
                    observed={
                        "deleteManagementDiscovered": json.dumps(
                            management,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteManagementDiscovered" not in current.observed
                        else current.observed["deleteManagementDiscovered"],
                        "deleteAzureDiscovered": json.dumps(
                            discovered,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteAzureDiscovered" not in current.observed
                        else current.observed["deleteAzureDiscovered"]
                    },
                )
            selected = tenant_names(spec)
            with timings.phase("orchestration-cleanup"):
                for uid_key, resource in (
                    (
                        "cloudValuesConfigMapUid",
                        f"configmap/{selected['cloudValues']}",
                    ),
                    (
                        "networkValuesConfigMapUid",
                        f"configmap/{selected['networkValues']}",
                    ),
                    ("addonJobUid", f"job/{selected['addonJob']}"),
                    (
                        "azureClusterIdentityUid",
                        f"azureclusteridentity/{selected['azureClusterIdentity']}",
                    ),
                ):
                    _exact_delete_management_resource(
                        root,
                        spec,
                        identity,
                        namespace=spec.namespace,
                        resource=resource,
                        uid_key=uid_key,
                        cascade="foreground",
                    )
                _exact_delete_management_resource(
                    root,
                    spec,
                    identity,
                    namespace=None,
                    resource=f"namespace/{spec.namespace}",
                    uid_key="namespaceUid",
                    cascade="foreground",
                )
                current = runtime.update_operation(
                    current,
                    phase="orchestration-deletion-requested",
                )
            with timings.phase("azure-absence"):
                azure = self._wait_for_tenant_absence(
                    root,
                    config,
                    spec,
                    identity,
                    azure,
                )
                current = runtime.update_operation(
                    current,
                    phase="tenant-resources-absent",
                    observed={
                        "deleteAzureFinalDiscovery": json.dumps(
                            azure,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteAzureFinalDiscovery" not in current.observed
                        else current.observed["deleteAzureFinalDiscovery"]
                    },
                )
            with timings.phase("foundation-verification"):
                foundation_after, healthy, blockers = _inspect_foundation(
                    root,
                    config,
                    require_healthy=False,
                )
                if not healthy:
                    raise RuntimeError(
                        "Azure management foundation changed during tenant deletion: "
                        + "; ".join(blockers)
                    )
                if foundation_after != foundation_before:
                    raise RuntimeError(
                        "Azure management foundation identity changed during "
                        "tenant deletion"
                    )
                current = runtime.update_operation(
                    current,
                    phase="foundation-verified",
                    observed={
                        "deleteFoundationHealthAfter": json.dumps(
                            {"healthy": healthy, "blockers": list(blockers)},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if "deleteFoundationHealthAfter" not in current.observed
                        else current.observed["deleteFoundationHealthAfter"]
                    },
                )
            with timings.phase("runtime-cleanup"):
                _remove_private_tree(azure_tenant_runtime_path(root, spec.name))
                status = self._inspect_absence(
                    root,
                    config,
                    spec.name,
                    foundation_healthy=True,
                )
                if status.classification != "absent":
                    raise RuntimeError(
                        "Azure tenant did not reach canonical absence: "
                        + "; ".join(status.blockers)
                    )
                runtime.update_operation(current, phase="canonical-absence")
        except BaseException as exc:
            if isinstance(exc, AzureDeletionError):
                management = exc.management
                azure = exc.azure
            try:
                _write_deletion_diagnostics(
                    runtime,
                    runtime.load_operation(),
                    _deletion_diagnostics(
                        root,
                        spec,
                        identity,
                        management=management
                        if isinstance(management, Mapping)
                        else None,
                        azure=azure if isinstance(azure, Mapping) else None,
                        error=exc,
                    ),
                )
            except BaseException as diagnostics_error:
                exc.add_note(
                    "Azure deletion diagnostics failed: "
                    + redact(str(diagnostics_error))
                )
            raise


def destroy(root: Path, config: Mapping[str, str]) -> None:
    _active_subscription(config)
    inventory = load_inventory(root, config)
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    resource_group_id = outputs["resourceGroupId"]
    observed = _az(
        "group",
        "show",
        "--name",
        str(outputs["resourceGroupName"]),
        "--query",
        "id",
        "-o",
        "tsv",
        check=False,
    )
    if observed.returncode == 0 and observed.stdout.strip() != resource_group_id:
        raise RuntimeError("Azure resource group identity changed")
    if observed.returncode == 0:
        _az(
            "group",
            "delete",
            "--name",
            str(outputs["resourceGroupName"]),
            "--yes",
            "--no-wait",
            timeout=120,
        )
        print("Azure foundation resource group deletion started")


def _run_profile_mutation(root: Path, config: Mapping[str, str], mutation) -> None:
    with e2e_lock(root, exclusive=False):
        with profile_lock(
            root,
            "azure",
            exclusive=True,
            create=True,
        ) as acquired:
            if not acquired:
                raise RuntimeError("Azure profile mutation lock is unavailable")
            with tools_lock(root, exclusive=True):
                mutation(root, config)


def _run_profile_status(root: Path, config: Mapping[str, str]) -> int:
    if not profile_lock_exists(root, "azure"):
        return foundation_status(root, config)
    with e2e_lock(root, exclusive=False, create=False) as e2e_acquired:
        if not e2e_acquired:
            raise RuntimeError("Azure E2E status lock is missing")
        with profile_lock(
            root,
            "azure",
            exclusive=False,
            create=False,
        ) as acquired:
            if not acquired:
                raise RuntimeError("Azure profile status lock disappeared")
            with tools_lock(root, exclusive=False, create=False) as tools_acquired:
                if not tools_acquired:
                    raise RuntimeError("Azure tools status lock is missing")
                return foundation_status(root, config)


def main(arguments: list[str]) -> int:
    os.umask(0o077)
    config = load_azure_configuration(ROOT)
    if not arguments:
        raise RuntimeError(
            "usage: azure.py "
            "<preflight|create-foundation|create-management|foundation-status|destroy>"
        )
    command = arguments[0]
    if command == "preflight":
        preflight(ROOT, config)
    elif command == "foundation-status":
        return _run_profile_status(ROOT, config)
    else:
        mutations = {
            "create-foundation": create_foundation,
            "create-management": create_management,
            "destroy": destroy,
        }
        try:
            mutation = mutations[command]
        except KeyError as exc:
            raise RuntimeError(f"unknown Azure command: {command}") from exc
        _run_profile_mutation(ROOT, config, mutation)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ConfigError, RuntimeError, subprocess.SubprocessError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
