use std::collections::BTreeMap;

use base64::{Engine, engine::general_purpose::STANDARD};
use k8s_openapi::api::core::v1::{ConfigMap, Namespace, ResourceQuota};
use kube::{Api, Client, ResourceExt};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tenant_controller::api::{Tenant, TenantPhase};
pub use tenant_database_runtime::{
    GATE_ENTRIES, GATE_NAMESPACE, GATE_STATE, GATE_TENANT_UID, GATE_UID, GateEntry,
    IntentLifecycle, MAX_DATABASES, QUOTA_KEY, QUOTA_NAME, SPEC_SHA256, TENANT_UID,
    database_namespace, gate_name,
};

use crate::api::{TenantDatabase, validate_spec};

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum AdmissionError {
    #[error("TenantDatabase request identity is invalid")]
    Request,
    #[error("TenantDatabase specification is invalid")]
    Spec,
    #[error("Tenant identity is unavailable or not Ready")]
    Tenant,
    #[error("Tenant database capability is unavailable")]
    Capability,
    #[error("database lifecycle Namespace is missing or replaced")]
    Namespace,
    #[error("database lifecycle quota is missing, replaced, or malformed")]
    Quota,
    #[error("Tenant database creation gate is closed or unavailable")]
    Gate,
    #[error("database create intent is missing, unclaimed, or replaced")]
    Reservation,
}

pub fn canonical_spec_hash(database: &TenantDatabase) -> Result<String, AdmissionError> {
    let spec = serde_json::to_vec(&database.spec).map_err(|_| AdmissionError::Spec)?;
    Ok(hex::encode(Sha256::digest(spec)))
}

pub fn validate_request(
    database: &TenantDatabase,
    tenant: &Tenant,
    namespace: &Namespace,
    quota: &ResourceQuota,
    gate: &ConfigMap,
) -> Result<(), AdmissionError> {
    let database_name = database
        .metadata
        .name
        .as_deref()
        .ok_or(AdmissionError::Spec)?;
    validate_spec(database_name, &database.spec).map_err(|_| AdmissionError::Spec)?;
    let tenant_uid = tenant
        .metadata
        .uid
        .as_deref()
        .ok_or(AdmissionError::Tenant)?;
    let expected_namespace = database_namespace(&database.spec.tenant_name);
    if tenant.name_any() != database.spec.tenant_name
        || tenant_uid != database.spec.tenant_uid
        || tenant.metadata.deletion_timestamp.is_some()
        || tenant.status.as_ref().and_then(|status| status.phase) != Some(TenantPhase::Ready)
        || tenant
            .status
            .as_ref()
            .and_then(|status| status.observed_generation)
            != tenant.metadata.generation
        || !tenant.status.as_ref().is_some_and(|status| {
            status.conditions.iter().any(|condition| {
                condition.type_ == "Ready"
                    && condition.status == "True"
                    && condition.observed_generation == tenant.metadata.generation
            })
        })
    {
        return Err(AdmissionError::Tenant);
    }
    let capability = tenant
        .status
        .as_ref()
        .and_then(|status| status.database_capability.as_ref())
        .filter(|capability| capability.available)
        .ok_or(AdmissionError::Capability)?;
    if database.metadata.namespace.as_deref() != Some(expected_namespace.as_str())
        || namespace.name_any() != expected_namespace
        || namespace.metadata.deletion_timestamp.is_some()
        || namespace
            .metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(TENANT_UID))
            .map(String::as_str)
            != Some(tenant_uid)
        || namespace.metadata.uid.as_deref() != Some(capability.namespace_uid.as_str())
    {
        return Err(AdmissionError::Namespace);
    }
    let hard = quota.spec.as_ref().and_then(|spec| spec.hard.as_ref());
    if quota.name_any() != QUOTA_NAME
        || quota.metadata.namespace.as_deref() != Some(expected_namespace.as_str())
        || quota.metadata.deletion_timestamp.is_some()
        || quota
            .metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(TENANT_UID))
            .map(String::as_str)
            != Some(tenant_uid)
        || quota.metadata.uid.as_deref() != Some(capability.quota_uid.as_str())
        || quota.spec.as_ref().is_some_and(|spec| {
            spec.scopes
                .as_ref()
                .is_some_and(|scopes| !scopes.is_empty())
                || spec.scope_selector.is_some()
        })
        || !hard.is_some_and(|hard| {
            hard.len() == 1
                && hard
                    .get(QUOTA_KEY)
                    .is_some_and(|quantity| quantity.0 == "3")
        })
    {
        return Err(AdmissionError::Quota);
    }
    validate_gate(gate, tenant_uid)?;
    if gate
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_STATE))
        .map(String::as_str)
        != Some("open")
    {
        return Err(AdmissionError::Gate);
    }
    if gate.metadata.uid.as_deref() != Some(capability.gate_uid.as_str()) {
        return Err(AdmissionError::Gate);
    }
    let entry = gate_entries(gate)?
        .get(database_name)
        .cloned()
        .ok_or(AdmissionError::Reservation)?;
    if entry.lifecycle != IntentLifecycle::Materializing
        || i32::from(entry.instances) != database.spec.instances
        || entry.bound_uid.is_some()
    {
        return Err(AdmissionError::Reservation);
    }
    Ok(())
}

