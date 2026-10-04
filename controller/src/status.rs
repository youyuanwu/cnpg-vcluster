use kube::{
    Api, Client, ResourceExt,
    api::{Patch, PatchParams},
};
use serde_json::{Value, json};

use crate::error::ControllerError;
use crate::{
    api::{CatalogCreateIntent, FINALIZER, Tenant, TenantStatus},
    readiness::set_condition,
};

fn invalid() -> ControllerError {
    ControllerError::OwnershipInvalid("Tenant identity changed".into())
}

pub fn validate_identity(
    original: &Tenant,
    current: &Tenant,
) -> Result<(String, String), ControllerError> {
    let uid = current
        .uid()
        .filter(|value| !value.is_empty())
        .ok_or_else(invalid)?;
    let resource_version = current
        .resource_version()
        .filter(|value| !value.is_empty())
        .ok_or_else(invalid)?;
    if Some(uid.clone()) != original.uid()
        || current.metadata.generation != original.metadata.generation
        || current.spec != original.spec
        || current.metadata.deletion_timestamp != original.metadata.deletion_timestamp
    {
        return Err(invalid());
    }
    Ok((uid, resource_version))
}

fn checked(uid: &str, updated: Tenant) -> Result<(), ControllerError> {
    (updated.uid().as_deref() == Some(uid))
        .then_some(())
        .ok_or_else(invalid)
}

async fn patch_status(
    client: Client,
    original: &Tenant,
    current: &Tenant,
    status: &TenantStatus,
    clear_allocation: bool,
) -> Result<(), ControllerError> {
    let (uid, resource_version) = validate_identity(original, current)?;
    let mut status = serde_json::to_value(status)
        .map_err(|error| ControllerError::InvalidInput(error.to_string()))?;
    if clear_allocation {
        status["provider"]["allocation"] = Value::Null;
        status["provider"]["networkAllocation"] = Value::Null;
    }
    let updated = Api::<Tenant>::all(client)
        .patch_status(
            &current.name_any(),
            &PatchParams::default(),
            &Patch::Merge(&json!({"metadata":{
                "uid":uid,"resourceVersion":resource_version
            },"status":status})),
        )
        .await?;
    checked(&uid, updated)
}

pub async fn set_finalizer(
    client: Client,
    original: &Tenant,
    current: &Tenant,
    finalizer: &str,
    present: bool,
) -> Result<bool, ControllerError> {
    let (uid, resource_version) = validate_identity(original, current)?;
    let mut finalizers = current.metadata.finalizers.clone().unwrap_or_default();
    let found = finalizers.iter().any(|value| value == finalizer);
    if found == present {
        return Ok(false);
    }
    if present {
        finalizers.push(finalizer.into());
    } else {
        finalizers.retain(|value| value != finalizer);
    }
    let updated = Api::<Tenant>::all(client)
        .patch(
            &current.name_any(),
            &PatchParams::default(),
            &Patch::Merge(&json!({"metadata":{
                "uid":uid,"resourceVersion":resource_version,"finalizers":finalizers
            }})),
        )
        .await?;
    checked(&uid, updated)?;
    Ok(true)
}

async fn mutate_status<F>(
    client: Client,
    original: &Tenant,
    current: &Tenant,
    mutate: F,
    retries: usize,
    clear_allocation: bool,
) -> Result<(), ControllerError>
where
    F: Fn(&mut TenantStatus) -> Result<(), ControllerError>,
{
    let api = Api::<Tenant>::all(client.clone());
    let mut current = current.clone();
    for attempt in 0..=retries {
        validate_identity(original, &current)?;
        let mut status = current.status.clone().unwrap_or_default();
        mutate(&mut status)?;
        status.observed_generation = current.metadata.generation;
        if current.status.as_ref() == Some(&status)
            || (current.status.is_none() && status == TenantStatus::default())
        {
            return Ok(());
        }

        match patch_status(
            client.clone(),
            original,
            &current,
            &status,
            clear_allocation,
        )
        .await
        {
            Ok(()) => return Ok(()),
            Err(ControllerError::Kube(kube::Error::Api(status)))
                if status.code == 409 && attempt < retries =>
            {
                current = api.get(&original.name_any()).await?;
            }
            Err(error) => return Err(error),
        }
    }
    unreachable!("bounded status retries return")
}

pub async fn update_status<F>(
    client: Client,
    tenant: &Tenant,
    mutate: F,
) -> Result<(), ControllerError>
where
    F: Fn(&mut TenantStatus) -> Result<(), ControllerError>,
{
    mutate_status(client, tenant, tenant, mutate, 4, false).await
}

pub const CATALOG_CREATE_CONDITION: &str = "CatalogCreate";

pub fn catalog_create_outcome(status: &TenantStatus) -> Option<&str> {
    status
        .conditions
        .iter()
        .find(|condition| condition.type_ == CATALOG_CREATE_CONDITION)
        .map(|condition| condition.reason.as_str())
}

