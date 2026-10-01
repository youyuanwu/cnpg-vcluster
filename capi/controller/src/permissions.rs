use k8s_openapi::api::rbac::v1::{ClusterRole, PolicyRule};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;

use crate::management::{
    AZURE_MANAGEMENT_RESOURCES, MANAGEMENT_RESOURCES, ManagementResource, ResourceClass,
};

pub const ROLE_NAME: &str = "tenant-controller-role";

const BASE_PERMISSIONS: &[(&str, &[&str], &[&str])] = &[
    (
        "",
        &["configmaps"],
        &[
            "create", "delete", "get", "list", "patch", "update", "watch",
        ],
    ),
    ("", &["events"], &["create", "patch", "update"]),
    (
        "rbac.authorization.k8s.io",
        &["roles", "rolebindings"],
        &["create", "get"],
    ),
    (
        "tenancy.cnpg-vcluster.io",
        &["tenantdatabasecatalogs"],
        &[
            "create", "delete", "get", "list", "patch", "update", "watch",
        ],
    ),
    (
        "tenancy.cnpg-vcluster.io",
        &["tenants"],
        &["get", "list", "patch", "update", "watch"],
    ),
    (
        "tenancy.cnpg-vcluster.io",
        &["tenants/finalizers"],
        &["update"],
    ),
    (
        "tenancy.cnpg-vcluster.io",
        &["tenants/status"],
        &["get", "patch", "update"],
    ),
];
const AZURE_EXTRA_PERMISSIONS: &[(&str, &[&str], &[&str])] =
    &[("cluster.x-k8s.io", &["clusters/status"], &["get", "patch"])];

fn rule(group: &str, resources: &[&str], verbs: &[&str]) -> PolicyRule {
    PolicyRule {
        api_groups: Some(vec![group.into()]),
        resources: Some(resources.iter().map(|value| (*value).into()).collect()),
        verbs: verbs.iter().map(|value| (*value).into()).collect(),
        ..Default::default()
    }
}

fn management_rule(resource: &ManagementResource) -> PolicyRule {
    let group = resource
        .api_version
        .split_once('/')
        .map_or("", |(group, _)| group);
    let mut verbs = match resource.kind {
        "Namespace" => vec!["create", "delete", "get", "list"],
        "Secret" => vec!["delete", "get", "list"],
        "Lease" => vec!["get", "list", "watch"],
        "KubeadmConfig" | "TenantControlPlane" if resource.class != ResourceClass::Root => {
            vec!["get", "list"]
        }
        _ if resource.class == ResourceClass::Root => {
            vec!["create", "delete", "get", "list", "patch"]
        }
        _ => vec!["create", "delete", "get", "list"],
    };
    if resource.watched && !verbs.contains(&"watch") {
        verbs.push("watch");
    }
    rule(group, &[resource.plural], &verbs)
}

fn azure_management_rule(resource: &ManagementResource) -> PolicyRule {
    if resource.class != ResourceClass::Descendant {
        return management_rule(resource);
    }
    let group = resource
        .api_version
        .split_once('/')
        .map_or("", |(group, _)| group);
    let mut verbs = vec!["get", "list"];
    if resource.kind == "Machine" {
        verbs.push("patch");
    }
    if resource.watched {
        verbs.push("watch");
    }
    rule(group, &[resource.plural], &verbs)
}

fn role(rules: Vec<PolicyRule>) -> ClusterRole {
    ClusterRole {
        metadata: ObjectMeta {
            name: Some(ROLE_NAME.into()),
            ..Default::default()
        },
        rules: Some(rules),
        ..Default::default()
    }
}

pub fn controller_role() -> ClusterRole {
    let rules = BASE_PERMISSIONS
        .iter()
        .map(|(group, resources, verbs)| rule(group, resources, verbs))
        .chain(
            MANAGEMENT_RESOURCES
                .iter()
                .filter(|resource| resource.kind != "Lease")
                .map(management_rule),
        )
        .collect();
    role(rules)
}

