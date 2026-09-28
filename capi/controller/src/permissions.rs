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
        "Lease" => vec!["create", "delete", "get", "list", "patch", "update"],
        "KubeadmConfig" | "TenantControlPlane" => vec!["get", "list"],
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
    if resource.watched {
        verbs.push("watch");
    }
    rule(group, &[resource.plural], &verbs)
}

pub fn controller_role() -> ClusterRole {
    let rules = BASE_PERMISSIONS
        .iter()
        .map(|(group, resources, verbs)| rule(group, resources, verbs))
        .chain(MANAGEMENT_RESOURCES.iter().map(management_rule))
        .chain(AZURE_MANAGEMENT_RESOURCES.iter().map(azure_management_rule))
        .collect();
    ClusterRole {
        metadata: ObjectMeta {
            name: Some(ROLE_NAME.into()),
            ..Default::default()
        },
        rules: Some(rules),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn role_grants_catalogued_resources_without_wildcards() {
        let role = controller_role();
        assert_eq!(role.metadata.name.as_deref(), Some(ROLE_NAME));
        let rules = role.rules.unwrap();
        assert_eq!(
            rules.len(),
            BASE_PERMISSIONS.len() + MANAGEMENT_RESOURCES.len() + AZURE_MANAGEMENT_RESOURCES.len()
        );
        assert!(
            rules
                .iter()
                .all(|rule| !rule.verbs.contains(&"*".to_string()))
        );
        for resource in MANAGEMENT_RESOURCES {
            let rule = rules
                .iter()
                .find(|rule| {
                    rule.resources
                        .as_ref()
                        .is_some_and(|values| values == &vec![resource.plural.to_string()])
                })
                .unwrap();
            assert!(rule.verbs.contains(&"get".into()));
            assert!(rule.verbs.contains(&"list".into()));
            assert_eq!(rule.verbs.contains(&"watch".into()), resource.watched);
        }
        for resource in AZURE_MANAGEMENT_RESOURCES {
            assert!(rules.contains(&azure_management_rule(resource)));
            if resource.class == ResourceClass::Descendant {
                let verbs = &azure_management_rule(resource).verbs;
                assert!(!verbs.contains(&"create".into()));
                assert!(!verbs.contains(&"delete".into()));
                assert!(!verbs.contains(&"patch".into()));
            }
        }
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
            (
                "Lease",
                &[
                    "create", "delete", "get", "list", "patch", "update", "watch",
                ][..],
            ),
            ("TenantControlPlane", &["get", "list"][..]),
        ] {
            let resource = MANAGEMENT_RESOURCES
                .iter()
                .find(|resource| resource.kind == kind)
                .unwrap();
            let rule = rules
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
    }
}
