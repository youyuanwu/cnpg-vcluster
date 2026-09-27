//! Fail-closed teardown from authoritative reads and exact observed identities.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use std::time::Duration;

use crate::allocation::{
    ClaimContext, ReleaseDecision, decide_release, recover_allocation, release,
};
use crate::api::{FINALIZER, Tenant, TenantPhase, TenantStatus, canonical_spec, spec_hash};
use crate::docker::{
    DockerClient, DockerError, DockerVolume, WorkerIdentity, validate_volume, worker_containers,
};
use crate::error::ControllerError as ReconcileError;
use crate::foundation::RuntimeFoundation;
use crate::management::{self, ManagementResource};
use crate::ownership::{
    Identity, validate_cluster_uid, validate_kubeconfig_secret_for_deletion, validate_owner_chain,
    validate_provider_owner, validate_provider_owner_for_deletion, validate_root_ownership,
};
use crate::status as tenant_status;
use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::{Namespace, Secret};
use kube::api::{DeleteParams, ListParams, Preconditions};
use kube::core::DynamicObject;
use kube::runtime::controller::Action;
use kube::{Api, Client};

const FOUNDATION_NAMESPACE: &str = "tenant-system";
const RETRY: Duration = Duration::from_secs(5);

fn invalid(message: impl Into<String>) -> ReconcileError {
    ReconcileError::OwnershipInvalid(message.into())
}

fn docker_error(error: DockerError) -> ReconcileError {
    match error {
        DockerError::Identity(_) => invalid(error.to_string()),
        _ => ReconcileError::Task(error.to_string()),
    }
}

fn pending() -> Action {
    Action::requeue(RETRY)
}

async fn dynamic_get(
    client: Client,
    resource: ManagementResource,
    namespace: &str,
    name: &str,
) -> Result<Option<DynamicObject>, ReconcileError> {
    Ok(
        Api::<DynamicObject>::namespaced_with(client, namespace, &resource.api_resource())
            .get_opt(name)
            .await?,
    )
}

async fn dynamic_list(
    client: Client,
    resource: ManagementResource,
    namespace: &str,
) -> Result<Vec<DynamicObject>, ReconcileError> {
    let mut items =
        Api::<DynamicObject>::namespaced_with(client, namespace, &resource.api_resource())
            .list(&ListParams::default())
            .await?
            .items;
    for item in &mut items {
        item.types.get_or_insert(kube::core::TypeMeta {
            api_version: resource.api_version.into(),
            kind: resource.kind.into(),
        });
    }
    Ok(items)
}

fn exact_delete(uid: &str, rv: &str) -> DeleteParams {
    DeleteParams {
        preconditions: Some(Preconditions {
            uid: Some(uid.into()),
            resource_version: Some(rv.into()),
        }),
        propagation_policy: Some(kube::api::PropagationPolicy::Background),
        ..Default::default()
    }
}

fn uid(
    meta: &k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta,
) -> Result<&str, ReconcileError> {
    meta.uid
        .as_deref()
        .filter(|uid| !uid.is_empty())
        .ok_or_else(|| invalid("missing resource UID"))
}

fn version(
    meta: &k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta,
) -> Result<&str, ReconcileError> {
    meta.resource_version
        .as_deref()
        .filter(|version| !version.is_empty())
        .ok_or_else(|| invalid("missing resourceVersion"))
}

pub struct Finalizer<D> {
    client: Client,
    docker: D,
    supported_version: String,
    foundation: Arc<RuntimeFoundation>,
}

impl<D: DockerClient> Finalizer<D> {
    pub fn with_docker(
        client: Client,
        docker: D,
        supported_version: impl Into<String>,
        foundation: Arc<RuntimeFoundation>,
    ) -> Self {
        Self {
            client,
            docker,
            supported_version: supported_version.into(),
            foundation,
        }
    }

    #[rustfmt::skip]
    async fn status(&self, original: &Tenant, current: &Tenant, status: &TenantStatus, clear_allocation: bool) -> Result<(), ReconcileError> {
        tenant_status::replace_status(self.client.clone(), original, current, status, clear_allocation).await
    }

