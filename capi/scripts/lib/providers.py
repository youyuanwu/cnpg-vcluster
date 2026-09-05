from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .files import IntegrityError
from .kube import ManagementClient
from .rendering import render_release_manifest


@dataclass(frozen=True)
class Provider:
    name: str
    source: str
    destination: str
    image_tagged_key: str
    image_key: str
    namespace: str
    deployment: str
    crds: tuple[str, ...]
    storage_version: str
    conversion: str | None
    variables: dict[str, str]


PROVIDERS = (
    Provider(
        name="capi-core",
        source="capi-core-components.yaml",
        destination="capi-core-components.yaml",
        image_tagged_key="CAPI_CORE_IMAGE_TAGGED",
        image_key="CAPI_CORE_IMAGE",
        namespace="CAPI_NAMESPACE",
        deployment="capi-controller-manager",
        crds=("clusters.cluster.x-k8s.io", "machines.cluster.x-k8s.io"),
        storage_version="v1beta2",
        conversion="Webhook",
        variables={},
    ),
    Provider(
        name="cabpk",
        source="capi-bootstrap-components.yaml",
        destination="capi-bootstrap-components.yaml",
        image_tagged_key="CAPI_BOOTSTRAP_IMAGE_TAGGED",
        image_key="CAPI_BOOTSTRAP_IMAGE",
        namespace="CABPK_NAMESPACE",
        deployment="capi-kubeadm-bootstrap-controller-manager",
        crds=(
            "kubeadmconfigs.bootstrap.cluster.x-k8s.io",
            "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io",
        ),
        storage_version="v1beta2",
        conversion="Webhook",
        variables={"KUBEADM_BOOTSTRAP_TOKEN_TTL": "10m"},
    ),
    Provider(
        name="capd",
        source="capd-components.yaml",
        destination="capd-components.yaml",
        image_tagged_key="CAPD_IMAGE_TAGGED",
        image_key="CAPD_IMAGE",
        namespace="CAPD_NAMESPACE",
        deployment="capd-controller-manager",
        crds=(
            "devclusters.infrastructure.cluster.x-k8s.io",
            "devmachines.infrastructure.cluster.x-k8s.io",
        ),
        storage_version="v1beta2",
        conversion="Webhook",
        variables={"CAPD_DOCKER_HOST": '""'},
    ),
    Provider(
        name="kamaji-control-plane",
        source="kamaji-capi-components.yaml",
        destination="kamaji-capi-components.yaml",
        image_tagged_key="KAMAJI_CAPI_IMAGE_TAGGED",
        image_key="KAMAJI_CAPI_IMAGE",
        namespace="KAMAJI_CAPI_NAMESPACE",
        deployment="capi-kamaji-controller-manager",
        crds=(
            "kamajicontrolplanes.controlplane.cluster.x-k8s.io",
            "kamajicontrolplanetemplates.controlplane.cluster.x-k8s.io",
        ),
        storage_version="v1alpha2",
        conversion=None,
        variables={},
    ),
)


def _provider_paths(root: Path, provider: Provider) -> tuple[Path, Path]:
    return (
        root / ".tools" / "inputs" / provider.source,
        root / ".runtime" / "rendered" / "providers" / provider.destination,
    )


def render_providers(root: Path, config: dict[str, str]) -> list[Path]:
    kamaji_settings = json.loads(
        (root / "manifests" / "management" / "kamaji-provider-settings.json").read_text(
            encoding="utf-8"
        )
    )
    feature_gates = kamaji_settings["featureGates"]
    kamaji_variables = {
        "CACPPK_DYNAMIC_INFRASTRUCTURE_CLUSTER_PATCH": str(
            feature_gates["DynamicInfrastructureClusterPatch"]
        ).lower(),
        "CACPPK_EXTERNAL_CLUSTER_REFERENCE": str(
            feature_gates["ExternalClusterReference"]
        ).lower(),
        "CACPPK_EXTERNAL_CLUSTER_REFERENCE_CROSS_NAMESPACE": str(
            feature_gates["ExternalClusterReferenceCrossNamespace"]
        ).lower(),
        "CACPPK_SKIP_INFRA_CLUSTER_PATCH": str(
            feature_gates["SkipInfraClusterPatch"]
        ).lower(),
        "CACPPK_INFRASTRUCTURE_CLUSTERS": ",".join(
            kamaji_settings["dynamicInfrastructureClusters"]
        ),
    }
    rendered: list[Path] = []
    for provider in PROVIDERS:
        source, destination = _provider_paths(root, provider)
        variables = {
            "CAPI_DIAGNOSTICS_ADDRESS": ":8443",
            "CAPI_INSECURE_DIAGNOSTICS": "false",
            **provider.variables,
        }
        if provider.name == "kamaji-control-plane":
            variables.update(kamaji_variables)
        render_release_manifest(
            source,
            destination,
            replacements={
                config[provider.image_tagged_key]: config[provider.image_key],
            },
            variables=variables,
        )
        text = destination.read_text(encoding="utf-8")
        if "${" in text or config[provider.image_key] not in text:
            raise IntegrityError(f"provider render is incomplete: {provider.name}")
        rendered.append(destination)
    return rendered


