#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.request
import base64
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lib.config import (
    ConfigError,
    load_configuration,
    load_env_file,
    parse_duration,
    require,
)
from scripts.lib.files import ensure_private_dir, write_private_file
from scripts.lib.management import _prepare_kamaji_chart
from scripts.lib.process import run


PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
REQUIRED_PROVIDERS = (
    "Microsoft.Authorization",
    "Microsoft.Compute",
    "Microsoft.ContainerService",
    "Microsoft.ManagedIdentity",
    "Microsoft.Network",
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
    require(
        config,
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_LOCATION",
        "AZURE_PREFIX",
        "AZURE_AKS_KUBERNETES_VERSION",
        "AZURE_TENANT_KUBERNETES_VERSION",
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
    )
    prefix = config["AZURE_PREFIX"]
    if not PREFIX_RE.fullmatch(prefix):
        raise ConfigError(
            "AZURE_PREFIX must start with a lowercase letter and contain "
            "2-20 lowercase letters, digits, or hyphens"
        )
    return config


def names(config: dict[str, str]) -> dict[str, str]:
    prefix = config["AZURE_PREFIX"]
    return {
        "deployment": f"{prefix}-foundation",
        "resourceGroup": f"{prefix}-rg",
        "aks": f"{prefix}-mgmt",
        "vnet": f"{prefix}-vnet",
        "identity": f"{prefix}-identity",
        "tenant": f"{prefix}-tenant",
    }


def _az(*arguments: str, timeout: int = 300, check: bool = True):
    return run(["az", *arguments], timeout=timeout, check=check)


def _json(command: list[str], timeout: int = 300) -> object:
    result = run(command, timeout=timeout)
    return json.loads(result.stdout)


def _validate_networks(config: dict[str, str]) -> None:
    networks = {
        key: ipaddress.ip_network(config[key])
        for key in (
            "AZURE_VNET_CIDR",
            "AZURE_AKS_SUBNET_CIDR",
            "AZURE_TENANT_SUBNET_CIDR",
            "AZURE_AKS_POD_CIDR",
            "AZURE_AKS_SERVICE_CIDR",
            "AZURE_TENANT_POD_CIDR",
            "AZURE_TENANT_SERVICE_CIDR",
        )
    }
    vnet = networks["AZURE_VNET_CIDR"]
    for key in ("AZURE_AKS_SUBNET_CIDR", "AZURE_TENANT_SUBNET_CIDR"):
        if not networks[key].subnet_of(vnet):
            raise ConfigError(f"{key} must be contained by AZURE_VNET_CIDR")
    items = list(networks.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1 :]:
            if left_name == "AZURE_VNET_CIDR" or right_name == "AZURE_VNET_CIDR":
                continue
            if left.overlaps(right):
                raise ConfigError(f"Azure network ranges overlap: {left_name}, {right_name}")
    dns = ipaddress.ip_address(config["AZURE_AKS_DNS_SERVICE_IP"])
    if dns not in networks["AZURE_AKS_SERVICE_CIDR"]:
        raise ConfigError("AZURE_AKS_DNS_SERVICE_IP is outside the AKS service CIDR")


def _active_subscription(config: dict[str, str]) -> dict[str, object]:
    account = _json(["az", "account", "show", "--output", "json"])
    if account.get("id") != config["AZURE_SUBSCRIPTION_ID"]:
        raise RuntimeError("active Azure subscription does not match azure.local.env")
    if account.get("state") != "Enabled":
        raise RuntimeError("configured Azure subscription is not enabled")
    return account


def _sku_available(config: dict[str, str], sku: str) -> None:
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
    location_blocks = [
        restriction
        for restriction in matching[0].get("restrictions", [])
        if restriction.get("type") == "Location"
    ]
    if location_blocks:
        raise RuntimeError(f"Azure VM SKU is blocked in the region: {sku}")


def _reference_image_available(config: dict[str, str]) -> None:
    version = config["AZURE_TENANT_KUBERNETES_VERSION"]
    result = _az(
        "sig",
        "image-version",
        "show-community",
        "--public-gallery-name",
        "ClusterAPI-f72ceb4f-5159-4c26-a0fe-2ea738f0d019",
        "--gallery-image-definition",
        "capi-ubun2-2404",
        "--gallery-image-version",
        version,
        "--location",
        config["AZURE_LOCATION"],
        "--output",
        "none",
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"CAPZ reference image {version} is unavailable in "
            f"{config['AZURE_LOCATION']}"
        )


def _defaults_checksum(root: Path) -> str:
    return hashlib.sha256(
        (root / "config" / "azure" / "defaults.env").read_bytes()
    ).hexdigest()


def preflight(
    root: Path,
    config: dict[str, str],
    *,
    emit: bool = True,
) -> dict[str, object]:
    _validate_networks(config)
    account = _active_subscription(config)
    for provider in REQUIRED_PROVIDERS:
        state = _az("provider", "show", "--namespace", provider, "--query", "registrationState", "-o", "tsv").stdout.strip()
        if state != "Registered":
            raise RuntimeError(f"Azure resource provider is not registered: {provider}")
    _sku_available(config, config["AZURE_AKS_NODE_SKU"])
    _sku_available(config, config["AZURE_TENANT_NODE_SKU"])
    _reference_image_available(config)
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
        "subscriptionId": account["id"],
        "location": config["AZURE_LOCATION"],
        "prefix": config["AZURE_PREFIX"],
        "names": names(config),
        "defaultsSha256": _defaults_checksum(root),
    }
    if emit:
        print(json.dumps(result, sort_keys=True))
    return result


