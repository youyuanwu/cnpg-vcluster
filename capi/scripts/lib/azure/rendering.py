from __future__ import annotations

from .common import *
from .foundation import _get_management_resource

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

