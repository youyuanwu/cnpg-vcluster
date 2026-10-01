use std::collections::BTreeMap;

use k8s_openapi::{
    api::{
        core::v1::{ConfigMap, Namespace, ResourceQuota, ResourceQuotaSpec},
        rbac::v1::{PolicyRule, Role, RoleBinding, RoleRef, Subject},
    },
    apimachinery::{pkg::api::resource::Quantity, pkg::apis::meta::v1::ObjectMeta},
};
use kube::{
    Api, Client, ResourceExt,
    api::{DeleteParams, ListParams, PostParams, Preconditions},
    core::{ApiResource, DynamicObject, GroupVersionKind},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const GATE_NAMESPACE: &str = "tenant-database-gates";
pub const QUOTA_NAME: &str = "tenant-database-limit";
pub const CREDENTIAL_ROLE: &str = "tenant-database-credentials";
pub const QUOTA_KEY: &str = "count/tenantdatabases.tenancy.cnpg-vcluster.io";
pub const TENANT_UID: &str = "tenancy.cnpg-vcluster.io/tenant-uid";
pub const GATE_UID: &str = "tenancy.cnpg-vcluster.io/gate-uid";
pub const SPEC_SHA256: &str = "tenancy.cnpg-vcluster.io/database-spec-sha256";
pub const GATE_ENTRIES: &str = "intents";
pub const GATE_STATE: &str = "state";
pub const GATE_TENANT_UID: &str = "tenantUID";
pub const MAX_DATABASES: usize = 3;

#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GateEntry {
    pub instances: u8,
    pub lifecycle: IntentLifecycle,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bound_uid: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "camelCase")]
pub enum IntentLifecycle {
    Pending,
    Materializing,
    Bound,
}

#[derive(Debug, thiserror::Error)]
pub enum GateError {
    #[error("Tenant database creation gate is missing, replaced, or malformed")]
    Invalid,
    #[error("Tenant database namespace or quota identity is missing or replaced")]
    Identity,
    #[error("Tenant database gate is closed")]
    Closed,
    #[error("TenantDatabase CREATE may still persist; materialization outcome is unknown")]
    UnknownRequest,
    #[error(transparent)]
    Api(#[from] kube::Error),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DrainState {
    Pending,
    ReadyToRetire(String),
    Done,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GateIdentity {
    pub namespace_uid: String,
    pub quota_uid: String,
    pub gate_uid: String,
    pub storage_namespace_uid: Option<String>,
}

impl GateIdentity {
    pub fn from_recorded(
        namespace_uid: &str,
        quota_uid: &str,
        gate_uid: &str,
        storage_namespace_uid: Option<&str>,
    ) -> Option<Self> {
        (!namespace_uid.is_empty() && !quota_uid.is_empty() && !gate_uid.is_empty()).then(|| Self {
            namespace_uid: namespace_uid.into(),
            quota_uid: quota_uid.into(),
            gate_uid: gate_uid.into(),
            storage_namespace_uid: storage_namespace_uid.map(str::to_owned),
        })
    }
}

pub fn database_namespace(tenant_name: &str) -> String {
    format!("tenant-db-{tenant_name}")
}

pub fn storage_namespace(tenant_name: &str) -> String {
    format!("tenant-db-storage-{tenant_name}")
}

pub fn gate_name(tenant_uid: &str) -> String {
    format!(
        "tenant-db-gate-{}",
        hex::encode(Sha256::digest(tenant_uid.as_bytes()))[..24].to_owned()
    )
}

pub fn gate_entries(gate: &ConfigMap) -> Result<BTreeMap<String, GateEntry>, GateError> {
    let encoded = gate
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_ENTRIES))
        .ok_or(GateError::Invalid)?;
    let entries: BTreeMap<String, GateEntry> =
        serde_json::from_str(encoded).map_err(|_| GateError::Invalid)?;
    if entries.len() > MAX_DATABASES
        || entries.iter().any(|(name, entry)| {
            name.is_empty()
                || name.len() > 30
                || !name
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
                || !name.as_bytes()[0].is_ascii_alphanumeric()
                || !name.as_bytes()[name.len() - 1].is_ascii_alphanumeric()
                || !(1..=3).contains(&entry.instances)
                || (entry.lifecycle == IntentLifecycle::Bound)
                    != entry
                        .bound_uid
                        .as_deref()
                        .is_some_and(|uid| !uid.is_empty())
        })
    {
        return Err(GateError::Invalid);
    }
    Ok(entries)
}

