use std::path::PathBuf;

use serde_json::{Value, json};
use tenant_database_controller::{admission::GATE_NAMESPACE, api::database_crd};

type Generated = Vec<(&'static str, Vec<u8>)>;

fn yaml<T: serde::Serialize>(resource: &T) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    Ok(format!("---\n{}", serde_yaml::to_string(resource)?).into_bytes())
}

fn rule(group: &str, resources: &[&str], verbs: &[&str]) -> Value {
    json!({"apiGroups": [group], "resources": resources, "verbs": verbs})
}

fn role(name: &str, namespace: &str, rules: Vec<Value>) -> Value {
    json!({
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": name, "namespace": namespace},
        "rules": rules,
    })
}

fn binding(name: &str, namespace: &str, role: &str, service_account: &str) -> Value {
    json!({
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role},
        "subjects": [{"kind": "ServiceAccount", "name": service_account, "namespace": "tenant-system"}],
    })
}

fn namespace(name: &str) -> Value {
    json!({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}})
}

fn admission_webhook(name: &str, path: &str, mutating: bool) -> Value {
    let mut webhook = json!({
        "name": name,
        "admissionReviewVersions": ["v1"],
        "failurePolicy": "Fail",
        "matchPolicy": "Exact",
        "sideEffects": "NoneOnDryRun",
        "timeoutSeconds": 30,
        "clientConfig": {"service": {
            "name": "database-admission", "namespace": "tenant-system",
            "path": path, "port": 443
        }},
        "rules": [{
            "apiGroups": ["tenancy.cnpg-vcluster.io"],
            "apiVersions": ["v1alpha1"],
            "operations": ["CREATE"],
            "resources": ["tenantdatabases"],
            "scope": "Namespaced"
        }]
    });
    if mutating {
        webhook["reinvocationPolicy"] = json!("Never");
    }
    webhook
}