pub fn validate_gate(gate: &ConfigMap, tenant_uid: &str) -> Result<(), AdmissionError> {
    tenant_database_runtime::validate_gate(gate, tenant_uid).map_err(|_| AdmissionError::Gate)
}

pub fn gate_entries(gate: &ConfigMap) -> Result<BTreeMap<String, GateEntry>, AdmissionError> {
    tenant_database_runtime::gate_entries(gate).map_err(|_| AdmissionError::Gate)
}

pub fn validate_controller_metadata(
    database: &TenantDatabase,
    gate: &ConfigMap,
) -> Result<(), AdmissionError> {
    let annotations = database
        .metadata
        .annotations
        .as_ref()
        .ok_or(AdmissionError::Reservation)?;
    validate_gate(gate, &database.spec.tenant_uid)?;
    let entry = gate_entries(gate)?
        .get(
            database
                .metadata
                .name
                .as_deref()
                .ok_or(AdmissionError::Spec)?,
        )
        .cloned()
        .ok_or(AdmissionError::Reservation)?;
    if database
        .metadata
        .finalizers
        .as_ref()
        .is_none_or(|finalizers| finalizers != &[crate::api::FINALIZER])
        || annotations.get(TENANT_UID).map(String::as_str)
            != Some(database.spec.tenant_uid.as_str())
        || annotations.get(GATE_UID).map(String::as_str) != gate.metadata.uid.as_deref()
        || annotations.get(SPEC_SHA256).map(String::as_str)
            != Some(canonical_spec_hash(database)?.as_str())
        || entry.lifecycle != IntentLifecycle::Materializing
        || i32::from(entry.instances) != database.spec.instances
        || entry.bound_uid.is_some()
    {
        return Err(AdmissionError::Reservation);
    }
    Ok(())
}

