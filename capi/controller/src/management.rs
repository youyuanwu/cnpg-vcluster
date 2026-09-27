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
#[serde(rename_all = "kebab-case")]
pub enum NamePolicy {
    Tenant,
    Worker,
    Kubeconfig,
    Observed,
    Allocation,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum EvidencePolicy {
    Named,
    Observed,
    Allocation,
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
    pub name_policy: NamePolicy,
    pub watched: bool,
    pub watch_name_suffix: Option<&'static str>,
    pub watch_cluster_label: Option<&'static str>,
    pub inventory_policy: InventoryPolicy,
    pub inventory_namespace: Option<&'static str>,
    pub evidence_policy: EvidencePolicy,
    pub exemptions: &'static [&'static str],
}

impl ManagementResource {
    pub fn api_resource(self) -> ApiResource {
        let (group, version) = self
            .api_version
            .split_once('/')
            .unwrap_or(("", self.api_version));
        let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(group, version, self.kind));
        resource.plural = self.plural.into();
        resource
    }

    #[rustfmt::skip]
    pub fn inventory_path(self) -> String {
        let (group, version) = self.api_version.split_once('/').unwrap_or(("", self.api_version));
        let base = if group.is_empty() { format!("/api/{version}") } else { format!("/apis/{group}/{version}") };
        self.inventory_namespace.map_or_else(|| format!("{base}/{}", self.plural), |namespace| format!("{base}/namespaces/{namespace}/{}", self.plural))
    }

    pub fn expected_name(self, tenant: &str) -> Option<String> {
        match self.name_policy {
            NamePolicy::Tenant => Some(tenant.into()),
            NamePolicy::Worker => Some(format!("{tenant}-worker")),
            NamePolicy::Kubeconfig => Some(format!("{tenant}-kubeconfig")),
            NamePolicy::Observed | NamePolicy::Allocation => None,
        }
    }

    pub fn exempts_unmarked(self) -> bool {
        matches!(
            (self.kind, self.inventory_policy, self.exemptions),
            (
                "Namespace",
                InventoryPolicy::TenantMarkers,
                ["management-infrastructure"]
            ) | (
                "Secret",
                InventoryPolicy::TenantMarkersOrKamajiOwner,
                ["controller-installation-secrets"]
            ) | (
                "Lease",
                InventoryPolicy::AllocationMarkers,
                ["controller-leader-election"]
            )
        )
    }

    #[rustfmt::skip]
    pub fn valid_inventory_contract(self) -> bool {
        if self.inventory_policy == InventoryPolicy::BlockAnyInstance { self.exemptions.is_empty() } else { self.exempts_unmarked() }
    }
}

macro_rules! entry {
    ($api:expr, $kind:expr, $plural:expr, $role:expr, $class:ident, $parent:expr,
     $alternate:expr, $name:ident, $watched:expr) => {
        ManagementResource {
            api_version: $api,
            kind: $kind,
            plural: $plural,
            namespaced: true,
            role: $role,
            class: ResourceClass::$class,
            parent_kind: $parent,
            alternate_parent_kind: $alternate,
            name_policy: NamePolicy::$name,
            watched: $watched,
            watch_name_suffix: None,
            watch_cluster_label: if $watched {
                Some("cluster.x-k8s.io/cluster-name")
            } else {
                None
            },
            inventory_policy: InventoryPolicy::BlockAnyInstance,
            inventory_namespace: None,
            evidence_policy: if matches!(ResourceClass::$class, ResourceClass::Root) {
                EvidencePolicy::Named
            } else {
                EvidencePolicy::Observed
            },
            exemptions: &[],
        }
    };
}

macro_rules! root {
    ($api:expr, $kind:expr, $plural:expr, $role:expr, $parent:expr, $name:ident) => {
        entry!(
            $api, $kind, $plural, $role, Root, $parent, None, $name, true
        )
    };
}

macro_rules! descendant {
    ($api:expr, $kind:expr, $plural:expr, $role:expr, $parent:expr, $watched:expr) => {
        entry!(
            $api,
            $kind,
            $plural,
            $role,
            Descendant,
            Some($parent),
            None,
            Observed,
            $watched
        )
    };
}

macro_rules! typed {
    ($api:expr, $kind:expr, $plural:expr, $namespaced:expr, $role:expr, $parent:expr,
     $name:ident, $suffix:expr, $policy:ident, $namespace:expr, $evidence:ident, $exemption:expr) => {
        ManagementResource {
            api_version: $api,
            kind: $kind,
            plural: $plural,
            namespaced: $namespaced,
            role: $role,
            class: ResourceClass::Typed,
            parent_kind: $parent,
            alternate_parent_kind: None,
            name_policy: NamePolicy::$name,
            watched: true,
            watch_name_suffix: $suffix,
            watch_cluster_label: None,
            inventory_policy: InventoryPolicy::$policy,
            inventory_namespace: $namespace,
            evidence_policy: EvidencePolicy::$evidence,
            exemptions: &[$exemption],
        }
    };
}