fn generated_files() -> Result<Generated, Box<dyn std::error::Error>> {
    let management_resources = json!([
        {"group": "tenancy.cnpg-vcluster.io", "version": "v1alpha1", "kind": "TenantDatabase", "resource": "tenantdatabases", "scope": "Namespaced"},
        {"group": "tenancy.cnpg-vcluster.io", "version": "v1alpha4", "kind": "Tenant", "resource": "tenants", "scope": "Cluster"},
        {"group": "", "version": "v1", "kind": "Namespace", "resource": "namespaces", "scope": "Cluster"},
        {"group": "", "version": "v1", "kind": "ResourceQuota", "resource": "resourcequotas", "scope": "Namespaced"},
        {"group": "", "version": "v1", "kind": "ConfigMap", "resource": "configmaps", "scope": "Namespaced"}
    ]);
    let mut azure_management_resources = management_resources.as_array().unwrap().clone();
    azure_management_resources.push(json!({
        "group": "compute.azure.com", "version": "v1api20240302",
        "kind": "Disk", "resource": "disks", "scope": "Namespaced"
    }));
    let mut resources = vec![
        (
            "crd/bases/tenancy.cnpg-vcluster.io_tenantdatabases.yaml",
            yaml(&database_crd())?,
        ),
        (
            "management-resources.json",
            format!("{}\n", serde_json::to_string_pretty(&management_resources)?).into_bytes(),
        ),
        (
            "azure-management-resources.json",
            format!(
                "{}\n",
                serde_json::to_string_pretty(&azure_management_resources)?
            )
            .into_bytes(),
        ),
    ];
    for (name, resource) in [
        ("rbac/gates-namespace.yaml", namespace(GATE_NAMESPACE)),
        (
            "rbac/admission-service-account.yaml",
            json!({"apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": "database-admission", "namespace": "tenant-system"}}),
        ),
        (
            "rbac/controller-service-account.yaml",
            json!({"apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": "database-controller", "namespace": "tenant-system"}}),
        ),
        (
            "rbac/admission-cluster-role.yaml",
            json!({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {"name": "database-admission"},
            "rules": [
                rule("tenancy.cnpg-vcluster.io", &["tenants"], &["get"]),
                rule("", &["namespaces"], &["get"]),
                rule("", &["resourcequotas"], &["get"])
            ]}),
        ),
        (
            "rbac/controller-cluster-role.yaml",
            json!({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {"name": "database-controller"},
            "rules": [
                rule("tenancy.cnpg-vcluster.io", &["tenantdatabases"], &["get", "list", "watch", "patch", "update"]),
                rule("tenancy.cnpg-vcluster.io", &["tenantdatabases/status"], &["get", "patch", "update"]),
                rule("tenancy.cnpg-vcluster.io", &["tenantdatabases/finalizers"], &["update"]),
                rule("tenancy.cnpg-vcluster.io", &["tenants"], &["get", "list", "watch"])
            ]}),
        ),
        (
            "rbac/controller-cluster-role-azure.yaml",
            json!({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
            "metadata": {"name": "database-controller"},
            "rules": [
                rule("tenancy.cnpg-vcluster.io", &["tenantdatabases"], &["get", "list", "watch", "patch", "update"]),
                rule("tenancy.cnpg-vcluster.io", &["tenantdatabases/status"], &["get", "patch", "update"]),
                rule("tenancy.cnpg-vcluster.io", &["tenantdatabases/finalizers"], &["update"]),
                rule("tenancy.cnpg-vcluster.io", &["tenants"], &["get", "list", "watch"])
            ]}),
        ),
        (
            "rbac/admission-gates-role.yaml",
            role(
                "database-admission",
                GATE_NAMESPACE,
                vec![rule("", &["configmaps"], &["get", "update", "patch"])],
            ),
        ),
        (
            "rbac/controller-gates-role.yaml",
            role(
                "database-controller",
                GATE_NAMESPACE,
                vec![rule("", &["configmaps"], &["get", "update", "patch"])],
            ),
        ),
        (
            "rbac/tenant-gates-role.yaml",
            role(
                "tenant-controller-database-gates",
                GATE_NAMESPACE,
                vec![rule(
                    "",
                    &["configmaps"],
                    &["create", "get", "update", "patch", "delete"],
                )],
            ),
        ),
        (
            "rbac/admission-gates-binding.yaml",
            binding(
                "database-admission",
                GATE_NAMESPACE,
                "database-admission",
                "database-admission",
            ),
        ),
        (
            "rbac/controller-gates-binding.yaml",
            binding(
                "database-controller",
                GATE_NAMESPACE,
                "database-controller",
                "database-controller",
            ),
        ),
        (
            "rbac/tenant-gates-binding.yaml",
            binding(
                "tenant-controller-database-gates",
                GATE_NAMESPACE,
                "tenant-controller-database-gates",
                "tenant-controller",
            ),
        ),
        (
            "admission/issuer.yaml",
            json!({"apiVersion": "cert-manager.io/v1", "kind": "Issuer",
                "metadata": {"name": "database-admission-selfsigned", "namespace": "tenant-system"},
                "spec": {"selfSigned": {}}}),
        ),
        (
            "admission/ca-certificate.yaml",
            json!({"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
            "metadata": {"name": "database-admission-ca", "namespace": "tenant-system"},
            "spec": {
                "isCA": true, "commonName": "database-admission-ca",
                "secretName": "database-admission-ca",
                "issuerRef": {"name": "database-admission-selfsigned", "kind": "Issuer"}
            }}),
        ),
        (
            "admission/ca-issuer.yaml",
            json!({"apiVersion": "cert-manager.io/v1", "kind": "Issuer",
                "metadata": {"name": "database-admission-ca", "namespace": "tenant-system"},
                "spec": {"ca": {"secretName": "database-admission-ca"}}}),
        ),
        (
            "admission/serving-certificate.yaml",
            json!({"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
            "metadata": {"name": "database-admission-serving", "namespace": "tenant-system"},
            "spec": {
                "secretName": "database-admission-serving",
                "dnsNames": ["database-admission.tenant-system.svc",
                    "database-admission.tenant-system.svc.cluster.local"],
                "issuerRef": {"name": "database-admission-ca", "kind": "Issuer"}
            }}),
        ),
        (
            "admission/service.yaml",
            json!({"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "database-admission", "namespace": "tenant-system"},
            "spec": {
                "selector": {"app.kubernetes.io/name": "database-admission"},
                "ports": [{"name": "https", "port": 443, "targetPort": 9443}]
            }}),
        ),
        (
            "admission/mutating-webhook.yaml",
            json!({"apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": "database-admission",
                "annotations": {"cert-manager.io/inject-ca-from": "tenant-system/database-admission-ca"}},
            "webhooks": [admission_webhook(
                "tenantdatabases.tenancy.cnpg-vcluster.io", "/mutate", true
            )]}),
        ),
        (
            "admission/validating-webhook.yaml",
            json!({"apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingWebhookConfiguration",
            "metadata": {"name": "database-admission",
                "annotations": {"cert-manager.io/inject-ca-from": "tenant-system/database-admission-ca"}},
            "webhooks": [admission_webhook(
                "tenantdatabases.tenancy.cnpg-vcluster.io", "/validate", false
            )]}),
        ),
    ] {
        resources.push((name, yaml(&resource)?));
    }
    for (name, account, role) in [
        ("admission", "database-admission", "database-admission"),
        ("controller", "database-controller", "database-controller"),
    ] {
        resources.push((
            if name == "admission" {
                "rbac/admission-cluster-binding.yaml"
            } else {
                "rbac/controller-cluster-binding.yaml"
            },
            yaml(&json!({
                "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                "metadata": {"name": role},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole", "name": role},
                "subjects": [{"kind": "ServiceAccount", "namespace": "tenant-system",
                    "name": account}]
            }))?,
        ));
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
    for (relative, contents) in generated_files()? {
        let path = output_dir.join(relative);
        if check {
            if std::fs::read(&path)? != contents {
                return Err(format!("generated fixture differs: {}", path.display()).into());
            }
        } else {
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

    #[test]
    fn generated_contracts_are_current() {
        run(&["--check".into()]).unwrap();
    }

    #[test]
    fn gate_roles_share_only_exact_namespace_and_no_lease_authority() {
        let artifacts = generated_files().unwrap();
        let find = |name: &str| {
            serde_yaml::from_slice::<Value>(
                &artifacts.iter().find(|(path, _)| *path == name).unwrap().1,
            )
            .unwrap()
        };
        let gates = find("rbac/admission-gates-role.yaml");
        let controller = find("rbac/controller-gates-role.yaml");
        let tenant = find("rbac/tenant-gates-role.yaml");
        assert_eq!(
            gates["rules"][0]["verbs"],
            json!(["get", "update", "patch"])
        );
        assert_eq!(
            controller["rules"][0]["verbs"],
            json!(["get", "update", "patch"])
        );
        assert_eq!(
            tenant["rules"][0]["verbs"],
            json!(["create", "get", "update", "patch", "delete"])
        );
        assert_eq!(gates["metadata"]["namespace"], GATE_NAMESPACE);
        assert_eq!(controller["metadata"]["namespace"], GATE_NAMESPACE);
        assert_eq!(tenant["metadata"]["namespace"], GATE_NAMESPACE);
        for role in [&gates, &controller, &tenant] {
            assert_eq!(role["rules"][0]["resources"], json!(["configmaps"]));
            assert_eq!(role["rules"][0]["apiGroups"], json!([""]));
        }
        let admission = find("rbac/admission-cluster-role.yaml");
        assert_eq!(admission["rules"].as_array().unwrap().len(), 3);
        assert!(admission["rules"].as_array().unwrap().iter().all(|rule| {
            rule["verbs"] == json!(["get"])
                && !rule["resources"]
                    .as_array()
                    .unwrap()
                    .contains(&json!("configmaps"))
        }));
        for path in [
            "admission/mutating-webhook.yaml",
            "admission/validating-webhook.yaml",
        ] {
            let webhook = find(path);
            let rule = &webhook["webhooks"][0];
            assert_eq!(rule["failurePolicy"], "Fail");
            assert_eq!(rule["sideEffects"], "NoneOnDryRun");
            assert_eq!(rule["rules"][0]["operations"], json!(["CREATE"]));
            assert_eq!(rule["rules"][0]["resources"], json!(["tenantdatabases"]));
            assert_eq!(
                rule["clientConfig"]["service"]["namespace"],
                "tenant-system"
            );
            assert_eq!(
                webhook["metadata"]["annotations"]["cert-manager.io/inject-ca-from"],
                "tenant-system/database-admission-ca"
            );
        }
        assert!(artifacts.iter().all(|(_, contents)| {
            !String::from_utf8_lossy(contents).contains("tenant-database-reservations")
        }));
        let catalog = serde_json::from_slice::<Value>(
            &artifacts
                .iter()
                .find(|(name, _)| *name == "management-resources.json")
                .unwrap()
                .1,
        )
        .unwrap();
        assert_eq!(catalog.as_array().unwrap().len(), 5);
        assert!(catalog.as_array().unwrap().iter().any(|resource| {
            resource["resource"] == "tenantdatabases" && resource["scope"] == "Namespaced"
        }));
        let azure_catalog = serde_json::from_slice::<Value>(
            &artifacts
                .iter()
                .find(|(name, _)| *name == "azure-management-resources.json")
                .unwrap()
                .1,
        )
        .unwrap();
        assert_eq!(azure_catalog.as_array().unwrap().len(), 6);
        assert_eq!(azure_catalog[5]["resource"], "disks");
        assert_eq!(azure_catalog[5]["version"], "v1api20240302");
        assert_eq!(
            find("rbac/controller-cluster-role.yaml")["rules"],
            find("rbac/controller-cluster-role-azure.yaml")["rules"]
        );
    }
}
