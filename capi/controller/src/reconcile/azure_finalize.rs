use std::collections::{BTreeMap, BTreeSet};

use k8s_openapi::api::core::v1::Secret;
use kube::{
    Api, Client, ResourceExt,
    api::{DeleteParams, ListParams, Patch, PatchParams, Preconditions, PropagationPolicy},
    core::DynamicObject,
    runtime::controller::Action,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{
    api::{
        AzureDeletionStatus, AzureKubeconfigStatus, AzureManagementStatus,
        AzureProviderResourceIdentity, CanonicalSpec, Tenant, TenantPhase, TenantProviderSpec,
        canonical_spec, spec_hash,
    },
    azure::{
        AzureConfiguration, AzureContext, EXTERNAL_CONTROL_PLANE_LABEL, desired_objects,
        validate_binding, validate_desired_object, validate_live_identity,
    },
    error::ControllerError,
    management,
    readiness::set_condition,
    status,
};

use super::{
    DEPENDENCY_INTERVAL, FINALIZER, PROGRESS_INTERVAL, ReconcileError,
    azure::{operation_id, require_current_configuration},
    objects,
};

const DRAIN: &str = "machine.cluster.x-k8s.io/exclude-node-draining";

fn blocked(message: impl Into<String>) -> ReconcileError {
    ReconcileError::OwnershipInvalid(message.into())
}

fn uid(object: &DynamicObject) -> Result<String, ReconcileError> {
    object
        .uid()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| blocked("Azure deletion object UID is missing"))
}

fn rv(object: &DynamicObject) -> Result<String, ReconcileError> {
    object
        .resource_version()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| blocked("Azure deletion object resourceVersion is missing"))
}

fn key(object: &DynamicObject) -> Result<String, ReconcileError> {
    let types = object
        .types
        .as_ref()
        .ok_or_else(|| blocked("Azure deletion object GVK is missing"))?;
    Ok(format!(
        "{}/{}/{}/{}",
        types.api_version,
        types.kind,
        object.namespace().unwrap_or_default(),
        object.name_any()
    ))
}

fn recorded_delete(
    object: &DynamicObject,
    deletion: &AzureDeletionStatus,
) -> Result<DeleteParams, ReconcileError> {
    deletion
        .resource_versions
        .get(&key(object)?)
        .ok_or_else(|| blocked("Azure deletion resourceVersion barrier is incomplete"))?;
    let live_resource_version = rv(object)?;
    Ok(DeleteParams {
        propagation_policy: Some(PropagationPolicy::Foreground),
        preconditions: Some(Preconditions {
            uid: Some(uid(object)?),
            resource_version: Some(live_resource_version),
        }),
        ..Default::default()
    })
}

async fn list(
    client: Client,
    tenant: &str,
    definition: management::ManagementResource,
) -> Result<Vec<DynamicObject>, ReconcileError> {
    let mut items =
        Api::<DynamicObject>::namespaced_with(client, tenant, &definition.api_resource())
            .list(&ListParams::default())
            .await?
            .items;
    for item in &mut items {
        item.types.get_or_insert(kube::core::TypeMeta {
            api_version: definition.api_version.into(),
            kind: definition.kind.into(),
        });
    }
    Ok(items)
}

fn resource_identity(
    object: &DynamicObject,
) -> Result<AzureProviderResourceIdentity, ReconcileError> {
    let types = object
        .types
        .as_ref()
        .ok_or_else(|| blocked("Azure provider resource GVK is missing"))?;
    let mut owner_uids: Vec<_> = object
        .owner_references()
        .iter()
        .map(|owner| owner.uid.clone())
        .collect();
    owner_uids.sort();
    owner_uids.dedup();
    let resource_id = [
        "/status/id",
        "/status/resourceId",
        "/status/providerID",
        "/spec/providerID",
    ]
    .into_iter()
    .find_map(|pointer| object.data.pointer(pointer).and_then(Value::as_str))
    .map(Into::into);
    Ok(AzureProviderResourceIdentity {
        api_version: types.api_version.clone(),
        kind: types.kind.clone(),
        namespace: object.namespace(),
        name: object.name_any(),
        uid: uid(object)?,
        resource_id,
        owner_uids,
    })
}