pub fn azure_controller_role() -> ClusterRole {
    let rules = BASE_PERMISSIONS
        .iter()
        .chain(AZURE_EXTRA_PERMISSIONS)
        .map(|(group, resources, verbs)| rule(group, resources, verbs))
        .chain(
            AZURE_MANAGEMENT_RESOURCES
                .iter()
                .filter(|resource| resource.kind != "Lease")
                .map(azure_management_rule),
        )
        .collect();
    role(rules)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_roles_grant_only_their_catalogued_resources() {
        let local = controller_role();
        let azure = azure_controller_role();
        assert_eq!(local.metadata.name.as_deref(), Some(ROLE_NAME));
        assert_eq!(azure.metadata.name.as_deref(), Some(ROLE_NAME));
        let local_rules = local.rules.unwrap();
        let azure_rules = azure.rules.unwrap();
        assert_eq!(
            local_rules.len(),
            BASE_PERMISSIONS.len() + MANAGEMENT_RESOURCES.len() - 1
        );
        assert_eq!(
            azure_rules.len(),
            BASE_PERMISSIONS.len()
                + AZURE_EXTRA_PERMISSIONS.len()
                + AZURE_MANAGEMENT_RESOURCES.len()
        );
        assert!(
            local_rules
                .iter()
                .chain(&azure_rules)
                .all(|rule| !rule.verbs.contains(&"*".to_string()))
        );
        for rules in [&local_rules, &azure_rules] {
            assert!(rules.iter().any(|rule| {
                rule.api_groups.as_deref() == Some(&["tenancy.cnpg-vcluster.io".into()])
                    && rule.resources.as_deref() == Some(&["tenantdatabasecatalogs".into()])
                    && rule.verbs.contains(&"get".into())
                    && rule.verbs.contains(&"update".into())
            }));
            assert!(rules.iter().all(|rule| {
                rule.resources.as_ref().is_none_or(|resources| {
                    !resources.iter().any(|resource| {
                        matches!(resource.as_str(), "tenantdatabases" | "resourcequotas")
                    })
                })
            }));
        }
        assert!(!azure_rules.iter().any(|rule| {
            rule.api_groups.as_deref() == Some(&["coordination.k8s.io".into()])
                && rule.resources.as_deref() == Some(&["leases".into()])
        }));
        for resource in MANAGEMENT_RESOURCES
            .iter()
            .filter(|resource| resource.kind != "Lease")
        {
            assert!(local_rules.contains(&management_rule(resource)));
        }
        for resource in AZURE_MANAGEMENT_RESOURCES
            .iter()
            .filter(|resource| resource.kind != "Lease")
        {
            assert!(azure_rules.contains(&azure_management_rule(resource)));
            if resource.class == ResourceClass::Descendant {
                let verbs = &azure_management_rule(resource).verbs;
                assert!(!verbs.contains(&"create".into()));
                assert!(!verbs.contains(&"delete".into()));
                assert_eq!(verbs.contains(&"patch".into()), resource.kind == "Machine");
            }
        }
        let azure_kubeadm = AZURE_MANAGEMENT_RESOURCES
            .iter()
            .find(|resource| resource.kind == "KubeadmConfig")
            .unwrap();
        assert!(
            azure_management_rule(azure_kubeadm)
                .verbs
                .contains(&"create".into())
        );
        assert!(azure_rules.iter().any(|rule| {
            rule.api_groups.as_deref() == Some(&["cluster.x-k8s.io".into()])
                && rule.resources.as_deref() == Some(&["clusters/status".into()])
                && rule.verbs == ["get", "patch"]
        }));
        for (kind, expected) in [
            (
                "Cluster",
                &["create", "delete", "get", "list", "patch", "watch"][..],
            ),
            ("Machine", &["create", "delete", "get", "list", "watch"][..]),
            ("KubeadmConfig", &["get", "list"][..]),
            (
                "Namespace",
                &["create", "delete", "get", "list", "watch"][..],
            ),
            ("Secret", &["delete", "get", "list", "watch"][..]),
            ("TenantControlPlane", &["get", "list"][..]),
        ] {
            let resource = MANAGEMENT_RESOURCES
                .iter()
                .find(|resource| resource.kind == kind)
                .unwrap();
            let rule = local_rules
                .iter()
                .find(|rule| {
                    rule.resources
                        .as_ref()
                        .is_some_and(|values| values == &vec![resource.plural.to_string()])
                })
                .unwrap();
            assert_eq!(rule.verbs, expected);
        }
        let mut kubeadm = crate::management::by_kind("KubeadmConfig").unwrap();
        assert!(!management_rule(&kubeadm).verbs.contains(&"watch".into()));
        kubeadm.watched = true;
        assert!(management_rule(&kubeadm).verbs.contains(&"watch".into()));
        assert!(!local_rules.iter().any(|rule| {
            rule.resources
                .as_ref()
                .is_some_and(|resources| resources.iter().any(|value| value == "azureclusters"))
        }));
        assert!(!azure_rules.iter().any(|rule| {
            rule.resources.as_ref().is_some_and(|resources| {
                resources
                    .iter()
                    .any(|value| matches!(value.as_str(), "devclusters" | "devmachines"))
            })
        }));
    }
}
