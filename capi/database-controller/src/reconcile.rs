use std::time::Duration;

use k8s_openapi::api::core::v1::Namespace;
use kube::{
    Api, Client, ResourceExt,
    api::{ApiResource, Patch, PatchParams},
    core::{DynamicObject, GroupVersionKind},
    runtime::controller::Action,
};
use serde_json::{Value, json};

use crate::api::{CatalogObservation, FINALIZER, TenantDatabaseCatalog, validate_spec};

const TENANT_UID_LABEL: &str = "tenancy.cnpg-vcluster.io/tenant-uid";
const TENANT_API_VERSION: &str = "tenancy.cnpg-vcluster.io/v1alpha4";
const TENANT_FINALIZER: &str = "tenancy.cnpg-vcluster.io/finalizer";
const RETRY: Duration = Duration::from_secs(15);
const RESYNC: Duration = Duration::from_secs(60);

#[derive(Debug, thiserror::Error)]
pub enum ObserveError {
    #[error("catalog, namespace, or Tenant identity is absent or inconsistent")]
    Identity,
    #[error("catalog has entries; provider adapters are not installed")]
    UnsupportedEntries,
    #[error("local resource ownership cannot be proven")]
    Foreign,
    #[error("local data path cannot be verified")]
    Path(#[from] crate::local_path::PathError),
    #[error("Azure disk identity, credential, or ARM response cannot be proven: {0}")]
    Azure(&'static str),
    #[error("catalog spec is invalid: {0}")]
    InvalidSpec(#[from] crate::api::CatalogError),
    #[error(transparent)]
    Api(#[from] kube::Error),
}

fn uid(metadata: &kube::core::ObjectMeta) -> Result<&str, ObserveError> {
    metadata
        .uid
        .as_deref()
        .filter(|value| !value.is_empty())
        .ok_or(ObserveError::Identity)
}

fn field<'a>(value: &'a Value, path: &str) -> Result<&'a str, ObserveError> {
    value
        .pointer(path)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or(ObserveError::Identity)
}

pub fn tenant_api(client: Client) -> Api<DynamicObject> {
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
        "tenancy.cnpg-vcluster.io",
        "v1alpha4",
        "Tenant",
    ));
    resource.plural = "tenants".into();
    Api::<DynamicObject>::all_with(client, &resource)
}

pub fn verify_identity(
    catalog: &TenantDatabaseCatalog,
    tenant: &DynamicObject,
    namespace: &Namespace,
) -> Result<(), ObserveError> {
    validate_spec(&catalog.spec)?;
    let tenant_name = &catalog.spec.tenant_name;
    let tenant_uid = uid(&tenant.metadata)?;
    let catalog_uid = uid(&catalog.metadata)?;
    let namespace_uid = uid(&namespace.metadata)?;
    if catalog.name_any() != *tenant_name
        || catalog.namespace().as_deref() != Some(format!("tenant-db-{tenant_name}").as_str())
        || tenant.name_any() != *tenant_name
        || tenant.namespace().is_some()
        || tenant
            .metadata
            .finalizers
            .as_ref()
            .is_none_or(|finalizers| {
                !finalizers
                    .iter()
                    .any(|finalizer| finalizer == TENANT_FINALIZER)
            })
        || tenant
            .types
            .as_ref()
            .is_none_or(|types| types.api_version != TENANT_API_VERSION || types.kind != "Tenant")
        || catalog.spec.tenant_uid != tenant_uid
        || tenant.metadata.deletion_timestamp.is_some() && !catalog.spec.closed
        || catalog.metadata.deletion_timestamp.is_some()
        || namespace.metadata.deletion_timestamp.is_some()
        || namespace.name_any() != format!("tenant-db-{tenant_name}")
        || namespace
            .metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(TENANT_UID_LABEL))
            .map(String::as_str)
            != Some(tenant_uid)
        || namespace
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || catalog
            .metadata
            .finalizers
            .as_ref()
            .is_none_or(|finalizers| !finalizers.iter().any(|finalizer| finalizer == FINALIZER))
        || catalog
            .metadata
            .owner_references
            .as_deref()
            .is_none_or(|owners| {
                owners.len() != 1
                    || owners[0].api_version != TENANT_API_VERSION
                    || owners[0].kind != "Tenant"
                    || owners[0].name != *tenant_name
                    || owners[0].uid != tenant_uid
            })
        || !matches!(
            field(&tenant.data, "/spec/provider/type")?,
            "local" | "azure"
        )
        || field(&tenant.data, "/status/catalogCreateIntent/namespace")? != namespace.name_any()
        || field(&tenant.data, "/status/catalogCreateIntent/name")? != *tenant_name
        || field(&tenant.data, "/status/catalogCreateIntent/tenantUID")? != tenant_uid
    {
        return Err(ObserveError::Identity);
    }
    if let Some(capability) = tenant.data.pointer("/status/databaseCapability")
        && (field(capability, "/namespace")? != namespace.name_any()
            || capability
                .pointer("/namespaceUID")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.is_empty() && value != namespace_uid)
            || capability
                .pointer("/catalogUID")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.is_empty() && value != catalog_uid))
    {
        return Err(ObserveError::Identity);
    }
    Ok(())
}

