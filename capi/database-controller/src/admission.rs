use std::collections::BTreeMap;

use base64::{Engine, engine::general_purpose::STANDARD};
use k8s_openapi::api::core::v1::{ConfigMap, Namespace, ResourceQuota};
use kube::{Api, Client, ResourceExt, api::PostParams};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tenant_controller::api::{Tenant, TenantPhase};

use crate::api::{TenantDatabase, validate_spec};

pub const GATE_NAMESPACE: &str = "tenant-database-gates";
pub const QUOTA_NAME: &str = "tenant-database-limit";
pub const QUOTA_KEY: &str = "count/tenantdatabases.tenancy.cnpg-vcluster.io";
pub const TENANT_UID: &str = "tenancy.cnpg-vcluster.io/tenant-uid";
pub const GATE_UID: &str = "tenancy.cnpg-vcluster.io/gate-uid";
pub const ADMISSION_UID: &str = "tenancy.cnpg-vcluster.io/admission-request-uid";
pub const SPEC_SHA256: &str = "tenancy.cnpg-vcluster.io/database-spec-sha256";
pub const GATE_ENTRIES: &str = "reservations";
pub const GATE_STATE: &str = "state";
pub const GATE_TENANT_UID: &str = "tenantUID";
const MAX_PENDING_REQUESTS: usize = 32;

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
    #[error("database create gate entry is missing or replaced")]
    Reservation,
}

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GateEntry {
    pub namespace: String,
    pub name: String,
    pub spec_sha256: String,
    pub created_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bound_uid: Option<String>,
}

pub fn database_namespace(tenant_name: &str) -> String {
    format!("tenant-db-{tenant_name}")
}

pub fn gate_name(tenant_uid: &str) -> String {
    format!(
        "tenant-db-gate-{}",
        hex::encode(Sha256::digest(tenant_uid.as_bytes()))[..24].to_owned()
    )
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
    validate_request_inner(database, tenant, namespace, quota, gate, false)
}

fn validate_request_inner(
    database: &TenantDatabase,
    tenant: &Tenant,
    namespace: &Namespace,
    quota: &ResourceQuota,
    gate: &ConfigMap,
    reserved: bool,
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
        || (!reserved && tenant.metadata.deletion_timestamp.is_some())
        || (!reserved
            && tenant.status.as_ref().and_then(|status| status.phase) != Some(TenantPhase::Ready))
        || (!reserved
            && tenant
                .status
                .as_ref()
                .and_then(|status| status.observed_generation)
                != tenant.metadata.generation)
        || (!reserved
            && !tenant.status.as_ref().is_some_and(|status| {
                status.conditions.iter().any(|condition| {
                    condition.type_ == "Ready"
                        && condition.status == "True"
                        && condition.observed_generation == tenant.metadata.generation
                })
            }))
    {
        return Err(AdmissionError::Tenant);
    }
    let capability = tenant
        .status
        .as_ref()
        .and_then(|status| status.database_capability.as_ref())
        .filter(|capability| reserved || capability.available)
        .ok_or(AdmissionError::Capability)?;
    if database.metadata.namespace.as_deref() != Some(expected_namespace.as_str())
        || namespace.name_any() != expected_namespace
        || (!reserved && namespace.metadata.deletion_timestamp.is_some())
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
        || (!reserved && quota.metadata.deletion_timestamp.is_some())
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
    if !reserved
        && gate
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
    Ok(())
}

pub fn validate_gate(gate: &ConfigMap, tenant_uid: &str) -> Result<(), AdmissionError> {
    if gate.name_any() != gate_name(tenant_uid)
        || gate.metadata.namespace.as_deref() != Some(GATE_NAMESPACE)
        || gate.metadata.uid.as_deref().is_none_or(str::is_empty)
        || gate.metadata.deletion_timestamp.is_some()
        || gate
            .metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(TENANT_UID))
            .map(String::as_str)
            != Some(tenant_uid)
        || gate
            .data
            .as_ref()
            .and_then(|data| data.get(GATE_TENANT_UID))
            .map(String::as_str)
            != Some(tenant_uid)
        || gate.data.as_ref().is_none_or(|data| data.len() != 3)
        || !matches!(
            gate.data
                .as_ref()
                .and_then(|data| data.get(GATE_STATE))
                .map(String::as_str),
            Some("open" | "closed")
        )
        || gate_entries(gate).is_err()
    {
        return Err(AdmissionError::Gate);
    }
    Ok(())
}

