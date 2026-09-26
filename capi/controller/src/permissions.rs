use k8s_openapi::api::rbac::v1::{ClusterRole, PolicyRule};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;

use crate::management::{MANAGEMENT_RESOURCES, ResourceClass};

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

fn management_rule(resource: &crate::management::ManagementResource) -> PolicyRule {
    let group = resource
        .api_version
        .split_once('/')
        .map_or("", |(group, _)| group);
    let verbs: &[&str] = match resource.kind {
        "Namespace" => &["create", "delete", "get", "list", "watch"],
        "Secret" => &["delete", "get", "list", "watch"],
        "Lease" => &[
            "create", "delete", "get", "list", "patch", "update", "watch",
        ],
        "KubeadmConfig" | "TenantControlPlane" => &["get", "list"],
        _ if resource.class == ResourceClass::Root => {
            &["create", "delete", "get", "list", "patch", "watch"]
        }
        _ => &["create", "delete", "get", "list", "watch"],
    };
    rule(group, &[resource.plural], verbs)
}

pub fn controller_role() -> ClusterRole {
    let rules = BASE_PERMISSIONS
        .iter()
        .map(|(group, resources, verbs)| rule(group, resources, verbs))
        .chain(MANAGEMENT_RESOURCES.iter().map(management_rule))
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
            BASE_PERMISSIONS.len() + MANAGEMENT_RESOURCES.len()
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
        }
    }
}