def _wait_provider(client: ManagementClient, config: dict[str, str], provider: Provider) -> None:
    timeout = config["PROVIDER_TIMEOUT"]
    for crd in provider.crds:
        client.kubectl(
            "wait",
            "--for=condition=Established",
            f"crd/{crd}",
            f"--timeout={timeout}",
        )
        payload = json.loads(client.kubectl("get", f"crd/{crd}", "-o", "json").stdout)
        versions = payload["spec"]["versions"]
        matching = [
            version
            for version in versions
            if version["name"] == provider.storage_version
        ]
        if (
            len(matching) != 1
            or not matching[0].get("served")
            or not matching[0].get("storage")
        ):
            raise RuntimeError(
                f"{provider.name} CRD {crd} does not expose "
                f"{provider.storage_version} as served/storage"
            )
        actual_conversion = payload["spec"].get("conversion", {}).get("strategy")
        if provider.conversion and actual_conversion != provider.conversion:
            raise RuntimeError(
                f"{provider.name} CRD {crd} conversion strategy mismatch"
            )
    namespace = config[provider.namespace]
    client.kubectl(
        "-n",
        namespace,
        "rollout",
        "status",
        f"deployment/{provider.deployment}",
        f"--timeout={timeout}",
    )
    image = client.kubectl(
        "-n",
        namespace,
        "get",
        f"deployment/{provider.deployment}",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    ).stdout
    if image != config[provider.image_key]:
        raise RuntimeError(
            f"{provider.name} live image mismatch: expected {config[provider.image_key]}, got {image}"
        )


def _verify_kamaji_provider_flags(client: ManagementClient, config: dict[str, str]) -> None:
    arguments = client.kubectl(
        "-n",
        config["KAMAJI_CAPI_NAMESPACE"],
        "get",
        "deployment/capi-kamaji-controller-manager",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].args}",
    ).stdout
    required = (
        "DynamicInfrastructureClusterPatch=false",
        "ExternalClusterReference=false",
        "ExternalClusterReferenceCrossNamespace=false",
        "SkipInfraClusterPatch=true",
    )
    if any(marker not in arguments for marker in required):
        raise RuntimeError("Kamaji provider feature gates do not match the local contract")
    if "DevCluster" in arguments:
        raise RuntimeError("Kamaji provider must not enable dynamic DevCluster patching")
    role = json.loads(
        client.kubectl(
            "get",
            "clusterrole/capi-kamaji-manager-role",
            "-o",
            "json",
        ).stdout
    )
    resources = {
        resource
        for rule in role.get("rules", [])
        for resource in rule.get("resources", [])
    }
    if "devclusters" in resources:
        raise RuntimeError("Kamaji provider unexpectedly has DevCluster patch RBAC")
    crd = json.loads(
        client.kubectl(
            "get",
            "crd/kamajicontrolplanes.controlplane.cluster.x-k8s.io",
            "-o",
            "json",
        ).stdout
    )
    if crd["metadata"].get("labels", {}).get("cluster.x-k8s.io/v1beta2") != "v1alpha2":
        raise RuntimeError("Kamaji provider live CAPI contract label mismatch")


def reconcile_providers(root: Path, config: dict[str, str], client: ManagementClient) -> None:
    render_providers(root, config)
    for provider in PROVIDERS:
        _, manifest = _provider_paths(root, provider)
        client.kubectl(
            "apply",
            "--server-side",
            "--field-manager=capi-kamaji-lab",
            "-f",
            str(manifest),
        )
        _wait_provider(client, config, provider)
    _verify_kamaji_provider_flags(client, config)


def provider_status(config: dict[str, str], client: ManagementClient) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for provider in PROVIDERS:
        namespace = config[provider.namespace]
        response = client.kubectl(
            "-n",
            namespace,
            "get",
            f"deployment/{provider.deployment}",
            "-o",
            "json",
            check=False,
        )
        if response.returncode != 0:
            result.append({"name": provider.name, "available": False, "reason": "missing"})
            continue
        payload = json.loads(response.stdout)
        desired = payload.get("spec", {}).get("replicas", 0)
        available = payload.get("status", {}).get("availableReplicas", 0)
        image = payload["spec"]["template"]["spec"]["containers"][0]["image"]
        configuration_ready = True
        if provider.name == "kamaji-control-plane":
            arguments = " ".join(
                payload["spec"]["template"]["spec"]["containers"][0].get("args", [])
            )
            configuration_ready = (
                "DynamicInfrastructureClusterPatch=false" in arguments
                and "SkipInfraClusterPatch=true" in arguments
                and "DevCluster" not in arguments
            )
        result.append(
            {
                "name": provider.name,
                "available": (
                    desired == available
                    and desired > 0
                    and image == config[provider.image_key]
                    and configuration_ready
                ),
                "desired": desired,
                "availableReplicas": available,
                "image": image,
            }
        )
    return result


def delete_providers(root: Path, config: dict[str, str], client: ManagementClient) -> None:
    render_providers(root, config)
    for provider in reversed(PROVIDERS):
        _, manifest = _provider_paths(root, provider)
        client.kubectl(
            "delete",
            "-f",
            str(manifest),
            "--ignore-not-found",
            "--wait=true",
            f"--timeout={config['DELETE_TIMEOUT']}",
            check=False,
        )