fn same_resource(
    recorded: &AzureProviderResourceIdentity,
    live: &AzureProviderResourceIdentity,
) -> bool {
    recorded.api_version == live.api_version
        && recorded.kind == live.kind
        && recorded.namespace == live.namespace
        && recorded.name == live.name
        && recorded.uid == live.uid
        && recorded.owner_uids == live.owner_uids
        && match (&recorded.resource_id, &live.resource_id) {
            (Some(left), Some(right)) => left.eq_ignore_ascii_case(right),
            (None, None) => true,
            _ => false,
        }
}

fn validate_identity(
    desired: &DynamicObject,
    live: &DynamicObject,
    recorded_uid: Option<&str>,
    deletion_recorded: bool,
) -> Result<(), ReconcileError> {
    let mut observed = live.clone();
    observed.metadata.deletion_timestamp = None;
    validate_live_identity(desired, &observed, recorded_uid)
        .map_err(|error| blocked(error.to_string()))?;
    if live.metadata.deletion_timestamp.is_some() {
        if !deletion_recorded {
            return Err(blocked(
                "Azure explicit object began deletion before its identity barrier",
            ));
        }
        if desired
            .types
            .as_ref()
            .is_some_and(|types| types.kind == "AzureCluster")
            && observed
                .data
                .pointer("/spec/networkSpec/apiServerLB")
                .is_none()
        {
            observed.data["spec"]["networkSpec"]["apiServerLB"] =
                desired.data["spec"]["networkSpec"]["apiServerLB"].clone();
        }
    }
    validate_desired_object(desired, &observed).map_err(|error| blocked(error.to_string()))
}

fn record_uid(
    status: &mut AzureManagementStatus,
    object: &DynamicObject,
    tenant: &str,
) -> Result<(), ControllerError> {
    let types = object
        .types
        .as_ref()
        .ok_or_else(|| ControllerError::OwnershipInvalid("Azure object GVK is missing".into()))?;
    super::azure::record_uid(
        status,
        &types.kind,
        &object.name_any(),
        tenant,
        object.uid().as_deref().unwrap_or(""),
    )
}

fn descendants(
    inventory: &[DynamicObject],
    root_uids: &BTreeSet<String>,
) -> Result<Vec<AzureProviderResourceIdentity>, ReconcileError> {
    let mut owned = root_uids.clone();
    let mut selected = BTreeSet::new();
    loop {
        let before = selected.len();
        for (index, object) in inventory.iter().enumerate() {
            let owners = object.owner_references();
            let controllers: Vec<_> = owners
                .iter()
                .filter(|owner| owner.controller == Some(true))
                .collect();
            let azure_pool_machine = object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "AzureMachinePoolMachine");
            let owner_match = if azure_pool_machine {
                owners.iter().any(|owner| owned.contains(&owner.uid))
            } else {
                controllers.len() == 1 && owned.contains(&controllers[0].uid)
            };
            if owner_match {
                owned.insert(uid(object)?);
                selected.insert(index);
            }
        }
        if before == selected.len() {
            break;
        }
    }
    if selected.len() != inventory.len() {
        let unknown = inventory
            .iter()
            .enumerate()
            .filter(|(index, _)| !selected.contains(index))
            .map(|(_, object)| {
                format!(
                    "{}/{}",
                    object
                        .types
                        .as_ref()
                        .map_or("unknown", |types| types.kind.as_str()),
                    object.name_any()
                )
            })
            .collect::<Vec<_>>()
            .join(",");
        return Err(blocked(format!(
            "unknown, foreign, or ownerless residue is present: {unknown}"
        )));
    }
    let mut result = inventory
        .iter()
        .map(resource_identity)
        .collect::<Result<Vec<_>, _>>()?;
    result.sort_by(|left, right| {
        (
            &left.api_version,
            &left.kind,
            &left.namespace,
            &left.name,
            &left.uid,
        )
            .cmp(&(
                &right.api_version,
                &right.kind,
                &right.namespace,
                &right.name,
                &right.uid,
            ))
    });
    Ok(result)
}