pub fn validate_gate(gate: &ConfigMap, tenant_uid: &str) -> Result<(), GateError> {
    if gate.name_any() != gate_name(tenant_uid)
        || gate.metadata.namespace.as_deref() != Some(GATE_NAMESPACE)
        || gate.metadata.uid.as_deref().is_none_or(str::is_empty)
        || gate.metadata.deletion_timestamp.is_some()
        || gate.immutable == Some(true)
        || gate
            .binary_data
            .as_ref()
            .is_some_and(|data| !data.is_empty())
        || gate
            .metadata
            .finalizers
            .as_ref()
            .is_some_and(|items| !items.is_empty())
        || gate
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|items| !items.is_empty())
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
        return Err(GateError::Invalid);
    }
    Ok(())
}

fn markers(name: &str, tenant_uid: &str) -> ObjectMeta {
    ObjectMeta {
        name: Some(name.into()),
        labels: Some(BTreeMap::from([(TENANT_UID.into(), tenant_uid.into())])),
        ..Default::default()
    }
}

fn owned(
    metadata: &ObjectMeta,
    name: &str,
    tenant_uid: &str,
    expected: Option<&str>,
) -> Result<String, GateError> {
    let uid = metadata
        .uid
        .as_deref()
        .filter(|uid| !uid.is_empty())
        .ok_or(GateError::Identity)?;
    if metadata.name.as_deref() != Some(name)
        || metadata.deletion_timestamp.is_some()
        || metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(TENANT_UID))
            .map(String::as_str)
            != Some(tenant_uid)
        || metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || expected.is_some_and(|expected| expected != uid)
    {
        return Err(GateError::Identity);
    }
    Ok(uid.into())
}

async fn ensure_namespace(
    client: Client,
    name: &str,
    tenant_uid: &str,
    expected: Option<&str>,
) -> Result<String, GateError> {
    let api = Api::<Namespace>::all(client);
    let namespace = match api.get_opt(name).await? {
        Some(namespace) => namespace,
        None if expected.is_some() => return Err(GateError::Identity),
        None => {
            let desired = Namespace {
                metadata: markers(name, tenant_uid),
                ..Default::default()
            };
            match api.create(&PostParams::default(), &desired).await {
                Ok(namespace) => namespace,
                Err(kube::Error::Api(error)) if error.code == 409 => api.get(name).await?,
                Err(error) => return Err(error.into()),
            }
        }
    };
    owned(&namespace.metadata, name, tenant_uid, expected)
}

async fn ensure_quota(
    client: Client,
    namespace: &str,
    tenant_uid: &str,
    expected: Option<&str>,
) -> Result<String, GateError> {
    let api = Api::<ResourceQuota>::namespaced(client, namespace);
    let quota = match api.get_opt(QUOTA_NAME).await? {
        Some(quota) => quota,
        None if expected.is_some() => return Err(GateError::Identity),
        None => {
            let desired = ResourceQuota {
                metadata: ObjectMeta {
                    namespace: Some(namespace.into()),
                    ..markers(QUOTA_NAME, tenant_uid)
                },
                spec: Some(ResourceQuotaSpec {
                    hard: Some(BTreeMap::from([(QUOTA_KEY.into(), Quantity("3".into()))])),
                    ..Default::default()
                }),
                ..Default::default()
            };
            match api.create(&PostParams::default(), &desired).await {
                Ok(quota) => quota,
                Err(kube::Error::Api(error)) if error.code == 409 => api.get(QUOTA_NAME).await?,
                Err(error) => return Err(error.into()),
            }
        }
    };
    let uid = owned(&quota.metadata, QUOTA_NAME, tenant_uid, expected)?;
    if quota.metadata.namespace.as_deref() != Some(namespace)
        || quota.spec.as_ref().is_none_or(|spec| {
            spec.scopes
                .as_ref()
                .is_some_and(|scopes| !scopes.is_empty())
                || spec.scope_selector.is_some()
                || spec.hard.as_ref().is_none_or(|hard| {
                    hard.len() != 1 || hard.get(QUOTA_KEY).is_none_or(|quantity| quantity.0 != "3")
                })
        })
    {
        return Err(GateError::Identity);
    }
    Ok(uid)
}