pub async fn admit_create(client: Client, database: &TenantDatabase) -> Result<(), AdmissionError> {
    let tenant = Api::<Tenant>::all(client.clone())
        .get(&database.spec.tenant_name)
        .await
        .map_err(|_| AdmissionError::Tenant)?;
    let namespace_name = database_namespace(&database.spec.tenant_name);
    let namespace = Api::<Namespace>::all(client.clone())
        .get(&namespace_name)
        .await
        .map_err(|_| AdmissionError::Namespace)?;
    let quota = Api::<ResourceQuota>::namespaced(client.clone(), &namespace_name)
        .get(QUOTA_NAME)
        .await
        .map_err(|_| AdmissionError::Quota)?;
    let gate = Api::<ConfigMap>::namespaced(client, GATE_NAMESPACE)
        .get(&gate_name(&database.spec.tenant_uid))
        .await
        .map_err(|_| AdmissionError::Gate)?;
    validate_request(database, &tenant, &namespace, &quota, &gate)?;
    validate_controller_metadata(database, &gate)
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdmissionReview {
    pub request: AdmissionRequest,
}

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdmissionRequest {
    pub uid: String,
    pub kind: GroupVersionKind,
    pub resource: GroupVersionResource,
    pub operation: String,
    pub namespace: Option<String>,
    #[serde(default)]
    pub dry_run: bool,
    pub object: Value,
    #[serde(default)]
    pub old_object: Option<Value>,
    #[serde(default)]
    pub user_info: Option<AdmissionUser>,
}

#[derive(Clone, Deserialize)]
pub struct AdmissionUser {
    pub username: String,
}

#[derive(Clone, Deserialize)]
pub struct GroupVersionKind {
    pub group: String,
    pub version: String,
    pub kind: String,
}

#[derive(Clone, Deserialize)]
pub struct GroupVersionResource {
    pub group: String,
    pub version: String,
    pub resource: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AdmissionAnswer {
    api_version: &'static str,
    kind: &'static str,
    response: AdmissionDecision,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AdmissionDecision {
    uid: String,
    allowed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    status: Option<AdmissionStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    patch_type: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    patch: Option<String>,
}

#[derive(Serialize)]
struct AdmissionStatus {
    code: u16,
    message: String,
}

impl AdmissionAnswer {
    fn decision(uid: String, result: Result<Option<Value>, AdmissionError>) -> Self {
        let response = match result {
            Ok(patch) => AdmissionDecision {
                uid,
                allowed: true,
                status: None,
                patch_type: patch.as_ref().map(|_| "JSONPatch"),
                patch: patch.map(|value| STANDARD.encode(value.to_string())),
            },
            Err(error) => AdmissionDecision {
                uid,
                allowed: false,
                status: Some(AdmissionStatus {
                    code: if error == AdmissionError::Capability {
                        503
                    } else {
                        403
                    },
                    message: error.to_string(),
                }),
                patch_type: None,
                patch: None,
            },
        };
        Self {
            api_version: "admission.k8s.io/v1",
            kind: "AdmissionReview",
            response,
        }
    }
}

pub async fn review(client: Client, review: AdmissionReview, validating: bool) -> AdmissionAnswer {
    let request = review.request;
    let uid = request.uid.clone();
    let result = if validating && request.operation == "UPDATE" {
        review_update(request)
    } else {
        review_create(client, request, validating).await
    };
    AdmissionAnswer::decision(uid, result)
}

fn review_update(request: AdmissionRequest) -> Result<Option<Value>, AdmissionError> {
    if request.uid.is_empty() {
        return Err(AdmissionError::Request);
    }
    let username = request
        .user_info
        .as_ref()
        .map(|user| user.username.as_str())
        .ok_or(AdmissionError::Request)?;
    let old = request.old_object.ok_or(AdmissionError::Request)?;
    if request.kind.group.is_empty()
        && request.kind.version == "v1"
        && request.kind.kind == "ConfigMap"
        && request.resource.group.is_empty()
        && request.resource.version == "v1"
        && request.resource.resource == "configmaps"
        && request.namespace.as_deref() == Some(GATE_NAMESPACE)
    {
        let before: ConfigMap = serde_json::from_value(old).map_err(|_| AdmissionError::Gate)?;
        let after: ConfigMap =
            serde_json::from_value(request.object).map_err(|_| AdmissionError::Gate)?;
        validate_gate_update(&before, &after, username)?;
    } else if request.kind.group == crate::api::GROUP
        && request.kind.version == crate::api::VERSION
        && request.kind.kind == "TenantDatabase"
        && request.resource.group == crate::api::GROUP
        && request.resource.version == crate::api::VERSION
        && request.resource.resource == "tenantdatabases"
    {
        let before: TenantDatabase =
            serde_json::from_value(old).map_err(|_| AdmissionError::Request)?;
        let after: TenantDatabase =
            serde_json::from_value(request.object).map_err(|_| AdmissionError::Request)?;
        validate_database_update(&before, &after, username, request.namespace.as_deref())?;
    } else {
        return Err(AdmissionError::Request);
    }
    Ok(None)
}

pub fn validate_gate_update(
    before: &ConfigMap,
    after: &ConfigMap,
    username: &str,
) -> Result<(), AdmissionError> {
    let tenant_uid = before
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_TENANT_UID))
        .ok_or(AdmissionError::Gate)?;
    validate_gate(before, tenant_uid)?;
    validate_gate(after, tenant_uid)?;
    if before.metadata.uid != after.metadata.uid
        || before
            .metadata
            .resource_version
            .as_deref()
            .is_none_or(str::is_empty)
        || before.metadata.resource_version != after.metadata.resource_version
        || before.metadata.labels != after.metadata.labels
        || before.metadata.annotations != after.metadata.annotations
        || before.immutable != after.immutable
        || before.binary_data != after.binary_data
        || before.metadata.owner_references != after.metadata.owner_references
        || before.metadata.finalizers != after.metadata.finalizers
    {
        return Err(AdmissionError::Gate);
    }
    let old_state = before
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_STATE))
        .ok_or(AdmissionError::Gate)?;
    let new_state = after
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_STATE))
        .ok_or(AdmissionError::Gate)?;
    let old_entries = gate_entries(before)?;
    let new_entries = gate_entries(after)?;
    match username {
        "system:serviceaccount:tenant-system:tenant-admin" => {
            if old_state != "open"
                || new_state != "open"
                || new_entries.len() != old_entries.len() + 1
                || old_entries
                    .iter()
                    .any(|(name, entry)| new_entries.get(name) != Some(entry))
                || new_entries.iter().any(|(name, entry)| {
                    !old_entries.contains_key(name)
                        && (entry.lifecycle != IntentLifecycle::Pending
                            || entry.bound_uid.is_some())
                })
            {
                return Err(AdmissionError::Gate);
            }
        }
        "system:serviceaccount:tenant-system:database-controller" => {
            if old_state != new_state
                || new_entries.iter().any(|(name, entry)| {
                    old_entries.get(name).is_none_or(|previous| {
                        let claim = old_state == "open"
                            && previous.lifecycle == IntentLifecycle::Pending
                            && entry.lifecycle == IntentLifecycle::Materializing
                            && entry.bound_uid.is_none();
                        let bind = previous.lifecycle == IntentLifecycle::Materializing
                            && entry.lifecycle == IntentLifecycle::Bound
                            && entry
                                .bound_uid
                                .as_deref()
                                .is_some_and(|uid| !uid.is_empty());
                        !((claim || bind) && entry.instances == previous.instances)
                            && entry != previous
                    })
                })
                || old_entries.iter().any(|(name, entry)| {
                    !new_entries.contains_key(name) && entry.lifecycle != IntentLifecycle::Bound
                })
            {
                return Err(AdmissionError::Gate);
            }
        }
        "system:serviceaccount:tenant-system:tenant-controller" => {
            if new_state != "closed"
                || new_entries
                    .iter()
                    .any(|(name, entry)| old_entries.get(name) != Some(entry))
                || (old_state == "open"
                    && old_entries.iter().any(|(name, entry)| {
                        !new_entries.contains_key(name)
                            && entry.lifecycle != IntentLifecycle::Pending
                    }))
            {
                return Err(AdmissionError::Gate);
            }
        }
        _ => return Err(AdmissionError::Gate),
    }
    Ok(())
}