fn exact_recorded_resources(
    recorded: &[AzureProviderResourceIdentity],
    live: &[AzureProviderResourceIdentity],
    pool_started: bool,
    cluster_started: bool,
    pool_root_uids: &BTreeSet<String>,
) -> Result<(), ReconcileError> {
    for current in live {
        if !recorded.iter().any(|item| same_resource(item, current))
            && !cluster_started
            && !(pool_started && pool_scoped(current, pool_root_uids))
        {
            return Err(blocked(
                "Azure provider residue is not present in the durable deletion inventory",
            ));
        }
    }
    for item in recorded {
        if live.iter().any(|current| same_resource(item, current))
            || cluster_started
            || (pool_started
                && (matches!(
                    item.kind.as_str(),
                    "Machine" | "MachineSet" | "AzureMachinePoolMachine"
                ) || item
                    .owner_uids
                    .iter()
                    .any(|uid| pool_root_uids.contains(uid))))
        {
            continue;
        }
        return Err(blocked(
            "recorded Azure provider residue disappeared before its root deletion",
        ));
    }
    Ok(())
}

fn pool_scoped(
    resource: &AzureProviderResourceIdentity,
    pool_root_uids: &BTreeSet<String>,
) -> bool {
    matches!(
        resource.kind.as_str(),
        "Machine" | "MachineSet" | "AzureMachinePoolMachine"
    ) || resource
        .owner_uids
        .iter()
        .any(|uid| pool_root_uids.contains(uid))
}

fn tenant_resource_ids(
    binding: &crate::api::AzureBindingStatus,
    resources: &[AzureProviderResourceIdentity],
    vmss: Option<&crate::api::AzureVmssStatus>,
) -> BTreeSet<String> {
    let foundation = [
        &binding.resource_group_id,
        &binding.virtual_network_id,
        &binding.tenant_subnet_id,
        &binding.identity_id,
    ];
    let mut result: BTreeSet<_> = resources
        .iter()
        .filter_map(|item| item.resource_id.clone())
        .filter(|id| {
            !foundation
                .iter()
                .any(|value| value.eq_ignore_ascii_case(id))
        })
        .collect();
    if let Some(vmss) = vmss {
        result.extend(vmss.id.iter().cloned());
        result.extend(vmss.instance_ids.iter().cloned());
    }
    result
}

fn descends_from(
    object: &DynamicObject,
    inventory: &[DynamicObject],
    roots: &BTreeSet<String>,
) -> bool {
    let mut current = object;
    let mut visited = BTreeSet::new();
    loop {
        let controllers: Vec<_> = current
            .owner_references()
            .iter()
            .filter(|owner| owner.controller == Some(true))
            .collect();
        let [owner] = controllers.as_slice() else {
            return false;
        };
        if roots.contains(&owner.uid) {
            return true;
        }
        if !visited.insert(owner.uid.clone()) {
            return false;
        }
        let Some(parent) = inventory
            .iter()
            .find(|candidate| candidate.uid().as_deref() == Some(&owner.uid))
        else {
            return false;
        };
        current = parent;
    }
}

async fn patch_drain(
    client: Client,
    configuration: &AzureConfiguration,
    machine: &DynamicObject,
) -> Result<bool, ReconcileError> {
    if machine.annotations().get(DRAIN).map(String::as_str) == Some("true") {
        return Ok(false);
    }
    require_current_configuration(client.clone(), configuration).await?;
    let updated = objects::object_api(client, machine)?
        .patch(
            &machine.name_any(),
            &PatchParams::default(),
            &Patch::Merge(&json!({"metadata":{
                "uid":uid(machine)?,"resourceVersion":rv(machine)?,
                "annotations":{DRAIN:"true"}
            }})),
        )
        .await?;
    if updated.uid() != machine.uid()
        || updated.annotations().get(DRAIN).map(String::as_str) != Some("true")
    {
        return Err(blocked("Machine drain exclusion patch changed identity"));
    }
    Ok(true)
}

