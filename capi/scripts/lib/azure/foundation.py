from __future__ import annotations

import base64
import hashlib
import tempfile
import uuid
from collections.abc import Callable

from .common import *
from scripts.lib.admin import (
    ADMIN_IDENTITY,
    ADMIN_NAMESPACE_LIMIT,
    admin_rbac_resource_paths,
    admin_review_namespaces,
    admin_role_name,
    admin_rules_review_request,
    build_admin_image,
    render_azure_admin_deployment,
    validate_admin_effective_rules,
)
from scripts.lib.controller import (
    CATALOG_LIFECYCLE_READY,
    _catalog_activation_complete,
    _catalog_lock_present,
    build_azure_controller_image,
    catalog_cutover_lock_documents,
    install_database_catalog,
    release_catalog_and_tenant_cutover_locks,
    verify_catalog_cutover_lock,
    require_empty_tenant_cutover,
    require_tenant_api_cutover_ready,
    render_azure_controller_manager,
    tenant_api_generation,
    tenant_api_cutover_state,
    tenant_crd_transition_document,
    tenant_cutover_lock_cleanup_refs,
    tenant_cutover_lock_documents,
)
from scripts.lib.database_controller import (
    CATALOG_CRD, build_database_controller_image, install_database_controller,
    install_azure_database_runtime, require_absent_legacy_database_crd,
)

ACR_PULL_ROLE_DEFINITION_ID = (
    "/providers/Microsoft.Authorization/roleDefinitions/"
    "7f951dda-4ed3-4680-a7ca-43fe172d538d"
)
TENANT_CONTROLLER_CONFIG = "tenant-azure-provider"
TENANT_CONTROLLER_CONFIG_KEY = "provider.json"
TENANT_ALLOCATION_CONFIG = "tenant-azure-allocation"
TENANT_ALLOCATION_CONFIG_KEY = "slots.json"
TENANT_ALLOCATION_APPROVAL = (
    "tenancy.cnpg-vcluster.io/approved-allocation-sha256"
)
ADMIN_NAME = "tenant-admin"
ADMIN_NAMESPACE = "tenant-system"
ADMIN_SERVICE_PROXY = (
    "/api/v1/namespaces/tenant-system/"
    "services/http:tenant-admin:http/proxy"
)
ADMIN_API_SCHEMA_VERSION = 4
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
DATABASE_OUTPUTS = (
    "databaseIdentityId",
    "databaseIdentityClientId",
    "databaseIdentityPrincipalId",
    "databaseDiskRoleId",
    "databaseDiskAssignmentId",
    "databaseFederationId",
)
DATABASE_DISK_ACTIONS = {
    "Microsoft.Compute/disks/read",
    "Microsoft.Compute/disks/delete",
}
DATABASE_SERVICE_ACCOUNT = "system:serviceaccount:tenant-system:database-controller"


def install_tenant_database_runtime(
    root: Path, config: Mapping[str, str], tenant_name: str,
) -> bool:
    validate_tenant_name(tenant_name)

    def current() -> tuple[dict, bytes]:
        tenant = json.loads(
            _kubectl(root, "get", f"tenant/{tenant_name}", "-o", "json").stdout
        )
        metadata = tenant.get("metadata", {})
        status = tenant.get("status", {})
        provider = status.get("provider", {})
        management = provider.get("management", {})
        bound = provider.get("kubeconfig", {})
        if (
            metadata.get("name") != tenant_name
            or not metadata.get("uid")
            or metadata.get("deletionTimestamp")
            or status.get("phase") != "Ready"
            or provider.get("type") != "azure"
            or provider.get("binding", {}).get("tenantUID") != metadata["uid"]
            or not management.get("namespaceUID")
            or not (management.get("clusterUID") or management.get("kamajiControlPlaneUID"))
            or not bound.get("secretUID")
            or not bound.get("contentSha256")
        ):
            raise RuntimeError(f"Azure Tenant {tenant_name} is not ready for database install")
        namespace = json.loads(
            _kubectl(root, "get", f"namespace/{tenant_name}", "-o", "json").stdout
        )
        if namespace.get("metadata", {}).get("uid") != management.get("namespaceUID"):
            raise RuntimeError("Azure database installer namespace identity changed")
        secret = json.loads(
            _kubectl(
                root, "-n", tenant_name, "get",
                f"secret/{tenant_name}-kubeconfig", "-o", "json",
            ).stdout
        )
        secret_meta = secret.get("metadata", {})
        expected_owners = {
            ("cluster.x-k8s.io/v1beta1", "Cluster", tenant_name,
             management.get("clusterUID")),
            ("controlplane.cluster.x-k8s.io/v1alpha1", "KamajiControlPlane",
             tenant_name, management.get("kamajiControlPlaneUID")),
        }
        expected_owners = {owner for owner in expected_owners if owner[3]}
        owners = secret_meta.get("ownerReferences", [])
        if (
            secret_meta.get("namespace") != tenant_name
            or secret_meta.get("uid") != bound["secretUID"]
            or secret.get("type") != "cluster.x-k8s.io/secret"
            or len(owners) != 1
            or owners[0].get("controller") is not True
            or (
                owners[0].get("apiVersion"), owners[0].get("kind"),
                owners[0].get("name"), owners[0].get("uid")
            ) not in expected_owners
        ):
            raise RuntimeError("Azure database installer kubeconfig ownership changed")
        try:
            content = base64.b64decode(secret["data"]["value"], validate=True)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Azure database installer kubeconfig is invalid") from exc
        if not content or hashlib.sha256(content).hexdigest() != bound["contentSha256"]:
            raise RuntimeError("Azure database installer kubeconfig digest changed")
        return metadata, content

    try:
        identity, content = current()
    except RuntimeError as exc:
        if str(exc) == f"Azure Tenant {tenant_name} is not ready for database install":
            return False
        raise

    scratch = root / ".runtime" / "azure-database-installer"
    ensure_private_dir(scratch)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        kubeconfig = Path(directory) / "tenant.kubeconfig"
        write_private_file(kubeconfig, content)

        def require_current() -> None:
            latest, latest_content = current()
            if latest["uid"] != identity["uid"] or latest_content != content:
                raise RuntimeError("Azure database installer Tenant identity changed")

        install_azure_database_runtime(
            root, dict(config), kubeconfig, require_current=require_current,
        )
    return True


def _validate_database_outputs(outputs: Mapping[str, object], prefix: str) -> None:
    missing = [
        key for key in DATABASE_OUTPUTS
        if not isinstance(outputs.get(key), str) or not outputs[key]
    ]
    if missing:
        raise RuntimeError(
            "Azure database foundation inventory is incomplete; clean foundation "
            "redeploy required: " + ", ".join(missing)
        )
    group = str(outputs.get("resourceGroupId", "")).rstrip("/")
    group_name = f"{prefix}-rg"
    identity = str(outputs["databaseIdentityId"]).rstrip("/")
    expected_identity = (
        f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
        f"{prefix}-database-controller"
    )
    authorization = f"{group}/providers/Microsoft.Authorization"
    if (
        not group
        or outputs.get("resourceGroupName") != group_name
        or re.fullmatch(
            rf"/subscriptions/[^/]+/resourceGroups/{re.escape(group_name)}",
            group,
            flags=re.IGNORECASE,
        ) is None
        or not _azure_id_equal(identity, expected_identity)
        or _azure_id_equal(identity, outputs.get("identityId"))
        or outputs["databaseIdentityClientId"] == outputs.get("identityClientId")
        or outputs["databaseIdentityPrincipalId"] == outputs.get("identityPrincipalId")
        or not str(outputs["databaseDiskRoleId"]).lower().startswith(
            (authorization + "/roleDefinitions/").lower()
        )
        or not str(outputs["databaseDiskAssignmentId"]).lower().startswith(
            (authorization + "/roleAssignments/").lower()
        )
        or not _azure_id_equal(
            outputs["databaseFederationId"],
            identity + "/federatedIdentityCredentials/database-controller",
        )
        or _azure_id_equal(
            outputs["databaseDiskAssignmentId"], outputs.get("roleAssignmentId")
        )
    ):
        raise RuntimeError("Azure database foundation identity or scope mismatch")


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
    _validate_database_outputs(outputs, config["AZURE_PREFIX"])
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


