use kube::core::{ApiResource, GroupVersionKind};
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum InventoryPolicy {
    BlockAnyInstance,
    TenantMarkers,
    TenantMarkersOrKamajiOwner,
    AllocationMarkers,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ManagementResource {
    pub api_version: &'static str,
    pub kind: &'static str,
    pub plural: &'static str,
    pub namespaced: bool,
    pub role: &'static str,
    pub inventory_policy: InventoryPolicy,
    pub exemptions: &'static [&'static str],
}

impl ManagementResource {
    pub fn api_resource(self) -> ApiResource {
        let (group, version) = self
            .api_version
            .split_once('/')
            .expect("dynamic management resources use grouped API versions");
        let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(group, version, self.kind));
        resource.plural = self.plural.into();
        resource
    }
}

const fn dynamic(
    api_version: &'static str,
    kind: &'static str,
    plural: &'static str,
    role: &'static str,
) -> ManagementResource {
    ManagementResource {
        api_version,
        kind,
        plural,
        namespaced: true,
        role,
        inventory_policy: InventoryPolicy::BlockAnyInstance,
        exemptions: &[],
    }
}

pub const ACTIVATION_RESOURCES: &[ManagementResource] = &[
    dynamic("cluster.x-k8s.io/v1beta2", "Cluster", "clusters", "cluster"),
    dynamic(
        "cluster.x-k8s.io/v1beta2",
        "MachineDeployment",
        "machinedeployments",
        "machine-deployment",
    ),
    dynamic(
        "cluster.x-k8s.io/v1beta2",
        "MachineSet",
        "machinesets",
        "machine",
    ),
    dynamic("cluster.x-k8s.io/v1beta2", "Machine", "machines", "machine"),
    dynamic(
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevCluster",
        "devclusters",
        "dev-cluster",
    ),
    dynamic(
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevMachineTemplate",
        "devmachinetemplates",
        "dev-machine-template",
    ),
    dynamic(
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevMachine",
        "devmachines",
        "machine",
    ),
    dynamic(
        "bootstrap.cluster.x-k8s.io/v1beta2",
        "KubeadmConfigTemplate",
        "kubeadmconfigtemplates",
        "kubeadm-config-template",
    ),
    dynamic(
        "bootstrap.cluster.x-k8s.io/v1beta2",
        "KubeadmConfig",
        "kubeadmconfigs",
        "machine",
    ),
    dynamic(
        "controlplane.cluster.x-k8s.io/v1alpha2",
        "KamajiControlPlane",
        "kamajicontrolplanes",
        "kamaji-control-plane",
    ),
    dynamic(
        "kamaji.clastix.io/v1alpha1",
        "TenantControlPlane",
        "tenantcontrolplanes",
        "provider",
    ),
    ManagementResource {
        api_version: "v1",
        kind: "Namespace",
        plural: "namespaces",
        namespaced: false,
        role: "namespace",
        inventory_policy: InventoryPolicy::TenantMarkers,
        exemptions: &["management-infrastructure"],
    },
    ManagementResource {
        api_version: "v1",
        kind: "Secret",
        plural: "secrets",
        namespaced: true,
        role: "tenant-kubeconfig",
        inventory_policy: InventoryPolicy::TenantMarkersOrKamajiOwner,
        exemptions: &["controller-installation-secrets"],
    },
    ManagementResource {
        api_version: "coordination.k8s.io/v1",
        kind: "Lease",
        plural: "leases",
        namespaced: true,
        role: "allocation-lease",
        inventory_policy: InventoryPolicy::AllocationMarkers,
        exemptions: &["controller-leader-election"],
    },
];