pub fn verify_empty(
    catalog: &TenantDatabaseCatalog,
    tenant: &DynamicObject,
    namespace: &Namespace,
) -> Result<(), ObserveError> {
    verify_identity(catalog, tenant, namespace)?;
    if !catalog.spec.entries.is_empty()
        || catalog
            .status
            .as_ref()
            .is_some_and(|status| !status.entries.is_empty())
    {
        return Err(ObserveError::UnsupportedEntries);
    }
    Ok(())
}

pub async fn verify_current(
    client: Client,
    observed: &TenantDatabaseCatalog,
) -> Result<TenantDatabaseCatalog, ObserveError> {
    let name = observed.name_any();
    let namespace = observed.namespace().ok_or(ObserveError::Identity)?;
    let catalogs = Api::<TenantDatabaseCatalog>::namespaced(client.clone(), &namespace);
    let current = catalogs.get(&name).await?;
    if uid(&current.metadata)? != uid(&observed.metadata)? {
        return Err(ObserveError::Identity);
    }
    let tenant = tenant_api(client.clone())
        .get(&current.spec.tenant_name)
        .await?;
    let database_namespace = Api::<Namespace>::all(client.clone())
        .get(&namespace)
        .await?;
    verify_identity(&current, &tenant, &database_namespace)?;
    let latest = catalogs.get(&name).await?;
    if uid(&latest.metadata)? != uid(&current.metadata)?
        || latest.metadata.resource_version != current.metadata.resource_version
        || latest.metadata.generation != current.metadata.generation
    {
        return Err(ObserveError::Identity);
    }
    Ok(latest)
}

fn observer_epoch_changed(
    catalog: &TenantDatabaseCatalog,
    pod_uid: &str,
    instance_id: &str,
) -> Result<bool, ObserveError> {
    let Some(observer) = catalog
        .status
        .as_ref()
        .and_then(|status| status.observer.as_ref())
    else {
        return Ok(false);
    };
    if observer.catalog_uid != uid(&catalog.metadata)? {
        return Err(ObserveError::Identity);
    }
    Ok(observer.pod_uid != pod_uid || observer.instance_id != instance_id)
}

fn observer_needs_refresh(
    catalog: &TenantDatabaseCatalog,
    pod_uid: &str,
    instance_id: &str,
) -> Result<bool, ObserveError> {
    let catalog_uid = uid(&catalog.metadata)?;
    let generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    Ok(catalog
        .status
        .as_ref()
        .and_then(|status| status.observer.as_ref())
        .is_none_or(|observer| {
            observer.catalog_uid != catalog_uid
                || observer.observed_generation != generation
                || observer.observed_resource_version.is_empty()
                || observer.pod_uid != pod_uid
                || observer.instance_id != instance_id
        }))
}

