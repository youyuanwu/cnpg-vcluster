use kube::core::{ApiResource, GroupVersionKind};
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ResourceClass {
    Root,
    Descendant,
    Typed,
}

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
    pub class: ResourceClass,
    pub parent_kind: Option<&'static str>,
    pub alternate_parent_kind: Option<&'static str>,
    pub worker_suffix: bool,
    pub watched: bool,
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

    pub fn name(self, tenant: &str) -> String {
        if self.worker_suffix {
            format!("{tenant}-worker")
        } else {
            tenant.into()
        }
    }
}

#[allow(clippy::too_many_arguments)]
const fn dynamic(
    api_version: &'static str,
    kind: &'static str,
    plural: &'static str,
    role: &'static str,
    class: ResourceClass,
    parent_kind: Option<&'static str>,
    worker_suffix: bool,
    watched: bool,
) -> ManagementResource {
    ManagementResource {
        api_version,
        kind,
        plural,
        namespaced: true,
        role,
        class,
        parent_kind,
        alternate_parent_kind: None,
        worker_suffix,
        watched,
        inventory_policy: InventoryPolicy::BlockAnyInstance,
        exemptions: &[],
    }
}

const fn template(
    api_version: &'static str,
    kind: &'static str,
    plural: &'static str,
    role: &'static str,
) -> ManagementResource {
    let mut resource = dynamic(
        api_version,
        kind,
        plural,
        role,
        ResourceClass::Root,
        Some("Cluster"),
        true,
        true,
    );
    resource.alternate_parent_kind = Some("MachineDeployment");
    resource
}

pub const MANAGEMENT_RESOURCES: &[ManagementResource] = &[
    dynamic(
        "cluster.x-k8s.io/v1beta2",
        "Cluster",
        "clusters",
        "cluster",
        ResourceClass::Root,
        None,
        false,
        true,
    ),
    dynamic(
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevCluster",
        "devclusters",
        "dev-cluster",
        ResourceClass::Root,
        Some("Cluster"),
        false,
        true,
    ),
    dynamic(
        "controlplane.cluster.x-k8s.io/v1alpha2",
        "KamajiControlPlane",
        "kamajicontrolplanes",
        "kamaji-control-plane",
        ResourceClass::Root,
        Some("Cluster"),
        false,
        true,
    ),
    template(
        "bootstrap.cluster.x-k8s.io/v1beta2",
        "KubeadmConfigTemplate",
        "kubeadmconfigtemplates",
        "kubeadm-config-template",
    ),
    template(
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevMachineTemplate",
        "devmachinetemplates",
        "dev-machine-template",
    ),
    dynamic(
        "cluster.x-k8s.io/v1beta2",
        "MachineDeployment",
        "machinedeployments",
        "machine-deployment",
        ResourceClass::Root,
        Some("Cluster"),
        true,
        true,
    ),
    dynamic(
        "cluster.x-k8s.io/v1beta2",
        "MachineSet",
        "machinesets",
        "machine",
        ResourceClass::Descendant,
        Some("MachineDeployment"),
        false,
        true,
    ),
    dynamic(
        "cluster.x-k8s.io/v1beta2",
        "Machine",
        "machines",
        "machine",
        ResourceClass::Descendant,
        Some("MachineSet"),
        false,
        true,
    ),
    dynamic(
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevMachine",
        "devmachines",
        "machine",
        ResourceClass::Descendant,
        Some("Machine"),
        false,
        true,
    ),
    dynamic(
        "bootstrap.cluster.x-k8s.io/v1beta2",
        "KubeadmConfig",
        "kubeadmconfigs",
        "machine",
        ResourceClass::Descendant,
        Some("Machine"),
        false,
        false,
    ),
    dynamic(
        "kamaji.clastix.io/v1alpha1",
        "TenantControlPlane",
        "tenantcontrolplanes",
        "provider",
        ResourceClass::Descendant,
        Some("KamajiControlPlane"),
        false,
        false,
    ),
    ManagementResource {
        api_version: "v1",
        kind: "Namespace",
        plural: "namespaces",
        namespaced: false,
        role: "namespace",
        class: ResourceClass::Typed,
        parent_kind: None,
        alternate_parent_kind: None,
        worker_suffix: false,
        watched: true,
        inventory_policy: InventoryPolicy::TenantMarkers,
        exemptions: &["management-infrastructure"],
    },
    ManagementResource {
        api_version: "v1",
        kind: "Secret",
        plural: "secrets",
        namespaced: true,
        role: "tenant-kubeconfig",
        class: ResourceClass::Typed,
        parent_kind: Some("KamajiControlPlane"),
        alternate_parent_kind: None,
        worker_suffix: false,
        watched: true,
        inventory_policy: InventoryPolicy::TenantMarkersOrKamajiOwner,
        exemptions: &["controller-installation-secrets"],
    },
    ManagementResource {
        api_version: "coordination.k8s.io/v1",
        kind: "Lease",
        plural: "leases",
        namespaced: true,
        role: "allocation-lease",
        class: ResourceClass::Typed,
        parent_kind: None,
        alternate_parent_kind: None,
        worker_suffix: false,
        watched: true,
        inventory_policy: InventoryPolicy::AllocationMarkers,
        exemptions: &["controller-leader-election"],
    },
];