async fn ensure_gate(
    client: Client,
    tenant_uid: &str,
    expected: Option<&str>,
) -> Result<String, GateError> {
    let api = Api::<ConfigMap>::namespaced(client, GATE_NAMESPACE);
    let name = gate_name(tenant_uid);
    let gate = match api.get_opt(&name).await? {
        Some(gate) => gate,
        None if expected.is_some() => return Err(GateError::Invalid),
        None => {
            let desired = ConfigMap {
                metadata: ObjectMeta {
                    namespace: Some(GATE_NAMESPACE.into()),
                    ..markers(&name, tenant_uid)
                },
                data: Some(BTreeMap::from([
                    (GATE_TENANT_UID.into(), tenant_uid.into()),
                    (GATE_STATE.into(), "open".into()),
                    (GATE_ENTRIES.into(), "{}".into()),
                ])),
                ..Default::default()
            };
            match api.create(&PostParams::default(), &desired).await {
                Ok(gate) => gate,
                Err(kube::Error::Api(error)) if error.code == 409 => api.get(&name).await?,
                Err(error) => return Err(error.into()),
            }
        }
    };
    validate_gate(&gate, tenant_uid)?;
    let uid = gate.metadata.uid.ok_or(GateError::Invalid)?;
    if expected.is_some_and(|expected| expected != uid) {
        return Err(GateError::Invalid);
    }
    if gate
        .data
        .as_ref()
        .and_then(|data| data.get(GATE_STATE))
        .map(String::as_str)
        != Some("open")
    {
        return Err(GateError::Closed);
    }
    Ok(uid)
}

async fn ensure_credentials(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    azure: bool,
) -> Result<(), GateError> {
    let secret = if azure {
        format!("{tenant_name}-admin-kubeconfig")
    } else {
        format!("{tenant_name}-kubeconfig")
    };
    let roles = Api::<Role>::namespaced(client.clone(), tenant_name);
    let desired = Role {
        metadata: ObjectMeta {
            namespace: Some(tenant_name.into()),
            ..markers(CREDENTIAL_ROLE, tenant_uid)
        },
        rules: Some(vec![PolicyRule {
            api_groups: Some(vec![String::new()]),
            resources: Some(vec!["secrets".into()]),
            resource_names: Some(vec![secret]),
            verbs: vec!["get".into()],
            ..Default::default()
        }]),
    };
    let role = match roles.get_opt(CREDENTIAL_ROLE).await? {
        Some(role) => role,
        None => match roles.create(&PostParams::default(), &desired).await {
            Ok(role) => role,
            Err(kube::Error::Api(error)) if error.code == 409 => roles.get(CREDENTIAL_ROLE).await?,
            Err(error) => return Err(error.into()),
        },
    };
    owned(&role.metadata, CREDENTIAL_ROLE, tenant_uid, None)?;
    if role.metadata.namespace.as_deref() != Some(tenant_name) || role.rules != desired.rules {
        return Err(GateError::Identity);
    }
    let bindings = Api::<RoleBinding>::namespaced(client, tenant_name);
    let binding = RoleBinding {
        metadata: desired.metadata,
        role_ref: RoleRef {
            api_group: Some("rbac.authorization.k8s.io".into()),
            kind: "Role".into(),
            name: CREDENTIAL_ROLE.into(),
        },
        subjects: Some(
            ["tenant-admin", "database-controller"]
                .map(|name| Subject {
                    kind: "ServiceAccount".into(),
                    name: name.into(),
                    namespace: Some("tenant-system".into()),
                    ..Default::default()
                })
                .to_vec(),
        ),
    };
    let current = match bindings.get_opt(CREDENTIAL_ROLE).await? {
        Some(current) => current,
        None => match bindings.create(&PostParams::default(), &binding).await {
            Ok(current) => current,
            Err(kube::Error::Api(error)) if error.code == 409 => {
                bindings.get(CREDENTIAL_ROLE).await?
            }
            Err(error) => return Err(error.into()),
        },
    };
    owned(&current.metadata, CREDENTIAL_ROLE, tenant_uid, None)?;
    if current.metadata.namespace.as_deref() != Some(tenant_name)
        || current.role_ref != binding.role_ref
        || current.subjects != binding.subjects
    {
        return Err(GateError::Identity);
    }
    Ok(())
}