pub fn gate_entries(gate: &ConfigMap) -> Result<BTreeMap<String, GateEntry>, AdmissionError> {
    let encoded = gate
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_ENTRIES))
        .ok_or(AdmissionError::Gate)?;
    let entries: BTreeMap<String, GateEntry> =
        serde_json::from_str(encoded).map_err(|_| AdmissionError::Gate)?;
    if entries.len() > MAX_PENDING_REQUESTS
        || entries.iter().any(|(uid, entry)| {
            !valid_request_uid(uid)
                || entry.namespace.is_empty()
                || entry.name.is_empty()
                || entry.spec_sha256.len() != 64
                || chrono::DateTime::parse_from_rfc3339(&entry.created_at).is_err()
                || entry.bound_uid.as_deref().is_some_and(str::is_empty)
        })
    {
        return Err(AdmissionError::Gate);
    }
    Ok(entries)
}

fn valid_request_uid(uid: &str) -> bool {
    (1..=64).contains(&uid.len())
        && uid
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() || byte == b'-')
}

fn entry_matches(entry: &GateEntry, database: &TenantDatabase) -> Result<bool, AdmissionError> {
    Ok(
        entry.namespace == database.metadata.namespace.as_deref().unwrap_or_default()
            && entry.name == database.metadata.name.as_deref().unwrap_or_default()
            && entry.spec_sha256 == canonical_spec_hash(database)?
            && entry.bound_uid.is_none(),
    )
}

fn reservation_update(
    gate: &ConfigMap,
    database: &TenantDatabase,
    request_uid: &str,
) -> Result<Option<ConfigMap>, AdmissionError> {
    validate_gate(gate, &database.spec.tenant_uid)?;
    let mut entries = gate_entries(gate)?;
    if let Some(entry) = entries.get(request_uid) {
        return if entry_matches(entry, database)? {
            Ok(None)
        } else {
            Err(AdmissionError::Reservation)
        };
    }
    if gate
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_STATE))
        .map(String::as_str)
        != Some("open")
        || entries.len() >= MAX_PENDING_REQUESTS
    {
        return Err(AdmissionError::Gate);
    }
    entries.insert(
        request_uid.into(),
        GateEntry {
            namespace: database
                .metadata
                .namespace
                .clone()
                .ok_or(AdmissionError::Request)?,
            name: database
                .metadata
                .name
                .clone()
                .ok_or(AdmissionError::Request)?,
            spec_sha256: canonical_spec_hash(database)?,
            created_at: chrono::Utc::now().to_rfc3339(),
            bound_uid: None,
        },
    );
    let mut updated = gate.clone();
    updated.data.as_mut().ok_or(AdmissionError::Gate)?.insert(
        GATE_ENTRIES.into(),
        serde_json::to_string(&entries).map_err(|_| AdmissionError::Gate)?,
    );
    Ok(Some(updated))
}

pub fn validate_injected_reservation(
    database: &TenantDatabase,
    gate: &ConfigMap,
) -> Result<(), AdmissionError> {
    let annotations = database
        .metadata
        .annotations
        .as_ref()
        .ok_or(AdmissionError::Reservation)?;
    let request_uid = annotations
        .get(ADMISSION_UID)
        .ok_or(AdmissionError::Reservation)?;
    validate_gate(gate, &database.spec.tenant_uid)?;
    let entry = gate_entries(gate)?
        .get(request_uid)
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
        || !entry_matches(&entry, database)?
    {
        return Err(AdmissionError::Reservation);
    }
    Ok(())
}