async fn retain_lb(
    client: Client,
    configuration: &AzureConfiguration,
    azure_cluster: &DynamicObject,
) -> Result<bool, ReconcileError> {
    if azure_cluster
        .data
        .pointer("/spec/controlPlaneEnabled")
        .and_then(Value::as_bool)
        != Some(false)
        || azure_cluster
            .labels()
            .get(EXTERNAL_CONTROL_PLANE_LABEL)
            .map(String::as_str)
            != Some("true")
    {
        return Err(blocked(
            "CAPZ external-control-plane ownership contract changed",
        ));
    }
    if azure_cluster
        .data
        .pointer("/spec/networkSpec/apiServerLB/type")
        .and_then(Value::as_str)
        == Some("Public")
    {
        return Ok(false);
    }
    require_current_configuration(client.clone(), configuration).await?;
    let updated = objects::object_api(client, azure_cluster)?
        .patch(
            &azure_cluster.name_any(),
            &PatchParams::default(),
            &Patch::Merge(&json!({"metadata":{
                "uid":uid(azure_cluster)?,"resourceVersion":rv(azure_cluster)?
            },"spec":{"networkSpec":{"apiServerLB":{"type":"Public"}}}})),
        )
        .await?;
    if updated.uid() != azure_cluster.uid()
        || updated
            .data
            .pointer("/spec/networkSpec/apiServerLB/type")
            .and_then(Value::as_str)
            != Some("Public")
    {
        return Err(blocked("CAPZ deletion placeholder was not retained"));
    }
    Ok(true)
}

