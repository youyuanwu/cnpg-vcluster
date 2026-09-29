from __future__ import annotations

import base64
import uuid

from .common import *
from scripts.lib.controller import (
    build_azure_controller_image,
    render_azure_controller_manager,
)

ACR_PULL_ROLE_DEFINITION_ID = (
    "/providers/Microsoft.Authorization/roleDefinitions/"
    "7f951dda-4ed3-4680-a7ca-43fe172d538d"
)
TENANT_CONTROLLER_CONFIG = "tenant-azure-provider"
TENANT_CONTROLLER_CONFIG_KEY = "provider.json"
CAPI_CAPZ_DEPLOYMENTS = (
    ("capi-system", "capi-controller-manager"),
    (
        "capi-kubeadm-bootstrap-system",
        "capi-kubeadm-bootstrap-controller-manager",
    ),
    (
        "capi-kubeadm-control-plane-system",
        "capi-kubeadm-control-plane-controller-manager",
    ),
    ("capz-system", "capz-controller-manager"),
    ("capz-system", "azureserviceoperator-controller-manager"),
)

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
    existing = [
        _get_management_resource(root, namespace, f"deployment/{deployment}")
        for namespace, deployment in CAPI_CAPZ_DEPLOYMENTS
    ]
    if any(value is not None for value in existing) and not all(
        value is not None for value in existing
    ):
        raise RuntimeError("Azure CAPI/CAPZ management installation is incomplete")
    environment = {
        **os.environ,
        "AZURE_SUBSCRIPTION_ID_B64": base64.b64encode(
            config["AZURE_SUBSCRIPTION_ID"].encode()
        ).decode(),
        "EXP_MACHINE_POOL": "true",
    }
    if not all(value is not None for value in existing):
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


def _push_controller_image(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> str:
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    login_server = str(outputs["acrLoginServer"])
    repository = config["AZURE_CONTROLLER_REPOSITORY"]
    tag = config["AZURE_CONTROLLER_TAG"]
    tagged_image = f"{login_server}/{repository}:{tag}"
    controller_config = load_configuration(root)
    build_azure_controller_image(root, controller_config, tagged_image)
    _az("acr", "login", "--name", str(outputs["acrName"]), timeout=120)
    pushed = run(
        ["docker", "push", tagged_image],
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]),
    )
    local_digests = {
        value.lower()
        for value in re.findall(
            r"\bdigest:\s*(sha256:[0-9a-fA-F]{64})\b",
            f"{pushed.stdout}\n{pushed.stderr}",
        )
    }
    local_digest = (
        next(iter(local_digests)) if len(local_digests) == 1 else None
    )
    resolved_tag = tag
    if local_digest is None:
        resolved_tag = f"publish-{uuid.uuid4().hex}"
        unique_image = f"{login_server}/{repository}:{resolved_tag}"
        run(
            ["docker", "tag", tagged_image, unique_image],
            timeout=120,
        )
        unique_push = run(
            ["docker", "push", unique_image],
            timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]),
        )
        unique_digests = {
            value.lower()
            for value in re.findall(
                r"\bdigest:\s*(sha256:[0-9a-fA-F]{64})\b",
                f"{unique_push.stdout}\n{unique_push.stderr}",
            )
        }
        local_digest = (
            next(iter(unique_digests)) if len(unique_digests) == 1 else None
        )
    registry_digest = _az(
        "acr",
        "manifest",
        "show-metadata",
        "--registry",
        str(outputs["acrName"]),
        "--name",
        f"{repository}:{resolved_tag}",
        "--query",
        "digest",
        "--output",
        "tsv",
        timeout=120,
    ).stdout.strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", registry_digest):
        raise RuntimeError("pushed Azure controller manifest digest is invalid")
    if local_digest is not None and registry_digest != local_digest:
        raise RuntimeError(
            "pushed Azure controller manifest digest changed before deployment"
        )
    return f"{login_server}/{repository}@{registry_digest}"