#[rustfmt::skip]
pub const MANAGEMENT_RESOURCES: &[ManagementResource] = &[
    root!("cluster.x-k8s.io/v1beta2", "Cluster", "clusters", "cluster", None, Tenant),
    root!("infrastructure.cluster.x-k8s.io/v1beta2", "DevCluster", "devclusters", "dev-cluster", Some("Cluster"), Tenant),
    root!("controlplane.cluster.x-k8s.io/v1alpha2", "KamajiControlPlane", "kamajicontrolplanes", "kamaji-control-plane", Some("Cluster"), Tenant),
    entry!("bootstrap.cluster.x-k8s.io/v1beta2", "KubeadmConfigTemplate", "kubeadmconfigtemplates", "kubeadm-config-template", Root, Some("Cluster"), Some("MachineDeployment"), Worker, true),
    entry!("infrastructure.cluster.x-k8s.io/v1beta2", "DevMachineTemplate", "devmachinetemplates", "dev-machine-template", Root, Some("Cluster"), Some("MachineDeployment"), Worker, true),
    root!("cluster.x-k8s.io/v1beta2", "MachineDeployment", "machinedeployments", "machine-deployment", Some("Cluster"), Worker),
    descendant!("cluster.x-k8s.io/v1beta2", "MachineSet", "machinesets", "machine", "MachineDeployment", true),
    descendant!("cluster.x-k8s.io/v1beta2", "Machine", "machines", "machine", "MachineSet", true),
    descendant!("infrastructure.cluster.x-k8s.io/v1beta2", "DevMachine", "devmachines", "machine", "Machine", true),
    descendant!("bootstrap.cluster.x-k8s.io/v1beta2", "KubeadmConfig", "kubeadmconfigs", "machine", "Machine", false),
    descendant!("kamaji.clastix.io/v1alpha1", "TenantControlPlane", "tenantcontrolplanes", "provider", "KamajiControlPlane", false),
    typed!("v1", "Namespace", "namespaces", false, "namespace", None, Tenant, None, TenantMarkers, None, Named, "management-infrastructure"),
    typed!("v1", "Secret", "secrets", true, "tenant-kubeconfig", Some("KamajiControlPlane"), Kubeconfig, Some("-kubeconfig"), TenantMarkersOrKamajiOwner, None, Named, "controller-installation-secrets"),
    typed!("coordination.k8s.io/v1", "Lease", "leases", true, "allocation-lease", None, Allocation, None, AllocationMarkers, Some("tenant-system"), Allocation, "controller-leader-election"),
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

pub fn by_kind(kind: &str) -> Option<ManagementResource> {
    MANAGEMENT_RESOURCES
        .iter()
        .copied()
        .find(|resource| resource.kind == kind)
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
            watched().map(|resource| resource.kind).collect::<Vec<_>>(),
            [
                "Cluster",
                "DevCluster",
                "KamajiControlPlane",
                "KubeadmConfigTemplate",
                "DevMachineTemplate",
                "MachineDeployment",
                "MachineSet",
                "Machine",
                "DevMachine",
                "Namespace",
                "Secret",
                "Lease",
            ]
        );
        assert_eq!(
            watched()
                .filter(|resource| resource.class != ResourceClass::Typed)
                .map(|resource| resource.kind)
                .collect::<Vec<_>>(),
            DYNAMIC_WATCHES
        );
        assert_eq!(
            MANAGEMENT_RESOURCES
                .iter()
                .find(|resource| resource.kind == "Secret")
                .and_then(|resource| resource.expected_name("tenant-a"))
                .as_deref(),
            Some("tenant-a-kubeconfig")
        );
        assert_eq!(
            MANAGEMENT_RESOURCES
                .iter()
                .find(|resource| resource.kind == "Machine")
                .and_then(|resource| resource.expected_name("tenant-a")),
            None
        );
        let namespace = MANAGEMENT_RESOURCES
            .iter()
            .find(|resource| resource.kind == "Namespace")
            .unwrap();
        let namespace_api = namespace.api_resource();
        assert_eq!(namespace_api.group, "");
        assert_eq!(namespace_api.version, "v1");
        assert_eq!(namespace_api.plural, "namespaces");
        let lease = MANAGEMENT_RESOURCES
            .iter()
            .find(|resource| resource.kind == "Lease")
            .unwrap();
        assert_eq!(lease.inventory_namespace, Some("tenant-system"));
        let mut invalid = by_kind("Secret").unwrap();
        invalid.inventory_policy = InventoryPolicy::TenantMarkers;
        assert!(!invalid.valid_inventory_contract());
        assert_eq!(
            descendants()
                .filter(|resource| resource.role != "provider")
                .map(|resource| resource.kind)
                .collect::<Vec<_>>(),
            FINALIZED_DESCENDANTS
        );
    }
}
