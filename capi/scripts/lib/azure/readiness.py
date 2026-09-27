from __future__ import annotations

from .common import *
from .foundation import _get_management_resource
from .rendering import (
    _external_azure_cluster_metadata,
    _require_markers,
)

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

