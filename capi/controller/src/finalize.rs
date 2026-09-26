//! Fail-closed management-plane teardown. Every pass reads authoritative
//! state; only exact, observed identities may be mutated.

use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::time::Duration;

use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::{ConfigMap, Namespace, Secret};
use kube::api::{DeleteParams, ListParams, Patch, PatchParams, Preconditions};
use kube::core::{ApiResource, DynamicObject, GroupVersionKind};
use kube::runtime::controller::Action;
use kube::{Api, Client};
use serde_json::{Value, json};

use crate::allocation::{ClaimContext, ReleaseDecision, decide_release, recover_allocation};
use crate::api::{Tenant, TenantPhase, canonical_spec, spec_hash};
use crate::docker::{
    BollardDockerClient, DockerClient, DockerError, DockerVolume, WorkerIdentity, validate_volume,
    worker_containers,
};
use crate::error::ControllerError as ReconcileError;
use crate::foundation::parse_for_deletion;
use crate::ownership::{
    Identity, validate_cluster_uid, validate_kubeconfig_secret_for_deletion, validate_owner_chain,
    validate_provider_owner, validate_provider_owner_for_deletion, validate_root_ownership,
};

const FOUNDATION_NAMESPACE: &str = "tenant-system";
const FOUNDATION_NAME: &str = "tenant-foundation";
const FINALIZER: &str = "tenancy.cnpg-vcluster.io/finalizer";
const RETRY: Duration = Duration::from_secs(5);

#[derive(Clone, Copy)]
struct Kind {
    group: &'static str,
    version: &'static str,
    kind: &'static str,
    plural: &'static str,
    resource: &'static str,
    worker_suffix: bool,
}

const ROOTS: [Kind; 6] = [
    Kind {
        group: "cluster.x-k8s.io",
        version: "v1beta2",
        kind: "Cluster",
        plural: "clusters",
        resource: "cluster",
        worker_suffix: false,
    },
    Kind {
        group: "infrastructure.cluster.x-k8s.io",
        version: "v1beta2",
        kind: "DevCluster",
        plural: "devclusters",
        resource: "dev-cluster",
        worker_suffix: false,
    },
    Kind {
        group: "controlplane.cluster.x-k8s.io",
        version: "v1alpha2",
        kind: "KamajiControlPlane",
        plural: "kamajicontrolplanes",
        resource: "kamaji-control-plane",
        worker_suffix: false,
    },
    Kind {
        group: "bootstrap.cluster.x-k8s.io",
        version: "v1beta2",
        kind: "KubeadmConfigTemplate",
        plural: "kubeadmconfigtemplates",
        resource: "kubeadm-config-template",
        worker_suffix: true,
    },
    Kind {
        group: "infrastructure.cluster.x-k8s.io",
        version: "v1beta2",
        kind: "DevMachineTemplate",
        plural: "devmachinetemplates",
        resource: "dev-machine-template",
        worker_suffix: true,
    },
    Kind {
        group: "cluster.x-k8s.io",
        version: "v1beta2",
        kind: "MachineDeployment",
        plural: "machinedeployments",
        resource: "machine-deployment",
        worker_suffix: true,
    },
];
const DESCENDANTS: [Kind; 4] = [
    Kind {
        group: "cluster.x-k8s.io",
        version: "v1beta2",
        kind: "MachineSet",
        plural: "machinesets",
        resource: "machine",
        worker_suffix: false,
    },
    Kind {
        group: "cluster.x-k8s.io",
        version: "v1beta2",
        kind: "Machine",
        plural: "machines",
        resource: "machine",
        worker_suffix: false,
    },
    Kind {
        group: "infrastructure.cluster.x-k8s.io",
        version: "v1beta2",
        kind: "DevMachine",
        plural: "devmachines",
        resource: "machine",
        worker_suffix: false,
    },
    Kind {
        group: "bootstrap.cluster.x-k8s.io",
        version: "v1beta2",
        kind: "KubeadmConfig",
        plural: "kubeadmconfigs",
        resource: "machine",
        worker_suffix: false,
    },
];

impl Kind {
    fn api_resource(self) -> ApiResource {
        let mut resource =
            ApiResource::from_gvk(&GroupVersionKind::gvk(self.group, self.version, self.kind));
        resource.plural = self.plural.into();
        resource
    }

