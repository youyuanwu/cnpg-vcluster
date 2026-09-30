use k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::{
    CustomResourceDefinition, ValidationRule,
};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::Condition;
use kube::{CustomResource, CustomResourceExt};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

pub const GROUP: &str = "tenancy.cnpg-vcluster.io";
pub const VERSION: &str = "v1alpha1";
pub const FINALIZER: &str = "tenancy.cnpg-vcluster.io/database-finalizer";
pub const DATABASE_LIMIT: usize = 3;

#[expect(
    clippy::duplicated_attributes,
    reason = "each printer column declares its own type"
)]
#[derive(CustomResource, Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[kube(
    group = "tenancy.cnpg-vcluster.io",
    version = "v1alpha1",
    kind = "TenantDatabase",
    plural = "tenantdatabases",
    shortname = "tdb",
    namespaced,
    status = "TenantDatabaseStatus",
    derive = "PartialEq",
    printcolumn(name = "Phase", type_ = "string", json_path = ".status.phase"),
    printcolumn(name = "Instances", type_ = "integer", json_path = ".spec.instances"),
    printcolumn(
        name = "Ready",
        type_ = "integer",
        json_path = ".status.readyInstances"
    )
)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TenantDatabaseSpec {
    pub tenant_name: String,
    #[serde(rename = "tenantUID")]
    pub tenant_uid: String,
    pub instances: i32,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TenantDatabaseStatus {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub observed_generation: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<DatabasePhase>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub conditions: Vec<Condition>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "namespaceUID")]
    pub namespace_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "clusterUID")]
    pub cluster_uid: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub storage: Vec<StorageIdentity>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub instances: Vec<InstanceObservation>,
    #[serde(default)]
    pub ready_instances: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deletion: Option<DeletionStatus>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum DatabasePhase {
    Pending,
    Progressing,
    Ready,
    Deleting,
    Degraded,
    OwnershipInvalid,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct StorageIdentity {
    pub ordinal: i32,
    #[serde(rename = "pvUID", skip_serializing_if = "Option::is_none")]
    pub pv_uid: Option<String>,
    #[serde(rename = "pvcUID", skip_serializing_if = "Option::is_none")]
    pub pvc_uid: Option<String>,
    #[serde(rename = "diskUID", skip_serializing_if = "Option::is_none")]
    pub disk_uid: Option<String>,
    #[serde(rename = "armID", skip_serializing_if = "Option::is_none")]
    pub arm_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct InstanceObservation {
    pub name: String,
    pub role: String,
    pub ready: bool,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DeletionStatus {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub verified_absent: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum SpecError {
    #[error("database cluster name must be a 1-30 character lowercase DNS label")]
    Name,
    #[error("Tenant name must be a 1-30 character lowercase DNS label")]
    TenantName,
    #[error("Tenant UID must be nonempty")]
    TenantUid,
    #[error("instances must be an integer from 1 through 3")]
    Instances,
}

pub fn valid_name(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=30).contains(&bytes.len())
        && bytes
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && bytes
            .last()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

pub fn validate_spec(name: &str, spec: &TenantDatabaseSpec) -> Result<(), SpecError> {
    if !valid_name(name) {
        return Err(SpecError::Name);
    }
    if !valid_name(&spec.tenant_name) {
        return Err(SpecError::TenantName);
    }
    if spec.tenant_uid.is_empty() {
        return Err(SpecError::TenantUid);
    }
    if !(1..=3).contains(&spec.instances) {
        return Err(SpecError::Instances);
    }
    Ok(())
}

pub fn database_crd() -> CustomResourceDefinition {
    let mut crd = TenantDatabase::crd();
    let schema = crd.spec.versions[0]
        .schema
        .as_mut()
        .expect("TenantDatabase has a derived schema")
        .open_api_v3_schema
        .as_mut()
        .expect("TenantDatabase has a structural schema");
    let fields = schema
        .properties
        .as_mut()
        .expect("schema has properties")
        .get_mut("spec")
        .expect("schema has spec")
        .properties
        .as_mut()
        .expect("spec has properties");
    let instances = fields.get_mut("instances").expect("spec has instances");
    instances.minimum = Some(1.0);
    instances.maximum = Some(3.0);
    let status = schema
        .properties
        .as_mut()
        .expect("schema has properties")
        .get_mut("status")
        .expect("schema has status");
    let status_fields = status.properties.as_mut().expect("status has properties");
    for field in ["storage", "instances"] {
        status_fields
            .get_mut(field)
            .expect("bounded observation")
            .max_items = Some(3);
    }
    let conditions = status_fields
        .get_mut("conditions")
        .expect("status has conditions");
    conditions.x_kubernetes_list_type = Some("map".into());
    conditions.x_kubernetes_list_map_keys = Some(vec!["type".into()]);
    let k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::JSONSchemaPropsOrArray::Schema(
        condition,
    ) = conditions.items.as_mut().expect("condition items") else {
        panic!("conditions have one item schema");
    };
    condition
        .properties
        .as_mut()
        .expect("condition fields")
        .get_mut("message")
        .expect("condition message")
        .max_length = Some(512);
    schema.x_kubernetes_validations = Some(vec![
        validation("self.spec == oldSelf.spec", "database spec is immutable"),
        validation(
            "self.metadata.name.matches('^[a-z0-9]([-a-z0-9]{0,28}[a-z0-9])?$')",
            "database name must be a lowercase DNS label",
        ),
        validation(
            "self.spec.tenantName.matches('^[a-z0-9]([-a-z0-9]{0,28}[a-z0-9])?$')",
            "Tenant name must be a lowercase DNS label",
        ),
        validation(
            "size(self.spec.tenantUID) > 0",
            "Tenant UID must be nonempty",
        ),
        validation(
            "self.spec.instances >= 1 && self.spec.instances <= 3",
            "instances must be between 1 and 3",
        ),
    ]);
    crd
}

fn validation(rule: &str, message: &str) -> ValidationRule {
    ValidationRule {
        rule: rule.into(),
        message: Some(message.into()),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn names_and_spec_are_bounded() {
        let spec = TenantDatabaseSpec {
            tenant_name: "tenant-a".into(),
            tenant_uid: "uid-1".into(),
            instances: 3,
        };
        assert_eq!(validate_spec("orders", &spec), Ok(()));
        for name in ["", "Wrong", "a.b", "-bad", "bad-", &"a".repeat(31)] {
            assert_eq!(validate_spec(name, &spec), Err(SpecError::Name));
        }
        for count in [-1, 0, 4] {
            assert_eq!(
                validate_spec(
                    "orders",
                    &TenantDatabaseSpec {
                        instances: count,
                        ..spec.clone()
                    }
                ),
                Err(SpecError::Instances)
            );
        }
    }

    #[test]
    fn crd_is_namespaced_and_immutable() {
        let crd = database_crd();
        assert_eq!(crd.spec.scope, "Namespaced");
        assert_eq!(crd.spec.versions[0].name, VERSION);
        assert!(
            crd.spec.versions[0]
                .subresources
                .as_ref()
                .unwrap()
                .status
                .is_some()
        );
        let rules = crd.spec.versions[0]
            .schema
            .as_ref()
            .unwrap()
            .open_api_v3_schema
            .as_ref()
            .unwrap()
            .x_kubernetes_validations
            .as_ref()
            .unwrap();
        assert!(
            rules
                .iter()
                .any(|rule| rule.rule == "self.spec == oldSelf.spec")
        );
        let resource = TenantDatabase::new(
            "orders",
            TenantDatabaseSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "uid-1".into(),
                instances: 2,
            },
        );
        assert_eq!(
            serde_json::to_value(resource).unwrap()["spec"],
            json!({
                "tenantName": "tenant-a", "tenantUID": "uid-1", "instances": 2
            })
        );
        let serialized = serde_json::to_value(&crd).unwrap();
        let schema = &serialized["spec"]["versions"][0]["schema"]["openAPIV3Schema"];
        assert_eq!(
            schema["properties"]["spec"]["properties"]["instances"]["minimum"],
            1.0
        );
        assert_eq!(
            schema["properties"]["spec"]["properties"]["instances"]["maximum"],
            3.0
        );
        assert_eq!(
            schema["properties"]["status"]["properties"]["storage"]["maxItems"],
            3
        );
        assert_eq!(
            schema["properties"]["status"]["properties"]["instances"]["maxItems"],
            3
        );
        assert_eq!(
            schema["properties"]["status"]["properties"]["conditions"]["items"]["properties"]["message"]
                ["maxLength"],
            512
        );
        assert!(
            serde_json::from_value::<TenantDatabaseSpec>(json!({
                "tenantName": "tenant-a", "tenantUID": "uid-1",
                "instances": 2, "storageClass": "foreign"
            }))
            .is_err()
        );
    }
}