    pub async fn reconcile(&self, tenant: &Tenant) -> Result<Action, ReconcileError> {
        let name = tenant
            .metadata
            .name
            .as_deref()
            .ok_or_else(|| invalid("Tenant name is missing"))?;
        let tenant_uid = uid(&tenant.metadata)?;
        if tenant.metadata.deletion_timestamp.is_none() {
            return Err(invalid("Tenant is not deleting"));
        }
        let spec = canonical_spec(name, &tenant.spec, &self.supported_version)
            .map_err(|error| ReconcileError::InvalidInput(error.to_string()))?;
        let spec_hash = spec_hash(&spec);
        let recorded_hash = tenant
            .status
            .as_ref()
            .and_then(|status| status.foundation_hash.as_deref());
        let foundation = self
            .foundation
            .deletion(recorded_hash)
            .map_err(|error| ReconcileError::InvalidInput(error.to_string()))?;
        let foundation_hash = &self.foundation.hash;
        let identity = Identity {
            tenant_name: name,
            tenant_uid,
            spec_hash: &spec_hash,
            foundation_hash,
            ownership_label: &foundation.inputs.ownership_label,
            lab_prefix: &foundation.inputs.lab_prefix,
        };
        let claim_context = ClaimContext {
            namespace: FOUNDATION_NAMESPACE,
            ownership_label: identity.ownership_label,
            lab_prefix: identity.lab_prefix,
            tenant_name: name,
            tenant_uid,
            spec_hash: &spec_hash,
            foundation_hash,
            slots: &[],
        };
        let lease_inventory = Api::<Lease>::namespaced(self.client.clone(), FOUNDATION_NAMESPACE)
            .list(&ListParams::default())
            .await?
            .items;
        let bound = tenant
            .status
            .as_ref()
            .and_then(|status| status.allocation.as_ref());
        let ns = Api::<Namespace>::all(self.client.clone())
            .get_opt(name)
            .await?;
        if let Some(ns) = &ns {
            validate_root_ownership(&ns.metadata, identity, "namespace")
                .map_err(|error| invalid(error.to_string()))?;
            if ns
                .metadata
                .owner_references
                .as_ref()
                .is_some_and(|owners| !owners.is_empty())
            {
                return Err(invalid("Namespace has a provider owner"));
            }
        }
        let root_kinds: Vec<_> = management::roots().collect();
        let mut roots = Vec::with_capacity(root_kinds.len());
        for root in &root_kinds {
            let expected_name = root
                .expected_name(name)
                .ok_or_else(|| invalid("management root has no declared name"))?;
            let object = dynamic_get(self.client.clone(), *root, name, &expected_name).await?;
            if let Some(object) = &object {
                if object.metadata.name.as_deref() != Some(expected_name.as_str())
                    || object.metadata.namespace.as_deref() != Some(name)
                    || object.types.as_ref().is_none_or(|types| {
                        types.kind != root.kind || types.api_version != root.api_version
                    })
                {
                    return Err(invalid("management root identity changed during GET"));
                }
                validate_root_ownership(&object.metadata, identity, root.role)
                    .map_err(|error| invalid(error.to_string()))?;
                if root.kind == "Cluster" {
                    validate_cluster_uid(tenant, &object.metadata)
                        .map_err(|error| invalid(error.to_string()))?;
                }
            }
            roots.push(object);
        }
        let cluster_index = root_kinds
            .iter()
            .position(|root| root.kind == "Cluster")
            .unwrap();
        let cluster = roots[cluster_index].as_ref();
        let cluster_uid = tenant
            .status
            .as_ref()
            .and_then(|status| status.cluster_uid.as_deref())
            .filter(|uid| !uid.is_empty());
        let mut inventory: Vec<DynamicObject> = roots.iter().flatten().cloned().collect();
        for (index, root) in roots.iter().enumerate() {
            if let Some(object) = root {
                let checked = if index == cluster_index || cluster_uid.is_none() {
                    validate_provider_owner(object, name, false, &inventory)
                } else {
                    validate_provider_owner_for_deletion(object, tenant, &inventory)
                };
                checked.map_err(|error| invalid(error.to_string()))?;
            }
        }
        let secret_name = management::by_kind("Secret")
            .and_then(|resource| resource.expected_name(name))
            .ok_or_else(|| invalid("Secret has no declared name"))?;
        let secret = Api::<Secret>::namespaced(self.client.clone(), name)
            .get_opt(&secret_name)
            .await?;
        if let Some(secret) = &secret {
            let control_plane = root_kinds
                .iter()
                .position(|root| root.kind == "KamajiControlPlane")
                .and_then(|index| roots[index].as_ref());
            validate_kubeconfig_secret_for_deletion(secret, control_plane)
                .map_err(|error| invalid(error.to_string()))?;
        }
        let descendant_kinds: Vec<_> = management::descendants()
            .filter(|resource| resource.role != "provider")
            .collect();
        let mut descendants: Vec<Vec<DynamicObject>> = Vec::with_capacity(descendant_kinds.len());
        for kind in &descendant_kinds {
            let observed = dynamic_list(self.client.clone(), *kind, name).await?;
            descendants.push(observed);
        }
        inventory.extend(descendants.iter().flatten().cloned());
        let mut inventory_uids = BTreeSet::new();
        for object in &inventory {
            if !inventory_uids.insert(uid(&object.metadata)?) {
                return Err(invalid("duplicate management inventory UID"));
            }
        }
        let mut machine_names = BTreeSet::new();
        for (index, group) in descendants.iter().enumerate() {
            for object in group {
                if object.metadata.namespace.as_deref() != Some(name)
                    || object.metadata.name.as_deref().is_none_or(str::is_empty)
                    || object.types.as_ref().is_none_or(|types| {
                        types.kind != descendant_kinds[index].kind
                            || types.api_version != descendant_kinds[index].api_version
                    })
                {
                    return Err(invalid("provider descendant inventory identity changed"));
                }
                let expected_owner = descendant_kinds[index]
                    .parent_kind
                    .ok_or_else(|| invalid("provider descendant parent is missing"))?;
                let expected_owner_api = management::MANAGEMENT_RESOURCES
                    .iter()
                    .find(|resource| resource.kind == expected_owner)
                    .map(|resource| resource.api_version)
                    .ok_or_else(|| invalid("provider descendant parent kind is unknown"))?;
                let owners = object
                    .metadata
                    .owner_references
                    .as_deref()
                    .unwrap_or_default();
                if !matches!(owners, [owner] if owner.kind == expected_owner
                    && owner.api_version == expected_owner_api)
                {
                    return Err(invalid("provider descendant has no exact provider owner"));
                }
                let deployment = root_kinds
                    .iter()
                    .position(|root| root.kind == "MachineDeployment")
                    .and_then(|index| roots[index].as_ref())
                    .ok_or_else(|| invalid("provider descendant has no live MachineDeployment"))?;
                validate_owner_chain(object, uid(&deployment.metadata)?, &inventory)
                    .map_err(|error| invalid(error.to_string()))?;
                if matches!(descendant_kinds[index].kind, "Machine" | "DevMachine") {
                    validate_root_ownership(&object.metadata, identity, "machine")
                        .map_err(|error| invalid(error.to_string()))?;
                    if descendant_kinds[index].kind == "Machine" {
                        machine_names.insert(
                            object
                                .metadata
                                .name
                                .clone()
                                .ok_or_else(|| invalid("Machine has no name"))?,
                        );
                    }
                }
            }
        }
        let containers = self.docker.list_containers().await.map_err(docker_error)?;
        let load_balancer_present = containers
            .iter()
            .any(|container| container.name == format!("{name}-lb"));
        let workers = worker_containers(
            containers,
            WorkerIdentity {
                tenant_name: name,
                network_id: &foundation.network_id,
                machine_names: &machine_names,
            },
        )
        .map_err(docker_error)?;
        let volume_name = format!("{}-{name}-storage", foundation.inputs.lab_prefix);
        let volume = self
            .docker
            .inspect_volume(&volume_name)
            .await
            .map_err(docker_error)?;
        if let Some(volume) = &volume {
            validate_storage(volume, &volume_name, identity)?;
        }
        let residue = ns.is_some()
            || roots.iter().any(Option::is_some)
            || secret.is_some()
            || descendants.iter().any(|items| !items.is_empty())
            || !workers.is_empty()
            || load_balancer_present
            || volume.is_some();
        let lease_decision = decide_release(&claim_context, &lease_inventory, bound, !residue)
            .map_err(|error| invalid(error.to_string()))?;
        let observed_old_claim = matches!(lease_decision, ReleaseDecision::Delete(_));

        let current = Api::<Tenant>::all(self.client.clone())
            .get_opt(name)
            .await?
            .ok_or_else(|| invalid("Tenant disappeared during deletion"))?;
        tenant_status::validate_identity(tenant, &current)?;
        if current.status != tenant.status {
            return Ok(pending());
        }
        let mut status = current.status.clone().unwrap_or_default();
        if status.phase != Some(TenantPhase::Deleting) {
            status.phase = Some(TenantPhase::Deleting);
            self.status(tenant, &current, &status, false).await?;
            return Ok(pending());
        }
        if status.foundation_hash.as_deref().is_none_or(str::is_empty)
            && (residue || matches!(lease_decision, ReleaseDecision::Delete(_)))
        {
            status.foundation_hash = Some(foundation_hash.clone());
            self.status(tenant, &current, &status, false).await?;
            return Ok(pending());
        }
        if let Some(cluster) = cluster
            && cluster_uid.is_none()
        {
            status.cluster_uid = Some(uid(&cluster.metadata)?.into());
            self.status(tenant, &current, &status, false).await?;
            return Ok(pending());
        }
        if status.allocation.is_none()
            && !matches!(lease_decision, ReleaseDecision::Complete)
            && let Some(allocation) = recover_allocation(&claim_context, &lease_inventory)
                .map_err(|error| invalid(error.to_string()))?
        {
            status.allocation = Some(allocation);
            self.status(tenant, &current, &status, false).await?;
            return Ok(pending());
        }
        if let Some(cluster) = cluster {
            return self
                .delete_root(root_kinds[cluster_index], name, cluster)
                .await;
        }
        for (index, root) in roots.iter().enumerate() {
            if index == cluster_index {
                continue;
            }
            if let Some(root) = root {
                return self.delete_root(root_kinds[index], name, root).await;
            }
        }
        if descendants.iter().any(|items| !items.is_empty())
            || !workers.is_empty()
            || load_balancer_present
        {
            return Ok(pending());
        }
        if volume.is_some() {
            self.docker
                .remove_volume(&volume_name)
                .await
                .map_err(docker_error)?;
            return Ok(pending());
        }
        if secret.is_some() {
            return Ok(pending());
        }
        if let Some(namespace) = ns {
            if namespace.metadata.deletion_timestamp.is_none() {
                Api::<Namespace>::all(self.client.clone())
                    .delete(
                        name,
                        &exact_delete(uid(&namespace.metadata)?, version(&namespace.metadata)?),
                    )
                    .await?;
            }
            return Ok(pending());
        }
        let decision = release(self.client.clone(), &claim_context, bound, true)
            .await
            .map_err(|error| invalid(error.to_string()))?;
        if observed_old_claim {
            return Ok(pending());
        }
        match decision {
            ReleaseDecision::Delete(_) | ReleaseDecision::Pending => return Ok(pending()),
            ReleaseDecision::Complete => {}
        }
        if status.allocation.is_some() {
            status.allocation = None;
            self.status(tenant, &current, &status, true).await?;
            return Ok(pending());
        }
        tenant_status::set_finalizer(self.client.clone(), tenant, &current, FINALIZER, false)
            .await?;
        Ok(Action::await_change())
    }