def _azure_provider_configuration(
    config: Mapping[str, str],
    inventory: Mapping[str, object],
    controller_image: str | None = None,
) -> dict[str, object]:
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    image = controller_image or inventory.get("controllerImage")
    if not isinstance(image, str) or not image:
        raise RuntimeError("Azure controller image identity is absent")
    provider = {
        "schema": 1,
        "subscriptionId": config["AZURE_SUBSCRIPTION_ID"],
        "tenantId": outputs["tenantId"],
        "location": config["AZURE_LOCATION"],
        "resourceGroupName": outputs["resourceGroupName"],
        "resourceGroupId": outputs["resourceGroupId"],
        "vnetName": outputs["vnetName"],
        "vnetId": outputs["vnetId"],
        "tenantSubnetName": outputs["tenantSubnetName"],
        "tenantSubnetId": outputs["tenantSubnetId"],
        "identityName": outputs["identityName"],
        "identityId": outputs["identityId"],
        "identityClientId": outputs["identityClientId"],
        "supportedKubernetesVersion": config[
            "AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"
        ],
        "workerSku": config["AZURE_TENANT_NODE_SKU"],
        "capiVersion": config["AZURE_CAPI_VERSION"],
        "capzVersion": config["AZURE_CAPZ_VERSION"],
        "kamajiCapiVersion": config["AZURE_KAMAJI_CAPI_VERSION"],
        "kamajiChartVersion": config["AZURE_KAMAJI_CHART_VERSION"],
        "asoVersion": "v2.11.0",
        "cloudProviderVersion": config["AZURE_CLOUD_PROVIDER_VERSION"],
        "calicoVersion": config["AZURE_CALICO_VERSION"],
        "controllerImage": image,
        "foundationDefaultsSha256": inventory["foundationDefaultsSha256"],
    }
    provider["foundationSha256"] = hashlib.sha256(
        json.dumps(
            provider,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return provider


def _install_tenant_controller(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> tuple[str, str]:
    image = _push_controller_image(root, config, inventory)
    for path in (
        root / "controller" / "config" / "namespace" / "namespace.yaml",
        root / "controller" / "config" / "crd" / "bases",
        root / "controller" / "config" / "rbac" / "role-azure.yaml",
        root / "controller" / "config" / "rbac" / "service-account.yaml",
        root / "controller" / "config" / "rbac" / "role-binding.yaml",
    ):
        _kubectl(
            root,
            "apply",
            "--server-side",
            "--field-manager=cnpg-vcluster-azure-controller",
            "--force-conflicts",
            "-f",
            str(path),
        )
    _kubectl(
        root,
        "wait",
        "--for=condition=Established",
        "crd/tenants.tenancy.cnpg-vcluster.io",
        f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]) + 60,
    )
    provider_config = _azure_provider_configuration(config, inventory, image)
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": TENANT_CONTROLLER_CONFIG,
            "namespace": "tenant-system",
        },
        "data": {
            TENANT_CONTROLLER_CONFIG_KEY: json.dumps(
                provider_config,
                sort_keys=True,
                separators=(",", ":"),
            )
        },
    }
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-azure-controller",
        "--force-conflicts",
        "-f",
        "-",
        input_text=json.dumps(config_map),
    )
    manager = render_azure_controller_manager(
        root,
        config["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
        image,
    )
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-azure-controller",
        "--force-conflicts",
        "-f",
        str(manager),
    )
    _kubectl(
        root,
        "-n",
        "tenant-system",
        "rollout",
        "status",
        "deployment/tenant-controller",
        f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]) + 60,
    )
    observed = _get_management_resource(
        root,
        "tenant-system",
        f"configmap/{TENANT_CONTROLLER_CONFIG}",
    )
    if observed is None:
        raise RuntimeError("Azure provider configuration ConfigMap is absent")
    uid = observed.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise RuntimeError("Azure provider configuration ConfigMap UID is absent")
    return image, uid


