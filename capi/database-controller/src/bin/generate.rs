use std::path::PathBuf;

use serde_json::{Value, json};
use tenant_database_controller::api::catalog_crd;

type Generated = Vec<(&'static str, Vec<u8>)>;

fn yaml<T: serde::Serialize>(resource: &T) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    Ok(format!("---\n{}", serde_yaml::to_string(resource)?).into_bytes())
}

fn rule(group: &str, resources: &[&str], verbs: &[&str]) -> Value {
    json!({"apiGroups": [group], "resources": resources, "verbs": verbs})
}

fn inventory(azure: bool) -> Value {
    let mut resources = vec![
        json!({"group": "tenancy.cnpg-vcluster.io", "version": "v1alpha1", "kind": "TenantDatabaseCatalog", "resource": "tenantdatabasecatalogs", "scope": "Namespaced"}),
        json!({"group": "tenancy.cnpg-vcluster.io", "version": "v1alpha4", "kind": "Tenant", "resource": "tenants", "scope": "Cluster"}),
        json!({"group": "", "version": "v1", "kind": "Namespace", "resource": "namespaces", "scope": "Cluster"}),
    ];
    if azure {
        resources.push(json!({"group": "compute.azure.com", "version": "v1api20240302", "kind": "Disk", "resource": "disks", "scope": "Namespaced"}));
    }
    Value::Array(resources)
}

fn controller_rules(azure: bool) -> Vec<Value> {
    let mut rules = vec![
        rule(
            "tenancy.cnpg-vcluster.io",
            &["tenantdatabasecatalogs"],
            &["get", "list", "watch", "update"],
        ),
        rule(
            "tenancy.cnpg-vcluster.io",
            &["tenantdatabasecatalogs/status"],
            &["get", "patch", "update"],
        ),
        rule(
            "tenancy.cnpg-vcluster.io",
            &["tenants"],
            &["get", "list", "watch"],
        ),
        rule("", &["namespaces"], &["get", "list", "watch"]),
    ];
    if azure {
        rules.push(rule(
            "compute.azure.com",
            &["disks"],
            &["get", "list", "watch"],
        ));
    }
    rules
}

fn generated_files() -> Result<Generated, Box<dyn std::error::Error>> {
    let mut resources = vec![
        (
            "crd/bases/tenancy.cnpg-vcluster.io_tenantdatabasecatalogs.yaml",
            yaml(&catalog_crd())?,
        ),
        (
            "management-resources.json",
            format!("{}\n", serde_json::to_string_pretty(&inventory(false))?).into_bytes(),
        ),
        (
            "azure-management-resources.json",
            format!("{}\n", serde_json::to_string_pretty(&inventory(true))?).into_bytes(),
        ),
        (
            "deployment/controller.yaml",
            yaml(&json!({
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "database-controller", "namespace": "tenant-system"},
                "spec": {
                    "replicas": 1, "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": {"app": "database-controller"}},
                    "template": {
                        "metadata": {"labels": {"app": "database-controller"}},
                        "spec": {
                            "serviceAccountName": "database-controller",
                            "automountServiceAccountToken": true,
                            "containers": [{
                                "name": "manager",
                                "image": "database-controller:configure-before-install",
                                "imagePullPolicy": "IfNotPresent",
                                "env": [{
                                    "name": "POD_UID",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}
                                }],
                                "ports": [{"name": "health", "containerPort": 8082}],
                                "livenessProbe": {"httpGet": {"path": "/healthz", "port": "health"}},
                                "readinessProbe": {"httpGet": {"path": "/readyz", "port": "health"}},
                                "securityContext": {
                                    "allowPrivilegeEscalation": false,
                                    "readOnlyRootFilesystem": true,
                                    "runAsNonRoot": true,
                                    "capabilities": {"drop": ["ALL"]}
                                }
                            }]
                        }
                    }
                }
            }))?,
        ),
    ];
    for (path, resource) in [
        (
            "rbac/controller-service-account.yaml",
            json!({"apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": "database-controller", "namespace": "tenant-system"}}),
        ),
        (
            "rbac/controller-cluster-role.yaml",
            json!({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                "metadata": {"name": "database-controller"}, "rules": controller_rules(false)}),
        ),
        (
            "rbac/controller-cluster-role-azure.yaml",
            json!({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                "metadata": {"name": "database-controller"}, "rules": controller_rules(true)}),
        ),
        (
            "rbac/controller-cluster-binding.yaml",
            json!({
                "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                "metadata": {"name": "database-controller"},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole", "name": "database-controller"},
                "subjects": [{"kind": "ServiceAccount", "namespace": "tenant-system",
                    "name": "database-controller"}]
            }),
        ),
    ] {
        resources.push((path, yaml(&resource)?));
    }
    Ok(resources)
}