fn has_issued_intents(catalog: &TenantDatabaseCatalog) -> bool {
    catalog.status.as_ref().is_some_and(|status| {
        status.entries.values().any(|entry| {
            entry
                .create_intents
                .iter()
                .any(|intent| intent.state == crate::api::CreateState::Issued)
        })
    })
}

async fn record_observer(
    client: Client,
    observed: &TenantDatabaseCatalog,
    pod_uid: &str,
    instance_id: &str,
) -> Result<TenantDatabaseCatalog, ObserveError> {
    let current = verify_current(client.clone(), observed).await?;
    let catalog_uid = uid(&current.metadata)?;
    let resource_version = current
        .metadata
        .resource_version
        .as_deref()
        .filter(|value| !value.is_empty())
        .ok_or(ObserveError::Identity)?;
    let generation = current.metadata.generation.ok_or(ObserveError::Identity)?;
    if pod_uid.is_empty() || instance_id.is_empty() {
        return Err(ObserveError::Identity);
    }
    let receipt = CatalogObservation {
        catalog_uid: catalog_uid.into(),
        observed_generation: generation,
        observed_resource_version: resource_version.into(),
        pod_uid: pod_uid.into(),
        instance_id: instance_id.into(),
    };
    let catalogs = Api::<TenantDatabaseCatalog>::namespaced(
        client.clone(),
        &current.namespace().ok_or(ObserveError::Identity)?,
    );
    let patched = catalogs
        .patch_status(
            &current.name_any(),
            &PatchParams::default(),
            &Patch::Merge(json!({
                "metadata": {"resourceVersion": resource_version},
                "status": {"observer": receipt}
            })),
        )
        .await?;
    if uid(&patched.metadata)? != catalog_uid || patched.metadata.generation != Some(generation) {
        return Err(ObserveError::Identity);
    }
    let latest = verify_current(client, &patched).await?;
    if latest
        .status
        .as_ref()
        .and_then(|status| status.observer.as_ref())
        != Some(&receipt)
        || latest.metadata.generation != Some(generation)
    {
        return Err(ObserveError::Identity);
    }
    Ok(latest)
}

pub async fn observe(
    client: Client,
    observed: &TenantDatabaseCatalog,
    pod_uid: &str,
    instance_id: &str,
) -> Result<(Action, TenantDatabaseCatalog), ObserveError> {
    let current = verify_current(client.clone(), observed).await?;
    if !current.spec.entries.is_empty()
        || current
            .status
            .as_ref()
            .is_some_and(|status| !status.entries.is_empty())
    {
        return Err(ObserveError::UnsupportedEntries);
    }
    if !observer_needs_refresh(&current, pod_uid, instance_id)? {
        return Ok((Action::requeue(RESYNC), current));
    }
    let latest = record_observer(client, &current, pod_uid, instance_id).await?;
    Ok((Action::requeue(RESYNC), latest))
}

pub fn error_policy() -> Action {
    Action::requeue(RETRY)
}

pub mod azure;
pub mod local;