pub fn admission_patch(
    gate: &ConfigMap,
    database: &TenantDatabase,
    request_uid: &str,
) -> Result<Value, AdmissionError> {
    let gate_uid = gate
        .metadata
        .uid
        .as_deref()
        .filter(|value| !value.is_empty())
        .ok_or(AdmissionError::Gate)?;
    let mut finalizers = database.metadata.finalizers.clone().unwrap_or_default();
    if !finalizers.iter().any(|item| item == crate::api::FINALIZER) {
        finalizers.push(crate::api::FINALIZER.into());
    }

    let mut annotations = database.metadata.annotations.clone().unwrap_or_default();
    annotations.insert(GATE_UID.into(), gate_uid.into());
    annotations.insert(ADMISSION_UID.into(), request_uid.into());
    annotations.insert(TENANT_UID.into(), database.spec.tenant_uid.clone());
    annotations.insert(SPEC_SHA256.into(), canonical_spec_hash(database)?);
    Ok(json!([
        {"op": if database.metadata.finalizers.is_some() {"replace"} else {"add"},
         "path": "/metadata/finalizers", "value": finalizers},
        {"op": if database.metadata.annotations.is_some() {"replace"} else {"add"},
         "path": "/metadata/annotations", "value": annotations},
    ]))
}

async fn reserve_entry(
    gates: &Api<ConfigMap>,
    database: &TenantDatabase,
    request_uid: &str,
    expected_uid: &str,
) -> Result<ConfigMap, AdmissionError> {
    for _ in 0..8 {
        let gate = gates
            .get(&gate_name(&database.spec.tenant_uid))
            .await
            .map_err(|_| AdmissionError::Gate)?;
        validate_gate(&gate, &database.spec.tenant_uid)?;
        if gate.metadata.uid.as_deref() != Some(expected_uid) {
            return Err(AdmissionError::Gate);
        }
        let Some(updated) = reservation_update(&gate, database, request_uid)? else {
            return Ok(gate);
        };
        match gates
            .replace(&gate.name_any(), &PostParams::default(), &updated)
            .await
        {
            Ok(saved) => {
                if saved.metadata.uid != gate.metadata.uid
                    || !gate_entries(&saved)?
                        .get(request_uid)
                        .is_some_and(|entry| entry_matches(entry, database) == Ok(true))
                {
                    return Err(AdmissionError::Gate);
                }
                return Ok(saved);
            }
            Err(kube::Error::Api(error)) if error.code == 409 => continue,
            Err(_) => return Err(AdmissionError::Gate),
        }
    }
    Err(AdmissionError::Gate)
}

