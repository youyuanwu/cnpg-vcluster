use kube::core::{ApiResource, GroupVersionKind};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ManagementResource {
    pub api_version: &'static str,
    pub kind: &'static str,
    pub plural: &'static str,
}

impl ManagementResource {
    pub fn api_resource(self) -> ApiResource {
        let (group, version) = self
            .api_version
            .split_once('/')
            .expect("management resources use grouped API versions");
        let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(group, version, self.kind));
        resource.plural = self.plural.into();
        resource
    }
}

pub const ACTIVATION_RESOURCES: &[ManagementResource] = &[
    ManagementResource {
        api_version: "cluster.x-k8s.io/v1beta2",
        kind: "Cluster",
        plural: "clusters",
    },
    ManagementResource {
        api_version: "cluster.x-k8s.io/v1beta2",
        kind: "MachineDeployment",
        plural: "machinedeployments",
    },
    ManagementResource {
        api_version: "cluster.x-k8s.io/v1beta2",
        kind: "MachineSet",
        plural: "machinesets",
    },
    ManagementResource {
        api_version: "cluster.x-k8s.io/v1beta2",
        kind: "Machine",
        plural: "machines",
    },
    ManagementResource {
        api_version: "infrastructure.cluster.x-k8s.io/v1beta2",
        kind: "DevCluster",
        plural: "devclusters",
    },
    ManagementResource {
        api_version: "infrastructure.cluster.x-k8s.io/v1beta2",
        kind: "DevMachineTemplate",
        plural: "devmachinetemplates",
    },
    ManagementResource {
        api_version: "infrastructure.cluster.x-k8s.io/v1beta2",
        kind: "DevMachine",
        plural: "devmachines",
    },
    ManagementResource {
        api_version: "bootstrap.cluster.x-k8s.io/v1beta2",
        kind: "KubeadmConfigTemplate",
        plural: "kubeadmconfigtemplates",
    },
    ManagementResource {
        api_version: "bootstrap.cluster.x-k8s.io/v1beta2",
        kind: "KubeadmConfig",
        plural: "kubeadmconfigs",
    },
    ManagementResource {
        api_version: "controlplane.cluster.x-k8s.io/v1alpha2",
        kind: "KamajiControlPlane",
        plural: "kamajicontrolplanes",
    },
    ManagementResource {
        api_version: "kamaji.clastix.io/v1alpha1",
        kind: "TenantControlPlane",
        plural: "tenantcontrolplanes",
    },
];
