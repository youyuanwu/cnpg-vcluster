use std::collections::BTreeMap;

use kube::{
    Api, Client, ResourceExt,
    api::{Patch, PatchParams, PostParams},
    core::{ApiResource, DynamicObject, GroupVersionKind},
};
use serde_json::Value;

use crate::{
    api::Tenant,
    ownership::{self, Identity, RESOURCE_ANNOTATION},
};

use super::ReconcileError;

pub const FIELD_MANAGER: &str = "cnpg-vcluster-tenant-controller";

pub fn resource(api_version: &str, kind: &str) -> ApiResource {
    let (group, version) = api_version.split_once('/').unwrap_or(("", api_version));
    ApiResource::from_gvk(&GroupVersionKind::gvk(group, version, kind))
}

pub fn object_api(
    client: Client,
    object: &DynamicObject,
) -> Result<Api<DynamicObject>, ReconcileError> {
    let types = object
        .types
        .as_ref()
        .ok_or_else(|| ReconcileError::InvalidInput("object has no kind".into()))?;
    let resource = resource(&types.api_version, &types.kind);
    Ok(api_for(
        client,
        &resource,
        object.metadata.namespace.as_deref(),
    ))
}

pub fn api_for(
    client: Client,
    resource: &ApiResource,
    namespace: Option<&str>,
) -> Api<DynamicObject> {
    match namespace.filter(|namespace| !namespace.is_empty()) {
        Some(namespace) => Api::namespaced_with(client, namespace, resource),
        None => Api::all_with(client, resource),
    }
}

fn role(object: &DynamicObject) -> Result<&str, ReconcileError> {
    object
        .annotations()
        .get(RESOURCE_ANNOTATION)
        .map(String::as_str)
        .filter(|role| !role.is_empty())
        .ok_or_else(|| {
            ReconcileError::InvalidInput("desired resource has no ownership role".into())
        })
}

fn identity_matches(
    current: &DynamicObject,
    desired: &DynamicObject,
) -> Result<(), ReconcileError> {
    if current.metadata.name != desired.metadata.name
        || current
            .metadata
            .namespace
            .as_deref()
            .filter(|s| !s.is_empty())
            != desired
                .metadata
                .namespace
                .as_deref()
                .filter(|s| !s.is_empty())
        || current.types != desired.types
    {
        return Err(ReconcileError::OwnershipInvalid(
            "API returned a different resource identity".into(),
        ));
    }
    Ok(())
}

fn already_exists(error: &kube::Error) -> bool {
    matches!(error, kube::Error::Api(status) if status.code == 409 && status.reason == "AlreadyExists")
}

#[derive(Debug)]
pub struct Ensured {
    pub object: DynamicObject,
    pub created: bool,
}

async fn read_or_create(
    client: Client,
    desired: &DynamicObject,
    known: Option<DynamicObject>,
    refuse_missing: bool,
) -> Result<Ensured, ReconcileError> {
    let api = object_api(client, desired)?;
    let current = match known {
        Some(current) => Some(current),
        None => api.get_opt(&desired.name_any()).await?,
    };
    let current = match current {
        Some(current) => current,
        None if refuse_missing => {
            return Err(ReconcileError::Degraded {
                reason: "RootClusterMissing",
                message: "recorded root Cluster is missing".into(),
            });
        }
        None => {
            let mut clean = desired.clone();
            if let Some(data) = clean.data.as_object_mut() {
                data.remove("status");
            }
            match api.create(&PostParams::default(), &clean).await {
                Ok(created) => {
                    identity_matches(&created, desired)?;
                    return Ok(Ensured {
                        object: created,
                        created: true,
                    });
                }
                Err(error) if already_exists(&error) => api.get(&desired.name_any()).await?,
                Err(error) => return Err(error.into()),
            }
        }
    };
    identity_matches(&current, desired)?;
    Ok(Ensured {
        object: current,
        created: false,
    })
}

pub async fn ensure_static(
    client: Client,
    desired: &DynamicObject,
    identity: Identity<'_>,
) -> Result<Ensured, ReconcileError> {
    let result = read_or_create(client, desired, None, false).await?;
    ownership::validate_tenant_object_ownership(&result.object.metadata, identity, role(desired)?)?;
    Ok(result)
}