pub fn roots() -> impl Iterator<Item = ManagementResource> {
    MANAGEMENT_RESOURCES
        .iter()
        .copied()
        .filter(|resource| resource.class == ResourceClass::Root)
}

pub fn descendants() -> impl Iterator<Item = ManagementResource> {
    MANAGEMENT_RESOURCES
        .iter()
        .copied()
        .filter(|resource| resource.class == ResourceClass::Descendant)
}

pub fn watched() -> impl Iterator<Item = ManagementResource> {
    MANAGEMENT_RESOURCES
        .iter()
        .copied()
        .filter(|resource| resource.watched)
}

pub fn activation_resources() -> impl Iterator<Item = ManagementResource> {
    MANAGEMENT_RESOURCES.iter().copied()
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;

    #[test]
    fn catalog_is_unique_and_covers_controller_operations() {
        let identities: BTreeSet<_> = MANAGEMENT_RESOURCES
            .iter()
            .map(|resource| (resource.api_version, resource.kind, resource.plural))
            .collect();
        assert_eq!(identities.len(), MANAGEMENT_RESOURCES.len());
        assert_eq!(
            roots().map(|resource| resource.kind).collect::<Vec<_>>(),
            [
                "Cluster",
                "DevCluster",
                "KamajiControlPlane",
                "KubeadmConfigTemplate",
                "DevMachineTemplate",
                "MachineDeployment",
            ]
        );
        assert_eq!(
            descendants()
                .filter(|resource| resource.role != "provider")
                .map(|resource| (resource.kind, resource.parent_kind))
                .collect::<Vec<_>>(),
            [
                ("MachineSet", Some("MachineDeployment")),
                ("Machine", Some("MachineSet")),
                ("DevMachine", Some("Machine")),
                ("KubeadmConfig", Some("Machine")),
            ]
        );
        for kind in ["Namespace", "Secret", "Lease"] {
            assert!(
                MANAGEMENT_RESOURCES
                    .iter()
                    .any(|resource| resource.kind == kind && resource.watched)
            );
        }
        const CREATED_ROOTS: &[&str] = &[
            "Cluster",
            "DevCluster",
            "KamajiControlPlane",
            "KubeadmConfigTemplate",
            "DevMachineTemplate",
            "MachineDeployment",
        ];
        const DYNAMIC_WATCHES: &[&str] = &[
            "Cluster",
            "DevCluster",
            "KamajiControlPlane",
            "KubeadmConfigTemplate",
            "DevMachineTemplate",
            "MachineDeployment",
            "MachineSet",
            "Machine",
            "DevMachine",
        ];
        const FINALIZED_DESCENDANTS: &[&str] =
            &["MachineSet", "Machine", "DevMachine", "KubeadmConfig"];
        assert_eq!(
            roots().map(|resource| resource.kind).collect::<Vec<_>>(),
            CREATED_ROOTS
        );
        assert_eq!(
            watched()
                .filter(|resource| resource.class != ResourceClass::Typed)
                .map(|resource| resource.kind)
                .collect::<Vec<_>>(),
            DYNAMIC_WATCHES
        );
        assert_eq!(
            descendants()
                .filter(|resource| resource.role != "provider")
                .map(|resource| resource.kind)
                .collect::<Vec<_>>(),
            FINALIZED_DESCENDANTS
        );
    }
}