def _push_acr_image(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
    *,
    repository: str,
    tag: str,
    description: str,
    build: Callable[[str], None],
) -> str:
    outputs = inventory["outputs"]
    assert isinstance(outputs, dict)
    login_server = str(outputs["acrLoginServer"])
    tagged_image = f"{login_server}/{repository}:{tag}"
    build(tagged_image)
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
        raise RuntimeError(
            f"pushed Azure {description} manifest digest is invalid"
        )
    if local_digest is not None and registry_digest != local_digest:
        raise RuntimeError(
            f"pushed Azure {description} manifest digest changed before deployment"
        )
    return f"{login_server}/{repository}@{registry_digest}"


def _push_controller_image(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> str:
    controller_config = load_configuration(root)
    return _push_acr_image(
        root,
        config,
        inventory,
        repository=config["AZURE_CONTROLLER_REPOSITORY"],
        tag=config["AZURE_CONTROLLER_TAG"],
        description="controller",
        build=lambda image: build_azure_controller_image(
            root,
            controller_config,
            image,
        ),
    )


def _push_database_controller_image(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> str:
    database_config = load_configuration(root)
    repository = config["AZURE_CONTROLLER_REPOSITORY"] + "-database"
    if len(repository) > 255:
        raise RuntimeError("Azure database controller repository exceeds OCI path limit")
    return _push_acr_image(
        root, config, inventory,
        repository=repository,
        tag=config["AZURE_CONTROLLER_TAG"],
        description="database controller",
        build=lambda image: build_database_controller_image(
            root, database_config, image=image,
        ),
    )


def _push_admin_image(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> str:
    admin_config = load_configuration(root)
    return _push_acr_image(
        root,
        config,
        inventory,
        repository=config["AZURE_ADMIN_REPOSITORY"],
        tag=config["AZURE_ADMIN_TAG"],
        description="admin",
        build=lambda image: build_admin_image(root, admin_config, image),
    )


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


def _azure_allocation_configuration(
    root: Path,
    config: Mapping[str, str],
) -> tuple[dict[str, object], str, str]:
    path = root / "config" / "azure" / "tenant-allocation-slots.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Azure Tenant allocation catalog is invalid: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "reservedCIDRs", "slots"}
        or payload.get("schema") != 1
        or not isinstance(payload.get("reservedCIDRs"), list)
        or not isinstance(payload.get("slots"), list)
        or not payload["slots"]
    ):
        raise RuntimeError("Azure Tenant allocation catalog has an invalid schema")
    ids = set()
    networks: list[tuple[str, ipaddress.IPv4Network]] = []
    for slot in payload["slots"]:
        if (
            not isinstance(slot, dict)
            or set(slot) != {"slotId", "podCIDR", "serviceCIDR"}
            or not isinstance(slot.get("slotId"), str)
            or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", slot["slotId"])
            is None
            or slot["slotId"] in ids
        ):
            raise RuntimeError("Azure Tenant allocation slot identity is invalid")
        ids.add(slot["slotId"])
        for field in ("podCIDR", "serviceCIDR"):
            try:
                network = ipaddress.ip_network(slot.get(field), strict=True)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Azure Tenant allocation slot {slot['slotId']} {field} is invalid"
                ) from exc
            if not isinstance(network, ipaddress.IPv4Network):
                raise RuntimeError("Azure Tenant allocation networks must be IPv4")
            if field == "serviceCIDR" and network.num_addresses <= 11:
                raise RuntimeError("Azure Tenant Service CIDR is too small")
            networks.append((f"{slot['slotId']} {field}", network))
    management = [
        (key, ipaddress.ip_network(config[key], strict=True))
        for key in (
            "AZURE_VNET_CIDR",
            "AZURE_AKS_SUBNET_CIDR",
            "AZURE_TENANT_SUBNET_CIDR",
            "AZURE_AKS_POD_CIDR",
            "AZURE_AKS_SERVICE_CIDR",
        )
    ]
    if payload["reservedCIDRs"] != [str(network) for _, network in management]:
        raise RuntimeError(
            "Azure Tenant allocation reserved CIDRs do not match management configuration"
        )
    for index, (label, network) in enumerate(networks):
        for other_label, other in networks[index + 1 :]:
            if network.overlaps(other):
                raise RuntimeError(
                    f"Azure Tenant allocation networks overlap: {label} and {other_label}"
                )
        for management_label, management_network in management:
            if network.overlaps(management_network):
                raise RuntimeError(
                    f"Azure Tenant allocation network {label} overlaps {management_label}"
                )
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if digest != config["AZURE_TENANT_ALLOCATION_APPROVED_SHA256"]:
        raise RuntimeError("Azure Tenant allocation catalog approval is missing or stale")
    return payload, raw, digest


def _azure_cutover_lock(root: Path, *, present: bool) -> None:
    if present:
        for document in tenant_cutover_lock_documents():
            _kubectl(
                root,
                "apply",
                "--server-side",
                "--field-manager=cnpg-vcluster-tenant-api-cutover",
                "--force-conflicts",
                "-f",
                "-",
                input_text=json.dumps(document),
            )
        return
    for resource in tenant_cutover_lock_cleanup_refs():
        _kubectl(
            root,
            "delete",
            resource,
            "--ignore-not-found=true",
            "--wait=true",
        )


def _azure_cutover_lock_present(root: Path) -> bool:
    present = [
        bool(_kubectl(
            root, "get", resource, "--ignore-not-found=true", "-o", "name",
        ).stdout.strip())
        for resource in tenant_cutover_lock_cleanup_refs()
    ]
    if present[0] != present[1]:
        raise RuntimeError("Azure Tenant cutover lock is partially installed")
    return present[0]


def _verify_azure_cutover_lock(root: Path, generation: str) -> None:
    document = {
        "apiVersion": f"tenancy.cnpg-vcluster.io/{generation}",
        "kind": "Tenant",
        "metadata": {"name": f"cutover-lock-probe-{uuid.uuid4().hex[:12]}"},
        "spec": {
            "kubernetesVersion": "1.36.4",
            "workers": 1,
            "provider": (
                {"type": "local", "databases": 1}
                if generation == "v1alpha3" else {"type": "local"}
            ),
        },
    }
    for _ in range(5):
        response = _kubectl(
            root, "create", "--dry-run=server", "-f", "-",
            input_text=json.dumps(document), check=False,
        )
        if (
            response.returncode == 0
            or "Tenant creation is locked during API cutover"
            not in response.stderr
        ):
            raise RuntimeError("Azure Tenant cutover create lock is not effective")


def _validated_azure_list(
    payload: object,
    api_version: str,
    kind: str,
) -> list[dict[str, object]]:
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    items = payload.get("items") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("apiVersion") != api_version
        or payload.get("kind") != f"{kind}List"
        or not isinstance(metadata, dict)
        or metadata.get("continue", "") != ""
        or not isinstance(items, list)
    ):
        raise RuntimeError(f"Azure cutover {kind} inventory is malformed")
    for item in items:
        item_metadata = item.get("metadata") if isinstance(item, dict) else None
        if (
            item.get("apiVersion") != api_version
            or item.get("kind") != kind
            or not isinstance(item_metadata, dict)
            or not isinstance(item_metadata.get("name"), str)
            or not item_metadata["name"]
            or not isinstance(item_metadata.get("uid"), str)
            or not item_metadata["uid"]
        ):
            raise RuntimeError(f"Azure cutover {kind} inventory is invalid")
    return items


