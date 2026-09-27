from __future__ import annotations

from .common import *

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