def create_management(root: Path, config: Mapping[str, str]) -> None:
    preflight(root, config, emit=False)
    inventory = load_inventory(root, config)
    _kubectl(root, "get", "--raw=/readyz")
    _install_capi_capz(root, config, inventory)
    _install_kamaji(root, config)
    _install_kamaji_provider(root, config)
    for namespace, deployment in PLATFORM_CONTROLLER_DEPLOYMENTS:
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
    controller_image, provider_config_uid = _install_tenant_controller(
        root,
        config,
        inventory,
    )
    updated = dict(inventory)
    updated["controllers"] = _controller_identities(root)
    updated["controllerImage"] = controller_image
    updated["azureProviderConfigUid"] = provider_config_uid
    _write_inventory(root, updated)
    print("Azure management controllers and Tenant manager are ready")


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
    for key in ("controllerImage", "azureProviderConfigUid"):
        if key in payload and (
            not isinstance(payload[key], str) or not payload[key]
        ):
            raise RuntimeError(f"Azure foundation {key} is invalid")
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
        "aksKubeletPrincipalId",
        "acrName",
        "acrId",
        "acrLoginServer",
        "acrPullRoleAssignmentId",
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
    controller_image = inventory.get("controllerImage")
    provider_config_uid = inventory.get("azureProviderConfigUid")
    if (
        not isinstance(controller_image, str)
        or not re.fullmatch(
            r"[^@\s]+@sha256:[0-9a-f]{64}",
            controller_image,
        )
        or not controller_image.startswith(f"{outputs['acrLoginServer']}/")
        or not isinstance(provider_config_uid, str)
        or not provider_config_uid
    ):
        raise RuntimeError("Azure Tenant controller inventory is incomplete")
    return {
        "foundationDefaultsSha256": str(inventory["foundationDefaultsSha256"]),
        "controllerImage": controller_image,
        "azureProviderConfigUid": provider_config_uid,
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
    expected_controller_prefix = (
        f"{outputs['acrLoginServer']}/"
        f"{config['AZURE_CONTROLLER_REPOSITORY']}@sha256:"
    )
    if not str(inventory.get("controllerImage", "")).startswith(
        expected_controller_prefix
    ):
        blockers.append("Azure Tenant manager repository binding changed")
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
    acr = _az(
        "acr",
        "show",
        "--name",
        str(outputs["acrName"]),
        "--query",
        (
            "{id:id,name:name,loginServer:loginServer,"
            "adminUserEnabled:adminUserEnabled,"
            "provisioningState:provisioningState}"
        ),
        "--output",
        "json",
        check=False,
    )
    if acr.returncode != 0:
        blockers.append("recorded Azure container registry is absent")
    else:
        payload = json.loads(acr.stdout)
        if not _azure_id_equal(payload.get("id"), outputs["acrId"]):
            blockers.append("Azure container registry identity changed")
        if payload.get("name") != outputs["acrName"]:
            blockers.append("Azure container registry name changed")
        if payload.get("loginServer") != outputs["acrLoginServer"]:
            blockers.append("Azure container registry login server changed")
        if payload.get("adminUserEnabled") is not False:
            blockers.append("Azure container registry admin user is enabled")
        if payload.get("provisioningState") != "Succeeded":
            blockers.append("Azure container registry provisioning is not Succeeded")
    acr_roles = _az(
        "role",
        "assignment",
        "list",
        "--scope",
        str(outputs["acrId"]),
        "--assignee-object-id",
        str(outputs["aksKubeletPrincipalId"]),
        "--output",
        "json",
        check=False,
    )
    if acr_roles.returncode != 0:
        blockers.append("AKS kubelet ACR role assignment inspection failed")
    else:
        assignments = json.loads(acr_roles.stdout)
        matching = [
            assignment
            for assignment in assignments
            if isinstance(assignment, dict)
            and _azure_id_equal(
                assignment.get("id"),
                outputs["acrPullRoleAssignmentId"],
            )
        ] if isinstance(assignments, list) else []
        if len(matching) != 1:
            blockers.append("recorded AKS kubelet AcrPull role assignment is absent")
        else:
            assignment = matching[0]
            if assignment.get("principalId") != outputs["aksKubeletPrincipalId"]:
                blockers.append("AKS kubelet AcrPull principal changed")
            if not _azure_id_equal(assignment.get("scope"), outputs["acrId"]):
                blockers.append("AKS kubelet AcrPull scope changed")
            if not str(assignment.get("roleDefinitionId", "")).lower().endswith(
                ACR_PULL_ROLE_DEFINITION_ID.lower()
            ):
                blockers.append("AKS kubelet AcrPull role changed")
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
            "container registry",
            ("resource", "show", "--ids", str(outputs["acrId"])),
            outputs["acrId"],
        ),
        (
            "AKS kubelet AcrPull role assignment",
            (
                "resource",
                "show",
                "--ids",
                str(outputs["acrPullRoleAssignmentId"]),
            ),
            outputs["acrPullRoleAssignmentId"],
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
            if (namespace, deployment) == ("tenant-system", "tenant-controller"):
                containers = observed.get("spec", {}).get(
                    "template", {}
                ).get("spec", {}).get("containers", [])
                manager = next(
                    (
                        item
                        for item in containers
                        if isinstance(item, dict) and item.get("name") == "manager"
                    ),
                    None,
                )
                if not isinstance(manager, dict):
                    blockers.append("Azure Tenant manager container is absent")
                else:
                    if manager.get("image") != inventory.get("controllerImage"):
                        blockers.append("Azure Tenant manager image identity changed")
                    arguments = manager.get("args")
                    if (
                        not isinstance(arguments, list)
                        or "--provider=azure" not in arguments
                    ):
                        blockers.append("Azure Tenant manager provider mode changed")
        provider_config = _get_management_resource(
            root,
            "tenant-system",
            f"configmap/{TENANT_CONTROLLER_CONFIG}",
        )
        if provider_config is None:
            blockers.append("Azure provider configuration ConfigMap is absent")
        else:
            if (
                provider_config.get("metadata", {}).get("uid")
                != inventory.get("azureProviderConfigUid")
            ):
                blockers.append("Azure provider configuration identity changed")
            raw = provider_config.get("data", {}).get(
                TENANT_CONTROLLER_CONFIG_KEY
            )
            try:
                observed_config = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                observed_config = None
            if observed_config != _azure_provider_configuration(config, inventory):
                blockers.append("Azure provider configuration changed")
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
