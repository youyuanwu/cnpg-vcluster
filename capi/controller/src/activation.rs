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
    management::ACTIVATION_RESOURCES,
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
    let uid = ticket
        .uid()
        .ok_or_else(|| ControllerError::Configuration("activation ticket has no UID".into()))?;
    let resource_version = ticket.resource_version().ok_or_else(|| {
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
    for resource in ACTIVATION_RESOURCES {
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
            Err(kube::Error::Api(status)) if status.code == 404 => {}
            Err(error) => return Err(error.into()),
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
            )
        })
    {
        return Err(ControllerError::Configuration(
            "worker or load-balancer containers block configuration activation".into(),
        ));
    }
    Ok(())
}
