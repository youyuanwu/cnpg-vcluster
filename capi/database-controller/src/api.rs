use std::collections::{BTreeMap, BTreeSet};

use k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::{
    CustomResourceDefinition, JSONSchemaProps, JSONSchemaPropsOrArray, ValidationRule,
};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::Condition;
use kube::{CustomResource, CustomResourceExt};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

pub const GROUP: &str = "tenancy.cnpg-vcluster.io";
pub const VERSION: &str = "v1alpha1";
pub const FINALIZER: &str = "tenancy.cnpg-vcluster.io/database-catalog-finalizer";
pub const DATABASE_LIMIT: usize = 3;

#[derive(CustomResource, Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[kube(
    group = "tenancy.cnpg-vcluster.io",
    version = "v1alpha1",
    kind = "TenantDatabaseCatalog",
    plural = "tenantdatabasecatalogs",
    shortname = "tdc",
    namespaced,
    status = "CatalogStatus",
    derive = "PartialEq",
    printcolumn(name = "Closed", type_ = "boolean", json_path = ".spec.closed")
)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TenantDatabaseCatalogSpec {
    pub tenant_name: String,
    #[serde(rename = "tenantUID")]
    pub tenant_uid: String,
    pub closed: bool,
    #[serde(default)]
    pub entries: BTreeMap<String, CatalogEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CatalogEntry {
    pub name: String,
    pub instances: i32,
    pub deleting: bool,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CatalogStatus {
    #[serde(default)]
    pub entries: BTreeMap<String, EntryStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub observer: Option<CatalogObservation>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CatalogObservation {
    #[serde(rename = "catalogUID")]
    pub catalog_uid: String,
    pub observed_generation: i64,
    pub observed_resource_version: String,
    #[serde(rename = "podUID")]
    pub pod_uid: String,
    pub instance_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EntryStatus {
    #[serde(rename = "logicalUID")]
    pub logical_uid: String,
    pub observed_generation: i64,
    pub phase: DatabasePhase,
    #[serde(default)]
    pub conditions: Vec<Condition>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<ProviderIdentity>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub namespace: Option<ResourceIdentity>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cnpg_cluster: Option<ResourceIdentity>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub credentials: Option<ResourceIdentity>,
    #[serde(default)]
    pub storage: Vec<StorageIdentity>,
    #[serde(default)]
    pub instances: Vec<InstanceObservation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub query: Option<QueryIdentity>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finalization: Option<FinalizationStatus>,
    #[serde(default)]
    pub create_intents: Vec<CreateIntent>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CreateIntent {
    pub kind: String,
    pub name: String,
    pub ordinal: i32,
    pub state: CreateState,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum CreateState {
    Planned,
    Issued,
    Rejected,
    Observed,
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
pub struct ResourceIdentity {
    pub name: String,
    pub uid: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ProviderIdentity {
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub storage_namespace: Option<ResourceIdentity>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct StorageIdentity {
    pub ordinal: i32,
    pub requested_bytes: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pv: Option<ResourceIdentity>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pvc: Option<ResourceIdentity>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub disk: Option<ResourceIdentity>,
    #[serde(rename = "armID", skip_serializing_if = "Option::is_none")]
    pub arm_id: Option<String>,
    pub healthy: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct InstanceObservation {
    pub name: String,
    pub uid: String,
    pub role: String,
    pub ready: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct QueryIdentity {
    pub cluster_uid: String,
    pub credential_uid: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FinalizationStatus {
    pub terminal_verified: bool,
    #[serde(default)]
    pub verified_absent: Vec<String>,
    #[serde(default)]
    pub pending: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum CatalogError {
    #[error("Tenant name must be a lowercase DNS label of 1–30 characters")]
    TenantName,
    #[error("Tenant UID must be nonempty")]
    TenantUid,
    #[error("catalog holds at most three databases")]
    Capacity,
    #[error("logical UID must be a canonical lowercase UUID")]
    LogicalUid,
    #[error("database names must be unique lowercase DNS labels of 1–30 characters")]
    Name,
    #[error("instance count must be from one through three")]
    Instances,
    #[error("Tenant identity and closure are immutable or monotonic")]
    IdentityOrClosure,
    #[error("database identity or deletion state is immutable or monotonic")]
    EntryMutation,
    #[error("removal requires prior deletion and exact controller-verified terminal absence")]
    Removal,
    #[error("catalog UID or resourceVersion changed; re-read before mutation")]
    Conflict,
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

pub fn valid_logical_uid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            if [8, 13, 18, 23].contains(&index) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
}

pub fn validate_spec(spec: &TenantDatabaseCatalogSpec) -> Result<(), CatalogError> {
    if !valid_name(&spec.tenant_name) {
        return Err(CatalogError::TenantName);
    }
    if spec.tenant_uid.is_empty() {
        return Err(CatalogError::TenantUid);
    }
    if spec.entries.len() > DATABASE_LIMIT {
        return Err(CatalogError::Capacity);
    }
    let mut names = BTreeSet::new();
    for (uid, entry) in &spec.entries {
        if !valid_logical_uid(uid) {
            return Err(CatalogError::LogicalUid);
        }
        if !valid_name(&entry.name) || !names.insert(&entry.name) {
            return Err(CatalogError::Name);
        }
        if !(1..=3).contains(&entry.instances) {
            return Err(CatalogError::Instances);
        }
    }
    Ok(())
}

pub fn validate_transition(
    old: &TenantDatabaseCatalog,
    new: &TenantDatabaseCatalog,
) -> Result<(), CatalogError> {
    validate_spec(&new.spec)?;
    if old.spec.tenant_name != new.spec.tenant_name
        || old.spec.tenant_uid != new.spec.tenant_uid
        || (old.spec.closed && !new.spec.closed)
    {
        return Err(CatalogError::IdentityOrClosure);
    }
    for (uid, entry) in &old.spec.entries {
        if let Some(next) = new.spec.entries.get(uid) {
            if entry.name != next.name
                || entry.instances != next.instances
                || (entry.deleting && !next.deleting)
            {
                return Err(CatalogError::EntryMutation);
            }
        } else if !entry.deleting
            || !old
                .status
                .as_ref()
                .and_then(|status| status.entries.get(uid))
                .is_some_and(|status| {
                    status.logical_uid == *uid
                        && status.finalization.as_ref().is_some_and(|state| {
                            state.terminal_verified && state.pending.is_empty()
                        })
                })
        {
            return Err(CatalogError::Removal);
        }
    }
    if new.spec.closed
        && (new.spec.entries.values().any(|entry| !entry.deleting)
            || new
                .spec
                .entries
                .keys()
                .any(|uid| !old.spec.entries.contains_key(uid)))
    {
        return Err(CatalogError::IdentityOrClosure);
    }
    Ok(())
}

pub fn validate_conditional_update(
    current: &TenantDatabaseCatalog,
    next: &TenantDatabaseCatalog,
    observed_uid: &str,
    observed_resource_version: &str,
) -> Result<(), CatalogError> {
    if observed_uid.is_empty()
        || observed_resource_version.is_empty()
        || current.metadata.uid.as_deref() != Some(observed_uid)
        || next.metadata.uid.as_deref() != Some(observed_uid)
        || current.metadata.resource_version.as_deref() != Some(observed_resource_version)
        || next.metadata.resource_version.as_deref() != Some(observed_resource_version)
        || current.metadata.name != next.metadata.name
        || current.metadata.namespace != next.metadata.namespace
    {
        return Err(CatalogError::Conflict);
    }
    validate_transition(current, next)
}

fn validation(rule: &str, message: &str) -> ValidationRule {
    ValidationRule {
        rule: rule.into(),
        message: Some(message.into()),
        ..Default::default()
    }
}

fn properties(
    schema: &mut JSONSchemaProps,
) -> &mut std::collections::BTreeMap<String, JSONSchemaProps> {
    schema
        .properties
        .as_mut()
        .expect("structural schema properties")
}

fn map_value(schema: &mut JSONSchemaProps) -> &mut JSONSchemaProps {
    use k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::JSONSchemaPropsOrBool;
    let JSONSchemaPropsOrBool::Schema(value) =
        schema.additional_properties.as_mut().expect("map values")
    else {
        panic!("map values have a structural schema");
    };
    value
}

pub fn catalog_crd() -> CustomResourceDefinition {
    let mut crd = TenantDatabaseCatalog::crd();
    let schema = crd.spec.versions[0]
        .schema
        .as_mut()
        .expect("catalog has a derived schema")
        .open_api_v3_schema
        .as_mut()
        .expect("catalog has a structural schema");
    let spec = properties(schema).get_mut("spec").expect("spec");
    let entries = properties(spec).get_mut("entries").expect("entries");
    entries.max_properties = Some(DATABASE_LIMIT as i64);
    let entry = map_value(entries);
    properties(entry)
        .get_mut("instances")
        .expect("count")
        .minimum = Some(1.0);
    properties(entry)
        .get_mut("instances")
        .expect("count")
        .maximum = Some(3.0);
    let status = properties(schema).get_mut("status").expect("status");
    let observations = properties(status).get_mut("entries").expect("observations");
    observations.max_properties = Some(DATABASE_LIMIT as i64);
    observations.x_kubernetes_validations = Some(vec![validation(
        "self.all(uid, self[uid].logicalUID == uid && (!has(self[uid].finalization) || !self[uid].finalization.terminalVerified || size(self[uid].finalization.pending) == 0))",
        "status identity must match its logical UID and terminal proof cannot have pending work",
    )]);
    let observation = map_value(observations);
    for field in ["storage", "instances"] {
        properties(observation)
            .get_mut(field)
            .expect("observation list")
            .max_items = Some(3);
    }
    properties(observation)
        .get_mut("createIntents")
        .expect("bounded create intents")
        .max_items = Some(12);
    let conditions = properties(observation)
        .get_mut("conditions")
        .expect("conditions");
    conditions.x_kubernetes_list_type = Some("map".into());
    conditions.x_kubernetes_list_map_keys = Some(vec!["type".into()]);
    let JSONSchemaPropsOrArray::Schema(condition) =
        conditions.items.as_mut().expect("condition items")
    else {
        panic!("conditions have one item schema");
    };
    properties(condition)
        .get_mut("message")
        .expect("condition message")
        .max_length = Some(512);
    schema.x_kubernetes_validations = Some(vec![
        validation(
            "self.metadata.name == self.spec.tenantName",
            "catalog name must equal Tenant name",
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
            "self.spec.entries.all(uid, uid.matches('^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$') && self.spec.entries[uid].name.matches('^[a-z0-9]([-a-z0-9]{0,28}[a-z0-9])?$') && self.spec.entries[uid].instances >= 1 && self.spec.entries[uid].instances <= 3)",
            "logical UIDs, names and instance counts must be valid",
        ),
        validation(
            "self.spec.entries.all(uid, self.spec.entries.filter(otherUID, self.spec.entries[otherUID].name == self.spec.entries[uid].name).size() == 1)",
            "database names must be unique",
        ),
        validation(
            "self.spec.tenantName == oldSelf.spec.tenantName && self.spec.tenantUID == oldSelf.spec.tenantUID",
            "Tenant identity is immutable",
        ),
        validation(
            "!oldSelf.spec.closed || self.spec.closed",
            "a closed catalog cannot reopen",
        ),
        validation(
            "!self.spec.closed || self.spec.entries.all(uid, self.spec.entries[uid].deleting)",
            "closing a catalog must mark every remaining entry deleting",
        ),
        validation(
            "!self.spec.closed || self.spec.entries.all(uid, uid in oldSelf.spec.entries)",
            "closing a catalog cannot accept new entries",
        ),
        validation(
            "oldSelf.spec.entries.all(uid, !(uid in self.spec.entries) || (self.spec.entries[uid].name == oldSelf.spec.entries[uid].name && self.spec.entries[uid].instances == oldSelf.spec.entries[uid].instances && (!oldSelf.spec.entries[uid].deleting || self.spec.entries[uid].deleting)))",
            "entry UID, name and count are immutable and deletion is monotonic",
        ),
        validation(
            "oldSelf.spec.entries.all(uid, (uid in self.spec.entries) || (oldSelf.spec.entries[uid].deleting && has(oldSelf.status) && uid in oldSelf.status.entries && oldSelf.status.entries[uid].logicalUID == uid && has(oldSelf.status.entries[uid].finalization) && oldSelf.status.entries[uid].finalization.terminalVerified))",
            "entry removal requires prior deletion and exact controller-published terminal proof",
        ),
    ]);
    crd
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const UID: &str = "12345678-1234-1234-1234-123456789abc";
    const NEXT_UID: &str = "12345678-1234-1234-1234-123456789abd";
    fn catalog() -> TenantDatabaseCatalog {
        TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant-uid".into(),
                closed: false,
                entries: BTreeMap::from([(
                    UID.into(),
                    CatalogEntry {
                        name: "orders".into(),
                        instances: 2,
                        deleting: false,
                    },
                )]),
            },
        )
    }
    fn terminal(uid: &str) -> EntryStatus {
        EntryStatus {
            logical_uid: uid.into(),
            observed_generation: 1,
            phase: DatabasePhase::Deleting,
            conditions: vec![],
            provider: None,
            namespace: None,
            cnpg_cluster: None,
            credentials: None,
            storage: vec![],
            instances: vec![],
            query: None,
            finalization: Some(FinalizationStatus {
                terminal_verified: true,
                verified_absent: vec![],
                pending: vec![],
            }),
            create_intents: vec![],
        }
    }
    #[test]
    fn bounds_and_unique_names() {
        let mut value = catalog();
        assert_eq!(validate_spec(&value.spec), Ok(()));
        for name in ["", "Upper", "a.b", "-bad", "bad-", &"x".repeat(31)] {
            value.spec.entries.get_mut(UID).unwrap().name = name.into();
            assert_eq!(validate_spec(&value.spec), Err(CatalogError::Name));
        }
        value = catalog();
        for count in [0, 4] {
            value.spec.entries.get_mut(UID).unwrap().instances = count;
            assert_eq!(validate_spec(&value.spec), Err(CatalogError::Instances));
        }
        value = catalog();
        value.spec.entries.insert(
            NEXT_UID.into(),
            CatalogEntry {
                name: "orders".into(),
                instances: 1,
                deleting: false,
            },
        );
        assert_eq!(validate_spec(&value.spec), Err(CatalogError::Name));
        value.spec.entries.get_mut(NEXT_UID).unwrap().name = "other".into();
        value.spec.entries.insert(
            "12345678-1234-1234-1234-123456789abe".into(),
            CatalogEntry {
                name: "third".into(),
                instances: 1,
                deleting: true,
            },
        );
        assert!(validate_spec(&value.spec).is_ok());
        value.spec.entries.insert(
            "12345678-1234-1234-1234-123456789abf".into(),
            CatalogEntry {
                name: "fourth".into(),
                instances: 1,
                deleting: false,
            },
        );
        assert_eq!(validate_spec(&value.spec), Err(CatalogError::Capacity));
    }
    #[test]
    fn transitions_require_old_exact_status_proof() {
        let old = catalog();
        let mut next = old.clone();
        next.spec.entries.get_mut(UID).unwrap().name = "renamed".into();
        assert_eq!(
            validate_transition(&old, &next),
            Err(CatalogError::EntryMutation)
        );
        next = old.clone();
        next.spec.entries.get_mut(UID).unwrap().instances = 3;
        assert_eq!(
            validate_transition(&old, &next),
            Err(CatalogError::EntryMutation)
        );
        next = old.clone();
        next.spec.entries.remove(UID);
        next.status = Some(CatalogStatus {
            entries: BTreeMap::from([(UID.into(), terminal(UID))]),
            observer: None,
        });
        assert_eq!(validate_transition(&old, &next), Err(CatalogError::Removal));
        let mut deleting = old.clone();
        deleting.spec.entries.get_mut(UID).unwrap().deleting = true;
        assert!(validate_transition(&old, &deleting).is_ok());
        assert_eq!(
            validate_transition(&deleting, &next),
            Err(CatalogError::Removal)
        );
        deleting.status = Some(CatalogStatus {
            entries: BTreeMap::from([(UID.into(), terminal(NEXT_UID))]),
            observer: None,
        });
        assert_eq!(
            validate_transition(&deleting, &next),
            Err(CatalogError::Removal)
        );
        deleting
            .status
            .as_mut()
            .unwrap()
            .entries
            .insert(UID.into(), terminal(UID));
        assert!(validate_transition(&deleting, &next).is_ok());
        let mut undo_deletion = deleting.clone();
        undo_deletion.spec.entries.get_mut(UID).unwrap().deleting = false;
        assert_eq!(
            validate_transition(&deleting, &undo_deletion),
            Err(CatalogError::EntryMutation)
        );
        deleting
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut(UID)
            .unwrap()
            .finalization
            .as_mut()
            .unwrap()
            .pending
            .push("disk".into());
        assert_eq!(
            validate_transition(&deleting, &next),
            Err(CatalogError::Removal)
        );
        deleting
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut(UID)
            .unwrap()
            .finalization
            .as_mut()
            .unwrap()
            .pending
            .clear();
        next.spec.entries.insert(
            NEXT_UID.into(),
            CatalogEntry {
                name: "other".into(),
                instances: 1,
                deleting: false,
            },
        );
        deleting.spec.closed = true;
        assert_eq!(
            validate_transition(&deleting, &next),
            Err(CatalogError::IdentityOrClosure)
        );
        next.spec.closed = true;
        assert_eq!(
            validate_transition(&deleting, &next),
            Err(CatalogError::IdentityOrClosure)
        );
        next.spec.entries.remove(NEXT_UID);
        assert!(validate_transition(&deleting, &next).is_ok());
        let mut active_closed = old.clone();
        active_closed.spec.closed = true;
        assert_eq!(
            validate_transition(&old, &active_closed),
            Err(CatalogError::IdentityOrClosure)
        );
    }
    #[test]
    fn conditional_updates_require_exact_catalog_uid_and_resource_version() {
        let mut current = catalog();
        current.metadata.uid = Some("catalog-uid".into());
        current.metadata.resource_version = Some("12".into());
        current.metadata.namespace = Some("tenant-db-tenant-a".into());
        let mut next = current.clone();
        next.spec.entries.insert(
            NEXT_UID.into(),
            CatalogEntry {
                name: "other".into(),
                instances: 1,
                deleting: false,
            },
        );
        assert_eq!(
            validate_conditional_update(&current, &next, "catalog-uid", "12"),
            Ok(())
        );
        assert_eq!(
            validate_conditional_update(&current, &next, "catalog-uid", "11"),
            Err(CatalogError::Conflict)
        );
        assert_eq!(
            validate_conditional_update(&current, &next, "replacement", "12"),
            Err(CatalogError::Conflict)
        );
        next.metadata.resource_version = Some("13".into());
        assert_eq!(
            validate_conditional_update(&current, &next, "catalog-uid", "12"),
            Err(CatalogError::Conflict)
        );
    }

    #[test]
    fn observer_receipt_is_separate_from_database_entries() {
        let receipt = CatalogObservation {
            catalog_uid: "catalog-uid".into(),
            observed_generation: 4,
            observed_resource_version: "17".into(),
            pod_uid: "pod-uid".into(),
            instance_id: "boot-uid".into(),
        };
        let status = CatalogStatus {
            entries: BTreeMap::from([(UID.into(), terminal(UID))]),
            observer: Some(receipt.clone()),
        };
        let encoded = serde_json::to_value(&status).unwrap();
        assert_eq!(encoded["observer"]["catalogUID"], "catalog-uid");
        assert_eq!(encoded["observer"]["observedResourceVersion"], "17");
        assert_eq!(encoded["observer"]["podUID"], "pod-uid");
        assert_eq!(encoded["observer"]["instanceId"], "boot-uid");
        assert!(encoded["entries"].get(UID).is_some());
        assert_eq!(
            serde_json::from_value::<CatalogStatus>(encoded).unwrap(),
            status
        );
    }
    #[test]
    fn crd_has_structural_status_and_old_status_transition() {
        let crd = catalog_crd();
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
        let schema = &serde_json::to_value(&crd).unwrap()["spec"]["versions"][0]["schema"]["openAPIV3Schema"];
        assert_eq!(
            schema["properties"]["spec"]["properties"]["entries"]["maxProperties"],
            3
        );
        assert_eq!(
            schema["properties"]["status"]["properties"]["entries"]["maxProperties"],
            3
        );
        let observer = &schema["properties"]["status"]["properties"]["observer"];
        for field in [
            "catalogUID",
            "observedGeneration",
            "observedResourceVersion",
            "podUID",
            "instanceId",
        ] {
            assert!(
                observer["required"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|required| required == field)
            );
        }
        assert_eq!(
            schema["properties"]["spec"]["properties"]["entries"]["additionalProperties"]["properties"]
                ["instances"]["maximum"],
            3.0
        );
        assert_eq!(
            schema["properties"]["status"]["properties"]["entries"]["additionalProperties"]["properties"]
                ["conditions"]["items"]["properties"]["message"]["maxLength"],
            512
        );
        let rules = schema["x-kubernetes-validations"].as_array().unwrap();
        assert!(
            schema["properties"]["status"]["properties"]["entries"]["x-kubernetes-validations"][0]
                ["rule"]
                .as_str()
                .unwrap()
                .contains("self[uid].logicalUID == uid")
        );
        assert!(rules.iter().any(|rule| {
            rule["rule"]
                .as_str()
                .unwrap()
                .contains("oldSelf.status.entries[uid].logicalUID == uid")
        }));
        assert!(rules.iter().any(|rule| {
            rule["rule"]
                .as_str()
                .unwrap()
                .contains("!self.spec.closed || self.spec.entries.all")
        }));
        assert_eq!(
            serde_json::to_value(catalog()).unwrap()["spec"]["tenantUID"],
            "tenant-uid"
        );
        assert!(
            serde_json::from_value::<CatalogEntry>(
                json!({"name":"orders","instances":2,"deleting":false,"storageClass":"bad"})
            )
            .is_err()
        );
    }
}