pub async fn admit_create(
    client: Client,
    database: &TenantDatabase,
    request_uid: &str,
    dry_run: bool,
) -> Result<Option<Value>, AdmissionError> {
    if !valid_request_uid(request_uid) {
        return Err(AdmissionError::Request);
    }
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
    let gates = Api::<ConfigMap>::namespaced(client.clone(), GATE_NAMESPACE);
    let gate = gates
        .get(&gate_name(&database.spec.tenant_uid))
        .await
        .map_err(|_| AdmissionError::Gate)?;
    let reserved = !dry_run
        && gate_entries(&gate)?
            .get(request_uid)
            .is_some_and(|entry| entry_matches(entry, database) == Ok(true));
    validate_request_inner(database, &tenant, &namespace, &quota, &gate, reserved)?;
    if reserved {
        return Ok(Some(admission_patch(&gate, database, request_uid)?));
    }
    if dry_run {
        return Ok(Some(admission_patch(&gate, database, request_uid)?));
    }
    let gate = reserve_entry(
        &gates,
        database,
        request_uid,
        &tenant
            .status
            .as_ref()
            .and_then(|status| status.database_capability.as_ref())
            .ok_or(AdmissionError::Capability)?
            .gate_uid,
    )
    .await?;
    Ok(Some(admission_patch(&gate, database, request_uid)?))
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
    if request.uid.is_empty() || !valid_request_uid(&request.uid) {
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
        "system:serviceaccount:tenant-system:database-admission" => {
            if old_state != "open"
                || new_state != "open"
                || old_entries
                    .iter()
                    .any(|(uid, entry)| new_entries.get(uid) != Some(entry))
            {
                return Err(AdmissionError::Gate);
            }
        }
        "system:serviceaccount:tenant-system:database-controller" => {
            if old_state != new_state
                || new_entries.iter().any(|(uid, entry)| {
                    old_entries.get(uid).is_none_or(|previous| {
                        let mut expected = previous.clone();
                        if expected.bound_uid.is_none() {
                            expected.bound_uid = entry.bound_uid.clone();
                        }
                        entry != &expected
                    })
                })
                || old_entries
                    .iter()
                    .any(|(uid, entry)| !new_entries.contains_key(uid) && entry.bound_uid.is_none())
            {
                return Err(AdmissionError::Gate);
            }
        }
        "system:serviceaccount:tenant-system:tenant-controller" => {
            if new_state != "closed"
                || old_entries.iter().any(|(uid, entry)| {
                    new_entries.get(uid).is_some_and(|current| current != entry)
                })
                || new_entries.keys().any(|uid| !old_entries.contains_key(uid))
                || (old_state == "open" && old_entries != new_entries)
                || old_entries
                    .iter()
                    .any(|(uid, entry)| !new_entries.contains_key(uid) && entry.bound_uid.is_some())
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
        || [GATE_UID, TENANT_UID, ADMISSION_UID, SPEC_SHA256]
            .iter()
            .any(|key| {
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
    let mut database: TenantDatabase =
        serde_json::from_value(request.object).map_err(|_| AdmissionError::Spec)?;
    if database.metadata.uid.is_some()
        || database.metadata.deletion_timestamp.is_some()
        || database
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || database
            .metadata
            .finalizers
            .as_ref()
            .is_some_and(|finalizers| {
                finalizers
                    .iter()
                    .any(|value| value != crate::api::FINALIZER)
            })
        || (!validating
            && database
                .metadata
                .annotations
                .as_ref()
                .is_some_and(|annotations| {
                    [TENANT_UID, GATE_UID, ADMISSION_UID, SPEC_SHA256]
                        .iter()
                        .any(|key| annotations.contains_key(*key))
                }))
    {
        return Err(AdmissionError::Request);
    }
    if database.metadata.namespace.is_some() && database.metadata.namespace != request.namespace {
        return Err(AdmissionError::Request);
    }
    database.metadata.namespace = request.namespace;
    if !validating {
        return admit_create(client, &database, &request.uid, request.dry_run).await;
    }
    if request.dry_run {
        let gate = Api::<ConfigMap>::namespaced(client.clone(), GATE_NAMESPACE)
            .get(&gate_name(&database.spec.tenant_uid))
            .await
            .map_err(|_| AdmissionError::Gate)?;
        let tenant = Api::<Tenant>::all(client.clone())
            .get(&database.spec.tenant_name)
            .await
            .map_err(|_| AdmissionError::Tenant)?;
        let namespace = Api::<Namespace>::all(client.clone())
            .get(&database_namespace(&database.spec.tenant_name))
            .await
            .map_err(|_| AdmissionError::Namespace)?;
        let quota = Api::<ResourceQuota>::namespaced(
            client,
            &database_namespace(&database.spec.tenant_name),
        )
        .get(QUOTA_NAME)
        .await
        .map_err(|_| AdmissionError::Quota)?;
        validate_request(&database, &tenant, &namespace, &quota, &gate)?;
        validate_injected_metadata(&database, &gate)?;
        return Ok(None);
    }
    let gate = Api::<ConfigMap>::namespaced(client, GATE_NAMESPACE)
        .get(&gate_name(&database.spec.tenant_uid))
        .await
        .map_err(|_| AdmissionError::Gate)?;
    validate_injected_reservation(&database, &gate)?;
    Ok(None)
}

fn validate_injected_metadata(
    database: &TenantDatabase,
    gate: &ConfigMap,
) -> Result<(), AdmissionError> {
    let annotations = database
        .metadata
        .annotations
        .as_ref()
        .ok_or(AdmissionError::Reservation)?;
    if database.metadata.finalizers.as_deref() != Some(&[crate::api::FINALIZER.into()])
        || annotations.get(GATE_UID).map(String::as_str) != gate.metadata.uid.as_deref()
        || annotations.get(TENANT_UID).map(String::as_str) != Some(&database.spec.tenant_uid)
        || annotations.get(SPEC_SHA256).map(String::as_str)
            != Some(canonical_spec_hash(database)?.as_str())
        || !annotations
            .get(ADMISSION_UID)
            .is_some_and(|uid| valid_request_uid(uid))
    {
        return Err(AdmissionError::Reservation);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    use axum::http::{Request, Response};
    use http_body_util::BodyExt;
    use k8s_openapi::api::core::v1::{ResourceQuotaSpec, ResourceQuotaStatus};
    use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
    use kube::client::Body;
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
                (GATE_ENTRIES.into(), "{}".into()),
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
    fn conditional_entry_survives_closure_and_replay_but_refuses_new_creates() {
        let mut database = TenantDatabase::new(
            "orders",
            crate::api::TenantDatabaseSpec {
                tenant_name: "test".into(),
                tenant_uid: "uid-1".into(),
                instances: 1,
            },
        );
        database.metadata.namespace = Some("tenant-db-test".into());
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
        let mut reserved = reservation_update(&gate, &database, "1234-5678")
            .unwrap()
            .unwrap();
        assert_eq!(reserved.metadata.resource_version.as_deref(), Some("1"));
        reserved.metadata.resource_version = Some("2".into());
        assert_eq!(gate_entries(&reserved).unwrap().len(), 1);
        reserved
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        assert_eq!(
            reservation_update(&reserved, &database, "1234-5678").unwrap(),
            None
        );
        assert_eq!(
            reservation_update(&reserved, &database, "8765-4321").unwrap_err(),
            AdmissionError::Gate
        );
        assert_eq!(
            admission_patch(&reserved, &database, "1234-5678").unwrap()[0]["value"][0],
            crate::api::FINALIZER
        );
        assert_eq!(
            admission_patch(&reserved, &database, "1234-5678").unwrap()[1]["value"][GATE_UID],
            "gate-uid"
        );
        let patch = admission_patch(&reserved, &database, "1234-5678").unwrap();
        database.metadata.finalizers = serde_json::from_value(patch[0]["value"].clone()).unwrap();
        database.metadata.annotations = serde_json::from_value(patch[1]["value"].clone()).unwrap();
        assert_eq!(validate_injected_reservation(&database, &reserved), Ok(()));
        assert_ne!(
            database.metadata.annotations.as_ref().unwrap()[ADMISSION_UID],
            "validation-webhook-has-a-different-request-uid"
        );
        database
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .insert(GATE_UID.into(), "successor-uid".into());
        assert_eq!(
            validate_injected_reservation(&database, &reserved),
            Err(AdmissionError::Reservation)
        );
        assert_eq!(gate_entries(&reserved).unwrap().len(), 1);
        let mut different = database;
        different.metadata.name = Some("other".into());
        assert_eq!(
            reservation_update(&reserved, &different, "1234-5678").unwrap_err(),
            AdmissionError::Reservation
        );
    }

    #[tokio::test]
    async fn gate_close_and_reservation_use_the_same_resource_version() {
        for close_before_update in [true, false] {
            let mut database = TenantDatabase::new(
                "orders",
                crate::api::TenantDatabaseSpec {
                    tenant_name: "test".into(),
                    tenant_uid: "uid-1".into(),
                    instances: 1,
                },
            );
            database.metadata.namespace = Some("tenant-db-test".into());
            let state = Arc::new(Mutex::new(ConfigMap {
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
            }));
            let server_state = state.clone();
            let client = Client::new(
                service_fn(move |request: Request<Body>| {
                    let state = server_state.clone();
                    async move {
                        let method = request.method().as_str().to_owned();
                        let path = request.uri().path().to_owned();
                        assert_eq!(
                            path,
                            format!(
                                "/api/v1/namespaces/{GATE_NAMESPACE}/configmaps/{}",
                                gate_name("uid-1")
                            )
                        );
                        let bytes = request.into_body().collect().await.unwrap().to_bytes();
                        let mut current = state.lock().unwrap();
                        let (code, response) = match method.as_str() {
                            "GET" => (200, serde_json::to_value(&*current).unwrap()),
                            "PUT" => {
                                let mut proposed: ConfigMap =
                                    serde_json::from_slice(&bytes).unwrap();
                                assert_eq!(
                                    proposed.metadata.resource_version.as_deref(),
                                    Some("1")
                                );
                                if close_before_update {
                                    current
                                        .data
                                        .as_mut()
                                        .unwrap()
                                        .insert(GATE_STATE.into(), "closed".into());
                                    current.metadata.resource_version = Some("2".into());
                                    (
                                        409,
                                        json!({
                                            "apiVersion":"v1", "kind":"Status",
                                            "status":"Failure", "reason":"Conflict", "code":409,
                                            "message":"gate changed"
                                        }),
                                    )
                                } else {
                                    proposed.metadata.resource_version = Some("2".into());
                                    let saved = serde_json::to_value(&proposed).unwrap();
                                    *current = proposed;
                                    current
                                        .data
                                        .as_mut()
                                        .unwrap()
                                        .insert(GATE_STATE.into(), "closed".into());
                                    current.metadata.resource_version = Some("3".into());
                                    (200, saved)
                                }
                            }
                            _ => panic!("unexpected gate operation {method}"),
                        };
                        Ok::<_, std::convert::Infallible>(
                            Response::builder()
                                .status(code)
                                .header("content-type", "application/json")
                                .body(Body::from(serde_json::to_vec(&response).unwrap()))
                                .unwrap(),
                        )
                    }
                }),
                "test",
            );
            let gates = Api::<ConfigMap>::namespaced(client, GATE_NAMESPACE);
            let outcome = reserve_entry(&gates, &database, "1234-5678", "gate-uid").await;
            let closed = state.lock().unwrap().clone();
            if close_before_update {
                assert_eq!(outcome.unwrap_err(), AdmissionError::Gate);
                assert!(gate_entries(&closed).unwrap().is_empty());
            } else {
                assert!(outcome.is_ok());
                assert_eq!(gate_entries(&closed).unwrap().len(), 1);
                let patch = admission_patch(&closed, &database, "1234-5678").unwrap();
                database.metadata.finalizers =
                    serde_json::from_value(patch[0]["value"].clone()).unwrap();
                database.metadata.annotations =
                    serde_json::from_value(patch[1]["value"].clone()).unwrap();
                assert_eq!(validate_injected_reservation(&database, &closed), Ok(()));
            }
        }
    }

    #[test]
    fn gate_update_authorizes_only_each_actors_entry_transition() {
        let mut database = TenantDatabase::new(
            "orders",
            crate::api::TenantDatabaseSpec {
                tenant_name: "test".into(),
                tenant_uid: "uid-1".into(),
                instances: 1,
            },
        );
        database.metadata.namespace = Some("tenant-db-test".into());
        let open = ConfigMap {
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
        let admission = "system:serviceaccount:tenant-system:database-admission";
        let controller = "system:serviceaccount:tenant-system:database-controller";
        let tenant = "system:serviceaccount:tenant-system:tenant-controller";
        let reserved = reservation_update(&open, &database, "1234-5678")
            .unwrap()
            .unwrap();
        assert_eq!(validate_gate_update(&open, &reserved, admission), Ok(()));
        assert_eq!(
            validate_gate_update(&open, &reserved, controller),
            Err(AdmissionError::Gate)
        );
        let mut closed = reserved.clone();
        closed
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_STATE.into(), "closed".into());
        assert_eq!(validate_gate_update(&reserved, &closed, tenant), Ok(()));
        for actor in [admission, controller, "system:admin"] {
            assert_eq!(
                validate_gate_update(&reserved, &closed, actor),
                Err(AdmissionError::Gate)
            );
        }
        let mut bound = closed.clone();
        let mut entries = gate_entries(&closed).unwrap();
        entries.get_mut("1234-5678").unwrap().bound_uid = Some("database-uid".into());
        bound.data.as_mut().unwrap().insert(
            GATE_ENTRIES.into(),
            serde_json::to_string(&entries).unwrap(),
        );
        assert_eq!(validate_gate_update(&closed, &bound, controller), Ok(()));
        assert_eq!(
            validate_gate_update(&closed, &bound, admission),
            Err(AdmissionError::Gate)
        );
        let mut drained = bound.clone();
        drained
            .data
            .as_mut()
            .unwrap()
            .insert(GATE_ENTRIES.into(), "{}".into());
        assert_eq!(validate_gate_update(&bound, &drained, controller), Ok(()));
        assert_eq!(
            validate_gate_update(&bound, &drained, tenant),
            Err(AdmissionError::Gate)
        );
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
            (ADMISSION_UID.into(), "1234-5678".into()),
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
}
