use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use k8s_openapi::api::{
    coordination::v1::Lease,
    core::v1::{ConfigMap, Namespace, Secret},
};
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
    management::{ACTIVATION_RESOURCES, InventoryPolicy},
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
) -> Result<(), ControllerError> {
    let config_maps = Api::<ConfigMap>::namespaced(client.clone(), NAMESPACE);
    let state = config_maps.get_opt(STATE_NAME).await?;
    if state
        .as_ref()
        .and_then(|state| state.data.as_ref())
        .and_then(|data| data.get("configurationHash"))
        .is_some_and(|hash| hash == configuration_hash)
    {
        return Ok(());
    }
    let ticket = config_maps
        .get_opt(TICKET_NAME)
        .await?
        .ok_or_else(|| ControllerError::Configuration("activation ticket is missing".into()))?;
    validate_ticket(&ticket, configuration_hash, activation_token)?;
    require_clean_inventory(client, docker).await?;
    let mut consumed = ticket.clone();
    consumed
        .data
        .get_or_insert_default()
        .insert("consumed".into(), "true".into());
    let consumed = config_maps
        .replace(TICKET_NAME, &PostParams::default(), &consumed)
        .await?;
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
) -> Result<(), ControllerError> {
    let data = ticket
        .data
        .as_ref()
        .ok_or_else(|| ControllerError::Configuration("activation ticket has no data".into()))?;
    if activation_token.is_empty()
        || data.get("configurationHash").map(String::as_str) != Some(configuration_hash)
        || data.get("token").map(String::as_str) != Some(activation_token)
        || data.get("hostClean").map(String::as_str) != Some("true")
        || data.get("consumed").is_some_and(|value| value != "false")
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
    Ok(())
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
    for resource in ACTIVATION_RESOURCES
        .iter()
        .filter(|resource| resource.inventory_policy == InventoryPolicy::BlockAnyInstance)
    {
        let items = Api::<DynamicObject>::all_with(client.clone(), &resource.api_resource())
            .list(&ListParams::default())
            .await;
        match items {
            Ok(items) if items.items.is_empty() => {}
            Ok(_) => {
                return Err(ControllerError::Configuration(format!(
                    "{} resources block configuration activation",
                    resource.kind
                )));
            }
            Err(error) => return Err(error.into()),
        }
    }
    for namespace in Api::<Namespace>::all(client.clone())
        .list(&ListParams::default())
        .await?
    {
        let annotations = namespace.metadata.annotations.as_ref();
        if annotations.is_some_and(|annotations| {
            annotations.contains_key("tenancy.cnpg-vcluster.io/tenant")
                || annotations.contains_key("tenancy.cnpg-vcluster.io/tenant-uid")
        }) {
            return Err(ControllerError::Configuration(
                "Tenant Namespace residue blocks configuration activation".into(),
            ));
        }
    }
    for secret in Api::<Secret>::all(client.clone())
        .list(&ListParams::default())
        .await?
    {
        let annotations = secret.metadata.annotations.as_ref();
        if annotations.is_some_and(|annotations| {
            annotations.contains_key("tenancy.cnpg-vcluster.io/tenant")
                || annotations.contains_key("tenancy.cnpg-vcluster.io/tenant-uid")
        }) || secret
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| {
                owners
                    .iter()
                    .any(|owner| owner.kind == "KamajiControlPlane")
            })
        {
            return Err(ControllerError::Configuration(
                "Tenant credential residue blocks configuration activation".into(),
            ));
        }
    }
    for lease in Api::<Lease>::namespaced(client.clone(), NAMESPACE)
        .list(&ListParams::default())
        .await?
    {
        if lease
            .metadata
            .labels
            .as_ref()
            .is_some_and(|labels| labels.contains_key("tenancy.cnpg-vcluster.io/slot-id"))
            || lease
                .metadata
                .annotations
                .as_ref()
                .is_some_and(|annotations| {
                    annotations
                        .get("tenancy.cnpg-vcluster.io/resource")
                        .map(String::as_str)
                        == Some("allocation-lease")
                })
        {
            return Err(ControllerError::Configuration(
                "allocation Lease residue blocks configuration activation".into(),
            ));
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
            ) || [
                "cnpg-vcluster.capi/role",
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
                || volume
                    .labels
                    .get("cnpg-vcluster.capi/role")
                    .map(String::as_str)
                    == Some("tenant-storage")
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
