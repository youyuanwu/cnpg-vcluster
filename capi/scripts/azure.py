#!/usr/bin/env python3
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



from scripts.lib.azure.common import (
    CAPZ_AZURECLUSTER_WEBHOOK,
    CAPZ_EXTERNAL_CONTROL_PLANE_LABEL,
    CONTROLLER_DEPLOYMENTS,
    FOUNDATION_DEFAULT_KEYS,
    FOUNDATION_INVENTORY_SCHEMA,
    KNOWN_ASO_FOUNDATION_REFERENCE_OUTPUTS,
    KNOWN_ASO_TENANT_KINDS,
    KNOWN_AZURE_TENANT_TYPES,
    KNOWN_CONTROLLER_MANAGEMENT_KINDS,
    KNOWN_NAMESPACE_CHILD_KINDS,
    KNOWN_ORCHESTRATION_MANAGEMENT_KINDS,
    MANAGEMENT_RESOURCE_PLURALS,
    READY_EVIDENCE_MAX_AGE_SECONDS,
    AzureDeletionError,
    _active_subscription,
    _az,
    _azure_id_equal,
    _foundation_defaults_checksum,
    _foundation_networks,
    _helm,
    _json,
    _kubectl,
    _management_kubeconfig,
    _recorded_azure_specs,
    _runtime_dir,
    _tenant_kubeconfig,
    _tenant_kubectl,
    _tenant_runtime_dir,
    _validate_networks,
    azure_tenant_runtime_path,
    load_azure_configuration,
    names,
    tenant_names,
)
from scripts.lib.azure.contracts import _expected_tenant_markers, _management_resource_specs
from scripts.lib.azure.ownership import (
    _classify_management_owned_resources,
    _discover_owned_repeatedly,
    _journal_discovery,
    _management_resource_name,
    _merge_management_discoveries,
    _merge_owned_discoveries,
    _recorded_owned_resources,
    _require_recorded_resources_present,
    classify_azure_owned_resources,
    discover_azure_owned_resources,
    discover_management_owned_resources,
)
from scripts.lib.azure.readiness import (
    _capture_tenant_kubeconfig,
    _capture_vmss_identities,
    _collect_ready_observations,
    _condition_true,
    _retain_external_control_plane_lb,
    _tenant_spec_blockers,
    _wait_addon_job,
    _wait_ready_observations,
    _wait_tenant_endpoint,
    _wait_worker_registered,
    _workload_ready,
)
from scripts.lib.azure.rendering import (
    RESOURCE_IDENTITY_KEYS,
    _azure_tags,
    _azure_tags_match,
    _external_azure_cluster_metadata,
    _identity_key,
    _marker_annotations,
    _metadata,
    _reconcile_manifest,
    _render_addon_job,
    _render_tenant_control_plane,
    _render_worker_pool,
    _require_markers,
    _resource_ref,
    _write_manifest,
)
from scripts.lib.azure.foundation import (
    _capz_external_control_plane_webhook_ready,
    _foundation_identity,
    _get_management_resource,
    _inspect_foundation,
    create_foundation,
    create_management,
    destroy,
    foundation_status,
    load_inventory,
    preflight,
)

















































































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
        runtime = TenantRuntime(root, tenant)
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
        verified_at = evidence.get("verifiedAt")
        now = self.clock()
        verified_at_is_valid = (
            isinstance(verified_at, (int, float))
            and not isinstance(verified_at, bool)
            and math.isfinite(verified_at)
        )
        if (
            evidence.get("profile") != "azure"
            or evidence.get("tenant") != tenant
            or evidence.get("specificationSha256") != spec.sha256()
            or evidence.get("foundationIdentity") != dict(identity.foundation_identity)
            or evidence.get("observed") != dict(identity.observed)
            or evidence.get("ready") != observations
            or not verified_at_is_valid
            or now < verified_at
            or now - verified_at > READY_EVIDENCE_MAX_AGE_SECONDS
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
        runtime = TenantRuntime(root, spec.name)
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


def _run_profile_mutation(root: Path, config: Mapping[str, str], mutation) -> None:
    with e2e_lock(root, exclusive=False):
        with azure_lock(
            root,
            exclusive=True,
            create=True,
        ) as acquired:
            if not acquired:
                raise RuntimeError("Azure profile mutation lock is unavailable")
            with tools_lock(root, exclusive=True):
                mutation(root, config)


def _run_profile_status(root: Path, config: Mapping[str, str]) -> int:
    if not azure_lock_exists(root):
        return foundation_status(root, config)
    with e2e_lock(root, exclusive=False, create=False) as e2e_acquired:
        if not e2e_acquired:
            raise RuntimeError("Azure E2E status lock is missing")
        with azure_lock(
            root,
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
