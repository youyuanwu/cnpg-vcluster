use std::collections::{BTreeMap, BTreeSet};

use kube::{Client, ResourceExt, api::ListParams, core::DynamicObject};

use crate::{
    docker::{self, DockerClient, WorkerIdentity},
    ownership::{self, Identity},
    readiness::{node_ready, object_ready, workload_available},
};

use super::{
    ReconcileError,
    objects::{api_for, resource},
};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WorkerObservation {
    pub inventory_complete: bool,
    pub all_ready: bool,
    pub network_ready: bool,
}

pub struct WorkerInputs<'a> {
    pub identity: Identity<'a>,
    pub network_id: &'a str,
    pub desired_count: i32,
    pub deployment: &'a DynamicObject,
    pub network_objects: &'a [DynamicObject],
}

async fn list(
    client: Client,
    version: &str,
    kind: &str,
    namespace: Option<&str>,
) -> Result<Vec<DynamicObject>, ReconcileError> {
    let resource = resource(version, kind);
    let mut items = api_for(client, &resource, namespace)
        .list(&ListParams::default())
        .await?
        .items;
    for item in &mut items {
        if item
            .types
            .as_ref()
            .is_some_and(|types| types.api_version != version || types.kind != kind)
            || item
                .metadata
                .namespace
                .as_deref()
                .filter(|namespace| !namespace.is_empty())
                != namespace
            || item.metadata.name.as_deref().is_none_or(str::is_empty)
            || item.metadata.uid.as_deref().is_none_or(str::is_empty)
        {
            return Err(ReconcileError::OwnershipInvalid(format!(
                "{kind} inventory identity is invalid"
            )));
        }
        item.types = Some(kube::core::TypeMeta {
            api_version: version.into(),
            kind: kind.into(),
        });
    }
    Ok(items)
}

pub async fn observe_workers<D: DockerClient>(
    management: Client,
    tenant_client: Client,
    docker: &D,
    inputs: WorkerInputs<'_>,
) -> Result<WorkerObservation, ReconcileError> {
    let WorkerInputs {
        identity,
        network_id,
        desired_count,
        deployment,
        network_objects,
    } = inputs;
    ownership::validate_root_ownership(&deployment.metadata, identity, "machine-deployment")?;
    let deployment_uid = deployment.uid().ok_or_else(|| {
        ReconcileError::OwnershipInvalid("MachineDeployment UID is missing".into())
    })?;
    let sets = list(
        management.clone(),
        ownership::CLUSTER_API_VERSION,
        "MachineSet",
        Some(identity.tenant_name),
    )
    .await?;
    let machines = list(
        management.clone(),
        ownership::CLUSTER_API_VERSION,
        "Machine",
        Some(identity.tenant_name),
    )
    .await?;
    let dev_machines = list(
        management,
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevMachine",
        Some(identity.tenant_name),
    )
    .await?;
    let mut inventory = vec![deployment.clone()];
    inventory.extend(sets.iter().chain(&machines).cloned());
    let mut uids = BTreeSet::new();
    for object in &inventory {
        if !uids.insert(object.uid()) {
            return Err(ReconcileError::OwnershipInvalid(
                "duplicate worker inventory UID".into(),
            ));
        }
    }
    for set in &sets {
        ownership::validate_owner_chain(set, &deployment_uid, &inventory)?;
    }
    let mut machine_names = BTreeSet::new();
    let mut by_uid = BTreeMap::new();
    for machine in &machines {
        ownership::validate_root_ownership(&machine.metadata, identity, "machine")?;
        ownership::validate_owner_chain(machine, &deployment_uid, &inventory)?;
        if !machine_names.insert(machine.name_any())
            || by_uid
                .insert(machine.uid().unwrap_or_default(), machine)
                .is_some()
        {
            return Err(ReconcileError::OwnershipInvalid(
                "duplicate Machine identity".into(),
            ));
        }
    }
    let mut dev_names = BTreeSet::new();
    for machine in &dev_machines {
        if !uids.insert(machine.uid()) {
            return Err(ReconcileError::OwnershipInvalid(
                "duplicate DevMachine inventory UID".into(),
            ));
        }
        let [owner] = machine.owner_references() else {
            return Err(ReconcileError::OwnershipInvalid(
                "DevMachine must have one exact Machine owner".into(),
            ));
        };
        let root = by_uid.get(&owner.uid).ok_or_else(|| {
            ReconcileError::OwnershipInvalid("DevMachine has no exact Machine".into())
        })?;
        if machine.name_any() != root.name_any() || !dev_names.insert(machine.name_any()) {
            return Err(ReconcileError::OwnershipInvalid(
                "DevMachine name does not match its exact Machine".into(),
            ));
        }
        ownership::validate_owner_chain(machine, &owner.uid, &inventory)?;
    }
    let containers = docker::worker_containers(
        docker.list_containers().await?,
        WorkerIdentity {
            tenant_name: identity.tenant_name,
            network_id,
            machine_names: &machine_names,
        },
    )?;
    // Validate the complete Node inventory even when another component is short.
    // Counts must never hide a foreign identity.
    let nodes = list(tenant_client.clone(), "v1", "Node", None).await?;
    let mut node_names = BTreeSet::new();
    let mut node_uids = BTreeSet::new();
    for node in &nodes {
        if !machine_names.contains(&node.name_any())
            || !node_names.insert(node.name_any())
            || !node_uids.insert(node.uid())
        {
            return Err(ReconcileError::OwnershipInvalid(
                "Node has no unique exact Machine".into(),
            ));
        }
    }
    let count = usize::try_from(desired_count)
        .map_err(|_| ReconcileError::InvalidInput("negative worker count".into()))?;
    let inventory_complete = count > 0
        && [
            machines.len(),
            dev_machines.len(),
            containers.len(),
            nodes.len(),
        ]
        .into_iter()
        .all(|value| value == count)
        && containers
            .iter()
            .all(|container| container.state == "running");
    let all_ready = inventory_complete
        && machines.iter().chain(&dev_machines).all(object_ready)
        && nodes.iter().all(node_ready);
    let network_ready = if inventory_complete {
        network_workloads_ready(tenant_client, network_objects).await?
    } else {
        false
    };
    Ok(WorkerObservation {
        inventory_complete,
        all_ready,
        network_ready,
    })
}

pub async fn network_workloads_ready(
    client: Client,
    observed: &[DynamicObject],
) -> Result<bool, ReconcileError> {
    for (kind, name) in [
        ("DaemonSet", "calico-node"),
        ("Deployment", "calico-kube-controllers"),
        ("DaemonSet", "capi-kube-proxy"),
        ("Deployment", "coredns"),
    ] {
        let cached = observed.iter().find(|object| {
            object
                .types
                .as_ref()
                .is_some_and(|types| types.api_version == "apps/v1" && types.kind == kind)
                && object.metadata.namespace.as_deref() == Some("kube-system")
                && object.name_any() == name
        });
        let fetched;
        let current = match cached {
            Some(current) => current,
            None => {
                fetched = api_for(
                    client.clone(),
                    &resource("apps/v1", kind),
                    Some("kube-system"),
                )
                .get_opt(name)
                .await?;
                let Some(current) = fetched.as_ref() else {
                    return Ok(false);
                };
                current
            }
        };
        if !workload_available(current) {
            return Ok(false);
        }
    }
    Ok(true)
}