pub fn validate_database_update(
    before: &TenantDatabase,
    after: &TenantDatabase,
    username: &str,
    namespace: Option<&str>,
) -> Result<(), AdmissionError> {
    let old_annotations = before
        .metadata
        .annotations
        .as_ref()
        .ok_or(AdmissionError::Reservation)?;
    let new_annotations = after
        .metadata
        .annotations
        .as_ref()
        .ok_or(AdmissionError::Reservation)?;
    if before.metadata.name != after.metadata.name
        || before.metadata.namespace.as_deref() != namespace
        || after.metadata.namespace.as_deref() != namespace
        || before.metadata.uid.as_deref().is_none_or(str::is_empty)
        || before.metadata.uid != after.metadata.uid
        || before.metadata.deletion_timestamp != after.metadata.deletion_timestamp
        || before.spec != after.spec
        || after
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || before
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || [GATE_UID, TENANT_UID, SPEC_SHA256].iter().any(|key| {
            old_annotations.get(*key).is_none_or(String::is_empty)
                || old_annotations.get(*key) != new_annotations.get(*key)
        })
        || old_annotations.get(TENANT_UID) != Some(&before.spec.tenant_uid)
        || old_annotations.get(SPEC_SHA256).map(String::as_str)
            != Some(canonical_spec_hash(before)?.as_str())
        || before.metadata.finalizers.as_deref() != Some(&[crate::api::FINALIZER.into()])
    {
        return Err(AdmissionError::Reservation);
    }
    let new_finalizers = after.metadata.finalizers.as_deref().unwrap_or(&[]);
    if new_finalizers != [crate::api::FINALIZER]
        && !(username == "system:serviceaccount:tenant-system:database-controller"
            && before.metadata.deletion_timestamp.is_some()
            && new_finalizers.is_empty())
    {
        return Err(AdmissionError::Reservation);
    }
    Ok(())
}

async fn review_create(
    client: Client,
    request: AdmissionRequest,
    validating: bool,
) -> Result<Option<Value>, AdmissionError> {
    if request.uid.is_empty()
        || request.operation != "CREATE"
        || request.kind.group != crate::api::GROUP
        || request.kind.version != crate::api::VERSION
        || request.kind.kind != "TenantDatabase"
        || request.resource.group != crate::api::GROUP
        || request.resource.version != crate::api::VERSION
        || request.resource.resource != "tenantdatabases"
        || request.object.get("apiVersion").and_then(Value::as_str)
            != Some("tenancy.cnpg-vcluster.io/v1alpha1")
        || request.object.get("kind").and_then(Value::as_str) != Some("TenantDatabase")
    {
        return Err(AdmissionError::Request);
    }
    if activation_lock_probe(&request) {
        return Ok(None);
    }
    if request
        .user_info
        .as_ref()
        .map(|user| user.username.as_str())
        != Some("system:serviceaccount:tenant-system:database-controller")
    {
        return Err(AdmissionError::Request);
    }
    let mut database: TenantDatabase =
        serde_json::from_value(request.object).map_err(|_| AdmissionError::Spec)?;
    if database.metadata.uid.is_some()
        || database.metadata.deletion_timestamp.is_some()
        || database
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || database.metadata.finalizers.as_deref() != Some(&[crate::api::FINALIZER.into()])
    {
        return Err(AdmissionError::Request);
    }
    if database.metadata.namespace.is_some() && database.metadata.namespace != request.namespace {
        return Err(AdmissionError::Request);
    }
    database.metadata.namespace = request.namespace;
    if validating {
        admit_create(client, &database).await?;
    }
    Ok(None)
}