def _runtime_dir(root: Path) -> Path:
    path = root / ".runtime" / "azure"
    ensure_private_dir(path)
    return path


def _management_kubeconfig(root: Path) -> Path:
    path = root / ".runtime" / "azure" / "management.kubeconfig"
    details = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("Azure management kubeconfig must be owner-only")
    return path


def _kubectl(root: Path, *arguments: str, timeout: int = 300, check: bool = True):
    return run(
        [
            str(root / ".tools" / "bin" / "kubectl"),
            "--kubeconfig",
            str(_management_kubeconfig(root)),
            *arguments,
        ],
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


def _deployment_parameters(config: dict[str, str]) -> list[str]:
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


def create_foundation(root: Path, config: dict[str, str]) -> dict[str, object]:
    expected = preflight(root, config)
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
        "schema": 1,
        **expected,
        "deploymentId": payload["id"],
        "deploymentName": deployment,
        "outputs": outputs,
    }
    write_private_file(
        _runtime_dir(root) / "resources.json",
        json.dumps(record, sort_keys=True) + "\n",
    )
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
    config: dict[str, str],
    inventory: dict[str, object],
) -> None:
    outputs = inventory["outputs"]
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
    _kubectl(
        root,
        "-n",
        "capz-system",
        "rollout",
        "restart",
        "deployment/azureserviceoperator-controller-manager",
    )
    _kubectl(
        root,
        "-n",
        "capz-system",
        "rollout",
        "restart",
        "deployment/capz-controller-manager",
    )