    fn name(self, tenant: &str) -> String {
        if self.worker_suffix {
            format!("{tenant}-worker")
        } else {
            tenant.into()
        }
    }
}

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

pub trait FinalizationKube: Send + Sync {
    fn foundation(&self) -> impl Future<Output = Result<ConfigMap, ReconcileError>> + Send;
    fn tenant(
        &self,
        name: &str,
    ) -> impl Future<Output = Result<Option<Tenant>, ReconcileError>> + Send;
    fn namespace(
        &self,
        name: &str,
    ) -> impl Future<Output = Result<Option<Namespace>, ReconcileError>> + Send;
    fn secret(
        &self,
        namespace: &str,
        name: &str,
    ) -> impl Future<Output = Result<Option<Secret>, ReconcileError>> + Send;
    fn root(
        &self,
        kind: &str,
        namespace: &str,
        name: &str,
    ) -> impl Future<Output = Result<Option<DynamicObject>, ReconcileError>> + Send;
    fn descendants(
        &self,
        kind: &str,
        namespace: &str,
    ) -> impl Future<Output = Result<Vec<DynamicObject>, ReconcileError>> + Send;
    fn leases(&self) -> impl Future<Output = Result<Vec<Lease>, ReconcileError>> + Send;
    fn lease(
        &self,
        name: &str,
    ) -> impl Future<Output = Result<Option<Lease>, ReconcileError>> + Send;
    fn delete_root(
        &self,
        kind: &str,
        namespace: &str,
        name: &str,
        uid: &str,
        rv: &str,
    ) -> impl Future<Output = Result<(), ReconcileError>> + Send;
    fn delete_namespace(
        &self,
        name: &str,
        uid: &str,
        rv: &str,
    ) -> impl Future<Output = Result<(), ReconcileError>> + Send;
    fn delete_lease(
        &self,
        name: &str,
        uid: &str,
        rv: &str,
    ) -> impl Future<Output = Result<(), ReconcileError>> + Send;
    fn status(
        &self,
        tenant: &Tenant,
        value: Value,
    ) -> impl Future<Output = Result<(), ReconcileError>> + Send;
    fn remove_finalizer(
        &self,
        tenant: &Tenant,
    ) -> impl Future<Output = Result<(), ReconcileError>> + Send;
}

#[derive(Clone)]
pub struct LiveKube {
    client: Client,
}

impl LiveKube {
    pub fn new(client: Client) -> Self {
        Self { client }
    }
}