fn activation_lock_probe(request: &AdmissionRequest) -> bool {
    // A dry-run probe cannot persist; letting it pass the webhook isolates policy denial.
    let name = request
        .object
        .pointer("/metadata/name")
        .and_then(Value::as_str);
    request.dry_run
        && request.namespace.as_deref() == Some("tenant-system")
        && request
            .object
            .pointer("/metadata/namespace")
            .and_then(Value::as_str)
            == Some("tenant-system")
        && name
            .and_then(|name| name.strip_prefix("database-lock-probe-"))
            .is_some_and(|suffix| {
                suffix.len() == 8 && suffix.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        && request.object.get("spec")
            == Some(&json!({
                "tenantName": "database-lock-probe", "tenantUID": "probe", "instances": 1
            }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{Request, Response};
    use k8s_openapi::api::core::v1::{ResourceQuotaSpec, ResourceQuotaStatus};
    use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
    use kube::client::Body;
    use std::sync::{Arc, Mutex};
    use tenant_controller::api::{DatabaseCapability, TenantSpec, TenantStatus};
    use tower::service_fn;

    #[test]
    fn quota_namespace_and_gate_must_be_exact() {
        let database = TenantDatabase::new(
            "orders",
            crate::api::TenantDatabaseSpec {
                tenant_name: "test".into(),
                tenant_uid: "uid-1".into(),
                instances: 3,
            },
        );
        let mut database = database;
        database.metadata.namespace = Some("tenant-db-test".into());
        let mut tenant = Tenant::new("test", TenantSpec::local("1.36.4", 1));
        tenant.metadata.uid = Some("uid-1".into());
        tenant.metadata.generation = Some(1);
        tenant.status = Some(TenantStatus {
            observed_generation: Some(1),
            phase: Some(TenantPhase::Ready),
            conditions: vec![k8s_openapi::apimachinery::pkg::apis::meta::v1::Condition {
                type_: "Ready".into(),
                status: "True".into(),
                observed_generation: Some(1),
                reason: "Ready".into(),
                message: String::new(),
                last_transition_time: serde_json::from_str(r#""2026-01-01T00:00:00Z""#).unwrap(),
            }],
            database_capability: Some(DatabaseCapability {
                available: true,
                reason: "Ready".into(),
                namespace_uid: "namespace-uid".into(),
                quota_uid: "quota-uid".into(),
                gate_uid: "gate-uid".into(),
                storage_namespace_uid: None,
            }),
            ..Default::default()
        });
        let metadata = ObjectMeta {
            labels: Some(BTreeMap::from([(TENANT_UID.into(), "uid-1".into())])),
            ..Default::default()
        };
        let namespace = Namespace {
            metadata: ObjectMeta {
                name: Some("tenant-db-test".into()),
                uid: Some("namespace-uid".into()),
                ..metadata.clone()
            },
            ..Default::default()
        };
        let mut quota = ResourceQuota {
            metadata: ObjectMeta {
                name: Some(QUOTA_NAME.into()),
                uid: Some("quota-uid".into()),
                namespace: Some("tenant-db-test".into()),
                ..metadata.clone()
            },
            spec: Some(ResourceQuotaSpec {
                hard: Some(BTreeMap::from([(QUOTA_KEY.into(), Quantity("3".into()))])),
                ..Default::default()
            }),
            status: Some(ResourceQuotaStatus::default()),
        };
        let gate = ConfigMap {
            metadata: ObjectMeta {
                name: Some(gate_name("uid-1")),
                namespace: Some(GATE_NAMESPACE.into()),
                uid: Some("gate-uid".into()),
                ..metadata
            },
            data: Some(BTreeMap::from([
                (GATE_TENANT_UID.into(), "uid-1".into()),
                (GATE_STATE.into(), "open".into()),
                (
                    GATE_ENTRIES.into(),
                    serde_json::to_string(&BTreeMap::from([(
                        "orders",
                        GateEntry {
                            instances: 3,
                            lifecycle: IntentLifecycle::Materializing,
                            bound_uid: None,
                        },
                    )]))
                    .unwrap(),
                ),
            ])),
            ..Default::default()
        };
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Ok(())
        );
        tenant.status.as_mut().unwrap().conditions[0].status = "False".into();
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Err(AdmissionError::Tenant)
        );
        tenant.status.as_mut().unwrap().conditions[0].status = "True".into();
        quota
            .spec
            .as_mut()
            .unwrap()
            .hard
            .as_mut()
            .unwrap()
            .insert("requests.cpu".into(), Quantity("1".into()));
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Err(AdmissionError::Quota)
        );
        quota
            .spec
            .as_mut()
            .unwrap()
            .hard
            .as_mut()
            .unwrap()
            .remove("requests.cpu");
        quota.spec.as_mut().unwrap().scopes = Some(vec!["BestEffort".into()]);
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Err(AdmissionError::Quota)
        );
        quota.spec.as_mut().unwrap().scopes = None;
        quota.metadata.uid = Some("successor-quota".into());
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Err(AdmissionError::Quota)
        );
        quota.metadata.uid = Some("quota-uid".into());
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Ok(())
        );
        let mut gate = gate;
        gate.data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Err(AdmissionError::Gate)
        );
        gate.data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "open".into());
        gate.metadata.uid = Some("successor-gate".into());
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Err(AdmissionError::Gate)
        );
        gate.metadata.uid = Some("gate-uid".into());
        gate.data
            .as_mut()
            .unwrap()
            .insert(GATE_ENTRIES.into(), "[]".into());
        assert_eq!(
            validate_request(&database, &tenant, &namespace, &quota, &gate),
            Err(AdmissionError::Gate)
        );
    }

    #[test]
    fn create_requires_claimed_named_intent_and_exact_controller_metadata() {
        let mut database = TenantDatabase::new(
            "orders",
            crate::api::TenantDatabaseSpec {
                tenant_name: "test".into(),
                tenant_uid: "uid-1".into(),
                instances: 2,
            },
        );
        database.metadata.namespace = Some("tenant-db-test".into());
        database.metadata.finalizers = Some(vec![crate::api::FINALIZER.into()]);
        database.metadata.annotations = Some(BTreeMap::from([
            (TENANT_UID.into(), "uid-1".into()),
            (GATE_UID.into(), "gate-uid".into()),
            (SPEC_SHA256.into(), canonical_spec_hash(&database).unwrap()),
        ]));
        let mut gate = ConfigMap {
            metadata: ObjectMeta {
                name: Some(gate_name("uid-1")),
                namespace: Some(GATE_NAMESPACE.into()),
                uid: Some("gate-uid".into()),
                labels: Some(BTreeMap::from([(TENANT_UID.into(), "uid-1".into())])),
                ..Default::default()
            },
            data: Some(BTreeMap::from([
                (GATE_TENANT_UID.into(), "uid-1".into()),
                (GATE_STATE.into(), "open".into()),
                (GATE_ENTRIES.into(), "{}".into()),
            ])),
            ..Default::default()
        };
        let set_entry = |gate: &mut ConfigMap, entry: GateEntry| {
            gate.data.as_mut().unwrap().insert(
                GATE_ENTRIES.into(),
                serde_json::to_string(&BTreeMap::from([("orders", entry)])).unwrap(),
            );
        };
        assert_eq!(
            validate_controller_metadata(&database, &gate),
            Err(AdmissionError::Reservation)
        );
        let mut entry = GateEntry {
            instances: 2,
            lifecycle: IntentLifecycle::Pending,
            bound_uid: None,
        };
        set_entry(&mut gate, entry.clone());
        assert_eq!(
            validate_controller_metadata(&database, &gate),
            Err(AdmissionError::Reservation)
        );
        entry.lifecycle = IntentLifecycle::Materializing;
        set_entry(&mut gate, entry.clone());
        assert_eq!(validate_controller_metadata(&database, &gate), Ok(()));
        database
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .insert(GATE_UID.into(), "replacement".into());
        assert_eq!(
            validate_controller_metadata(&database, &gate),
            Err(AdmissionError::Reservation)
        );
        database
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .insert(GATE_UID.into(), "gate-uid".into());
        entry.instances = 3;
        set_entry(&mut gate, entry.clone());
        assert_eq!(
            validate_controller_metadata(&database, &gate),
            Err(AdmissionError::Reservation)
        );
        entry.instances = 2;
        entry.lifecycle = IntentLifecycle::Bound;
        entry.bound_uid = Some("database-uid".into());
        set_entry(&mut gate, entry);
        assert_eq!(
            validate_controller_metadata(&database, &gate),
            Err(AdmissionError::Reservation)
        );
    }

    #[test]
    fn gate_updates_separate_admin_claim_bind_and_close() {
        let mut gate = ConfigMap {
            metadata: ObjectMeta {
                name: Some(gate_name("uid-1")),
                namespace: Some(GATE_NAMESPACE.into()),
                uid: Some("gate-uid".into()),
                resource_version: Some("1".into()),
                labels: Some(BTreeMap::from([(TENANT_UID.into(), "uid-1".into())])),
                ..Default::default()
            },
            data: Some(BTreeMap::from([
                (GATE_TENANT_UID.into(), "uid-1".into()),
                (GATE_STATE.into(), "open".into()),
                (GATE_ENTRIES.into(), "{}".into()),
            ])),
            ..Default::default()
        };
        let admin = "system:serviceaccount:tenant-system:tenant-admin";
        let controller = "system:serviceaccount:tenant-system:database-controller";
        let tenant = "system:serviceaccount:tenant-system:tenant-controller";
        let open = gate.clone();
        let mut entries = BTreeMap::from([(
            "orders",
            GateEntry {
                instances: 2,
                lifecycle: IntentLifecycle::Pending,
                bound_uid: None,
            },
        )]);
        let set_entries = |gate: &mut ConfigMap, entries: &BTreeMap<&str, GateEntry>| {
            gate.data
                .as_mut()
                .unwrap()
                .insert(GATE_ENTRIES.into(), serde_json::to_string(entries).unwrap());
        };
        set_entries(&mut gate, &entries);
        assert_eq!(validate_gate_update(&open, &gate, admin), Ok(()));
        for actor in [
            controller,
            tenant,
            "system:serviceaccount:tenant-system:database-admission",
        ] {
            assert_eq!(
                validate_gate_update(&open, &gate, actor),
                Err(AdmissionError::Gate)
            );
        }
        let pending = gate.clone();
        entries.get_mut("orders").unwrap().lifecycle = IntentLifecycle::Materializing;
        set_entries(&mut gate, &entries);
        assert_eq!(validate_gate_update(&pending, &gate, controller), Ok(()));
        assert_eq!(
            validate_gate_update(&pending, &gate, admin),
            Err(AdmissionError::Gate)
        );
        let claimed = gate.clone();
        gate.data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        assert_eq!(validate_gate_update(&claimed, &gate, tenant), Ok(()));
        assert_eq!(
            validate_gate_update(&claimed, &gate, controller),
            Err(AdmissionError::Gate)
        );
        let closed = gate.clone();
        entries.get_mut("orders").unwrap().lifecycle = IntentLifecycle::Bound;
        entries.get_mut("orders").unwrap().bound_uid = Some("persisted-uid".into());
        set_entries(&mut gate, &entries);
        assert_eq!(validate_gate_update(&closed, &gate, controller), Ok(()));
        assert_eq!(
            validate_gate_update(&closed, &gate, admin),
            Err(AdmissionError::Gate)
        );
        let bound = gate.clone();
        set_entries(&mut gate, &BTreeMap::new());
        assert_eq!(validate_gate_update(&bound, &gate, tenant), Ok(()));
        let mut invalid = claimed.clone();
        invalid
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        invalid
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_ENTRIES.into(), "{}".into());
        assert_eq!(
            validate_gate_update(&claimed, &invalid, tenant),
            Err(AdmissionError::Gate)
        );
        let mut close_pending = pending.clone();
        close_pending
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        close_pending
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_ENTRIES.into(), "{}".into());
        assert_eq!(
            validate_gate_update(&pending, &close_pending, tenant),
            Ok(())
        );
        let mut closed_pending = pending.clone();
        closed_pending
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        let mut late_claim = closed_pending.clone();
        let mut claimed_entry = gate_entries(&closed_pending).unwrap();
        claimed_entry.get_mut("orders").unwrap().lifecycle = IntentLifecycle::Materializing;
        late_claim.data.as_mut().unwrap().insert(
            GATE_ENTRIES.into(),
            serde_json::to_string(&claimed_entry).unwrap(),
        );
        assert_eq!(
            validate_gate_update(&closed_pending, &late_claim, controller),
            Err(AdmissionError::Gate)
        );
        let mut late_admin = closed_pending.clone();
        let mut fourth = gate_entries(&closed_pending).unwrap();
        fourth.insert(
            "late".into(),
            GateEntry {
                instances: 1,
                lifecycle: IntentLifecycle::Pending,
                bound_uid: None,
            },
        );
        late_admin
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_ENTRIES.into(), serde_json::to_string(&fourth).unwrap());
        assert_eq!(
            validate_gate_update(&closed_pending, &late_admin, admin),
            Err(AdmissionError::Gate)
        );
    }

    #[tokio::test]
    async fn validating_create_checks_identity_before_reads_and_mutating_create_never_writes() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let observed = calls.clone();
        let client = Client::new(
            service_fn(move |request: Request<Body>| {
                let calls = observed.clone();
                async move {
                    calls.lock().unwrap().push(request.method().to_string());
                    Ok::<_, std::convert::Infallible>(Response::builder()
                    .status(503).header("content-type", "application/json")
                    .body(Body::from(r#"{"apiVersion":"v1","kind":"Status","status":"Failure","code":503,"reason":"ServiceUnavailable","message":"unavailable"}"#.as_bytes().to_vec()))
                    .unwrap())
                }
            }),
            "test",
        );
        let database = TenantDatabase::new(
            "orders",
            crate::api::TenantDatabaseSpec {
                tenant_name: "test".into(),
                tenant_uid: "uid-1".into(),
                instances: 1,
            },
        );
        let mut object = serde_json::to_value(database).unwrap();
        object["metadata"]["namespace"] = json!("tenant-db-test");
        object["metadata"]["finalizers"] = json!([crate::api::FINALIZER]);
        object["metadata"]["annotations"] = json!({
            TENANT_UID: "uid-1",
            GATE_UID: "gate-uid",
            SPEC_SHA256: "not-used-before-tenant-read"
        });
        let request = AdmissionRequest {
            uid: "request-1".into(),
            kind: GroupVersionKind {
                group: crate::api::GROUP.into(),
                version: crate::api::VERSION.into(),
                kind: "TenantDatabase".into(),
            },
            resource: GroupVersionResource {
                group: crate::api::GROUP.into(),
                version: crate::api::VERSION.into(),
                resource: "tenantdatabases".into(),
            },
            operation: "CREATE".into(),
            namespace: Some("tenant-db-test".into()),
            dry_run: false,
            object,
            old_object: None,
            user_info: Some(AdmissionUser {
                username: "system:serviceaccount:tenant-system:tenant-admin".into(),
            }),
        };
        assert_eq!(
            review_create(client.clone(), request.clone(), true).await,
            Err(AdmissionError::Request)
        );
        assert_eq!(
            review_create(client.clone(), request.clone(), false).await,
            Err(AdmissionError::Request)
        );
        assert!(calls.lock().unwrap().is_empty());
        let mut controller = request;
        controller.user_info = Some(AdmissionUser {
            username: "system:serviceaccount:tenant-system:database-controller".into(),
        });
        assert_eq!(
            review_create(client.clone(), controller.clone(), false).await,
            Ok(None)
        );
        assert!(calls.lock().unwrap().is_empty());
        assert_eq!(
            review_create(client, controller, true).await,
            Err(AdmissionError::Tenant)
        );
        assert_eq!(&*calls.lock().unwrap(), &["GET"]);
    }

    #[test]
    fn admitted_metadata_cannot_be_changed_or_unfinalized_by_other_actors() {
        let mut original = TenantDatabase::new(
            "orders",
            crate::api::TenantDatabaseSpec {
                tenant_name: "test".into(),
                tenant_uid: "uid-1".into(),
                instances: 1,
            },
        );
        original.metadata.namespace = Some("tenant-db-test".into());
        original.metadata.uid = Some("database-uid".into());
        original.metadata.finalizers = Some(vec![crate::api::FINALIZER.into()]);
        original.metadata.annotations = Some(BTreeMap::from([
            (TENANT_UID.into(), "uid-1".into()),
            (GATE_UID.into(), "gate-uid".into()),
            (SPEC_SHA256.into(), canonical_spec_hash(&original).unwrap()),
        ]));
        let namespace = Some("tenant-db-test");
        let admin = "system:serviceaccount:tenant-system:tenant-admin";
        let controller = "system:serviceaccount:tenant-system:database-controller";
        assert_eq!(
            validate_database_update(&original, &original, admin, namespace),
            Ok(())
        );
        let mut modified = original.clone();
        modified
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .remove(GATE_UID);
        assert_eq!(
            validate_database_update(&original, &modified, controller, namespace),
            Err(AdmissionError::Reservation)
        );
        let mut modified = original.clone();
        modified.metadata.finalizers = None;
        assert_eq!(
            validate_database_update(&original, &modified, admin, namespace),
            Err(AdmissionError::Reservation)
        );
        assert_eq!(
            validate_database_update(&original, &modified, controller, namespace),
            Err(AdmissionError::Reservation)
        );
        original.metadata.deletion_timestamp =
            Some(serde_json::from_str(r#""2026-01-01T00:00:00Z""#).unwrap());
        modified.metadata.deletion_timestamp = original.metadata.deletion_timestamp.clone();
        assert_eq!(
            validate_database_update(&original, &modified, controller, namespace),
            Ok(())
        );
        assert_eq!(
            validate_database_update(&original, &modified, admin, namespace),
            Err(AdmissionError::Reservation)
        );
    }

    #[test]
    fn validating_review_rejects_untrusted_gate_close_and_missing_old_object() {
        let gate = ConfigMap {
            metadata: ObjectMeta {
                name: Some(gate_name("uid-1")),
                namespace: Some(GATE_NAMESPACE.into()),
                uid: Some("gate-uid".into()),
                resource_version: Some("1".into()),
                labels: Some(BTreeMap::from([(TENANT_UID.into(), "uid-1".into())])),
                ..Default::default()
            },
            data: Some(BTreeMap::from([
                (GATE_TENANT_UID.into(), "uid-1".into()),
                (GATE_STATE.into(), "open".into()),
                (GATE_ENTRIES.into(), "{}".into()),
            ])),
            ..Default::default()
        };
        let mut closed = gate.clone();
        closed
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        let request = AdmissionRequest {
            uid: "1234-5678".into(),
            kind: GroupVersionKind {
                group: String::new(),
                version: "v1".into(),
                kind: "ConfigMap".into(),
            },
            resource: GroupVersionResource {
                group: String::new(),
                version: "v1".into(),
                resource: "configmaps".into(),
            },
            operation: "UPDATE".into(),
            namespace: Some(GATE_NAMESPACE.into()),
            dry_run: false,
            object: serde_json::to_value(&closed).unwrap(),
            old_object: Some(serde_json::to_value(&gate).unwrap()),
            user_info: Some(AdmissionUser {
                username: "system:serviceaccount:tenant-system:database-admission".into(),
            }),
        };
        let mut missing = request.clone();
        missing.old_object = None;
        assert_eq!(review_update(missing).unwrap_err(), AdmissionError::Request);
        assert_eq!(
            review_update(request.clone()).unwrap_err(),
            AdmissionError::Gate
        );
        let mut trusted = request;
        trusted.user_info = Some(AdmissionUser {
            username: "system:serviceaccount:tenant-system:tenant-controller".into(),
        });
        assert!(review_update(trusted).is_ok());
    }

    #[tokio::test]
    async fn activation_probe_bypasses_webhook_only_when_nonpersisting_and_exact() {
        let client = Client::new(
            service_fn(|_request: Request<Body>| async {
                Ok::<_, std::convert::Infallible>(
                    Response::builder().status(503)
                        .header("content-type", "application/json")
                        .body(Body::from(r#"{"apiVersion":"v1","kind":"Status","status":"Failure","code":503,"reason":"ServiceUnavailable","message":"unavailable"}"#.as_bytes().to_vec()))
                        .unwrap()
                )
            }),
            "test",
        );
        let probe = AdmissionRequest {
            uid: "1234-5678".into(),
            kind: GroupVersionKind {
                group: crate::api::GROUP.into(),
                version: crate::api::VERSION.into(),
                kind: "TenantDatabase".into(),
            },
            resource: GroupVersionResource {
                group: crate::api::GROUP.into(),
                version: crate::api::VERSION.into(),
                resource: "tenantdatabases".into(),
            },
            operation: "CREATE".into(),
            namespace: Some("tenant-system".into()),
            dry_run: true,
            object: json!({
                "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1", "kind": "TenantDatabase",
                "metadata": {"name": "database-lock-probe-1234abcd", "namespace": "tenant-system"},
                "spec": {"tenantName": "database-lock-probe", "tenantUID": "probe", "instances": 1}
            }),
            old_object: None,
            user_info: None,
        };
        assert_eq!(
            review_create(client.clone(), probe.clone(), false)
                .await
                .unwrap(),
            None
        );
        assert_eq!(
            review_create(client.clone(), probe.clone(), true)
                .await
                .unwrap(),
            None
        );
        let mut persistent = probe.clone();
        persistent.dry_run = false;
        persistent.user_info = Some(AdmissionUser {
            username: "system:serviceaccount:tenant-system:database-controller".into(),
        });
        assert_eq!(
            review_create(client.clone(), persistent, false)
                .await
                .unwrap_err(),
            AdmissionError::Request
        );
        let mut untrusted = probe.clone();
        untrusted.dry_run = false;
        assert_eq!(
            review_create(client.clone(), untrusted, true)
                .await
                .unwrap_err(),
            AdmissionError::Request
        );
        let mut different = probe;
        different.object["metadata"]["name"] = json!("database-lock-probe-other");
        different.user_info = Some(AdmissionUser {
            username: "system:serviceaccount:tenant-system:database-controller".into(),
        });
        assert_eq!(
            review_create(client, different, false).await.unwrap_err(),
            AdmissionError::Request
        );
    }
}