def _install_capi_capz(
    root: Path,
    config: dict[str, str],
    inventory: dict[str, object],
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


def _install_kamaji(root: Path, config: dict[str, str]) -> None:
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


def _install_kamaji_provider(root: Path, config: dict[str, str]) -> None:
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


def create_management(root: Path, config: dict[str, str]) -> None:
    preflight(root, config)
    inventory = load_inventory(root, config)
    _kubectl(root, "get", "--raw=/readyz")
    _install_capi_capz(root, config, inventory)
    _install_kamaji(root, config)
    _install_kamaji_provider(root, config)
    checks = (
        ("capi-system", "capi-controller-manager"),
        ("capi-kubeadm-bootstrap-system", "capi-kubeadm-bootstrap-controller-manager"),
        ("capz-system", "capz-controller-manager"),
        ("kamaji-system", "kamaji"),
        ("kamaji-system", "capi-kamaji-controller-manager"),
    )
    for namespace, deployment in checks:
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
    print("Azure management controllers are ready")


def _render_tenant_control_plane(
    root: Path,
    config: dict[str, str],
    inventory: dict[str, object],
) -> Path:
    tenant = names(config)["tenant"]
    outputs = inventory["outputs"]
    content = f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
kind: AzureClusterIdentity
metadata:
  name: {outputs["identityName"]}
  namespace: {tenant}
spec:
  type: WorkloadIdentity
  tenantID: {outputs["tenantId"]}
  clientID: {outputs["identityClientId"]}
  allowedNamespaces:
    list:
      - {tenant}
---
apiVersion: cluster.x-k8s.io/v1beta1
kind: Cluster
metadata:
  name: {tenant}
  namespace: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
spec:
  clusterNetwork:
    apiServerPort: 6443
    pods:
      cidrBlocks:
        - {config["AZURE_TENANT_POD_CIDR"]}
    services:
      cidrBlocks:
        - {config["AZURE_TENANT_SERVICE_CIDR"]}
    serviceDomain: cluster.local
  controlPlaneRef:
    apiVersion: controlplane.cluster.x-k8s.io/v1alpha1
    kind: KamajiControlPlane
    name: {tenant}
  infrastructureRef:
    apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
    kind: AzureCluster
    name: {tenant}
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
kind: AzureCluster
metadata:
  name: {tenant}
  namespace: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
spec:
  subscriptionID: {config["AZURE_SUBSCRIPTION_ID"]}
  location: {config["AZURE_LOCATION"]}
  resourceGroup: {outputs["resourceGroupName"]}
  controlPlaneEnabled: false
  identityRef:
    apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
    kind: AzureClusterIdentity
    name: {outputs["identityName"]}
  networkSpec:
    apiServerLB:
      type: Public
    vnet:
      name: {outputs["vnetName"]}
      resourceGroup: {outputs["resourceGroupName"]}
    subnets:
      - name: {outputs["tenantSubnetName"]}
        role: node
  additionalTags:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
---
apiVersion: controlplane.cluster.x-k8s.io/v1alpha1
kind: KamajiControlPlane
metadata:
  name: {tenant}
  namespace: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
spec:
  version: {config["AZURE_TENANT_KUBERNETES_VERSION"]}
  replicas: 1
  dataStoreName: default
  controllerManager:
    extraArgs:
      - --cloud-provider=external
      - --cluster-name={tenant}
      - --allocate-node-cidrs=false
  network:
    serviceType: LoadBalancer
    serviceAnnotations:
      service.beta.kubernetes.io/azure-load-balancer-internal: "true"
    certSANs: []
    dnsServiceIPs:
      - {config["AZURE_TENANT_DNS_SERVICE_IP"]}
  addons:
    coreDNS:
      dnsServiceIPs:
        - {config["AZURE_TENANT_DNS_SERVICE_IP"]}
    kubeProxy: {{}}
    konnectivity:
      server:
        port: 8132
      agent:
        mode: DaemonSet
        hostNetwork: true
        tolerations:
          - key: node.kubernetes.io/not-ready
            operator: Exists
            effect: NoSchedule
          - key: node.kubernetes.io/not-ready
            operator: Exists
            effect: NoExecute
          - key: node.cloudprovider.kubernetes.io/uninitialized
            operator: Exists
            effect: NoSchedule
"""
    path = _runtime_dir(root) / "tenant-control-plane.yaml"
    write_private_file(path, content)
    return path


def _wait_tenant_endpoint(
    root: Path,
    config: dict[str, str],
) -> dict[str, object]:
    tenant = names(config)["tenant"]
    deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    patched_endpoint: dict[str, object] | None = None
    while time.monotonic() < deadline:
        infrastructure = _kubectl(
            root,
            "-n",
            tenant,
            "get",
            f"azurecluster/{tenant}",
            "-o",
            "json",
            check=False,
        )
        if infrastructure.returncode == 0:
            azure_cluster = json.loads(infrastructure.stdout)
            ready = any(
                condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in azure_cluster.get("status", {}).get("conditions", [])
            )
            if ready:
                legacy_cluster = _json(
                    [
                        str(root / ".tools" / "bin" / "kubectl"),
                        "--kubeconfig",
                        str(_management_kubeconfig(root)),
                        "get",
                        "--raw",
                        (
                            "/apis/cluster.x-k8s.io/v1beta1/namespaces/"
                            f"{tenant}/clusters/{tenant}"
                        ),
                    ]
                )
                if legacy_cluster.get("status", {}).get("infrastructureReady") is not True:
                    _kubectl(
                        root,
                        "-n",
                        tenant,
                        "patch",
                        f"clusters.v1beta1.cluster.x-k8s.io/{tenant}",
                        "--subresource=status",
                        "--type=merge",
                        "-p",
                        '{"status":{"infrastructureReady":true}}',
                    )
        response = _kubectl(
            root,
            "-n",
            tenant,
            "get",
            f"kamajicontrolplane/{tenant}",
            "-o",
            "json",
            check=False,
        )
        if response.returncode == 0:
            payload = json.loads(response.stdout)
            endpoint = payload.get("spec", {}).get("controlPlaneEndpoint", {})
            if endpoint.get("host") and endpoint.get("port") and endpoint != patched_endpoint:
                _kubectl(
                    root,
                    "-n",
                    tenant,
                    "patch",
                    f"cluster/{tenant}",
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
                and endpoint.get("host")
                and endpoint.get("port")
            ):
                return payload
        time.sleep(5)
    raise RuntimeError("Kamaji tenant control plane did not become ready")


def create_tenant_control_plane(root: Path, config: dict[str, str]) -> None:
    preflight(root, config)
    inventory = load_inventory(root, config)
    manifest = _render_tenant_control_plane(root, config, inventory)
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-azure",
        "-f",
        str(manifest),
        timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"]),
    )
    payload = _wait_tenant_endpoint(root, config)
    endpoint = payload["spec"]["controlPlaneEndpoint"]
    write_private_file(
        _runtime_dir(root) / "tenant-endpoint.json",
        json.dumps(endpoint, sort_keys=True) + "\n",
    )
    print(f"Kamaji tenant control plane is ready: {endpoint['host']}:{endpoint['port']}")


def _render_worker_pool(
    root: Path,
    config: dict[str, str],
    inventory: dict[str, object],
) -> Path:
    tenant = names(config)["tenant"]
    pool = f"{tenant}-worker"
    outputs = inventory["outputs"]
    identity_provider_id = (
        "azure:///subscriptions/"
        f"{config['AZURE_SUBSCRIPTION_ID']}/resourceGroups/"
        f"{outputs['resourceGroupName']}/providers/Microsoft.ManagedIdentity/"
        f"userAssignedIdentities/{outputs['identityName']}"
    )
    content = f"""\
apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
kind: KubeadmConfig
metadata:
  name: {pool}
  namespace: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
spec:
  files:
    - contentFrom:
        secret:
          name: {pool}-azure-json
          key: worker-node-azure.json
      owner: root:root
      path: /etc/kubernetes/azure.json
      permissions: "0644"
  joinConfiguration:
    nodeRegistration:
      name: '{{{{ ds.meta_data["local_hostname"] }}}}'
      kubeletExtraArgs:
        cloud-provider: external
        feature-gates: KubeletCrashLoopBackOffMax=true
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
kind: AzureMachinePool
metadata:
  name: {pool}
  namespace: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
spec:
  location: {config["AZURE_LOCATION"]}
  orchestrationMode: Uniform
  platformFaultDomainCount: 1
  identity: UserAssigned
  userAssignedIdentities:
    - providerID: {identity_provider_id}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
      deletePolicy: Oldest
  template:
    vmSize: {config["AZURE_TENANT_NODE_SKU"]}
    networkInterfaces:
      - subnetName: {outputs["tenantSubnetName"]}
    osDisk:
      diskSizeGB: 30
      osType: Linux
      managedDisk:
        storageAccountType: StandardSSD_LRS
    image:
      computeGallery:
        gallery: ClusterAPI-f72ceb4f-5159-4c26-a0fe-2ea738f0d019
        name: capi-ubun2-2404
        version: {config["AZURE_TENANT_KUBERNETES_VERSION"]}
    sshPublicKey: ""
  additionalTags:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
---
apiVersion: cluster.x-k8s.io/v1beta1
kind: MachinePool
metadata:
  name: {pool}
  namespace: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
spec:
  clusterName: {tenant}
  replicas: {config["AZURE_TENANT_NODE_COUNT"]}
  template:
    metadata:
      labels:
        cnpg-vcluster-experiment: azure-capi
        cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
    spec:
      clusterName: {tenant}
      version: v{config["AZURE_TENANT_KUBERNETES_VERSION"]}
      bootstrap:
        configRef:
          apiVersion: bootstrap.cluster.x-k8s.io/v1beta1
          kind: KubeadmConfig
          name: {pool}
      infrastructureRef:
        apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
        kind: AzureMachinePool
        name: {pool}
"""
    path = _runtime_dir(root) / "tenant-worker.yaml"
    write_private_file(path, content)
    return path


def _wait_worker_registered(root: Path, config: dict[str, str]) -> dict[str, object]:
    tenant = names(config)["tenant"]
    pool = f"{tenant}-worker"
    deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    while time.monotonic() < deadline:
        response = _kubectl(
            root,
            "-n",
            tenant,
            "get",
            f"machinepool/{pool}",
            "-o",
            "json",
            check=False,
        )
        if response.returncode == 0:
            payload = json.loads(response.stdout)
            instances = _json(
                [
                    "az",
                    "vmss",
                    "list-instances",
                    "--resource-group",
                    names(config)["resourceGroup"],
                    "--name",
                    pool,
                    "--query",
                    "[].instanceId",
                    "--output",
                    "json",
                ]
            )
            if len(instances) == int(config["AZURE_TENANT_NODE_COUNT"]):
                joined = True
                for instance in instances:
                    result = _az(
                        "vmss",
                        "run-command",
                        "invoke",
                        "--resource-group",
                        names(config)["resourceGroup"],
                        "--name",
                        pool,
                        "--instance-id",
                        str(instance),
                        "--command-id",
                        "RunShellScript",
                        "--scripts",
                        (
                            "sudo grep -q 'This node has joined the cluster' "
                            "/var/log/cloud-init-output.log && echo joined"
                        ),
                        "--query",
                        "value[0].message",
                        "--output",
                        "tsv",
                        timeout=180,
                        check=False,
                    )
                    joined = joined and result.returncode == 0 and "joined" in result.stdout
                if joined:
                    return payload
        time.sleep(10)
    raise RuntimeError("Azure VMSS worker did not register with the tenant API")


def create_worker(root: Path, config: dict[str, str]) -> None:
    preflight(root, config)
    inventory = load_inventory(root, config)
    manifest = _render_worker_pool(root, config, inventory)
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-azure",
        "-f",
        str(manifest),
        timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"]),
    )
    pool = _wait_worker_registered(root, config)
    node_refs = [
        item["name"] for item in pool.get("status", {}).get("nodeRefs", [])
    ]
    detail = ", ".join(node_refs) if node_refs else f"{names(config)['tenant']}-worker VMSS"
    print(f"Azure VMSS worker registered: {detail}")


def _render_addon_job(root: Path, config: dict[str, str]) -> Path:
    tenant = names(config)["tenant"]
    cloud_values = f"""\
infra:
  clusterName: {tenant}
cloudControllerManager:
  allocateNodeCidrs: "false"
  clusterCIDR: {config["AZURE_TENANT_POD_CIDR"]}
  configureCloudRoutes: "false"
  nodeSelector: null
  replicas: 1
  tolerations:
    - operator: Exists
cloudNodeManager:
  cloudConfig: /etc/kubernetes/azure.json
"""
    calico_values = f"""\
installation:
  cni:
    type: Calico
    ipam:
      type: Calico
  calicoNetwork:
    bgp: Disabled
    mtu: 1350
    ipPools:
      - cidr: {config["AZURE_TENANT_POD_CIDR"]}
        encapsulation: VXLAN
  serviceCIDRs:
    - {config["AZURE_TENANT_SERVICE_CIDR"]}
tolerations:
  - operator: Exists
"""
    cloud_values_block = "\n".join(
        f"    {line}" for line in cloud_values.splitlines()
    )
    calico_values_block = "\n".join(
        f"    {line}" for line in calico_values.splitlines()
    )
    content = f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: azure-cloud-provider-values
  namespace: {tenant}
data:
  values.yaml: |
{cloud_values_block}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: calico-values
  namespace: {tenant}
data:
  values.yaml: |
{calico_values_block}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: install-tenant-addons
  namespace: {tenant}
  labels:
    cnpg-vcluster-experiment: azure-capi
    cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
spec:
  backoffLimit: 1
  template:
    metadata:
      labels:
        cnpg-vcluster-experiment: azure-capi
        cnpg-vcluster-prefix: {config["AZURE_PREFIX"]}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      containers:
        - name: helm
          image: alpine/helm:3.19.0
          command:
            - sh
            - -ec
          args:
            - |
              helm repo add cloud-provider-azure https://raw.githubusercontent.com/kubernetes-sigs/cloud-provider-azure/master/helm/repo
              helm repo add projectcalico https://docs.tigera.io/calico/charts
              helm upgrade --install cloud-provider-azure cloud-provider-azure/cloud-provider-azure \\
                --kubeconfig /tenant/value \\
                --version {config["AZURE_CLOUD_PROVIDER_VERSION"].removeprefix("v")} \\
                --namespace kube-system \\
                --values /values/cloud-provider.yaml \\
                --wait --timeout 10m
              helm upgrade --install calico-crds projectcalico/crd.projectcalico.org.v1 \
                --kubeconfig /tenant/value \
                --version {config["AZURE_CALICO_VERSION"]} \
                --namespace tigera-operator \
                --create-namespace \
                --wait --timeout 5m
              helm upgrade --install calico projectcalico/tigera-operator \\
                --kubeconfig /tenant/value \\
                --version {config["AZURE_CALICO_VERSION"]} \\
                --namespace tigera-operator \\
                --create-namespace \\
                --values /values/calico.yaml \\
                --wait --timeout 10m
          volumeMounts:
            - name: tenant-kubeconfig
              mountPath: /tenant
              readOnly: true
            - name: cloud-values
              mountPath: /values/cloud-provider.yaml
              subPath: values.yaml
              readOnly: true
            - name: calico-values
              mountPath: /values/calico.yaml
              subPath: values.yaml
              readOnly: true
      volumes:
        - name: tenant-kubeconfig
          secret:
            secretName: {tenant}-kubeconfig
        - name: cloud-values
          configMap:
            name: azure-cloud-provider-values
        - name: calico-values
          configMap:
            name: calico-values
"""
    path = _runtime_dir(root) / "tenant-addons.yaml"
    write_private_file(path, content)
    return path


def _wait_worker_ready(root: Path, config: dict[str, str]) -> dict[str, object]:
    tenant = names(config)["tenant"]
    pool = f"{tenant}-worker"
    deadline = time.monotonic() + parse_duration(config["AZURE_TENANT_TIMEOUT"])
    while time.monotonic() < deadline:
        response = _kubectl(
            root,
            "-n",
            tenant,
            "get",
            f"machinepool/{pool}",
            "-o",
            "json",
            check=False,
        )
        if response.returncode == 0:
            payload = json.loads(response.stdout)
            status = payload.get("status", {})
            if (
                status.get("readyReplicas") == int(config["AZURE_TENANT_NODE_COUNT"])
                and len(status.get("nodeRefs") or [])
                == int(config["AZURE_TENANT_NODE_COUNT"])
            ):
                return payload
        time.sleep(10)
    raise RuntimeError("Azure VMSS worker did not become Ready")


def install_addons(root: Path, config: dict[str, str]) -> None:
    preflight(root, config)
    load_inventory(root, config)
    tenant = names(config)["tenant"]
    _kubectl(
        root,
        "-n",
        tenant,
        "delete",
        "job/install-tenant-addons",
        "--ignore-not-found",
        "--wait=true",
        timeout=180,
    )
    manifest = _render_addon_job(root, config)
    _kubectl(root, "apply", "-f", str(manifest))
    result = _kubectl(
        root,
        "-n",
        tenant,
        "wait",
        "--for=condition=Complete",
        "job/install-tenant-addons",
        f"--timeout={config['AZURE_TENANT_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_TENANT_TIMEOUT"]) + 60,
        check=False,
    )
    if result.returncode != 0:
        logs = _kubectl(
            root,
            "-n",
            tenant,
            "logs",
            "job/install-tenant-addons",
            "--tail=200",
            check=False,
        )
        raise RuntimeError(f"tenant add-on installation failed: {logs.stdout}{logs.stderr}")
    pool = _wait_worker_ready(root, config)
    nodes = [item["name"] for item in pool["status"]["nodeRefs"]]
    print(f"Azure VMSS worker is Ready: {', '.join(nodes)}")


