use kube::{CustomResource, CustomResourceExt};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const GROUP: &str = "tenancy.cnpg-vcluster.io";
pub const VERSION: &str = "v1alpha2";
pub const SUPPORTED_KUBERNETES_VERSION: &str = "1.36.4";

#[expect(
    clippy::duplicated_attributes,
    reason = "each printer column declares its own type"
)]
#[derive(CustomResource, Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[kube(
    group = "tenancy.cnpg-vcluster.io",
    version = "v1alpha2",
    kind = "Tenant",
    plural = "tenants",
    shortname = "tn",
    status = "TenantStatus",
    derive = "PartialEq",
    printcolumn(name = "Phase", type_ = "string", json_path = ".status.phase"),
    printcolumn(
        name = "Ready",
        type_ = "string",
        json_path = ".status.conditions[?(@.type==\"Ready\")].status"
    ),
    printcolumn(
        name = "Endpoint",
        type_ = "string",
        json_path = ".status.allocation.endpoint"
    ),
    printcolumn(name = "Workers", type_ = "integer", json_path = ".spec.workers"),
    printcolumn(name = "Databases", type_ = "integer", json_path = ".spec.databases")
)]
#[serde(rename_all = "camelCase")]
pub struct TenantSpec {
    pub kubernetes_version: String,
    pub workers: i32,
    pub databases: i32,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct TenantStatus {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub observed_generation: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<TenantPhase>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub conditions: Vec<k8s_openapi::apimachinery::pkg::apis::meta::v1::Condition>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allocation: Option<AllocationStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub foundation_hash: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(rename = "clusterUID")]
    pub cluster_uid: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AllocationStatus {
    pub slot_id: String,
    pub endpoint: String,
    #[serde(rename = "podCIDR")]
    pub pod_cidr: String,
    #[serde(rename = "serviceCIDR")]
    pub service_cidr: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum TenantPhase {
    Pending,
    Progressing,
    Ready,
    Deleting,
    Degraded,
    Failed,
    OwnershipInvalid,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct CanonicalSpec {
    pub kubernetes_version: String,
    pub workers: i32,
    pub databases: i32,
}

#[derive(Debug, PartialEq, Eq)]
pub enum SpecError {
    Name,
    Version,
    UnsupportedVersion,
    Workers,
    Databases,
}

impl std::fmt::Display for SpecError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Name => "tenant name must be a 1-30 character lowercase DNS label",
            Self::Version => "kubernetesVersion must be a three-component numeric version",
            Self::UnsupportedVersion => "kubernetesVersion is not supported by this controller",
            Self::Workers => "workers must be an integer from 1 through 3",
            Self::Databases => "databases must be an integer from 1 through 3",
        })
    }
}

impl std::error::Error for SpecError {}

pub fn canonical_spec(
    name: &str,
    spec: &TenantSpec,
    supported_version: &str,
) -> Result<CanonicalSpec, SpecError> {
    let bytes = name.as_bytes();
    if !(1..=30).contains(&bytes.len())
        || !bytes
            .first()
            .is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        || !bytes
            .last()
            .is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        || !bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
    {
        return Err(SpecError::Name);
    }
    let version = spec
        .kubernetes_version
        .strip_prefix('v')
        .unwrap_or(&spec.kubernetes_version);
    if version.split('.').count() != 3
        || !version
            .split('.')
            .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(SpecError::Version);
    }
    if version
        != supported_version
            .strip_prefix('v')
            .unwrap_or(supported_version)
    {
        return Err(SpecError::UnsupportedVersion);
    }
    if !(1..=3).contains(&spec.workers) {
        return Err(SpecError::Workers);
    }
    if !(1..=3).contains(&spec.databases) {
        return Err(SpecError::Databases);
    }
    Ok(CanonicalSpec {
        kubernetes_version: version.to_owned(),
        workers: spec.workers,
        databases: spec.databases,
    })
}

pub fn spec_hash(spec: &CanonicalSpec) -> String {
    let json = serde_json::to_vec(spec).expect("canonical Tenant spec is serializable");
    hex::encode(Sha256::digest(json))
}

