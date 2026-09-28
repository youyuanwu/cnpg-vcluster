use kube::{
    Api, Client, ResourceExt,
    api::{Patch, PatchParams},
};
use serde_json::{Value, json};

use crate::api::{Tenant, TenantStatus};
use crate::error::ControllerError;

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

pub async fn replace_status(
    client: Client,
    original: &Tenant,
    current: &Tenant,
    status: &TenantStatus,
    clear_allocation: bool,
) -> Result<(), ControllerError> {
    if (current.status.as_ref() == Some(status)
        || (current.status.is_none() && status == &TenantStatus::default()))
        && (!clear_allocation || status.allocation().is_none())
    {
        return Ok(());
    }
    patch_status(client, original, current, status, clear_allocation).await
}
