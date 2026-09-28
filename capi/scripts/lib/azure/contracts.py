from __future__ import annotations

from types import MappingProxyType

from .common import *

MANAGEMENT_RESOURCE_DESCRIPTORS = (
    ("namespaceUid", None, "Namespace", "namespace"),
    ("azureClusterIdentityUid", "namespace", "AzureClusterIdentity", "azureClusterIdentity"),
    ("clusterUid", "namespace", "Cluster", "cluster"),
    ("azureClusterUid", "namespace", "AzureCluster", "azureCluster"),
    ("kamajiControlPlaneUid", "namespace", "KamajiControlPlane", "controlPlane"),
    ("kubeadmConfigUid", "namespace", "KubeadmConfig", "pool"),
    ("azureMachinePoolUid", "namespace", "AzureMachinePool", "pool"),
    ("machinePoolUid", "namespace", "MachinePool", "pool"),
    ("cloudValuesConfigMapUid", "namespace", "ConfigMap", "cloudValues"),
    ("networkValuesConfigMapUid", "namespace", "ConfigMap", "networkValues"),
    ("statusProbeDeploymentUid", "namespace", "Deployment", "statusProbe"),
    ("addonJobUid", "namespace", "Job", "addonJob"),
)

RESOURCE_IDENTITY_KEYS = MappingProxyType({
    "Namespace": "namespaceUid",
    "AzureClusterIdentity": "azureClusterIdentityUid",
    "Cluster": "clusterUid",
    "AzureCluster": "azureClusterUid",
    "KamajiControlPlane": "kamajiControlPlaneUid",
    "KubeadmConfig": "kubeadmConfigUid",
    "MachinePool": "machinePoolUid",
    "AzureMachinePool": "azureMachinePoolUid",
    "ConfigMap": MappingProxyType({
        "cloud": "cloudValuesConfigMapUid",
        "network": "networkValuesConfigMapUid",
    }),
    "Deployment": "statusProbeDeploymentUid",
    "Job": "addonJobUid",
})


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
    selected = {"namespace": spec.namespace, **tenant_names(spec)}
    return tuple(
        (
            key,
            selected[namespace_key] if namespace_key is not None else None,
            kind,
            selected[name_key],
        )
        for key, namespace_key, kind, name_key in MANAGEMENT_RESOURCE_DESCRIPTORS
    )
