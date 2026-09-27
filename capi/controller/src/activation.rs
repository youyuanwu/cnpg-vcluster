use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use k8s_openapi::api::core::v1::ConfigMap;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use kube::{
    Api, Client, ResourceExt,
    api::{DeleteParams, ListParams, PostParams, Preconditions},
    core::DynamicObject,
};

use crate::{
    api::Tenant,
    docker::{DockerClient, WORKER_ROLE_LABEL},
    error::ControllerError,
    management::{InventoryPolicy, MANAGEMENT_RESOURCES, ManagementResource},
};

pub const STATE_NAME: &str = "tenant-controller-state";
pub const TICKET_NAME: &str = "tenant-controller-activation";
const NAMESPACE: &str = "tenant-system";
const MAX_TICKET_AGE_SECONDS: i64 = 600;

pub async fn admit<D: DockerClient>(
    client: Client,
    docker: &D,
    configuration_hash: &str,
    activation_token: &str,
    creation_valid: bool,
) -> Result<(), ControllerError> {
    let config_maps = Api::<ConfigMap>::namespaced(client.clone(), NAMESPACE);
    let state = config_maps.get_opt(STATE_NAME).await?;
    if state
        .as_ref()
        .and_then(|state| state.data.as_ref())
        .is_some_and(|data| data.contains_key("rollbackToken"))
    {
        return Err(ControllerError::Configuration(
            "controller configuration rollback is in progress".into(),
        ));
    }
    if state
        .as_ref()
        .and_then(|state| state.data.as_ref())
        .and_then(|data| data.get("configurationHash"))
        .is_some_and(|hash| hash == configuration_hash)
    {
        return Ok(());
    }
    if !creation_valid {
        return Err(ControllerError::Configuration(
            "changed configuration is invalid for Tenant creation".into(),
        ));
    }
    let ticket = config_maps
        .get_opt(TICKET_NAME)
        .await?
        .ok_or_else(|| ControllerError::Configuration("activation ticket is missing".into()))?;
    let current_hash = state
        .as_ref()
        .and_then(|state| state.data.as_ref())
        .and_then(|data| data.get("configurationHash"))
        .map(String::as_str);
    let consumed = validate_ticket(&ticket, configuration_hash, activation_token, current_hash)?;
    require_clean_inventory(client, docker).await?;
    let consumed = if consumed {
        ticket
    } else {
        let mut consumed = ticket;
        consumed
            .data
            .get_or_insert_default()
            .insert("consumed".into(), "true".into());
        config_maps
            .replace(TICKET_NAME, &PostParams::default(), &consumed)
            .await?
    };
    let replacement = ConfigMap {
        metadata: ObjectMeta {
            name: Some(STATE_NAME.into()),
            namespace: Some(NAMESPACE.into()),
            resource_version: state.as_ref().and_then(|state| state.resource_version()),
            ..Default::default()
        },
        data: Some(BTreeMap::from([(
            "configurationHash".into(),
            configuration_hash.into(),
        )])),
        ..Default::default()
    };
    match state {
        Some(_) => {
            config_maps
                .replace(STATE_NAME, &PostParams::default(), &replacement)
                .await?;
        }
        None => {
            config_maps
                .create(&PostParams::default(), &replacement)
                .await?;
        }
    }
    let uid = consumed
        .uid()
        .ok_or_else(|| ControllerError::Configuration("activation ticket has no UID".into()))?;
    let resource_version = consumed.resource_version().ok_or_else(|| {
        ControllerError::Configuration("activation ticket has no resourceVersion".into())
    })?;
    config_maps
        .delete(
            TICKET_NAME,
            &DeleteParams {
                preconditions: Some(Preconditions {
                    uid: Some(uid),
                    resource_version: Some(resource_version),
                }),
                ..Default::default()
            },
        )
        .await?;
    Ok(())
}

fn validate_ticket(
    ticket: &ConfigMap,
    configuration_hash: &str,
    activation_token: &str,
    current_hash: Option<&str>,
) -> Result<bool, ControllerError> {
    let data = ticket
        .data
        .as_ref()
        .ok_or_else(|| ControllerError::Configuration("activation ticket has no data".into()))?;
    if activation_token.is_empty()
        || data.get("configurationHash").map(String::as_str) != Some(configuration_hash)
        || data.get("token").map(String::as_str) != Some(activation_token)
        || data.get("hostClean").map(String::as_str) != Some("true")
        || data.get("previousConfigurationHash").map(String::as_str)
            != Some(current_hash.unwrap_or(""))
    {
        return Err(ControllerError::Configuration(
            "activation ticket identity is invalid".into(),
        ));
    }
    let created = data
        .get("createdAt")
        .ok_or_else(|| ControllerError::Configuration("activation ticket has no time".into()))?
        .parse::<DateTime<Utc>>()
        .map_err(|_| ControllerError::Configuration("activation ticket time is invalid".into()))?;
    let age = Utc::now().signed_duration_since(created).num_seconds();
    if !(0..=MAX_TICKET_AGE_SECONDS).contains(&age) {
        return Err(ControllerError::Configuration(
            "activation ticket is stale".into(),
        ));
    }
    match data.get("consumed").map(String::as_str) {
        None | Some("false") => Ok(false),
        Some("true") => Ok(true),
        _ => Err(ControllerError::Configuration(
            "activation ticket consumption state is invalid".into(),
        )),
    }
}