pub fn tenant_crd()
-> k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::CustomResourceDefinition {
    use k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::ValidationRule;

    let mut crd = Tenant::crd();
    let schema = crd.spec.versions[0]
        .schema
        .as_mut()
        .expect("Tenant has a derived schema")
        .open_api_v3_schema
        .as_mut()
        .expect("Tenant has a structural schema");
    let properties = schema.properties.as_mut().expect("Tenant has properties");
    let spec = properties.get_mut("spec").expect("Tenant has a spec");
    let fields = spec.properties.as_mut().expect("spec has fields");
    let version = fields.get_mut("kubernetesVersion").expect("version field");
    version.pattern = Some(r"^v?[0-9]+\.[0-9]+\.[0-9]+$".into());
    for field in ["workers", "databases"] {
        let count = fields.get_mut(field).expect("count field");
        count.minimum = Some(1.0);
        count.maximum = Some(3.0);
    }
    let conditions = properties
        .get_mut("status")
        .and_then(|status| status.properties.as_mut())
        .and_then(|status| status.get_mut("conditions"))
        .expect("status has conditions");
    conditions.x_kubernetes_list_type = Some("map".into());
    conditions.x_kubernetes_list_map_keys = Some(vec!["type".into()]);
    let rule = |rule: &str, message: &str| ValidationRule {
        rule: rule.into(),
        message: Some(message.into()),
        ..Default::default()
    };
    schema.x_kubernetes_validations = Some(vec![
        rule("self.spec == oldSelf.spec", "Tenant spec is immutable"),
        rule(
            "self.metadata.name.matches('^[a-z0-9]([-a-z0-9]{0,28}[a-z0-9])?$')",
            "Tenant name must be a 1-30 character lowercase DNS label",
        ),
        rule(
            "self.spec.workers >= 1 && self.spec.workers <= 3 && self.spec.databases >= 1 && self.spec.databases <= 3",
            "workers and databases must each be between 1 and 3",
        ),
        rule(
            "self.spec.kubernetesVersion.matches('^v?[0-9]+[.][0-9]+[.][0-9]+$')",
            "kubernetesVersion must be a three-component numeric version",
        ),
    ]);
    crd
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn spec() -> TenantSpec {
        TenantSpec {
            kubernetes_version: "v1.36.4".into(),
            workers: 2,
            databases: 3,
        }
    }

    #[test]
    fn spec_and_status_json_shape() {
        let mut tenant = Tenant::new("tenant-a", spec());
        let condition = serde_json::from_value(json!({
            "type":"Ready", "status":"True", "reason":"Reconciled",
            "message":"Ready", "lastTransitionTime":"2026-09-25T00:00:00Z",
            "observedGeneration":2
        }))
        .unwrap();
        tenant.status = Some(TenantStatus {
            observed_generation: Some(2),
            phase: Some(TenantPhase::Progressing),
            conditions: vec![condition],
            allocation: Some(AllocationStatus {
                slot_id: "slot-a".into(),
                endpoint: "10.0.0.8".into(),
                pod_cidr: "10.73.0.0/16".into(),
                service_cidr: "10.143.0.0/16".into(),
            }),
            foundation_hash: Some("abc".into()),
            ..Default::default()
        });
        let value = serde_json::to_value(&tenant).unwrap();
        assert_eq!(value["apiVersion"], "tenancy.cnpg-vcluster.io/v1alpha2");
        assert_eq!(value["kind"], "Tenant");
        assert_eq!(
            value["spec"],
            json!({"kubernetesVersion":"v1.36.4","workers":2,"databases":3})
        );
        assert_eq!(value["status"]["allocation"]["podCIDR"], "10.73.0.0/16");
        assert_eq!(
            value["status"]["allocation"]["serviceCIDR"],
            "10.143.0.0/16"
        );
        assert_eq!(value["status"]["observedGeneration"], 2);
        assert_eq!(value["status"]["conditions"][0]["type"], "Ready");
        assert_eq!(
            value["status"]["conditions"][0]["lastTransitionTime"],
            "2026-09-25T00:00:00Z"
        );
        assert_eq!(serde_json::from_value::<Tenant>(value).unwrap(), tenant);
        let mut status = tenant.status.unwrap();
        status.cluster_uid = Some("cluster-uid".into());
        assert_eq!(
            serde_json::to_value(status).unwrap()["clusterUID"],
            "cluster-uid"
        );
    }

    #[test]
    fn unknown_fields_are_not_preserved_by_typed_api() {
        let spec: TenantSpec = serde_json::from_value(json!({
            "kubernetesVersion":"1.36.4", "workers":1, "databases":1, "unsupported":true
        }))
        .unwrap();
        assert_eq!(serde_json::to_value(spec).unwrap().get("unsupported"), None);
    }

    #[test]
    fn canonical_hash_normalizes_version_and_is_stable() {
        let first = canonical_spec("tenant-a", &spec(), SUPPORTED_KUBERNETES_VERSION).unwrap();
        let mut equivalent = spec();
        equivalent.kubernetes_version = "1.36.4".into();
        let second = canonical_spec("tenant-a", &equivalent, "v1.36.4").unwrap();
        assert_eq!(first, second);
        assert_eq!(
            serde_json::to_string(&first).unwrap(),
            r#"{"kubernetesVersion":"1.36.4","workers":2,"databases":3}"#
        );
        assert_eq!(spec_hash(&first), spec_hash(&second));
        assert_eq!(
            spec_hash(&first),
            "407b1043eea0ee8c421764c43dcb8f0b9d7676eafac0737d815c58c493d98ebf"
        );
        equivalent.workers += 1;
        assert_ne!(
            spec_hash(&first),
            spec_hash(&canonical_spec("tenant-a", &equivalent, "1.36.4").unwrap())
        );
    }

    #[test]
    fn invalid_names_versions_and_counts_fail() {
        for name in ["", "-a", "a-", "UPPER", "a.b", "a".repeat(31).as_str()] {
            assert_eq!(
                canonical_spec(name, &spec(), "1.36.4"),
                Err(SpecError::Name)
            );
        }
        for version in ["", "1.36", "1.x.4", "1.36.4.0", "v1.36.4 "] {
            let mut invalid = spec();
            invalid.kubernetes_version = version.into();
            assert_eq!(
                canonical_spec("valid", &invalid, "1.36.4"),
                Err(SpecError::Version)
            );
        }
        assert_eq!(
            canonical_spec("valid", &spec(), "1.36.5"),
            Err(SpecError::UnsupportedVersion)
        );
        for count in [0, 4, -1] {
            let mut invalid = spec();
            invalid.workers = count;
            assert_eq!(
                canonical_spec("valid", &invalid, "1.36.4"),
                Err(SpecError::Workers)
            );
            invalid.workers = 1;
            invalid.databases = count;
            assert_eq!(
                canonical_spec("valid", &invalid, "1.36.4"),
                Err(SpecError::Databases)
            );
        }
    }

    #[test]
    fn crd_has_structural_pruned_schema_and_transition_rules() {
        let crd = tenant_crd();
        assert_eq!(
            crd.metadata.name.as_deref(),
            Some("tenants.tenancy.cnpg-vcluster.io")
        );
        assert_eq!(crd.spec.group, GROUP);
        assert_eq!(crd.spec.scope, "Cluster");
        assert_eq!(crd.spec.names.kind, "Tenant");
        assert_eq!(crd.spec.names.plural, "tenants");
        assert_eq!(crd.spec.names.short_names.as_ref().unwrap(), &["tn"]);
        assert_eq!(crd.spec.versions.len(), 1);
        let v = &crd.spec.versions[0];
        assert_eq!(v.name, VERSION);
        assert!(v.served && v.storage);
        assert!(v.subresources.as_ref().unwrap().status.is_some());
        let columns: Vec<_> = v
            .additional_printer_columns
            .as_ref()
            .unwrap()
            .iter()
            .map(|c| (c.name.as_str(), c.json_path.as_str()))
            .collect();
        assert_eq!(
            columns,
            [
                ("Phase", ".status.phase"),
                ("Ready", ".status.conditions[?(@.type==\"Ready\")].status"),
                ("Endpoint", ".status.allocation.endpoint"),
                ("Workers", ".spec.workers"),
                ("Databases", ".spec.databases"),
            ]
        );
        let schema = v
            .schema
            .as_ref()
            .unwrap()
            .open_api_v3_schema
            .as_ref()
            .unwrap();
        let properties = schema.properties.as_ref().unwrap();
        assert_eq!(schema.type_.as_deref(), Some("object"));
        assert!(schema.x_kubernetes_preserve_unknown_fields.is_none());
        let spec = &properties["spec"];
        assert!(spec.x_kubernetes_preserve_unknown_fields.is_none());
        assert_eq!(
            spec.required.as_ref().unwrap(),
            &["databases", "kubernetesVersion", "workers"]
        );
        let fields = spec.properties.as_ref().unwrap();
        assert_eq!(fields.len(), 3);
        assert_eq!(fields["workers"].minimum, Some(1.0));
        assert_eq!(fields["databases"].maximum, Some(3.0));
        assert_eq!(
            fields["kubernetesVersion"].pattern.as_deref(),
            Some(r"^v?[0-9]+\.[0-9]+\.[0-9]+$")
        );
        let rules: Vec<_> = schema
            .x_kubernetes_validations
            .as_ref()
            .unwrap()
            .iter()
            .map(|r| r.rule.as_str())
            .collect();
        assert!(rules.contains(&"self.spec == oldSelf.spec"));
        assert!(rules.iter().any(|r| r.contains("metadata.name")));
        assert!(
            rules
                .iter()
                .any(|r| r.contains("spec.workers") && r.contains("spec.databases"))
        );
        assert!(rules.iter().any(|r| r.contains("spec.kubernetesVersion")));
        let conditions = &properties["status"].properties.as_ref().unwrap()["conditions"];
        assert_eq!(conditions.x_kubernetes_list_type.as_deref(), Some("map"));
        assert_eq!(
            conditions.x_kubernetes_list_map_keys.as_ref().unwrap(),
            &["type"]
        );
        assert!(
            properties["status"].properties.as_ref().unwrap()["allocation"]
                .properties
                .as_ref()
                .unwrap()
                .contains_key("podCIDR")
        );
        assert!(
            properties["status"]
                .properties
                .as_ref()
                .unwrap()
                .contains_key("clusterUID")
        );
        assert!(
            properties["status"]
                .x_kubernetes_preserve_unknown_fields
                .is_none()
        );
        let json: Value = serde_json::to_value(crd).unwrap();
        assert!(
            json["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
                .get("x-kubernetes-preserve-unknown-fields")
                .is_none()
        );
    }
}
