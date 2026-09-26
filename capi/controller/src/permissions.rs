use k8s_openapi::api::rbac::v1::{ClusterRole, PolicyRule};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;

pub const ROLE_NAME: &str = "tenant-controller-role";

struct Permission {
    group: &'static str,
    resources: &'static [&'static str],
    verbs: &'static [&'static str],
}

const PERMISSIONS: &[Permission] = &[
    Permission {
        group: "",
        resources: &["configmaps"],
        verbs: &[
            "create", "delete", "get", "list", "patch", "update", "watch",
        ],
    },
    Permission {
        group: "",
        resources: &["events"],
        verbs: &["create", "patch", "update"],
    },
    Permission {
        group: "",
        resources: &["namespaces"],
        verbs: &["create", "delete", "get", "list", "watch"],
    },
    Permission {
        group: "",
        resources: &["secrets"],
        verbs: &["delete", "get", "list", "watch"],
    },
    Permission {
        group: "bootstrap.cluster.x-k8s.io",
        resources: &["kubeadmconfigs"],
        verbs: &["get", "list"],
    },
    Permission {
        group: "bootstrap.cluster.x-k8s.io",
        resources: &["kubeadmconfigtemplates"],
        verbs: &["create", "delete", "get", "list", "patch", "watch"],
    },
    Permission {
        group: "cluster.x-k8s.io",
        resources: &["clusters", "machinedeployments"],
        verbs: &["create", "delete", "get", "list", "patch", "watch"],
    },
    Permission {
        group: "cluster.x-k8s.io",
        resources: &["machines", "machinesets"],
        verbs: &["create", "delete", "get", "list", "watch"],
    },
    Permission {
        group: "controlplane.cluster.x-k8s.io",
        resources: &["kamajicontrolplanes"],
        verbs: &["create", "delete", "get", "list", "patch", "watch"],
    },
    Permission {
        group: "coordination.k8s.io",
        resources: &["leases"],
        verbs: &[
            "create", "delete", "get", "list", "patch", "update", "watch",
        ],
    },
    Permission {
        group: "infrastructure.cluster.x-k8s.io",
        resources: &["devclusters", "devmachinetemplates"],
        verbs: &["create", "delete", "get", "list", "patch", "watch"],
    },
    Permission {
        group: "infrastructure.cluster.x-k8s.io",
        resources: &["devmachines"],
        verbs: &["create", "delete", "get", "list", "watch"],
    },
    Permission {
        group: "tenancy.cnpg-vcluster.io",
        resources: &["tenants"],
        verbs: &["get", "list", "patch", "update", "watch"],
    },
    Permission {
        group: "tenancy.cnpg-vcluster.io",
        resources: &["tenants/finalizers"],
        verbs: &["update"],
    },
    Permission {
        group: "tenancy.cnpg-vcluster.io",
        resources: &["tenants/status"],
        verbs: &["get", "patch", "update"],
    },
];

pub fn controller_role() -> ClusterRole {
    ClusterRole {
        metadata: ObjectMeta {
            name: Some(ROLE_NAME.into()),
            ..Default::default()
        },
        rules: Some(
            PERMISSIONS
                .iter()
                .map(|permission| PolicyRule {
                    api_groups: Some(vec![permission.group.into()]),
                    resources: Some(permission.resources.iter().map(|s| (*s).into()).collect()),
                    verbs: permission.verbs.iter().map(|s| (*s).into()).collect(),
                    ..Default::default()
                })
                .collect(),
        ),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn role_grants_exact_controller_permissions_without_wildcards() {
        let role = controller_role();
        assert_eq!(role.metadata.name.as_deref(), Some(ROLE_NAME));
        let rules = role.rules.unwrap();
        assert_eq!(rules.len(), 15);
        assert!(rules.iter().all(|r| !r.verbs.contains(&"*".to_string())));
        let lease = rules
            .iter()
            .find(|r| r.resources.as_ref().unwrap() == &["leases"])
            .unwrap();
        assert_eq!(lease.api_groups.as_ref().unwrap(), &["coordination.k8s.io"]);
        assert_eq!(
            lease.verbs,
            [
                "create", "delete", "get", "list", "patch", "update", "watch"
            ]
        );
        let status = rules
            .iter()
            .find(|r| r.resources.as_ref().unwrap() == &["tenants/status"])
            .unwrap();
        assert_eq!(status.verbs, ["get", "patch", "update"]);
        let tenants = rules
            .iter()
            .find(|r| r.resources.as_ref().unwrap() == &["tenants"])
            .unwrap();
        assert_eq!(tenants.verbs, ["get", "list", "patch", "update", "watch"]);
        let finalizers = rules
            .iter()
            .find(|r| r.resources.as_ref().unwrap() == &["tenants/finalizers"])
            .unwrap();
        assert_eq!(finalizers.verbs, ["update"]);
    }
}