async fn require_clean_inventory<D: DockerClient>(
    client: Client,
    docker: &D,
) -> Result<(), ControllerError> {
    if !Api::<Tenant>::all(client.clone())
        .list(&ListParams::default())
        .await?
        .items
        .is_empty()
    {
        return Err(ControllerError::Configuration(
            "Tenant resources block configuration activation".into(),
        ));
    }
    for &resource in MANAGEMENT_RESOURCES {
        let api = match resource.inventory_namespace {
            Some(namespace) => Api::<DynamicObject>::namespaced_with(
                client.clone(),
                namespace,
                &resource.api_resource(),
            ),
            None => Api::<DynamicObject>::all_with(client.clone(), &resource.api_resource()),
        };
        for item in api.list(&ListParams::default()).await? {
            validate_inventory_item(resource, &item)?;
            if inventory_blocks(resource, &item) {
                return Err(ControllerError::Configuration(format!(
                    "{} resources block configuration activation",
                    resource.kind
                )));
            }
        }
    }
    if docker
        .list_containers()
        .await
        .map_err(|error| {
            ControllerError::Configuration(format!("Docker inventory failed: {error}"))
        })?
        .iter()
        .any(|container| {
            matches!(
                container.labels.get(WORKER_ROLE_LABEL).map(String::as_str),
                Some("worker" | "external-load-balancer")
            ) || container
                .labels
                .get("cnpg-vcluster.capi/role")
                .is_some_and(|role| role != "offline-registry")
                || [
                    "cnpg-vcluster.capi/tenant",
                    "tenancy.cnpg-vcluster.io/tenant-uid",
                ]
                .iter()
                .any(|key| container.labels.contains_key(*key))
        })
    {
        return Err(ControllerError::Configuration(
            "worker or load-balancer containers block configuration activation".into(),
        ));
    }
    if docker
        .list_volumes()
        .await
        .map_err(|error| {
            ControllerError::Configuration(format!("Docker volume inventory failed: {error}"))
        })?
        .iter()
        .any(|volume| {
            volume.name.ends_with("-storage")
                || volume.labels.contains_key("cnpg-vcluster.capi/role")
                || volume.labels.contains_key("cnpg-vcluster.capi/tenant")
                || volume
                    .labels
                    .contains_key("tenancy.cnpg-vcluster.io/tenant-uid")
        })
    {
        return Err(ControllerError::Configuration(
            "Tenant storage volume residue blocks configuration activation".into(),
        ));
    }
    Ok(())
}

fn validate_inventory_item(
    resource: ManagementResource,
    item: &DynamicObject,
) -> Result<(), ControllerError> {
    let valid_type = item.types.as_ref().is_some_and(|types| {
        types.api_version == resource.api_version && types.kind == resource.kind
    });
    let valid_namespace = if resource.namespaced {
        item.metadata.namespace.as_deref().is_some_and(|namespace| {
            !namespace.is_empty()
                && resource
                    .inventory_namespace
                    .is_none_or(|expected| namespace == expected)
        })
    } else {
        item.metadata.namespace.as_deref().is_none_or(str::is_empty)
    };
    if !valid_type
        || !valid_namespace
        || item.metadata.name.as_deref().is_none_or(str::is_empty)
        || item.metadata.uid.as_deref().is_none_or(str::is_empty)
    {
        return Err(ControllerError::Configuration(format!(
            "{} inventory identity is invalid",
            resource.kind
        )));
    }
    Ok(())
}

fn inventory_blocks(resource: ManagementResource, item: &DynamicObject) -> bool {
    let annotations = item.metadata.annotations.as_ref();
    let labels = item.metadata.labels.as_ref();
    let tenant_marked = annotations.is_some_and(|annotations| {
        annotations.contains_key("tenancy.cnpg-vcluster.io/tenant")
            || annotations.contains_key("tenancy.cnpg-vcluster.io/tenant-uid")
    });
    let marked = match resource.inventory_policy {
        InventoryPolicy::BlockAnyInstance => return true,
        InventoryPolicy::TenantMarkers => tenant_marked,
        InventoryPolicy::TenantMarkersOrKamajiOwner => {
            tenant_marked
                || item
                    .metadata
                    .owner_references
                    .as_ref()
                    .is_some_and(|owners| {
                        owners
                            .iter()
                            .any(|owner| owner.kind == "KamajiControlPlane")
                    })
        }
        InventoryPolicy::AllocationMarkers => {
            labels.is_some_and(|labels| {
                labels.contains_key("tenancy.cnpg-vcluster.io/slot-id")
                    || labels.contains_key("tenancy.cnpg-vcluster.io/tenant")
            }) || annotations.is_some_and(|annotations| {
                annotations
                    .get("tenancy.cnpg-vcluster.io/resource")
                    .map(String::as_str)
                    == Some("allocation-lease")
                    || annotations.contains_key("tenancy.cnpg-vcluster.io/slot-id")
                    || annotations.contains_key("tenancy.cnpg-vcluster.io/tenant")
                    || annotations.contains_key("tenancy.cnpg-vcluster.io/tenant-uid")
            })
        }
    };
    marked || resource.exemptions.is_empty()
}