def _azure_cutover_inventory(root: Path) -> tuple[dict[str, object], list[str]]:
    class InventoryClient:
        def kubectl(self, *args, **kwargs):
            return _kubectl(root, *args, **kwargs)

    require_absent_legacy_database_crd(InventoryClient())
    tenants = json.loads(_kubectl(root, "get", "tenants", "-o", "json").stdout)
    tenant_api_version = (
        tenants.get("apiVersion") if isinstance(tenants, dict) else None
    )
    if (
        not isinstance(tenant_api_version, str)
        or not tenant_api_version.startswith("tenancy.cnpg-vcluster.io/")
    ):
        raise RuntimeError("Azure cutover Tenant inventory is malformed")
    _validated_azure_list(tenants, tenant_api_version, "Tenant")
    catalog_crd = _kubectl(
        root, "get", f"crd/{CATALOG_CRD}",
        "--ignore-not-found=true", "-o", "name",
    ).stdout.strip()
    catalogs = []
    if catalog_crd:
        catalogs = _validated_azure_list(
            json.loads(_kubectl(
                root, "get", CATALOG_CRD,
                "--all-namespaces", "-o", "json",
            ).stdout),
            "tenancy.cnpg-vcluster.io/v1alpha1", "TenantDatabaseCatalog",
        )
    catalog = json.loads(
        (root / "controller" / "config" / "azure-management-resources.json").read_text(
            encoding="utf-8"
        )
    )
    resources = {}
    for entry in catalog:
        group = entry["apiVersion"].partition("/")[0] if "/" in entry["apiVersion"] else ""
        resource = f"{entry['plural']}.{group}" if group else entry["plural"]
        resources[resource] = (
            entry["inventoryPolicy"],
            entry["class"],
            entry["kind"],
            entry["apiVersion"],
        )
    resources["leases.coordination.k8s.io"] = (
        "allocation-markers",
        "typed",
        "Lease",
        "coordination.k8s.io/v1",
    )
    items = []
    for resource, policy in sorted(resources.items()):
        payload = json.loads(
            _kubectl(root, "get", resource, "--all-namespaces", "-o", "json").stdout
        )
        inventory_policy, resource_class, kind, api_version = policy
        items.extend(
            (item, inventory_policy, resource_class, kind)
            for item in _validated_azure_list(payload, api_version, kind)
        )
    owned_uids = set()
    owned_namespaces = set()
    shared_core_kinds = {
        "Namespace", "ConfigMap", "Secret", "Deployment", "Job",
        "Role", "RoleBinding", "PodDisruptionBudget",
    }
    for item, policy, resource_class, catalog_kind in items:
        metadata = item.get("metadata", {})
        annotations = metadata.get("annotations") or {}
        labels = metadata.get("labels") or {}
        if (
            annotations.get("lifecycle.cnpg-vcluster.capi/profile") == "azure"
            or annotations.get("lifecycle.cnpg-vcluster.capi/tenant")
            or labels.get("cnpg-vcluster-tenant")
            or str(metadata.get("name", "")).startswith("tenant-azure-slot-")
            or (
                policy == "block-any-instance"
                and resource_class == "root"
                and catalog_kind not in shared_core_kinds
            )
        ):
            if metadata.get("uid"):
                owned_uids.add(metadata["uid"])
            if metadata.get("namespace"):
                owned_namespaces.add(metadata["namespace"])
            if item.get("kind") == "Namespace":
                owned_namespaces.add(metadata.get("name"))
    changed = True
    while changed:
        changed = False
        for item, _, _, _ in items:
            metadata = item.get("metadata", {})
            if metadata.get("uid") in owned_uids:
                continue
            owners = metadata.get("ownerReferences") or []
            if metadata.get("namespace") in owned_namespaces or any(
                owner.get("uid") in owned_uids for owner in owners
            ):
                if metadata.get("uid"):
                    owned_uids.add(metadata["uid"])
                changed = True
    residue = [
        f"{item.get('kind', 'resource')}/{item.get('metadata', {}).get('name', '')}"
        for item, _, _, _ in items
        if item.get("metadata", {}).get("uid") in owned_uids
    ]
    residue.extend(
        f"TenantDatabaseCatalog/{item['metadata']['namespace']}/{item['metadata']['name']}"
        for item in catalogs
    )
    return tenants, residue


def _scale_azure_controller(
    root: Path,
    config: Mapping[str, str],
    replicas: int,
) -> None:
    _kubectl(
        root,
        "-n",
        "tenant-system",
        "scale",
        "deployment/tenant-controller",
        f"--replicas={replicas}",
    )
    if replicas:
        _kubectl(
            root,
            "-n",
            "tenant-system",
            "rollout",
            "status",
            "deployment/tenant-controller",
            f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
        )
    else:
        _kubectl(
            root,
            "-n",
            "tenant-system",
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app.kubernetes.io/name=tenant-controller",
            f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
        )


def _prepare_azure_tenant_api_cutover(
    root: Path,
    config: Mapping[str, str],
) -> bool:
    class CutoverClient:
        @property
        def kubeconfig(self):
            return _management_kubeconfig(root)

        def kubectl(self, *args, **kwargs):
            return _kubectl(root, *args, **kwargs)

        def json(self, *args):
            return json.loads(self.kubectl(*args, "-o", "json").stdout)

    client = CutoverClient()
    require_absent_legacy_database_crd(client)
    observed = _get_management_resource(
        root, None, "crd/tenants.tenancy.cnpg-vcluster.io"
    )
    if observed is not None and tenant_api_cutover_state(observed) == "v1alpha4":
        if (
            not _azure_cutover_lock_present(root)
            and not _catalog_lock_present(client)
            and _catalog_activation_complete(client)
        ):
            return False
    for document in catalog_cutover_lock_documents():
        _kubectl(
            root, "apply", "--server-side",
            "--field-manager=cnpg-vcluster-catalog-cutover",
            "--force-conflicts", "-f", "-", input_text=json.dumps(document),
        )
    if observed is None:
        if not _azure_cutover_lock_present(root):
            _azure_cutover_lock(root, present=True)
        return True
    generation = tenant_api_cutover_state(observed)
    if generation == "v1alpha4":
        if not _azure_cutover_lock_present(root):
            _azure_cutover_lock(root, present=True)
        return True
    rendered = _kubectl(
        root,
        "create",
        "--dry-run=client",
        "-f",
        str(
            root
            / "controller/config/crd/bases"
            / "tenancy.cnpg-vcluster.io_tenants.yaml"
        ),
        "-o",
        "json",
    ).stdout
    try:
        desired = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise RuntimeError("generated Azure Tenant CRD is invalid") from exc
    if not isinstance(desired, dict):
        raise RuntimeError("generated Azure Tenant CRD is invalid")
    transition = generation == "transitioning"
    transition_document = (
        None
        if transition
        else tenant_crd_transition_document(observed, desired)
    )
    if transition:
        if not _azure_cutover_lock_present(root):
            raise RuntimeError("Azure Tenant CRD transition is missing its create lock")
    else:
        if not _azure_cutover_lock_present(root):
            _azure_cutover_lock(root, present=True)
    try:
        _verify_azure_cutover_lock(
            root,
            "v1alpha4" if transition else generation,
        )
        if not transition:
            tenants, residue = _azure_cutover_inventory(root)
            require_empty_tenant_cutover(tenants, residue)
        _scale_azure_controller(root, config, 0)
        tenants, residue = _azure_cutover_inventory(root)
        require_empty_tenant_cutover(tenants, residue)
        if not transition:
            transition = True
            _kubectl(
                root,
                "apply",
                "--server-side",
                "--field-manager=cnpg-vcluster-tenant-api-cutover",
                "--force-conflicts",
                "-f",
                "-",
                input_text=json.dumps(transition_document),
            )
        _verify_azure_cutover_lock(root, "v1alpha4")
        tenants, residue = _azure_cutover_inventory(root)
        require_empty_tenant_cutover(tenants, residue)
        _kubectl(
            root,
            "patch",
            "crd/tenants.tenancy.cnpg-vcluster.io",
            "--subresource=status",
            "--type=merge",
            "-p",
            json.dumps({"status": {"storedVersions": ["v1alpha4"]}}),
        )
        _kubectl(
            root,
            "apply",
            "--server-side",
            "--field-manager=cnpg-vcluster-azure-controller",
            "--force-conflicts",
            "-f",
            "-",
            input_text=json.dumps(desired),
        )
        tenants, residue = _azure_cutover_inventory(root)
        require_empty_tenant_cutover(tenants, residue)
        return True
    except Exception:
        if not transition:
            _scale_azure_controller(root, config, 1)
        raise