pub async fn apply_exact(
    client: Client,
    desired: &DynamicObject,
    current: &DynamicObject,
) -> Result<DynamicObject, ReconcileError> {
    if current.metadata.deletion_timestamp.is_some() {
        return Err(ReconcileError::Pending(format!(
            "{} is deleting",
            current.name_any()
        )));
    }
    if current.uid().is_none_or(|uid| uid.is_empty())
        || current.resource_version().is_none_or(|rv| rv.is_empty())
    {
        return Err(ReconcileError::OwnershipInvalid(
            "dynamic object has no UID/resourceVersion".into(),
        ));
    }
    let mut applied = desired.clone();
    applied.metadata.uid = current.uid();
    applied.metadata.resource_version = current.resource_version();
    if let Some(data) = applied.data.as_object_mut() {
        data.remove("status");
    }
    let response = object_api(client, desired)?
        .patch(
            &desired.name_any(),
            &PatchParams::apply(FIELD_MANAGER).force(),
            &Patch::Apply(&applied),
        )
        .await
        .map_err(|error| match error {
            kube::Error::Api(status) if status.code == 422 => ReconcileError::Degraded {
                reason: "ImmutableDrift",
                message: "owned resource cannot accept its desired immutable fields".into(),
            },
            error => error.into(),
        })?;
    identity_matches(&response, desired)?;
    if response.uid() != current.uid() {
        return Err(ReconcileError::OwnershipInvalid(
            "resource identity changed during apply".into(),
        ));
    }
    Ok(response)
}

pub async fn ensure_dynamic(
    client: Client,
    desired: &DynamicObject,
    identity: Identity<'_>,
) -> Result<Ensured, ReconcileError> {
    let mut result = ensure_static(client.clone(), desired, identity).await?;
    if !result.created {
        result.object = apply_exact(client, desired, &result.object).await?;
        ownership::validate_tenant_object_ownership(
            &result.object.metadata,
            identity,
            role(desired)?,
        )?;
    }
    Ok(result)
}

pub async fn ensure_management(
    client: Client,
    desired: &DynamicObject,
    tenant: &Tenant,
    identity: Identity<'_>,
    inventory: &mut Vec<DynamicObject>,
) -> Result<Ensured, ReconcileError> {
    let is_cluster = desired
        .types
        .as_ref()
        .is_some_and(|types| types.kind == "Cluster");
    let known = inventory
        .iter()
        .find(|object| {
            object.types == desired.types && object.metadata.name == desired.metadata.name
        })
        .cloned();
    let bound = tenant
        .status
        .as_ref()
        .and_then(|status| status.cluster_uid.as_deref())
        .is_some_and(|uid| !uid.is_empty());
    let mut result = read_or_create(client.clone(), desired, known, is_cluster && bound).await?;
    ownership::validate_root_ownership(&result.object.metadata, identity, role(desired)?)?;
    if is_cluster {
        ownership::validate_cluster_uid(tenant, &result.object.metadata)?;
    }
    if result
        .object
        .owner_references()
        .iter()
        .any(|owner| owner.kind == "MachineDeployment")
        && !inventory.iter().any(|object| {
            object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "MachineDeployment")
        })
    {
        let api = api_for(
            client.clone(),
            &resource(ownership::CLUSTER_API_VERSION, "MachineDeployment"),
            Some(identity.tenant_name),
        );
        let deployment = api.get(&format!("{}-worker", identity.tenant_name)).await?;
        ownership::validate_root_ownership(&deployment.metadata, identity, "machine-deployment")?;
        ownership::validate_provider_owner(&deployment, identity.tenant_name, false, inventory)?;
        inventory.push(deployment);
    }
    ownership::validate_provider_owner(&result.object, identity.tenant_name, false, inventory)?;
    if result.object.metadata.deletion_timestamp.is_some() {
        return Err(ReconcileError::Pending(format!(
            "{} is deleting",
            result.object.name_any()
        )));
    }
    if !result.created {
        result.object = apply_exact(client, desired, &result.object).await?;
        ownership::validate_root_ownership(&result.object.metadata, identity, role(desired)?)?;
        ownership::validate_provider_owner(&result.object, identity.tenant_name, false, inventory)?;
    }
    inventory.retain(|object| {
        !(object.types == desired.types && object.metadata.name == desired.metadata.name)
    });
    inventory.push(result.object.clone());
    Ok(result)
}