fn run(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let mut output_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("config");
    let mut check = false;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--output-dir" => output_dir = PathBuf::from(rest.next().ok_or("missing directory")?),
            "--check" if !check => check = true,
            _ => return Err("unknown or repeated generator argument".into()),
        }
    }
    let expected = generated_files()?;
    if check {
        for (relative, contents) in &expected {
            let path = output_dir.join(relative);
            if std::fs::read(&path)? != *contents {
                return Err(format!("generated fixture differs: {}", path.display()).into());
            }
        }
        for dir in ["admission", "rbac", "crd/bases", "deployment"] {
            let path = output_dir.join(dir);
            if path.exists() {
                for artifact in std::fs::read_dir(path)? {
                    let artifact = artifact?.path();
                    let relative = artifact
                        .strip_prefix(&output_dir)?
                        .to_str()
                        .ok_or("invalid path")?;
                    if !expected.iter().any(|(name, _)| *name == relative) {
                        return Err(
                            format!("obsolete generated fixture: {}", artifact.display()).into(),
                        );
                    }
                }
            }
        }
    } else {
        for (relative, contents) in &expected {
            let path = output_dir.join(relative);
            std::fs::create_dir_all(path.parent().ok_or("invalid output path")?)?;
            std::fs::write(path, contents)?;
        }
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    run(&std::env::args().skip(1).collect::<Vec<_>>())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn find<'a>(artifacts: &'a Generated, name: &str) -> &'a [u8] {
        &artifacts.iter().find(|(path, _)| *path == name).unwrap().1
    }

    #[test]
    fn generated_contracts_are_current() {
        run(&["--check".into()]).unwrap();
    }

    #[test]
    fn inventories_and_roles_are_exact_and_provider_specific() {
        let artifacts = generated_files().unwrap();
        let local: Value =
            serde_json::from_slice(find(&artifacts, "management-resources.json")).unwrap();
        let azure: Value =
            serde_json::from_slice(find(&artifacts, "azure-management-resources.json")).unwrap();
        assert_eq!(local.as_array().unwrap().len(), 3);
        assert_eq!(azure.as_array().unwrap().len(), 4);
        assert_eq!(azure[3]["resource"], "disks");
        assert!(
            local
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["resource"] == "tenantdatabasecatalogs" && r["scope"] == "Namespaced")
        );
        for artifact in [&local, &azure] {
            assert!(artifact.as_array().unwrap().iter().all(|r| {
                !["tenantdatabases", "configmaps", "resourcequotas"]
                    .contains(&r["resource"].as_str().unwrap())
            }));
        }
        let local_role: Value =
            serde_yaml::from_slice(find(&artifacts, "rbac/controller-cluster-role.yaml")).unwrap();
        let azure_role: Value =
            serde_yaml::from_slice(find(&artifacts, "rbac/controller-cluster-role-azure.yaml"))
                .unwrap();
        for role in [&local_role, &azure_role] {
            let rules = role["rules"].as_array().unwrap();
            assert_eq!(rules[0]["resources"], json!(["tenantdatabasecatalogs"]));
            assert_eq!(rules[0]["verbs"], json!(["get", "list", "watch", "update"]));
            assert_eq!(
                rules[1]["resources"],
                json!(["tenantdatabasecatalogs/status"])
            );
            assert!(
                rules.iter().all(
                    |rule| !rule["resources"].as_array().unwrap().iter().any(|r| [
                        "tenantdatabases",
                        "resourcequotas",
                        "configmaps",
                        "tenantdatabasecatalogs/finalizers"
                    ]
                    .contains(&r.as_str().unwrap()))
                )
            );
            assert!(rules.iter().all(|rule| rule["resources"]
                != json!(["tenantdatabasecatalogs"])
                || !rule["verbs"].as_array().unwrap().contains(&json!("create"))));
        }
        assert!(local_role["rules"].as_array().unwrap().iter().all(|r| {
            ![
                "persistentvolumes",
                "persistentvolumeclaims",
                "secrets",
                "pods",
                "clusters",
            ]
            .contains(&r["resources"][0].as_str().unwrap())
        }));
        assert!(
            azure_role["rules"]
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["resources"] == json!(["disks"])
                    && r["verbs"] == json!(["get", "list", "watch"]))
        );
        assert!(
            artifacts
                .iter()
                .all(|(name, _)| !name.starts_with("admission/") && !name.contains("gates"))
        );
        let deployment: Value =
            serde_yaml::from_slice(find(&artifacts, "deployment/controller.yaml")).unwrap();
        assert_eq!(
            deployment["spec"]["template"]["spec"]["serviceAccountName"],
            "database-controller"
        );
        assert_eq!(
            deployment["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"],
            "/readyz"
        );
        assert_eq!(
            deployment["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"]["fieldRef"]
                ["fieldPath"],
            "metadata.uid"
        );
    }
}