impl FinalizationKube for LiveKube {
    async fn foundation(&self) -> Result<ConfigMap, ReconcileError> {
        Ok(
            Api::<ConfigMap>::namespaced(self.client.clone(), FOUNDATION_NAMESPACE)
                .get(FOUNDATION_NAME)
                .await?,
        )
    }
    async fn tenant(&self, name: &str) -> Result<Option<Tenant>, ReconcileError> {
        Ok(Api::<Tenant>::all(self.client.clone())
            .get_opt(name)
            .await?)
    }
    async fn namespace(&self, name: &str) -> Result<Option<Namespace>, ReconcileError> {
        Ok(Api::<Namespace>::all(self.client.clone())
            .get_opt(name)
            .await?)
    }
    async fn secret(&self, namespace: &str, name: &str) -> Result<Option<Secret>, ReconcileError> {
        Ok(Api::<Secret>::namespaced(self.client.clone(), namespace)
            .get_opt(name)
            .await?)
    }
    async fn root(
        &self,
        kind: &str,
        namespace: &str,
        name: &str,
    ) -> Result<Option<DynamicObject>, ReconcileError> {
        let resource = ROOTS
            .iter()
            .find(|item| item.kind == kind)
            .ok_or_else(|| invalid("unknown root kind"))?;
        Ok(Api::<DynamicObject>::namespaced_with(
            self.client.clone(),
            namespace,
            &resource.api_resource(),
        )
        .get_opt(name)
        .await?)
    }
    async fn descendants(
        &self,
        kind: &str,
        namespace: &str,
    ) -> Result<Vec<DynamicObject>, ReconcileError> {
        let resource = DESCENDANTS
            .iter()
            .find(|item| item.kind == kind)
            .ok_or_else(|| invalid("unknown descendant kind"))?;
        let mut items = Api::<DynamicObject>::namespaced_with(
            self.client.clone(),
            namespace,
            &resource.api_resource(),
        )
        .list(&ListParams::default())
        .await?
        .items;
        for item in &mut items {
            if item.types.is_none() {
                item.types = Some(kube::core::TypeMeta {
                    api_version: format!("{}/{}", resource.group, resource.version),
                    kind: resource.kind.into(),
                });
            }
        }
        Ok(items)
    }
    async fn leases(&self) -> Result<Vec<Lease>, ReconcileError> {
        Ok(
            Api::<Lease>::namespaced(self.client.clone(), FOUNDATION_NAMESPACE)
                .list(&ListParams::default())
                .await?
                .items,
        )
    }
    async fn lease(&self, name: &str) -> Result<Option<Lease>, ReconcileError> {
        Ok(
            Api::<Lease>::namespaced(self.client.clone(), FOUNDATION_NAMESPACE)
                .get_opt(name)
                .await?,
        )
    }
    async fn delete_root(
        &self,
        kind: &str,
        namespace: &str,
        name: &str,
        uid: &str,
        rv: &str,
    ) -> Result<(), ReconcileError> {
        let resource = ROOTS
            .iter()
            .find(|item| item.kind == kind)
            .ok_or_else(|| invalid("unknown root kind"))?;
        Api::<DynamicObject>::namespaced_with(
            self.client.clone(),
            namespace,
            &resource.api_resource(),
        )
        .delete(name, &exact_delete(uid, rv))
        .await?;
        Ok(())
    }
    async fn delete_namespace(
        &self,
        name: &str,
        uid: &str,
        rv: &str,
    ) -> Result<(), ReconcileError> {
        Api::<Namespace>::all(self.client.clone())
            .delete(name, &exact_delete(uid, rv))
            .await?;
        Ok(())
    }
    async fn delete_lease(&self, name: &str, uid: &str, rv: &str) -> Result<(), ReconcileError> {
        Api::<Lease>::namespaced(self.client.clone(), FOUNDATION_NAMESPACE)
            .delete(name, &exact_delete(uid, rv))
            .await?;
        Ok(())
    }
    async fn status(&self, tenant: &Tenant, value: Value) -> Result<(), ReconcileError> {
        let name = tenant
            .metadata
            .name
            .as_deref()
            .ok_or_else(|| invalid("Tenant name is missing"))?;
        let rv = version(&tenant.metadata)?;
        Api::<Tenant>::all(self.client.clone())
            .patch_status(
                name,
                &PatchParams::default(),
                &Patch::Merge(&json!({
                    "metadata":{"resourceVersion":rv}, "status":value
                })),
            )
            .await?;
        Ok(())
    }
    async fn remove_finalizer(&self, tenant: &Tenant) -> Result<(), ReconcileError> {
        let name = tenant
            .metadata
            .name
            .as_deref()
            .ok_or_else(|| invalid("Tenant name is missing"))?;
        let rv = version(&tenant.metadata)?;
        let uid = uid(&tenant.metadata)?;
        let finalizers: Vec<_> = tenant
            .metadata
            .finalizers
            .as_deref()
            .unwrap_or_default()
            .iter()
            .filter(|value| value.as_str() != FINALIZER)
            .collect();
        Api::<Tenant>::all(self.client.clone())
            .patch(
                name,
                &PatchParams::default(),
                &Patch::Merge(&json!({
                    "metadata":{"resourceVersion":rv,"uid":uid,"finalizers":finalizers}
                })),
            )
            .await?;
        Ok(())
    }
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

pub struct Finalizer<K = LiveKube, D = BollardDockerClient> {
    kube: K,
    docker: D,
    supported_version: String,
}

impl Finalizer<LiveKube, BollardDockerClient> {
    pub fn new(
        client: Client,
        docker: BollardDockerClient,
        supported_version: impl Into<String>,
    ) -> Self {
        Self {
            kube: LiveKube::new(client),
            docker,
            supported_version: supported_version.into(),
        }
    }
}

impl<K: FinalizationKube, D: DockerClient> Finalizer<K, D> {
    pub fn with_adapters(kube: K, docker: D, supported_version: impl Into<String>) -> Self {
        Self {
            kube,
            docker,
            supported_version: supported_version.into(),
        }
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
        let cm = self.kube.foundation().await?;
        let data = cm
            .data
            .as_ref()
            .ok_or_else(|| invalid("foundation data is missing"))?;
        let raw = data
            .get("foundation.json")
            .ok_or_else(|| invalid("foundation JSON is missing"))?;
        let hash = data
            .get("foundation.sha256")
            .ok_or_else(|| invalid("foundation checksum is missing"))?;
        let recorded_hash = tenant
            .status
            .as_ref()
            .and_then(|status| status.foundation_hash.as_deref());
        let verified = parse_for_deletion(raw, hash, recorded_hash)
            .map_err(|error| ReconcileError::InvalidInput(error.to_string()))?;
        let foundation = &verified.value;
        let foundation_hash = &verified.hash;
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
        let lease_inventory = self.kube.leases().await?;
        let bound = tenant
            .status
            .as_ref()
            .and_then(|status| status.allocation.as_ref());
        let ns = self.kube.namespace(name).await?;
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
        let mut roots = Vec::with_capacity(ROOTS.len());
        for root in ROOTS {
            let object = self.kube.root(root.kind, name, &root.name(name)).await?;
            if let Some(object) = &object {
                let expected_version = format!("{}/{}", root.group, root.version);
                if object.metadata.name.as_deref() != Some(root.name(name).as_str())
                    || object.metadata.namespace.as_deref() != Some(name)
                    || object.types.as_ref().is_none_or(|types| {
                        types.kind != root.kind || types.api_version != expected_version
                    })
                {
                    return Err(invalid("management root identity changed during GET"));
                }
                validate_root_ownership(&object.metadata, identity, root.resource)
                    .map_err(|error| invalid(error.to_string()))?;
                if root.kind == "Cluster" {
                    validate_cluster_uid(tenant, &object.metadata)
                        .map_err(|error| invalid(error.to_string()))?;
                }
            }
            roots.push(object);
        }
        let cluster = roots[0].as_ref();
        let cluster_uid = tenant
            .status
            .as_ref()
            .and_then(|status| status.cluster_uid.as_deref())
            .filter(|uid| !uid.is_empty());
        let mut inventory: Vec<DynamicObject> = roots.iter().flatten().cloned().collect();
        for (index, root) in roots.iter().enumerate() {
            if let Some(object) = root {
                let checked = if index == 0 || cluster_uid.is_none() {
                    validate_provider_owner(object, name, false, &inventory)
                } else {
                    validate_provider_owner_for_deletion(object, tenant, &inventory)
                };
                checked.map_err(|error| invalid(error.to_string()))?;
            }
        }
        let secret = self
            .kube
            .secret(name, &format!("{name}-kubeconfig"))
            .await?;
        if let Some(secret) = &secret {
            validate_kubeconfig_secret_for_deletion(secret, roots[2].as_ref())
                .map_err(|error| invalid(error.to_string()))?;
        }
        let mut descendants: Vec<Vec<DynamicObject>> = Vec::with_capacity(DESCENDANTS.len());
        for kind in DESCENDANTS {
            let observed = self.kube.descendants(kind.kind, name).await?;
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
                        types.kind != DESCENDANTS[index].kind
                            || types.api_version
                                != format!(
                                    "{}/{}",
                                    DESCENDANTS[index].group, DESCENDANTS[index].version
                                )
                    })
                {
                    return Err(invalid("provider descendant inventory identity changed"));
                }
                let expected_owner = match index {
                    0 => "MachineDeployment",
                    1 => "MachineSet",
                    _ => "Machine",
                };
                let owners = object
                    .metadata
                    .owner_references
                    .as_deref()
                    .unwrap_or_default();
                if !matches!(owners, [owner] if owner.kind == expected_owner
                    && owner.api_version == crate::ownership::CLUSTER_API_VERSION)
                {
                    return Err(invalid("provider descendant has no exact provider owner"));
                }
                let deployment = roots[5]
                    .as_ref()
                    .ok_or_else(|| invalid("provider descendant has no live MachineDeployment"))?;
                validate_owner_chain(object, uid(&deployment.metadata)?, &inventory)
                    .map_err(|error| invalid(error.to_string()))?;
                if index == 1 || index == 2 {
                    validate_root_ownership(&object.metadata, identity, "machine")
                        .map_err(|error| invalid(error.to_string()))?;
                    if index == 1 {
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

        let current = self
            .kube
            .tenant(name)
            .await?
            .ok_or_else(|| invalid("Tenant disappeared during deletion"))?;
        if current.metadata.uid.as_deref() != Some(tenant_uid)
            || current.metadata.deletion_timestamp.is_none()
            || current.spec != tenant.spec
        {
            return Err(invalid("Tenant identity changed during deletion"));
        }
        if current.status != tenant.status {
            return Ok(pending());
        }
        let mut status = current.status.clone().unwrap_or_default();
        if status.phase != Some(TenantPhase::Deleting) {
            status.phase = Some(TenantPhase::Deleting);
            self.kube
                .status(
                    &current,
                    serde_json::to_value(status).map_err(|error| invalid(error.to_string()))?,
                )
                .await?;
            return Ok(pending());
        }
        if status.foundation_hash.as_deref().is_none_or(str::is_empty)
            && (residue || matches!(lease_decision, ReleaseDecision::Delete(_)))
        {
            status.foundation_hash = Some(foundation_hash.clone());
            self.kube
                .status(
                    &current,
                    serde_json::to_value(status).map_err(|error| invalid(error.to_string()))?,
                )
                .await?;
            return Ok(pending());
        }
        if let Some(cluster) = cluster
            && cluster_uid.is_none()
        {
            status.cluster_uid = Some(uid(&cluster.metadata)?.into());
            self.kube
                .status(
                    &current,
                    serde_json::to_value(status).map_err(|error| invalid(error.to_string()))?,
                )
                .await?;
            return Ok(pending());
        }
        if status.allocation.is_none()
            && !matches!(lease_decision, ReleaseDecision::Complete)
            && let Some(allocation) = recover_allocation(&claim_context, &lease_inventory)
                .map_err(|error| invalid(error.to_string()))?
        {
            status.allocation = Some(allocation);
            self.kube
                .status(
                    &current,
                    serde_json::to_value(status).map_err(|error| invalid(error.to_string()))?,
                )
                .await?;
            return Ok(pending());
        }
        if let Some(cluster) = cluster {
            return self.delete_root(ROOTS[0], name, cluster).await;
        }
        for (index, root) in roots.iter().enumerate().skip(1) {
            if let Some(root) = root {
                return self.delete_root(ROOTS[index], name, root).await;
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
                self.kube
                    .delete_namespace(
                        name,
                        uid(&namespace.metadata)?,
                        version(&namespace.metadata)?,
                    )
                    .await?;
            }
            return Ok(pending());
        }
        let decision = decide_release(&claim_context, &lease_inventory, bound, true)
            .map_err(|error| invalid(error.to_string()))?;
        match decision {
            ReleaseDecision::Delete(intent) => {
                let live = self.kube.lease(&intent.name).await?;
                if let Some(live) = live {
                    let fresh = decide_release(&claim_context, &[live], bound, true)
                        .map_err(|error| invalid(error.to_string()))?;
                    if let ReleaseDecision::Delete(fresh) = fresh {
                        if fresh.uid != intent.uid {
                            return Err(invalid("Lease UID changed during release"));
                        }
                        self.kube
                            .delete_lease(&fresh.name, &fresh.uid, &fresh.resource_version)
                            .await?;
                    }
                }
                return Ok(pending());
            }
            ReleaseDecision::Pending => return Ok(pending()),
            ReleaseDecision::Complete => {}
        }
        if status.allocation.is_some() {
            status.allocation = None;
            let mut value =
                serde_json::to_value(status).map_err(|error| invalid(error.to_string()))?;
            value["allocation"] = Value::Null;
            self.kube.status(&current, value).await?;
            return Ok(pending());
        }
        if current
            .metadata
            .finalizers
            .as_ref()
            .is_some_and(|values| values.iter().any(|value| value == FINALIZER))
        {
            self.kube.remove_finalizer(&current).await?;
        }
        Ok(Action::await_change())
    }

    async fn delete_root(
        &self,
        kind: Kind,
        namespace: &str,
        observed: &DynamicObject,
    ) -> Result<Action, ReconcileError> {
        if observed.metadata.deletion_timestamp.is_none() {
            self.kube
                .delete_root(
                    kind.kind,
                    namespace,
                    &kind.name(namespace),
                    uid(&observed.metadata)?,
                    version(&observed.metadata)?,
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