def _verify_azure_cutover_probe(
    root: Path,
    config: Mapping[str, str],
) -> None:
    name = f"cutover-probe-{uuid.uuid4().hex[:12]}"
    document = {
        "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha4",
        "kind": "Tenant",
        "metadata": {"name": name},
        "spec": {
            "kubernetesVersion": config[
                "AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"
            ],
            "workers": 1,
            "provider": {"type": "azure"},
        },
    }
    _kubectl(root, "create", "-f", "-", input_text=json.dumps(document))
    deadline = time.monotonic() + parse_duration(config["AZURE_CONTROLLER_TIMEOUT"])
    try:
        while time.monotonic() < deadline:
            tenant = json.loads(_kubectl(root, "get", f"tenant/{name}", "-o", "json").stdout)
            allocation = tenant.get("status", {}).get("provider", {}).get(
                "networkAllocation"
            )
            if isinstance(allocation, dict) and allocation.get("slotId"):
                return
            time.sleep(2)
        raise RuntimeError("Azure Tenant cutover allocation probe timed out")
    finally:
        _kubectl(
            root,
            "delete",
            f"tenant/{name}",
            "--ignore-not-found=true",
            "--wait=true",
            f"--timeout={config['AZURE_TENANT_TIMEOUT']}",
        )


def _verify_azure_controller_allocation_readiness(
    root: Path,
    allocation_raw: str,
    allocation_sha256: str,
) -> None:
    allocation = json.loads(
        _kubectl(
            root,
            "-n",
            "tenant-system",
            "get",
            f"configmap/{TENANT_ALLOCATION_CONFIG}",
            "-o",
            "json",
        ).stdout
    )
    if (
        allocation.get("data", {}).get(TENANT_ALLOCATION_CONFIG_KEY)
        != allocation_raw
        or allocation.get("metadata", {}).get("annotations", {}).get(
            TENANT_ALLOCATION_APPROVAL
        )
        != allocation_sha256
    ):
        raise RuntimeError("Azure Tenant allocation ConfigMap approval is not current")
    for verb in ("create", "delete", "get", "list"):
        allowed = _kubectl(
            root,
            "auth",
            "can-i",
            verb,
            "leases.coordination.k8s.io",
            "--as=system:serviceaccount:tenant-system:tenant-controller",
            "--namespace=tenant-system",
        ).stdout.strip()
        if allowed != "yes":
            raise RuntimeError(
                f"Azure Tenant controller cannot {verb} allocation Leases"
            )
    pods = json.loads(
        _kubectl(
            root,
            "-n",
            "tenant-system",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=tenant-controller",
            "-o",
            "json",
        ).stdout
    ).get("items", [])
    ready = [
        pod
        for pod in pods
        if not pod.get("metadata", {}).get("deletionTimestamp")
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
    ]
    if len(ready) != 1:
        raise RuntimeError("Azure Tenant controller has no exact Ready Pod")
    pod_name = ready[0]["metadata"]["name"]
    _kubectl(
        root,
        "get",
        "--raw",
        f"/api/v1/namespaces/tenant-system/pods/{pod_name}:8081/proxy/readyz",
    )
    lease = json.loads(
        _kubectl(
            root,
            "-n",
            "tenant-system",
            "get",
            "lease/tenant-controller.tenancy.cnpg-vcluster.io",
            "-o",
            "json",
        ).stdout
    )
    spec = lease.get("spec", {})
    if not spec.get("holderIdentity") or not spec.get("renewTime"):
        raise RuntimeError("Azure Tenant controller leader Lease is not active")