pub async fn ensure(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    azure: bool,
    expected: Option<&GateIdentity>,
) -> Result<GateIdentity, GateError> {
    let namespace_uid = ensure_namespace(
        client.clone(),
        &database_namespace(tenant_name),
        tenant_uid,
        expected.map(|identity| identity.namespace_uid.as_str()),
    )
    .await?;
    let storage_namespace_uid = if azure {
        Some(
            ensure_namespace(
                client.clone(),
                &storage_namespace(tenant_name),
                tenant_uid,
                expected.and_then(|identity| identity.storage_namespace_uid.as_deref()),
            )
            .await?,
        )
    } else {
        None
    };
    let quota_uid = ensure_quota(
        client.clone(),
        &database_namespace(tenant_name),
        tenant_uid,
        expected.map(|identity| identity.quota_uid.as_str()),
    )
    .await?;
    let gate_uid = ensure_gate(
        client.clone(),
        tenant_uid,
        expected.map(|identity| identity.gate_uid.as_str()),
    )
    .await?;
    ensure_credentials(client, tenant_name, tenant_uid, azure).await?;
    Ok(GateIdentity {
        namespace_uid,
        quota_uid,
        gate_uid,
        storage_namespace_uid,
    })
}

fn exact_delete(metadata: &ObjectMeta) -> Result<DeleteParams, GateError> {
    Ok(DeleteParams {
        preconditions: Some(Preconditions {
            uid: Some(
                metadata
                    .uid
                    .clone()
                    .filter(|uid| !uid.is_empty())
                    .ok_or(GateError::Identity)?,
            ),
            resource_version: Some(
                metadata
                    .resource_version
                    .clone()
                    .filter(|version| !version.is_empty())
                    .ok_or(GateError::Identity)?,
            ),
        }),
        ..Default::default()
    })
}

fn database_api(client: Client, namespace: &str) -> Api<DynamicObject> {
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
        "tenancy.cnpg-vcluster.io",
        "v1alpha1",
        "TenantDatabase",
    ));
    resource.plural = "tenantdatabases".into();
    Api::namespaced_with(client, namespace, &resource)
}