    async fn delete_root(
        &self,
        kind: ManagementResource,
        namespace: &str,
        observed: &DynamicObject,
    ) -> Result<Action, ReconcileError> {
        if observed.metadata.deletion_timestamp.is_none() {
            Api::<DynamicObject>::namespaced_with(
                self.client.clone(),
                namespace,
                &kind.api_resource(),
            )
            .delete(
                &kind
                    .expected_name(namespace)
                    .ok_or_else(|| invalid("management root has no declared name"))?,
                &exact_delete(uid(&observed.metadata)?, version(&observed.metadata)?),
            )
            .await?;
        }
        Ok(pending())
    }
}

fn validate_storage(
    volume: &DockerVolume,
    name: &str,
    identity: Identity<'_>,
) -> Result<(), ReconcileError> {
    let labels: BTreeMap<String, String> = [
        (identity.ownership_label, identity.lab_prefix),
        ("cnpg-vcluster.capi/role", "tenant-storage"),
        ("cnpg-vcluster.capi/tenant", identity.tenant_name),
        ("tenancy.cnpg-vcluster.io/tenant-uid", identity.tenant_uid),
        ("tenancy.cnpg-vcluster.io/spec-hash", identity.spec_hash),
        (
            "tenancy.cnpg-vcluster.io/foundation-hash",
            identity.foundation_hash,
        ),
    ]
    .into_iter()
    .map(|(key, value)| (key.into(), value.into()))
    .collect();
    validate_volume(volume, name, &labels).map_err(|error| invalid(error.to_string()))
}