def _install_tenant_controller(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> tuple[str, str, str]:
    require_tenant_api_cutover_ready()
    image = _push_controller_image(root, config, inventory)
    database_image = _push_database_controller_image(root, config, inventory)
    provider_config = _azure_provider_configuration(config, inventory, image)
    _, allocation_raw, allocation_sha256 = _azure_allocation_configuration(root, config)
    cutover_locked = _prepare_azure_tenant_api_cutover(root, config)
    for path in (
        root / "controller" / "config" / "namespace" / "namespace.yaml",
        root / "controller" / "config" / "crd" / "bases",
        root / "controller" / "config" / "rbac" / "role-azure.yaml",
        root / "controller" / "config" / "rbac" / "service-account.yaml",
        root / "controller" / "config" / "rbac" / "role-binding.yaml",
        root / "controller" / "config" / "rbac" / "allocation-role.yaml",
        root / "controller" / "config" / "rbac" / "allocation-binding.yaml",
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
    allocation_config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": TENANT_ALLOCATION_CONFIG,
            "namespace": "tenant-system",
            "annotations": {TENANT_ALLOCATION_APPROVAL: allocation_sha256},
        },
        "data": {TENANT_ALLOCATION_CONFIG_KEY: allocation_raw},
    }
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-azure-controller",
        "--force-conflicts",
        "-f",
        "-",
        input_text=json.dumps(allocation_config_map),
    )
    manager = render_azure_controller_manager(
        root,
        config["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"],
        image,
        allocation_sha256,
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
    allocation = _get_management_resource(
        root,
        "tenant-system",
        f"configmap/{TENANT_ALLOCATION_CONFIG}",
    )
    allocation_uid = (
        allocation.get("metadata", {}).get("uid")
        if isinstance(allocation, dict)
        else None
    )
    if not isinstance(allocation_uid, str) or not allocation_uid:
        raise RuntimeError("Azure Tenant allocation ConfigMap UID is absent")
    class AzureCatalogClient:
        kubectl_path = root / ".tools" / "bin" / "kubectl"

        @property
        def kubeconfig(self):
            return _management_kubeconfig(root)

        def kubectl(self, *args, **kwargs):
            return _kubectl(root, *args, **kwargs)

        def json(self, *args):
            return json.loads(self.kubectl(*args, "-o", "json").stdout)

    catalog_config = load_configuration(root)
    catalog_config["CONDITION_TIMEOUT"] = config["AZURE_CONTROLLER_TIMEOUT"]
    catalog_config["DELETE_TIMEOUT"] = config["AZURE_TENANT_TIMEOUT"]
    catalog_config["AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"] = config[
        "AZURE_SUPPORTED_TENANT_KUBERNETES_VERSION"
    ]
    install_database_catalog(
        root, catalog_config, AzureCatalogClient(),
        azure=True, cutover_locked=cutover_locked,
    )
    install_database_controller(
        root, AzureCatalogClient(), database_image, azure=True,
        azure_identity_client_id=inventory["outputs"]["databaseIdentityClientId"],
    )
    if cutover_locked:
        verify_catalog_cutover_lock(
            AzureCatalogClient(), namespace="tenant-system",
        )
        _verify_azure_cutover_lock(root, "v1alpha4")
        _verify_azure_controller_allocation_readiness(
            root,
            allocation_raw,
            allocation_sha256,
        )
        if CATALOG_LIFECYCLE_READY:
            release_catalog_and_tenant_cutover_locks(
                catalog_config, AzureCatalogClient(), provider="azure",
                tenant_image=image,
                database_image=database_image,
            )
    return image, uid, allocation_uid


def _admin_deployment_blockers(
    payload: Mapping[str, object],
    image: str,
    expected_uid: str | None,
) -> list[str]:
    blockers = []
    metadata = payload.get("metadata")
    spec = payload.get("spec")
    status = payload.get("status")
    if (
        not isinstance(metadata, dict)
        or not isinstance(spec, dict)
        or not isinstance(status, dict)
    ):
        return ["Azure admin Deployment is malformed"]
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        blockers.append("Azure admin Deployment UID is absent")
    elif expected_uid is not None and uid != expected_uid:
        blockers.append("Azure admin Deployment identity changed")
    generation = metadata.get("generation")
    if (
        spec.get("replicas") != 1
        or spec.get("strategy") != {"type": "Recreate"}
        or status.get("availableReplicas") != 1
        or status.get("updatedReplicas") != 1
        or not isinstance(generation, int)
        or status.get("observedGeneration") != generation
    ):
        blockers.append("Azure admin Deployment is unavailable")
    pod = spec.get("template", {}).get("spec", {})
    if not isinstance(pod, dict):
        return blockers + ["Azure admin Pod template is malformed"]
    expected_pod_security = {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    if (
        pod.get("serviceAccountName") != ADMIN_NAME
        or pod.get("automountServiceAccountToken") is not True
        or pod.get("enableServiceLinks") is not False
        or pod.get("securityContext") != expected_pod_security
    ):
        blockers.append("Azure admin Pod security contract changed")
    containers = pod.get("containers")
    admin = (
        next(
            (
                item
                for item in containers
                if isinstance(item, dict) and item.get("name") == "admin"
            ),
            None,
        )
        if isinstance(containers, list)
        else None
    )
    if not isinstance(admin, dict):
        return blockers + ["Azure admin container is absent"]
    if admin.get("image") != image:
        blockers.append("Azure admin image identity changed")
    if admin.get("imagePullPolicy") != "IfNotPresent":
        blockers.append("Azure admin image pull policy changed")
    if admin.get("env") != [
        {"name": "TENANT_ADMIN_PROVIDER", "value": "azure"}
    ]:
        blockers.append("Azure admin provider mode changed")
    if admin.get("ports") != [
        {"name": "http", "containerPort": 8080, "protocol": "TCP"}
    ]:
        blockers.append("Azure admin container port changed")
    expected_container_security = {
        "runAsNonRoot": True,
        "privileged": False,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    if admin.get("securityContext") != expected_container_security:
        blockers.append("Azure admin container security contract changed")
    expected_resources = {
        "requests": {"cpu": "25m", "memory": "32Mi"},
        "limits": {"cpu": "250m", "memory": "128Mi"},
    }
    if admin.get("resources") != expected_resources:
        blockers.append("Azure admin resource limits changed")
    for probe_name, path in (
        ("livenessProbe", "/healthz"),
        ("readinessProbe", "/readyz"),
    ):
        probe = admin.get(probe_name)
        if (
            not isinstance(probe, dict)
            or probe.get("httpGet", {}).get("path") != path
            or probe.get("httpGet", {}).get("port") != "http"
        ):
            blockers.append(f"Azure admin {probe_name} changed")
    return blockers


def _admin_service_blockers(payload: Mapping[str, object]) -> list[str]:
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        return ["Azure admin Service is malformed"]
    if (
        spec.get("type") != "ClusterIP"
        or spec.get("selector") != {"app.kubernetes.io/name": ADMIN_NAME}
    ):
        return ["Azure admin Service identity changed"]
    ports = spec.get("ports")
    if not isinstance(ports, list) or len(ports) != 1:
        return ["Azure admin Service port changed"]
    port = ports[0]
    if not isinstance(port, dict) or any(
        port.get(key) != value
        for key, value in {
            "name": "http",
            "port": 80,
            "targetPort": 8080,
            "protocol": "TCP",
        }.items()
    ):
        return ["Azure admin Service port changed"]
    return []


def _tracked_admin_resource(
    root: Path,
    relative_path: str,
) -> dict[str, object]:
    path = root / relative_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"tracked Azure admin resource is invalid: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"tracked Azure admin resource is invalid: {path}")
    return payload


def _normalized_admin_rules(
    value: object,
) -> list[
    tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]
] | None:
    if not isinstance(value, list):
        return None
    normalized = []
    for rule in value:
        if not isinstance(rule, dict) or not set(rule).issubset({
            "apiGroups", "resources", "verbs", "resourceNames",
        }):
            return None
        groups = rule["apiGroups"]
        resources = rule["resources"]
        verbs = rule["verbs"]
        names = rule.get("resourceNames", [])
        if (
            not isinstance(groups, list)
            or not isinstance(resources, list)
            or not isinstance(verbs, list)
            or not isinstance(names, list)
            or not all(
                isinstance(item, str)
                for item in (*groups, *resources, *verbs, *names)
            )
        ):
            return None
        normalized.append(
            (
                tuple(sorted(groups)),
                tuple(sorted(resources)),
                tuple(sorted(verbs)),
                tuple(sorted(names)),
            )
        )
    return sorted(normalized)


def _admin_service_account_blockers(
    payload: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[str]:
    metadata = payload.get("metadata")
    expected_metadata = expected.get("metadata")
    if (
        payload.get("apiVersion") != expected.get("apiVersion")
        or payload.get("kind") != expected.get("kind")
        or not isinstance(metadata, dict)
        or not isinstance(expected_metadata, dict)
        or metadata.get("name") != expected_metadata.get("name")
        or metadata.get("namespace") != expected_metadata.get("namespace")
        or payload.get("automountServiceAccountToken")
        is not expected.get("automountServiceAccountToken")
    ):
        return ["Azure admin ServiceAccount contract changed"]
    return []


def _admin_role_blockers(
    payload: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[str]:
    metadata = payload.get("metadata")
    expected_metadata = expected.get("metadata")
    if (
        payload.get("apiVersion") != expected.get("apiVersion")
        or payload.get("kind") != expected.get("kind")
        or not isinstance(metadata, dict)
        or not isinstance(expected_metadata, dict)
        or metadata.get("name") != expected_metadata.get("name")
        or payload.get("aggregationRule") != expected.get("aggregationRule")
        or _normalized_admin_rules(payload.get("rules"))
        != _normalized_admin_rules(expected.get("rules"))
    ):
        return ["Azure admin ClusterRole contract changed"]
    return []


def _admin_binding_blockers(
    payload: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[str]:
    metadata = payload.get("metadata")
    expected_metadata = expected.get("metadata")
    if (
        payload.get("apiVersion") != expected.get("apiVersion")
        or payload.get("kind") != expected.get("kind")
        or not isinstance(metadata, dict)
        or not isinstance(expected_metadata, dict)
        or metadata.get("name") != expected_metadata.get("name")
        or payload.get("roleRef") != expected.get("roleRef")
        or payload.get("subjects") != expected.get("subjects")
    ):
        return ["Azure admin ClusterRoleBinding contract changed"]
    return []


def _admin_authorization_blockers(root: Path) -> list[str]:
    inventory = _kubectl(
        root,
        "get",
        "--raw",
        f"/api/v1/namespaces?limit={ADMIN_NAMESPACE_LIMIT + 1}",
        check=False,
    )
    if inventory.returncode != 0:
        return ["Azure admin Namespace inventory failed"]
    try:
        namespaces = admin_review_namespaces(json.loads(inventory.stdout))
    except (json.JSONDecodeError, RuntimeError) as exc:
        return [str(exc).replace("Tenant Admin", "Azure admin", 1)]
    for namespace in namespaces:
        response = _kubectl(
            root,
            "create",
            "--validate=false",
            "-f",
            "-",
            "-o",
            "json",
            f"--as={ADMIN_IDENTITY}",
            input_text=admin_rules_review_request(namespace),
            check=False,
        )
        if response.returncode != 0:
            return [
                f"Azure admin effective RBAC review failed in {namespace}"
            ]
        try:
            review = json.loads(response.stdout)
            def credential_resource(scope: str, resource: str) -> object:
                args = ("get", resource, "-o", "json")
                if not resource.startswith("namespace/"):
                    args = ("get", "-n", scope, resource, "-o", "json")
                result = _kubectl(root, *args, check=False)
                if result.returncode != 0:
                    raise RuntimeError(
                        "Azure admin effective RBAC credential identity is unavailable"
                    )
                return json.loads(result.stdout)

            validate_admin_effective_rules(
                root, "azure", review, namespace, credential_resource
            )
        except (json.JSONDecodeError, RuntimeError) as exc:
            return [str(exc).replace("Tenant Admin", "Azure admin", 1)]
    return []


def _azure_admin_overview_names(raw: str) -> tuple[str, ...]:
    try:
        overview = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Azure admin overview API returned invalid JSON") from exc
    overview_data = overview.get("data") if isinstance(overview, dict) else None
    overview_summary = (
        overview_data.get("overview")
        if isinstance(overview_data, dict)
        else None
    )
    overview_tenants = (
        overview_data.get("tenants")
        if isinstance(overview_data, dict)
        else None
    )
    counts = (
        overview_summary.get("tenants")
        if isinstance(overview_summary, dict)
        else None
    )
    names = (
        [
            item.get("name")
            for item in overview_tenants
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        if isinstance(overview_tenants, list)
        else []
    )
    if (
        not isinstance(overview, dict)
        or overview.get("schemaVersion") != ADMIN_API_SCHEMA_VERSION
        or not isinstance(overview_data, dict)
        or set(overview_data) != {"overview", "tenants"}
        or not isinstance(overview_summary, dict)
        or overview_summary.get("providerMode") != "azure"
        or not isinstance(overview_tenants, list)
        or len(names) != len(overview_tenants)
        or names != sorted(names)
        or not isinstance(counts, dict)
        or not isinstance(counts.get("total"), int)
        or counts.get("total") != len(overview_tenants)
    ):
        raise RuntimeError("Azure admin overview API contract changed")
    return tuple(names)


def _azure_admin_tenant_disappeared(root: Path, name: str) -> bool:
    response = _kubectl(
        root,
        "get",
        "--raw",
        f"{ADMIN_SERVICE_PROXY}/api/v1/overview",
        check=False,
    )
    if response.returncode != 0:
        raise RuntimeError("Azure admin overview API is unavailable")
    return name not in _azure_admin_overview_names(response.stdout)


def _admin_api_blockers(root: Path) -> list[str]:
    blockers = []
    responses = {}
    for endpoint in ("healthz", "readyz", "api/v1/overview", "api/v1/tenants"):
        response = _kubectl(
            root,
            "get",
            "--raw",
            f"{ADMIN_SERVICE_PROXY}/{endpoint}",
            check=False,
        )
        if response.returncode != 0:
            blockers.append(f"Azure admin API is unavailable: /{endpoint}")
        else:
            responses[endpoint] = response.stdout
    if blockers:
        return blockers
    try:
        overview_names = _azure_admin_overview_names(
            responses["api/v1/overview"]
        )
    except RuntimeError as exc:
        return [str(exc)]
    try:
        tenants = json.loads(responses["api/v1/tenants"])
    except (TypeError, json.JSONDecodeError):
        return ["Azure admin API returned invalid JSON"]
    tenant_data = tenants.get("data") if isinstance(tenants, dict) else None
    if (
        not isinstance(tenants, dict)
        or tenants.get("schemaVersion") != ADMIN_API_SCHEMA_VERSION
        or not isinstance(tenant_data, list)
        or not all(isinstance(item, dict) for item in tenant_data)
    ):
        blockers.append("Azure admin Tenant list API contract changed")
    else:
        listed_names = [item.get("name") for item in tenant_data]
        if (
            not all(isinstance(name, str) and name for name in listed_names)
            or listed_names != sorted(listed_names)
        ):
            blockers.append("Azure admin Tenant list API contract changed")
    if blockers or not overview_names:
        return blockers
    for name in overview_names:
        endpoint_responses = {}
        tenant_disappeared = False
        tenant_request_failed = False
        for endpoint in (
            f"api/v1/tenants/{name}",
            f"api/v1/tenants/{name}/topology",
        ):
            response = _kubectl(
                root,
                "get",
                "--raw",
                f"{ADMIN_SERVICE_PROXY}/{endpoint}",
                check=False,
            )
            if response.returncode != 0:
                try:
                    tenant_disappeared = _azure_admin_tenant_disappeared(
                        root,
                        name,
                    )
                except RuntimeError as exc:
                    blockers.append(str(exc))
                    tenant_request_failed = True
                if not tenant_disappeared and not tenant_request_failed:
                    blockers.append(
                        f"Azure admin API is unavailable: /{endpoint}"
                    )
                    tenant_request_failed = True
                break
            try:
                envelope = json.loads(response.stdout)
            except (TypeError, json.JSONDecodeError):
                blockers.append(f"Azure admin API returned invalid JSON: /{endpoint}")
                continue
            if (
                not isinstance(envelope, dict)
                or envelope.get("schemaVersion") != ADMIN_API_SCHEMA_VERSION
            ):
                blockers.append(f"Azure admin API contract changed: /{endpoint}")
                continue
            endpoint_responses[endpoint] = envelope.get("data")
        if tenant_disappeared:
            continue
        if tenant_request_failed:
            continue
        snapshot = endpoint_responses.get(f"api/v1/tenants/{name}")
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "identity",
            "detail",
            "database",
            "topology",
        }:
            blockers.append("Azure admin Tenant snapshot API contract changed")
        else:
            identity = snapshot.get("identity")
            detail = snapshot.get("detail")
            database = snapshot.get("database")
            topology = snapshot.get("topology")
            if (
                not isinstance(identity, dict)
                or not isinstance(detail, dict)
                or not isinstance(database, dict)
                or not isinstance(topology, dict)
                or not isinstance(detail.get("summary"), dict)
                or detail["summary"].get("name") != name
                or set(database)
                != {"state", "observedAt", "freshness", "reason"}
                or database.get("state") != "not-applicable"
                or not isinstance(database.get("observedAt"), str)
                or not database["observedAt"]
                or database.get("freshness") != "live"
                or database.get("reason") != "provider-unsupported"
                or topology.get("tenantName") != name
                or not isinstance(topology.get("nodes"), list)
                or not isinstance(topology.get("edges"), list)
                or identity.get("uid") != detail.get("uid")
                or identity.get("generation") != detail.get("generation")
                or identity.get("observedGeneration")
                != detail.get("observedGeneration")
            ):
                blockers.append("Azure admin Tenant snapshot API contract changed")
        topology = endpoint_responses.get(
            f"api/v1/tenants/{name}/topology"
        )
        if (
            not isinstance(topology, dict)
            or set(topology) != {"tenantName", "provider", "nodes", "edges"}
            or topology.get("tenantName") != name
            or not isinstance(topology.get("provider"), str)
            or not isinstance(topology.get("nodes"), list)
            or not isinstance(topology.get("edges"), list)
        ):
            blockers.append("Azure admin topology API contract changed")
    return blockers


def _inspect_admin(
    root: Path,
    image: str,
    expected_uid: str | None,
    *,
    verify_api: bool,
) -> tuple[str | None, tuple[str, ...]]:
    blockers = []
    deployment = _get_management_resource(
        root,
        ADMIN_NAMESPACE,
        f"deployment/{ADMIN_NAME}",
    )
    observed_uid = None
    if deployment is None:
        blockers.append("Azure admin Deployment is absent")
    else:
        blockers.extend(
            _admin_deployment_blockers(deployment, image, expected_uid)
        )
        uid = deployment.get("metadata", {}).get("uid")
        if isinstance(uid, str) and uid:
            observed_uid = uid
    service = _get_management_resource(
        root,
        ADMIN_NAMESPACE,
        f"service/{ADMIN_NAME}",
    )
    if service is None:
        blockers.append("Azure admin Service is absent")
    else:
        blockers.extend(_admin_service_blockers(service))
    expected_service_account = _tracked_admin_resource(
        root,
        "admin/config/rbac/service-account.json",
    )
    service_account = _get_management_resource(
        root,
        ADMIN_NAMESPACE,
        f"serviceaccount/{ADMIN_NAME}",
    )
    if service_account is None:
        blockers.append("Azure admin ServiceAccount is absent")
    else:
        blockers.extend(
            _admin_service_account_blockers(
                service_account,
                expected_service_account,
            )
        )
    expected_role = _tracked_admin_resource(
        root,
        "admin/config/rbac/cluster-role-azure.json",
    )
    role = _get_management_resource(
        root,
        None,
        f"clusterrole/{admin_role_name('azure')}",
    )
    if role is None:
        blockers.append("Azure admin ClusterRole is absent")
    else:
        blockers.extend(_admin_role_blockers(role, expected_role))
    expected_binding = _tracked_admin_resource(
        root,
        "admin/config/rbac/cluster-role-binding-azure.json",
    )
    binding = _get_management_resource(
        root,
        None,
        f"clusterrolebinding/{ADMIN_NAME}",
    )
    if binding is None:
        blockers.append("Azure admin ClusterRoleBinding is absent")
    else:
        blockers.extend(_admin_binding_blockers(binding, expected_binding))
    blockers.extend(_admin_authorization_blockers(root))
    if verify_api and deployment is not None and service is not None:
        blockers.extend(_admin_api_blockers(root))
    return observed_uid, tuple(blockers)


def _verify_recorded_admin(
    root: Path,
    inventory: Mapping[str, object],
) -> None:
    image = inventory.get("adminImage")
    uid = inventory.get("adminDeploymentUid")
    if image is None and uid is None:
        return
    assert isinstance(image, str)
    assert isinstance(uid, str)
    _, blockers = _inspect_admin(root, image, uid, verify_api=True)
    if blockers:
        raise RuntimeError(
            "recorded Azure admin identity is unhealthy: "
            + "; ".join(blockers)
        )


def _install_admin(
    root: Path,
    config: Mapping[str, str],
    inventory: Mapping[str, object],
) -> tuple[str, str]:
    image = _push_admin_image(root, config, inventory)
    recorded_image = inventory.get("adminImage")
    recorded_uid = inventory.get("adminDeploymentUid")
    if recorded_image is not None and image != recorded_image:
        raise RuntimeError("recorded Azure admin image identity changed")
    for path in admin_rbac_resource_paths(root, "azure"):
        _kubectl(
            root,
            "apply",
            "--server-side",
            "--field-manager=cnpg-vcluster-azure-admin",
            "--force-conflicts",
            "-f",
            str(path),
        )
    deployment = render_azure_admin_deployment(root, image)
    _kubectl(
        root,
        "apply",
        "--server-side",
        "--field-manager=cnpg-vcluster-azure-admin",
        "--force-conflicts",
        "-f",
        str(deployment),
    )
    _kubectl(
        root,
        "-n",
        ADMIN_NAMESPACE,
        "rollout",
        "status",
        f"deployment/{ADMIN_NAME}",
        f"--timeout={config['AZURE_CONTROLLER_TIMEOUT']}",
        timeout=parse_duration(config["AZURE_CONTROLLER_TIMEOUT"]) + 60,
    )
    uid, blockers = _inspect_admin(
        root,
        image,
        recorded_uid if isinstance(recorded_uid, str) else None,
        verify_api=True,
    )
    if blockers:
        raise RuntimeError(
            "Azure admin installation is unhealthy: " + "; ".join(blockers)
        )
    if uid is None:
        raise RuntimeError("Azure admin Deployment UID is absent")
    return image, uid


def create_management(root: Path, config: Mapping[str, str]) -> None:
    preflight(root, config, emit=False)
    inventory = load_inventory(root, config)
    _kubectl(root, "get", "--raw=/readyz")
    _verify_recorded_admin(root, inventory)
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
    controller_image, provider_config_uid, allocation_config_uid = _install_tenant_controller(
        root,
        config,
        inventory,
    )
    admin_image, admin_deployment_uid = _install_admin(
        root,
        config,
        inventory,
    )
    updated = dict(inventory)
    updated["controllers"] = _controller_identities(root)
    updated["controllerImage"] = controller_image
    updated["azureProviderConfigUid"] = provider_config_uid
    updated["azureAllocationConfigUid"] = allocation_config_uid
    updated["adminImage"] = admin_image
    updated["adminDeploymentUid"] = admin_deployment_uid
    _write_inventory(root, updated)
    print("Azure management controllers, Tenant manager, and admin UI are ready")


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
    _validate_database_outputs(outputs, config["AZURE_PREFIX"])
    if not isinstance(controllers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in controllers.items()
    ):
        raise RuntimeError("Azure foundation controller inventory is invalid")
    for key in (
        "controllerImage",
        "azureProviderConfigUid",
        "azureAllocationConfigUid",
        "adminImage",
        "adminDeploymentUid",
    ):
        if key in payload and (
            not isinstance(payload[key], str) or not payload[key]
        ):
            raise RuntimeError(f"Azure foundation {key} is invalid")
    if ("adminImage" in payload) != ("adminDeploymentUid" in payload):
        raise RuntimeError(
            "Azure foundation admin image and Deployment UID must be recorded together"
        )
    if "adminImage" in payload and (
        not re.fullmatch(
            r"[^@\s]+@sha256:[0-9a-f]{64}",
            str(payload["adminImage"]),
        )
        or not str(payload["adminImage"]).startswith(
            f"{outputs.get('acrLoginServer', '')}/"
        )
    ):
        raise RuntimeError("Azure foundation admin image is invalid")
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
        *DATABASE_OUTPUTS,
    )
    missing = [key for key in required_outputs if not outputs.get(key)]
    if missing:
        raise RuntimeError(
            "Azure foundation inventory is incomplete: " + ", ".join(missing)
        )
    _validate_database_outputs(outputs, str(inventory["prefix"]))
    if set(controllers) != {
        f"{namespace}/{deployment}"
        for namespace, deployment in CONTROLLER_DEPLOYMENTS
    }:
        raise RuntimeError("Azure management controller inventory is incomplete")
    controller_image = inventory.get("controllerImage")
    provider_config_uid = inventory.get("azureProviderConfigUid")
    allocation_config_uid = inventory.get("azureAllocationConfigUid")
    if (
        not isinstance(controller_image, str)
        or not re.fullmatch(
            r"[^@\s]+@sha256:[0-9a-f]{64}",
            controller_image,
        )
        or not controller_image.startswith(f"{outputs['acrLoginServer']}/")
        or not isinstance(provider_config_uid, str)
        or not provider_config_uid
        or not isinstance(allocation_config_uid, str)
        or not allocation_config_uid
    ):
        raise RuntimeError("Azure Tenant controller inventory is incomplete")
    identity = {
        "foundationDefaultsSha256": str(inventory["foundationDefaultsSha256"]),
        "controllerImage": controller_image,
        "azureProviderConfigUid": provider_config_uid,
        "azureAllocationConfigUid": allocation_config_uid,
        **{key: str(outputs[key]) for key in required_outputs},
        **{
            f"controller:{key}": str(value)
            for key, value in sorted(controllers.items())
        },
    }
    admin_image = inventory.get("adminImage")
    admin_uid = inventory.get("adminDeploymentUid")
    if admin_image is not None or admin_uid is not None:
        if (
            not isinstance(admin_image, str)
            or not re.fullmatch(
                r"[^@\s]+@sha256:[0-9a-f]{64}",
                admin_image,
            )
            or not admin_image.startswith(f"{outputs['acrLoginServer']}/")
            or not isinstance(admin_uid, str)
            or not admin_uid
        ):
            raise RuntimeError("Azure admin inventory is incomplete")
        identity["adminImage"] = admin_image
        identity["adminDeploymentUid"] = admin_uid
    return identity


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


def _database_identity_blockers(outputs: Mapping[str, str]) -> list[str]:
    blockers: list[str] = []

    def inspect(description: str, *arguments: str) -> dict[str, object] | None:
        response = _az(*arguments, "--output", "json", check=False)
        if response.returncode != 0:
            blockers.append(f"recorded Azure database {description} is absent")
            return None
        try:
            resource = json.loads(response.stdout)
        except (ValueError, TypeError):
            resource = None
        if not isinstance(resource, dict):
            blockers.append(f"Azure database {description} inspection failed")
            return None
        return resource

    identity = inspect(
        "identity", "identity", "show",
        "--resource-group", outputs["resourceGroupName"],
        "--name", outputs["resourceGroupName"].removesuffix("-rg")
        + "-database-controller",
    )
    if identity is not None and (
        not _azure_id_equal(identity.get("id"), outputs["databaseIdentityId"])
        or not _azure_id_equal(
            identity.get("clientId"), outputs["databaseIdentityClientId"]
        )
        or not _azure_id_equal(
            identity.get("principalId"), outputs["databaseIdentityPrincipalId"]
        )
    ):
        blockers.append("Azure database identity binding changed")

    role = inspect(
        "disk role", "resource", "show", "--ids", outputs["databaseDiskRoleId"],
        "--api-version", "2022-04-01",
    )
    if role is not None:
        properties = role.get("properties")
        permissions = (
            properties.get("permissions") if isinstance(properties, dict) else None
        )
        permission = (
            permissions[0]
            if isinstance(permissions, list) and len(permissions) == 1
            else None
        )
        if (
            not _azure_id_equal(role.get("id"), outputs["databaseDiskRoleId"])
            or not isinstance(properties, dict)
            or properties.get("type") != "CustomRole"
            or not isinstance(properties.get("assignableScopes"), list)
            or len(properties["assignableScopes"]) != 1
            or not _azure_id_equal(
                properties["assignableScopes"][0], outputs["resourceGroupId"]
            )
            or not isinstance(permission, dict)
            or not isinstance(permission.get("actions"), list)
            or len(permission["actions"]) != len(DATABASE_DISK_ACTIONS)
            or {str(action).lower() for action in permission["actions"]}
            != {action.lower() for action in DATABASE_DISK_ACTIONS}
            or any(
                permission.get(key) != []
                for key in ("notActions", "dataActions", "notDataActions")
            )
        ):
            blockers.append("Azure database disk role permissions changed")

    assignment = inspect(
        "disk assignment", "resource", "show",
        "--ids", outputs["databaseDiskAssignmentId"],
        "--api-version", "2022-04-01",
    )
    if assignment is not None:
        properties = assignment.get("properties")
        if (
            not _azure_id_equal(assignment.get("id"), outputs["databaseDiskAssignmentId"])
            or not isinstance(properties, dict)
            or not _azure_id_equal(
                properties.get("principalId"), outputs["databaseIdentityPrincipalId"]
            )
            or not _azure_id_equal(
                properties.get("roleDefinitionId"), outputs["databaseDiskRoleId"]
            )
            or properties.get("principalType") != "ServicePrincipal"
            or (
                properties.get("scope") is not None
                and not _azure_id_equal(properties["scope"], outputs["resourceGroupId"])
            )
        ):
            blockers.append("Azure database disk assignment changed")

    federation = inspect(
        "federated credential", "resource", "show",
        "--ids", outputs["databaseFederationId"],
        "--api-version", "2023-01-31",
    )
    if federation is not None:
        properties = federation.get("properties")
        if (
            not _azure_id_equal(federation.get("id"), outputs["databaseFederationId"])
            or not isinstance(properties, dict)
            or properties.get("issuer") != outputs["aksOidcIssuer"]
            or properties.get("subject") != DATABASE_SERVICE_ACCOUNT
            or properties.get("audiences") != ["api://AzureADTokenExchange"]
        ):
            blockers.append("Azure database ServiceAccount federation changed")

    response = _az(
        "role", "assignment", "list",
        "--assignee-object-id", outputs["databaseIdentityPrincipalId"],
        "--all", "--include-inherited", "--output", "json", check=False,
    )
    if response.returncode != 0:
        blockers.append("Azure database identity role assignment inspection failed")
    else:
        try:
            assignments = json.loads(response.stdout)
        except (ValueError, TypeError):
            assignments = None
        if (
            not isinstance(assignments, list)
            or len(assignments) != 1
            or not isinstance(assignments[0], dict)
            or not _azure_id_equal(
                assignments[0].get("id"), outputs["databaseDiskAssignmentId"]
            )
            or not _azure_id_equal(
                assignments[0].get("scope"), outputs["resourceGroupId"]
            )
            or not _azure_id_equal(
                assignments[0].get("principalId"),
                outputs["databaseIdentityPrincipalId"],
            )
            or not _azure_id_equal(
                assignments[0].get("roleDefinitionId"), outputs["databaseDiskRoleId"]
            )
        ):
            blockers.append("Azure database identity has missing or excess role authority")
    return blockers


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
    if "adminImage" in inventory:
        expected_admin_prefix = (
            f"{outputs['acrLoginServer']}/"
            f"{config['AZURE_ADMIN_REPOSITORY']}@sha256:"
        )
        if not str(inventory["adminImage"]).startswith(expected_admin_prefix):
            blockers.append("Azure admin repository binding changed")
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
    blockers.extend(_database_identity_blockers(outputs))
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
        allocation_config = _get_management_resource(
            root,
            "tenant-system",
            f"configmap/{TENANT_ALLOCATION_CONFIG}",
        )
        if allocation_config is None:
            blockers.append("Azure Tenant allocation ConfigMap is absent")
        else:
            if (
                allocation_config.get("metadata", {}).get("uid")
                != inventory.get("azureAllocationConfigUid")
            ):
                blockers.append("Azure Tenant allocation identity changed")
            try:
                _, expected_raw, expected_sha256 = _azure_allocation_configuration(
                    root, config
                )
            except RuntimeError:
                expected_raw = expected_sha256 = None
            if (
                allocation_config.get("data", {}).get(
                    TENANT_ALLOCATION_CONFIG_KEY
                )
                != expected_raw
                or allocation_config.get("metadata", {})
                .get("annotations", {})
                .get(TENANT_ALLOCATION_APPROVAL)
                != expected_sha256
            ):
                blockers.append("Azure Tenant allocation configuration changed")
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
        if "adminImage" in inventory:
            _, admin_blockers = _inspect_admin(
                root,
                str(inventory["adminImage"]),
                str(inventory["adminDeploymentUid"]),
                verify_api=True,
            )
            blockers.extend(admin_blockers)
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
    database_blockers = _database_identity_blockers(outputs)
    if database_blockers:
        raise RuntimeError(
            "Azure database foundation is unhealthy: " + "; ".join(database_blockers)
        )
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