pub async fn finalize(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    azure: bool,
    expected: Option<&GateIdentity>,
    recorded_gate_uid: Option<&str>,
    retiring: bool,
) -> Result<DrainState, GateError> {
    let namespace_name = database_namespace(tenant_name);
    let namespace_api = Api::<Namespace>::all(client.clone());
    let namespace = namespace_api.get_opt(&namespace_name).await?;
    if let Some(namespace) = &namespace {
        let mut metadata = namespace.metadata.clone();
        metadata.deletion_timestamp = None;
        owned(
            &metadata,
            &namespace_name,
            tenant_uid,
            expected.map(|identity| identity.namespace_uid.as_str()),
        )?;
    }
    let storage_name = storage_namespace(tenant_name);
    let storage_namespace = if azure {
        namespace_api.get_opt(&storage_name).await?
    } else {
        None
    };
    if let Some(storage) = &storage_namespace {
        let mut metadata = storage.metadata.clone();
        metadata.deletion_timestamp = None;
        owned(
            &metadata,
            &storage_name,
            tenant_uid,
            expected.and_then(|identity| identity.storage_namespace_uid.as_deref()),
        )?;
    }
    let gates = Api::<ConfigMap>::namespaced(client.clone(), GATE_NAMESPACE);
    let name = gate_name(tenant_uid);
    let gate = gates.get_opt(&name).await?;
    if let Some(gate) = &gate {
        let mut checked = gate.clone();
        checked.metadata.deletion_timestamp = None;
        validate_gate(&checked, tenant_uid)?;
        if recorded_gate_uid.is_some_and(|uid| gate.metadata.uid.as_deref() != Some(uid)) {
            return Err(GateError::Invalid);
        }
        if gate
            .data
            .as_ref()
            .and_then(|data| data.get(GATE_STATE))
            .map(String::as_str)
            == Some("open")
        {
            if retiring || gate.metadata.deletion_timestamp.is_some() {
                return Err(GateError::Invalid);
            }
            let mut closed = gate.clone();
            closed
                .data
                .as_mut()
                .ok_or(GateError::Invalid)?
                .insert(GATE_STATE.into(), "closed".into());
            let mut entries = gate_entries(gate)?;
            entries.retain(|_, entry| entry.lifecycle != IntentLifecycle::Pending);
            closed.data.as_mut().ok_or(GateError::Invalid)?.insert(
                GATE_ENTRIES.into(),
                serde_json::to_string(&entries).map_err(|_| GateError::Invalid)?,
            );
            gates
                .replace(&name, &PostParams::default(), &closed)
                .await?;
            return Ok(DrainState::Pending);
        }
    } else if recorded_gate_uid.is_some() && !retiring {
        return Err(GateError::Invalid);
    }
    let entries = gate
        .as_ref()
        .map(gate_entries)
        .transpose()?
        .unwrap_or_default();
    let databases = database_api(client.clone(), &namespace_name);
    if namespace.is_some() {
        let listing = databases.list(&ListParams::default()).await?;
        if listing
            .metadata
            .continue_
            .as_deref()
            .is_some_and(|token| !token.is_empty())
        {
            return Err(GateError::Identity);
        }
        for database in &listing.items {
            let database_name = database
                .metadata
                .name
                .as_deref()
                .ok_or(GateError::Identity)?;
            let uid = database
                .metadata
                .uid
                .as_deref()
                .filter(|uid| !uid.is_empty())
                .ok_or(GateError::Identity)?;
            let annotations = database
                .metadata
                .annotations
                .as_ref()
                .ok_or(GateError::Identity)?;
            let entry = entries.get(database_name).ok_or(GateError::Identity)?;
            if database.metadata.namespace.as_deref() != Some(namespace_name.as_str())
                || entry.lifecycle == IntentLifecycle::Pending
                || entry.bound_uid.as_deref().is_some_and(|bound| bound != uid)
                || annotations.get(GATE_UID)
                    != gate.as_ref().and_then(|gate| gate.metadata.uid.as_ref())
                || annotations.get(TENANT_UID).map(String::as_str) != Some(tenant_uid)
                || annotations.get(SPEC_SHA256).is_none_or(|hash| {
                    hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit())
                })
                || database
                    .data
                    .pointer("/spec/tenantName")
                    .and_then(serde_json::Value::as_str)
                    != Some(tenant_name)
                || database
                    .data
                    .pointer("/spec/tenantUID")
                    .and_then(serde_json::Value::as_str)
                    != Some(tenant_uid)
                || database
                    .data
                    .pointer("/spec/instances")
                    .and_then(serde_json::Value::as_u64)
                    != Some(u64::from(entry.instances))
                || !database
                    .finalizers()
                    .iter()
                    .any(|finalizer| finalizer == "tenancy.cnpg-vcluster.io/database-finalizer")
            {
                return Err(GateError::Identity);
            }
        }
        if let Some(database) = listing.items.into_iter().next() {
            if database.metadata.deletion_timestamp.is_none() {
                databases
                    .delete(
                        database
                            .metadata
                            .name
                            .as_deref()
                            .ok_or(GateError::Identity)?,
                        &exact_delete(&database.metadata)?,
                    )
                    .await?;
            }
            return Ok(DrainState::Pending);
        }
    }
    if let Some(namespace) = &namespace {
        if namespace.metadata.deletion_timestamp.is_none() {
            namespace_api
                .delete(&namespace_name, &exact_delete(&namespace.metadata)?)
                .await?;
        }
        return Ok(DrainState::Pending);
    }
    if !entries.is_empty() {
        if entries
            .values()
            .any(|entry| entry.lifecycle == IntentLifecycle::Materializing)
        {
            return Err(GateError::UnknownRequest);
        }
        let gate = gate.ok_or(GateError::Invalid)?;
        let mut updated = gate.clone();
        updated
            .data
            .as_mut()
            .ok_or(GateError::Invalid)?
            .insert(GATE_ENTRIES.into(), "{}".into());
        gates
            .replace(&name, &PostParams::default(), &updated)
            .await?;
        return Ok(DrainState::Pending);
    }
    if let Some(storage) = &storage_namespace {
        let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
            "compute.azure.com",
            "v1api20240302",
            "Disk",
        ));
        resource.plural = "disks".into();
        let disks: Api<DynamicObject> =
            Api::namespaced_with(client.clone(), &storage_name, &resource);
        let listing = disks.list(&ListParams::default()).await?;
        if listing
            .metadata
            .continue_
            .as_deref()
            .is_some_and(|token| !token.is_empty())
            || !listing.items.is_empty()
        {
            return Err(GateError::Identity);
        }
        if storage.metadata.deletion_timestamp.is_none() {
            namespace_api
                .delete(&storage_name, &exact_delete(&storage.metadata)?)
                .await?;
        }
        return Ok(DrainState::Pending);
    }
    let Some(gate) = gate else {
        return Ok(DrainState::Done);
    };
    if !retiring {
        return Ok(DrainState::ReadyToRetire(
            gate.metadata.uid.clone().ok_or(GateError::Invalid)?,
        ));
    }
    if gate.metadata.deletion_timestamp.is_none() {
        gates.delete(&name, &exact_delete(&gate.metadata)?).await?;
    }
    Ok(DrainState::Pending)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gate_entry_requires_named_bounded_intent_and_exact_bound_uid() {
        let gate = ConfigMap {
            data: Some(BTreeMap::from([(
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
            )])),
            ..Default::default()
        };
        assert_eq!(gate_entries(&gate).unwrap()["orders"].instances, 3);
        let mut entries = gate_entries(&gate).unwrap();
        entries.get_mut("orders").unwrap().lifecycle = IntentLifecycle::Bound;
        let mut invalid = gate.clone();
        invalid.data.as_mut().unwrap().insert(
            GATE_ENTRIES.into(),
            serde_json::to_string(&entries).unwrap(),
        );
        assert!(gate_entries(&invalid).is_err());
        entries.get_mut("orders").unwrap().bound_uid = Some("exact-uid".into());
        invalid.data.as_mut().unwrap().insert(
            GATE_ENTRIES.into(),
            serde_json::to_string(&entries).unwrap(),
        );
        assert_eq!(
            gate_entries(&invalid).unwrap()["orders"]
                .bound_uid
                .as_deref(),
            Some("exact-uid")
        );
        for name in ["second", "third"] {
            entries.insert(
                name.into(),
                GateEntry {
                    instances: 1,
                    lifecycle: IntentLifecycle::Pending,
                    bound_uid: None,
                },
            );
        }
        invalid.data.as_mut().unwrap().insert(
            GATE_ENTRIES.into(),
            serde_json::to_string(&entries).unwrap(),
        );
        assert_eq!(gate_entries(&invalid).unwrap().len(), MAX_DATABASES);
        entries.insert(
            "fourth".into(),
            GateEntry {
                instances: 1,
                lifecycle: IntentLifecycle::Pending,
                bound_uid: None,
            },
        );
        invalid.data.as_mut().unwrap().insert(
            GATE_ENTRIES.into(),
            serde_json::to_string(&entries).unwrap(),
        );
        assert!(gate_entries(&invalid).is_err());
    }
}