pub async fn close_catalog_creation_without_intent(
    client: Client,
    tenant: &Tenant,
) -> Result<(), ControllerError> {
    if tenant.metadata.deletion_timestamp.is_none()
        || !tenant.finalizers().iter().any(|value| value == FINALIZER)
    {
        return Err(invalid());
    }
    update_status(client, tenant, |status| {
        if status.catalog_create_intent.is_some()
            || !matches!(catalog_create_outcome(status), None | Some("Closed"))
        {
            return Err(invalid());
        }
        set_condition(
            status,
            tenant,
            CATALOG_CREATE_CONDITION,
            false,
            "Closed",
            "Tenant deletion forbids catalog CREATE",
        );
        Ok(())
    })
    .await
}

pub async fn record_catalog_create_intent(
    client: Client,
    tenant: &Tenant,
    intent: &CatalogCreateIntent,
) -> Result<(), ControllerError> {
    let uid = tenant.uid().ok_or_else(invalid)?;
    if tenant.metadata.deletion_timestamp.is_some()
        || !tenant
            .finalizers()
            .iter()
            .any(|finalizer| finalizer == FINALIZER)
        || intent.tenant_uid != uid
        || intent.namespace != tenant_database_runtime::database_namespace(&tenant.name_any())
        || intent.name != tenant.name_any()
    {
        return Err(invalid());
    }
    update_status(client, tenant, |status| {
        if status
            .catalog_create_intent
            .as_ref()
            .is_some_and(|recorded| recorded != intent)
        {
            return Err(ControllerError::OwnershipInvalid(
                "catalog creation intent identity changed".into(),
            ));
        }
        if status.catalog_create_intent.is_none() {
            status.catalog_create_intent = Some(intent.clone());
            set_condition(
                status,
                tenant,
                CATALOG_CREATE_CONDITION,
                false,
                "Prepared",
                "Catalog CREATE has not been issued",
            );
        }
        Ok(())
    })
    .await
}

pub async fn set_catalog_create_outcome(
    client: Client,
    tenant: &Tenant,
    intent: &CatalogCreateIntent,
    from: &str,
    to: &str,
) -> Result<(), ControllerError> {
    if tenant
        .status
        .as_ref()
        .and_then(|status| status.catalog_create_intent.as_ref())
        != Some(intent)
        || !tenant
            .finalizers()
            .iter()
            .any(|finalizer| finalizer == FINALIZER)
        || (matches!(to, "Unknown" | "Preparing" | "Prepared")
            && tenant.metadata.deletion_timestamp.is_some())
    {
        return Err(invalid());
    }
    update_status(client, tenant, |status| {
        if status.catalog_create_intent.as_ref() != Some(intent)
            || catalog_create_outcome(status) != Some(from)
        {
            return Err(invalid());
        }
        set_condition(
            status,
            tenant,
            CATALOG_CREATE_CONDITION,
            to == "Observed",
            to,
            "Catalog CREATE outcome",
        );
        Ok(())
    })
    .await
}

pub async fn record_catalog_namespaces(
    client: Client,
    tenant: &Tenant,
    intent: &CatalogCreateIntent,
    namespace_uid: &str,
    storage_uid: Option<&str>,
) -> Result<(), ControllerError> {
    update_status(client, tenant, |status| {
        if status.catalog_create_intent.as_ref() != Some(intent)
            || catalog_create_outcome(status) != Some("Preparing")
        {
            return Err(invalid());
        }
        let capability = status
            .database_capability
            .get_or_insert_with(Default::default);
        if !capability.catalog_uid.is_empty()
            || (!capability.namespace_uid.is_empty() && capability.namespace_uid != namespace_uid)
            || (capability.storage_namespace_uid.is_some()
                && capability.storage_namespace_uid.as_deref() != storage_uid)
        {
            return Err(invalid());
        }
        capability.namespace = intent.namespace.clone();
        capability.namespace_uid = namespace_uid.into();
        capability.storage_namespace_uid = storage_uid.map(str::to_owned);
        capability.available = false;
        capability.reason = "CatalogNotReady".into();
        set_condition(
            status,
            tenant,
            CATALOG_CREATE_CONDITION,
            false,
            "Namespaced",
            "Catalog namespace identities persisted before CREATE",
        );
        Ok(())
    })
    .await
}

pub async fn replace_status(
    client: Client,
    original: &Tenant,
    current: &Tenant,
    status: &TenantStatus,
    clear_allocation: bool,
) -> Result<(), ControllerError> {
    if (current.status.as_ref() == Some(status)
        || (current.status.is_none() && status == &TenantStatus::default()))
        && (!clear_allocation
            || (status.allocation().is_none()
                && status
                    .azure()
                    .is_none_or(|azure| azure.network_allocation.is_none())))
    {
        return Ok(());
    }
    patch_status(client, original, current, status, clear_allocation).await
}