def load_inventory(root: Path, config: dict[str, str]) -> dict[str, object]:
    path = root / ".runtime" / "azure" / "resources.json"
    details = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeError("Azure resource inventory must be an owner-only regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "subscriptionId": config["AZURE_SUBSCRIPTION_ID"],
        "location": config["AZURE_LOCATION"],
        "prefix": config["AZURE_PREFIX"],
        "defaultsSha256": _defaults_checksum(root),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RuntimeError(f"Azure resource inventory does not match {key}")
    return payload


def status(root: Path, config: dict[str, str]) -> int:
    _validate_networks(config)
    _active_subscription(config)
    path = root / ".runtime" / "azure" / "resources.json"
    if not path.is_file():
        print(json.dumps({"foundation": "absent"}, sort_keys=True))
        return 0
    inventory = load_inventory(root, config)
    outputs = inventory["outputs"]
    aks = _az(
        "aks",
        "show",
        "--resource-group",
        outputs["resourceGroupName"],
        "--name",
        outputs["aksName"],
        "--query",
        "{provisioningState:provisioningState,powerState:powerState.code,kubernetesVersion:kubernetesVersion}",
        "--output",
        "json",
        check=False,
    )
    result = {
        "foundation": "present" if aks.returncode == 0 else "missing",
        "aks": json.loads(aks.stdout) if aks.returncode == 0 else None,
        "names": inventory["names"],
    }
    if aks.returncode == 0 and (root / ".runtime" / "azure" / "management.kubeconfig").is_file():
        tenant = names(config)["tenant"]
        resources = {}
        for key, resource in (
            ("cluster", f"cluster/{tenant}"),
            ("controlPlane", f"kamajicontrolplane/{tenant}"),
            ("machinePool", f"machinepool/{tenant}-worker"),
            ("azureMachinePool", f"azuremachinepool/{tenant}-worker"),
        ):
            response = _kubectl(
                root,
                "-n",
                tenant,
                "get",
                resource,
                "-o",
                "json",
                check=False,
            )
            resources[key] = json.loads(response.stdout) if response.returncode == 0 else None
        result["tenant"] = resources
    print(json.dumps(result, sort_keys=True))
    return 0 if aks.returncode == 0 else 1


def destroy(root: Path, config: dict[str, str]) -> None:
    _active_subscription(config)
    inventory = load_inventory(root, config)
    resource_group_id = inventory["outputs"]["resourceGroupId"]
    observed = _az(
        "group",
        "show",
        "--name",
        inventory["outputs"]["resourceGroupName"],
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
            inventory["outputs"]["resourceGroupName"],
            "--yes",
            "--no-wait",
            timeout=120,
        )
        print(f"Azure resource group deletion started: {resource_group_id}")


def main(arguments: list[str]) -> int:
    os.umask(0o077)
    config = load_azure_configuration(ROOT)
    if not arguments:
        raise RuntimeError(
            "usage: azure.py "
            "<preflight|create-foundation|create-management|"
            "create-tenant-control-plane|create-worker|install-addons|status|destroy>"
        )
    command = arguments[0]
    if command == "preflight":
        preflight(ROOT, config)
    elif command == "create-foundation":
        create_foundation(ROOT, config)
    elif command == "create-management":
        create_management(ROOT, config)
    elif command == "create-tenant-control-plane":
        create_tenant_control_plane(ROOT, config)
    elif command == "create-worker":
        create_worker(ROOT, config)
    elif command == "install-addons":
        install_addons(ROOT, config)
    elif command == "status":
        return status(ROOT, config)
    elif command == "destroy":
        destroy(ROOT, config)
    else:
        raise RuntimeError(f"unknown Azure command: {command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ConfigError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