pub async fn ensure_namespace(
    client: Client,
    desired: &DynamicObject,
    identity: Identity<'_>,
) -> Result<Ensured, ReconcileError> {
    let result = read_or_create(client, desired, None, false).await?;
    ownership::validate_root_ownership(&result.object.metadata, identity, "namespace")?;
    ownership::validate_provider_owner(&result.object, identity.tenant_name, false, &[])?;
    if result.object.metadata.deletion_timestamp.is_some() {
        return Err(ReconcileError::Pending("Namespace is deleting".into()));
    }
    Ok(result)
}

#[derive(Debug, Default)]
pub struct BatchResult {
    pub created: bool,
    pub pending: bool,
    pub objects: Vec<DynamicObject>,
}

fn group(object: &DynamicObject) -> u8 {
    match object.types.as_ref().map(|types| types.kind.as_str()) {
        Some("Namespace" | "CustomResourceDefinition") => 0,
        Some(
            "ServiceAccount"
            | "ConfigMap"
            | "Secret"
            | "Service"
            | "PersistentVolume"
            | "PersistentVolumeClaim"
            | "StorageClass"
            | "Role"
            | "ClusterRole"
            | "RoleBinding"
            | "ClusterRoleBinding",
        ) => 1,
        _ => 2,
    }
}

pub async fn ensure_batch(
    client: Client,
    objects: &[DynamicObject],
    identity: Identity<'_>,
) -> Result<BatchResult, ReconcileError> {
    let mut result = BatchResult::default();
    for index in 0..3 {
        for desired in objects.iter().filter(|object| group(object) == index) {
            let ensured = ensure_static(client.clone(), desired, identity).await?;
            result.created |= ensured.created;
            result.pending |= ensured.object.metadata.deletion_timestamp.is_some();
            result.objects.push(ensured.object);
        }
        if index == 0 && (result.pending || !crds_ready(client.clone(), &result.objects).await?) {
            result.pending = true;
            break;
        }
    }
    Ok(result)
}

async fn crds_ready(client: Client, objects: &[DynamicObject]) -> Result<bool, ReconcileError> {
    let crds: Vec<_> = objects
        .iter()
        .filter(|object| {
            object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "CustomResourceDefinition")
        })
        .collect();
    for crd in &crds {
        let established = crd
            .data
            .pointer("/status/conditions")
            .and_then(Value::as_array)
            .is_some_and(|conditions| {
                conditions.iter().any(|condition| {
                    condition["type"] == "Established" && condition["status"] == "True"
                })
            });
        if crd.metadata.deletion_timestamp.is_some() || !established {
            return Ok(false);
        }
    }
    let mut discovered = BTreeMap::new();
    for crd in crds {
        let required = |pointer| {
            crd.data
                .pointer(pointer)
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    ReconcileError::InvalidInput("CRD discovery identity is incomplete".into())
                })
        };
        let group = required("/spec/group")?;
        let kind = required("/spec/names/kind")?;
        let plural = required("/spec/names/plural")?;
        let namespaced = required("/spec/scope")? == "Namespaced";
        let versions = crd
            .data
            .pointer("/spec/versions")
            .and_then(Value::as_array)
            .ok_or_else(|| ReconcileError::InvalidInput("CRD versions are malformed".into()))?;
        for version in versions.iter().filter(|version| version["served"] == true) {
            let version = version["name"]
                .as_str()
                .ok_or_else(|| ReconcileError::InvalidInput("CRD version is malformed".into()))?;
            let api_version = format!("{group}/{version}");
            if !discovered.contains_key(&api_version) {
                match client.list_api_group_resources(&api_version).await {
                    Ok(resources) => {
                        discovered.insert(api_version.clone(), resources);
                    }
                    Err(kube::Error::Api(status)) if status.code == 404 => return Ok(false),
                    Err(error) => return Err(error.into()),
                }
            }
            if !discovered[&api_version].resources.iter().any(|resource| {
                resource.name == plural
                    && resource.kind == kind
                    && resource.namespaced == namespaced
            }) {
                return Ok(false);
            }
        }
    }
    Ok(true)
}