pub async fn finalize(
    client: Client,
    configuration: &AzureConfiguration,
    tenant: &Tenant,
    supported_version: &str,
) -> Result<Action, ReconcileError> {
    require_current_configuration(client.clone(), configuration).await?;
    let name = tenant.name_any();
    let tenant_uid = tenant
        .uid()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| blocked("Tenant UID is missing"))?;
    let spec: CanonicalSpec = canonical_spec(&name, &tenant.spec, supported_version)
        .map_err(|error| ReconcileError::InvalidInput(error.to_string()))?;
    if !matches!(spec.provider, TenantProviderSpec::Azure { .. }) {
        return Err(blocked("Azure finalizer received a non-Azure Tenant"));
    }
    let specification_sha256 = spec_hash(&spec);
    let operation = operation_id(&tenant_uid, &specification_sha256);
    let binding = configuration.binding(&tenant_uid, &specification_sha256, &operation);
    let azure_status = tenant
        .status
        .as_ref()
        .and_then(|status| status.azure())
        .ok_or_else(|| blocked("Azure typed provider status is absent"))?;
    validate_binding(
        azure_status
            .binding
            .as_ref()
            .ok_or_else(|| blocked("Azure provider binding is absent"))?,
        &binding,
    )
    .map_err(|error| blocked(error.to_string()))?;
    let context = AzureContext {
        tenant,
        spec: &spec,
        specification_sha256: &specification_sha256,
        foundation_sha256: &configuration.values.foundation_sha256,
        operation_id: &operation,
        configuration: &configuration.values,
    };
    let desired = desired_objects(&context)
        .map_err(|error| ReconcileError::InvalidInput(error.to_string()))?;
    let recorded_management = azure_status.management.clone().unwrap_or_default();
    let mut management = recorded_management.clone();
    let mut explicit = BTreeMap::new();
    for desired in &desired {
        if let Some(live) = objects::object_api(client.clone(), desired)?
            .get_opt(&desired.name_any())
            .await?
        {
            let types = desired.types.as_ref().expect("desired type");
            validate_identity(
                desired,
                &live,
                management.uid_for(&types.kind, &desired.name_any(), &name),
                azure_status.deletion.is_some(),
            )?;
            record_uid(&mut management, &live, &name)?;
            explicit
                .entry(types.kind.clone())
                .or_insert_with(Vec::new)
                .push(live);
        }
    }
    if azure_status.deletion.is_none() {
        let live_uids: BTreeSet<_> = explicit
            .values()
            .flatten()
            .map(uid)
            .collect::<Result<_, _>>()?;
        if recorded_management
            .recorded_uids()
            .iter()
            .any(|recorded| !live_uids.contains(*recorded))
        {
            return Err(blocked(
                "recorded Azure management object disappeared before deletion discovery",
            ));
        }
    }
    for object in explicit.values().flatten() {
        super::azure::validate_parent(object, &management, &name)?;
    }
    let mut inventory = Vec::new();
    let mut seen = BTreeSet::new();
    for definition in management::AZURE_MANAGEMENT_RESOURCES
        .iter()
        .filter(|definition| {
            definition.namespaced
                && (definition.class == management::ResourceClass::Descendant
                    || matches!(definition.kind, "ConfigMap" | "Deployment" | "Secret"))
        })
    {
        if seen.insert((definition.api_version, definition.plural)) {
            inventory.extend(list(client.clone(), &name, *definition).await?);
        }
    }
    inventory.retain(|object| {
        !(object
            .types
            .as_ref()
            .is_some_and(|types| types.kind == "ConfigMap")
            && object.name_any() == "kube-root-ca.crt")
    });
    let explicit_uids: BTreeSet<_> = explicit
        .values()
        .flatten()
        .map(uid)
        .collect::<Result<_, _>>()?;
    let explicit_keys: BTreeSet<_> = explicit
        .values()
        .flatten()
        .map(|object| {
            let types = object.types.as_ref().expect("live type");
            (
                types.api_version.clone(),
                types.kind.clone(),
                object.name_any(),
            )
        })
        .collect();
    let mut provider_objects = Vec::new();
    for object in inventory {
        let types = object
            .types
            .as_ref()
            .ok_or_else(|| blocked("Azure inventory GVK is missing"))?;
        let identity = (
            types.api_version.clone(),
            types.kind.clone(),
            object.name_any(),
        );
        if explicit_keys.contains(&identity) {
            continue;
        }
        if types.kind == "Secret" && object.name_any() == format!("{name}-kubeconfig") {
            continue;
        }
        provider_objects.push(object);
    }
    let provider_resources = descendants(&provider_objects, &explicit_uids)?;
    if azure_status.deletion.is_none() {
        for recorded in &azure_status.provider_resources {
            if !provider_resources
                .iter()
                .any(|current| same_resource(recorded, current))
            {
                return Err(blocked(
                    "recorded Azure provider resource disappeared before deletion discovery",
                ));
            }
        }
    }
    let cluster_started = explicit
        .get("Cluster")
        .and_then(|objects| objects.first())
        .is_none_or(|object| object.metadata.deletion_timestamp.is_some());
    let pool_started = explicit
        .get("MachinePool")
        .and_then(|objects| objects.first())
        .is_none_or(|object| object.metadata.deletion_timestamp.is_some());
    if let Some(recorded) = azure_status.deletion.as_ref() {
        let pool_root_uids = [
            management.kubeadm_config_uid.as_ref(),
            management.azure_machine_pool_uid.as_ref(),
            management.machine_pool_uid.as_ref(),
        ]
        .into_iter()
        .flatten()
        .cloned()
        .collect();
        exact_recorded_resources(
            &azure_status.provider_resources,
            &provider_resources,
            pool_started,
            cluster_started,
            &pool_root_uids,
        )?;
        if provider_resources.iter().any(|item| {
            !recorded.verified_provider_uids.contains(&item.uid)
                && !cluster_started
                && !(pool_started && pool_scoped(item, &pool_root_uids))
        }) {
            return Err(blocked("Azure provider deletion UID barrier changed"));
        }
        let expected: BTreeSet<_> = tenant_resource_ids(
            &binding,
            &azure_status.provider_resources,
            azure_status.vmss.as_ref(),
        )
        .into_iter()
        .map(|value| value.to_ascii_lowercase())
        .collect();
        let verified: BTreeSet<_> = recorded
            .verified_azure_resource_ids
            .iter()
            .map(|value| value.to_ascii_lowercase())
            .collect();
        if expected != verified {
            return Err(blocked("Azure resource deletion identity barrier changed"));
        }
    }
    let secret = Api::<Secret>::namespaced(client.clone(), &name)
        .get_opt(&format!("{name}-kubeconfig"))
        .await?;
    let mut kubeconfig = azure_status.kubeconfig.clone();
    if let Some(secret) = &secret {
        let secret_uid = secret
            .uid()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| blocked("Tenant kubeconfig Secret UID is missing"))?;
        let content = secret
            .data
            .as_ref()
            .and_then(|data| data.get("value"))
            .filter(|value| !value.0.is_empty())
            .ok_or_else(|| blocked("Tenant kubeconfig Secret content is invalid"))?;
        let observed = AzureKubeconfigStatus {
            secret_uid,
            content_sha256: hex::encode(Sha256::digest(&content.0)),
        };
        if kubeconfig
            .as_ref()
            .is_some_and(|recorded| recorded != &observed)
        {
            return Err(blocked("Tenant kubeconfig Secret identity changed"));
        }
        let owners = secret.owner_references();
        let kubeconfig_owners: BTreeSet<_> = ["Cluster", "KamajiControlPlane"]
            .into_iter()
            .filter_map(|kind| explicit.get(kind))
            .flatten()
            .map(uid)
            .collect::<Result<_, _>>()?;
        if owners.len() != 1 || !kubeconfig_owners.contains(&owners[0].uid) {
            return Err(blocked("Tenant kubeconfig Secret owner closure is invalid"));
        }
        kubeconfig = Some(observed);
    } else if !cluster_started && kubeconfig.is_some() {
        return Err(blocked(
            "Tenant kubeconfig Secret disappeared before Cluster deletion",
        ));
    }
    if azure_status.deletion.is_none() {
        let mut resource_versions = BTreeMap::new();
        for object in explicit.values().flatten() {
            resource_versions.insert(key(object)?, rv(object)?);
        }
        let deletion = AzureDeletionStatus {
            resource_versions,
            verified_azure_resource_ids: tenant_resource_ids(
                &binding,
                &provider_resources,
                azure_status.vmss.as_ref(),
            )
            .into_iter()
            .collect(),
            verified_provider_uids: provider_resources
                .iter()
                .map(|item| item.uid.clone())
                .collect(),
        };
        require_current_configuration(client.clone(), configuration).await?;
        status::update_status(client, tenant, |status| {
            let azure = status.azure_mut()?;
            validate_binding(
                azure.binding.as_ref().ok_or_else(|| {
                    ControllerError::OwnershipInvalid("Azure binding is absent".into())
                })?,
                &binding,
            )
            .map_err(|error| ControllerError::OwnershipInvalid(error.to_string()))?;
            azure.management = Some(management.clone());
            azure.kubeconfig = kubeconfig.clone();
            azure.provider_resources = provider_resources.clone();
            azure.deletion = Some(deletion.clone());
            status.phase = Some(TenantPhase::Deleting);
            set_condition(
                status,
                tenant,
                "Ready",
                false,
                "Deleting",
                "Azure deletion identity is recorded",
            );
            Ok(())
        })
        .await?;
        return Ok(Action::requeue(PROGRESS_INTERVAL));
    }
    let deletion = azure_status.deletion.as_ref().expect("checked");
    let machines: Vec<_> = provider_objects
        .iter()
        .filter(|object| {
            object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "Machine")
        })
        .collect();
    if machines
        .iter()
        .any(|machine| !super::azure::markers_match(machine, &binding))
    {
        return Err(blocked("Machine lifecycle markers changed"));
    }
    if let Some(pool) = explicit
        .get("MachinePool")
        .and_then(|objects| objects.first())
    {
        if !explicit.contains_key("Cluster") {
            return Err(blocked("Cluster disappeared before MachinePool deletion"));
        }
        let pool_uid = uid(pool)?;
        for machine in &machines {
            let owners = machine.owner_references();
            if owners.len() != 1 || owners[0].uid != pool_uid {
                return Err(blocked("Machine has no exact MachinePool owner"));
            }
            if patch_drain(client.clone(), configuration, machine).await? {
                return Ok(Action::requeue(PROGRESS_INTERVAL));
            }
        }
        if pool.metadata.deletion_timestamp.is_none() {
            require_current_configuration(client.clone(), configuration).await?;
            objects::object_api(client, pool)?
                .delete(&pool.name_any(), &recorded_delete(pool, deletion)?)
                .await?;
        }
        return Ok(Action::requeue(DEPENDENCY_INTERVAL));
    }
    if !machines.is_empty()
        || explicit.contains_key("AzureMachinePool")
        || provider_objects.iter().any(|object| {
            object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "AzureMachinePoolMachine")
        })
    {
        return Ok(Action::requeue(DEPENDENCY_INTERVAL));
    }
    if let Some(cluster) = explicit.get("Cluster").and_then(|objects| objects.first()) {
        if cluster.metadata.deletion_timestamp.is_none() {
            require_current_configuration(client.clone(), configuration).await?;
            objects::object_api(client, cluster)?
                .delete(&cluster.name_any(), &recorded_delete(cluster, deletion)?)
                .await?;
        }
        return Ok(Action::requeue(DEPENDENCY_INTERVAL));
    }
    if let Some(azure_cluster) = explicit
        .get("AzureCluster")
        .and_then(|objects| objects.first())
    {
        if azure_cluster.metadata.deletion_timestamp.is_none() {
            return Ok(Action::requeue(DEPENDENCY_INTERVAL));
        }
        if retain_lb(client.clone(), configuration, azure_cluster).await? {
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
    }
    let provider_roots = [
        "AzureCluster",
        "KamajiControlPlane",
        "KubeadmConfig",
        "AzureMachinePool",
    ];
    let residual_root_uids: BTreeSet<_> = ["Deployment", "Job"]
        .into_iter()
        .filter_map(|kind| explicit.get(kind))
        .flatten()
        .map(uid)
        .collect::<Result<_, _>>()?;
    if provider_roots
        .iter()
        .any(|kind| explicit.contains_key(*kind))
        || provider_objects
            .iter()
            .any(|object| !descends_from(object, &provider_objects, &residual_root_uids))
        || secret.is_some()
    {
        return Ok(Action::requeue(DEPENDENCY_INTERVAL));
    }
    if let Some(config_maps) = explicit.get("ConfigMap") {
        let mut deleted = false;
        for object in config_maps {
            if object.metadata.deletion_timestamp.is_none() {
                require_current_configuration(client.clone(), configuration).await?;
                objects::object_api(client.clone(), object)?
                    .delete(&object.name_any(), &recorded_delete(object, deletion)?)
                    .await?;
                deleted = true;
            }
        }
        if deleted || !config_maps.is_empty() {
            return Ok(Action::requeue(DEPENDENCY_INTERVAL));
        }
    }
    for kind in ["Job", "Deployment", "AzureClusterIdentity"] {
        if let Some(object) = explicit.get(kind).and_then(|objects| objects.first()) {
            if object.metadata.deletion_timestamp.is_none() {
                require_current_configuration(client.clone(), configuration).await?;
                objects::object_api(client.clone(), object)?
                    .delete(&object.name_any(), &recorded_delete(object, deletion)?)
                    .await?;
            }
            return Ok(Action::requeue(DEPENDENCY_INTERVAL));
        }
    }
    if !provider_objects.is_empty() {
        return Ok(Action::requeue(DEPENDENCY_INTERVAL));
    }
    if let Some(namespace) = explicit
        .get("Namespace")
        .and_then(|objects| objects.first())
    {
        if namespace.metadata.deletion_timestamp.is_none() {
            require_current_configuration(client.clone(), configuration).await?;
            objects::object_api(client.clone(), namespace)?
                .delete(
                    &namespace.name_any(),
                    &recorded_delete(namespace, deletion)?,
                )
                .await?;
        }
        return Ok(Action::requeue(DEPENDENCY_INTERVAL));
    }
    let current = Api::<Tenant>::all(client.clone())
        .get_opt(&name)
        .await?
        .ok_or_else(|| blocked("Tenant disappeared during Azure finalization"))?;
    require_current_configuration(client.clone(), configuration).await?;
    status::set_finalizer(client, tenant, &current, FINALIZER, false).await?;
    Ok(Action::await_change())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn resource(kind: &str, uid: &str, owner: &str) -> AzureProviderResourceIdentity {
        AzureProviderResourceIdentity {
            api_version: "v1".into(),
            kind: kind.into(),
            namespace: Some("tenant".into()),
            name: uid.into(),
            uid: uid.into(),
            resource_id: None,
            owner_uids: vec![owner.into()],
        }
    }

    #[test]
    fn pool_scoped_absence_requires_a_recorded_pool_owner() {
        let pool_uids = BTreeSet::from(["pool-uid".into()]);
        let worker_secret = resource("Secret", "worker-secret", "pool-uid");
        assert!(
            exact_recorded_resources(
                std::slice::from_ref(&worker_secret),
                &[],
                true,
                false,
                &pool_uids,
            )
            .is_ok()
        );
        let control_plane_secret = resource("Secret", "control-plane-secret", "cluster-uid");
        assert!(
            exact_recorded_resources(&[control_plane_secret], &[], true, false, &pool_uids,)
                .is_err()
        );
        let replacement = resource("AzureMachinePoolMachine", "replacement", "pool-uid");
        assert!(exact_recorded_resources(&[], &[replacement], true, false, &pool_uids).is_ok());
        let foreign = resource("Secret", "foreign", "foreign-uid");
        assert!(exact_recorded_resources(&[], &[foreign], true, false, &pool_uids).is_err());
    }
}