pub async fn reconcile(
    client: Client,
    observed: &TenantDatabaseCatalog,
    pod_uid: &str,
    instance_id: &str,
) -> Result<(Action, TenantDatabaseCatalog), ObserveError> {
    let current = verify_current(client.clone(), observed).await?;
    if current.spec.entries.is_empty()
        && current.status.as_ref().is_none_or(|s| s.entries.is_empty())
    {
        return observe(client, &current, pod_uid, instance_id).await;
    }
    let replay_issued = observer_epoch_changed(&current, pod_uid, instance_id)?;
    let (action, verified) = match field(
        &tenant_api(client.clone())
            .get(&current.spec.tenant_name)
            .await?
            .data,
        "/spec/provider/type",
    )? {
        "local" => local::reconcile(client.clone(), &current).await,
        "azure" => azure::reconcile(client.clone(), &current, replay_issued).await,
        _ => Err(ObserveError::UnsupportedEntries),
    }?;
    if !has_issued_intents(&verified) && observer_needs_refresh(&verified, pod_uid, instance_id)? {
        let latest = record_observer(client, &verified, pod_uid, instance_id).await?;
        return Ok((Action::requeue(RETRY), latest));
    }
    Ok((action, verified))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{
        CatalogObservation, CatalogStatus, CreateIntent, CreateState, DatabasePhase, EntryStatus,
        TenantDatabaseCatalogSpec,
    };
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference;
    use serde_json::json;

    fn fixtures() -> (TenantDatabaseCatalog, DynamicObject, Namespace) {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant-uid".into(),
                closed: false,
                entries: Default::default(),
            },
        );
        catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
        catalog.metadata.uid = Some("catalog-uid".into());
        catalog.metadata.finalizers = Some(vec![FINALIZER.into()]);
        catalog.metadata.owner_references = Some(vec![OwnerReference {
            api_version: TENANT_API_VERSION.into(),
            kind: "Tenant".into(),
            name: "tenant-a".into(),
            uid: "tenant-uid".into(),
            ..Default::default()
        }]);
        let mut tenant: DynamicObject = serde_json::from_value(json!({
            "apiVersion": TENANT_API_VERSION,
            "kind": "Tenant",
            "metadata": {
                "name": "tenant-a", "uid": "tenant-uid",
                "finalizers": [TENANT_FINALIZER]
            },
            "spec": {"provider": {"type": "local"}},
            "status": {
                "catalogCreateIntent": {
                    "namespace": "tenant-db-tenant-a", "name": "tenant-a",
                    "tenantUID": "tenant-uid"
                },
                "databaseCapability": {
                    "namespace": "tenant-db-tenant-a", "namespaceUID": "namespace-uid",
                    "catalogUID": "catalog-uid"
                }
            }
        }))
        .unwrap();
        tenant.types = Some(kube::core::TypeMeta {
            api_version: TENANT_API_VERSION.into(),
            kind: "Tenant".into(),
        });
        let namespace: Namespace = serde_json::from_value(json!({
            "metadata": {
                "name": "tenant-db-tenant-a", "uid": "namespace-uid",
                "labels": {TENANT_UID_LABEL: "tenant-uid"}
            }
        }))
        .unwrap();
        (catalog, tenant, namespace)
    }

    #[test]
    fn observer_epoch_change_enables_only_restart_bounded_replay() {
        let (mut catalog, _, _) = fixtures();
        catalog.status = Some(CatalogStatus {
            entries: [(
                "entry-uid".into(),
                EntryStatus {
                    logical_uid: "entry-uid".into(),
                    observed_generation: 1,
                    phase: DatabasePhase::Progressing,
                    conditions: vec![],
                    provider: None,
                    namespace: None,
                    cnpg_cluster: None,
                    credentials: None,
                    storage: vec![],
                    instances: vec![],
                    query: None,
                    finalization: None,
                    create_intents: vec![CreateIntent {
                        kind: "Namespace".into(),
                        name: "db-entry".into(),
                        ordinal: 0,
                        state: CreateState::Issued,
                    }],
                },
            )]
            .into(),
            observer: Some(CatalogObservation {
                catalog_uid: "catalog-uid".into(),
                observed_generation: 1,
                observed_resource_version: "1".into(),
                pod_uid: "pod-a".into(),
                instance_id: "instance-a".into(),
            }),
        });
        assert!(!observer_epoch_changed(&catalog, "pod-a", "instance-a").unwrap());
        assert!(observer_epoch_changed(&catalog, "pod-a", "instance-b").unwrap());
        assert!(has_issued_intents(&catalog));
        catalog
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut("entry-uid")
            .unwrap()
            .create_intents[0]
            .state = CreateState::Observed;
        assert!(!has_issued_intents(&catalog));
    }

    #[test]
    fn empty_open_and_closed_catalogs_are_observable_without_writes() {
        let (mut catalog, mut tenant, namespace) = fixtures();
        assert!(verify_empty(&catalog, &tenant, &namespace).is_ok());
        catalog.spec.closed = true;
        assert!(verify_empty(&catalog, &tenant, &namespace).is_ok());
        tenant.metadata.deletion_timestamp =
            Some(serde_json::from_value(json!("2026-01-01T00:00:00Z")).unwrap());
        assert!(verify_empty(&catalog, &tenant, &namespace).is_ok());
        catalog.spec.closed = false;
        assert!(verify_empty(&catalog, &tenant, &namespace).is_err());
    }

    #[test]
    fn missing_or_replaced_identity_is_rejected() {
        let (catalog, tenant, namespace) = fixtures();
        let mut replaced = catalog.clone();
        replaced.spec.tenant_uid = "successor".into();
        assert!(matches!(
            verify_empty(&replaced, &tenant, &namespace),
            Err(ObserveError::Identity)
        ));
        let mut replaced = tenant.clone();
        replaced.metadata.uid = Some("successor".into());
        assert!(verify_empty(&catalog, &replaced, &namespace).is_err());
        let mut replaced = tenant.clone();
        replaced.metadata.finalizers = None;
        assert!(verify_empty(&catalog, &replaced, &namespace).is_err());
        let mut replaced = namespace.clone();
        replaced.metadata.uid = Some("successor".into());
        assert!(verify_empty(&catalog, &tenant, &replaced).is_err());
        let mut replaced = namespace.clone();
        replaced.metadata.labels = None;
        assert!(verify_empty(&catalog, &tenant, &replaced).is_err());
        let mut replaced = catalog.clone();
        replaced.metadata.owner_references = None;
        assert!(verify_empty(&replaced, &tenant, &namespace).is_err());
        let mut replaced = catalog.clone();
        replaced.metadata.finalizers = None;
        assert!(verify_empty(&replaced, &tenant, &namespace).is_err());
        let mut replaced = tenant.clone();
        replaced.data["status"]["catalogCreateIntent"]["name"] = json!("other");
        assert!(verify_empty(&catalog, &replaced, &namespace).is_err());
        let mut replaced = tenant.clone();
        replaced.data["status"]["databaseCapability"]["catalogUID"] = json!("successor");
        assert!(verify_empty(&catalog, &replaced, &namespace).is_err());
        let mut replaced = tenant.clone();
        replaced.data["spec"]["provider"]["type"] = json!("unknown");
        assert!(verify_empty(&catalog, &replaced, &namespace).is_err());
    }

    #[test]
    fn unsupported_entries_and_status_are_preserved_not_adopted() {
        let (mut catalog, tenant, namespace) = fixtures();
        let uid = "12345678-1234-1234-1234-123456789abc";
        catalog.spec.entries.insert(
            uid.into(),
            crate::api::CatalogEntry {
                name: "orders".into(),
                instances: 1,
                deleting: false,
            },
        );
        assert!(matches!(
            verify_empty(&catalog, &tenant, &namespace),
            Err(ObserveError::UnsupportedEntries)
        ));
        catalog.spec.entries.get_mut(uid).unwrap().deleting = true;
        assert!(matches!(
            verify_empty(&catalog, &tenant, &namespace),
            Err(ObserveError::UnsupportedEntries)
        ));
        catalog.spec.entries.clear();
        catalog.status = Some(crate::api::CatalogStatus {
            entries: [(
                uid.into(),
                serde_json::from_value(json!({
                    "logicalUID": uid, "observedGeneration": 1, "phase": "Pending"
                }))
                .unwrap(),
            )]
            .into(),
            observer: None,
        });
        assert!(matches!(
            verify_empty(&catalog, &tenant, &namespace),
            Err(ObserveError::UnsupportedEntries)
        ));
    }
}
