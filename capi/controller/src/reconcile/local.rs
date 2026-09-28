use std::{future::Future, path::Path, sync::Arc};

use kube::{Client, ResourceExt, core::DynamicObject, runtime::controller::Action};

use crate::{
    allocation::{self, ClaimContext},
    api::{CanonicalSpec, Tenant, TenantProviderSpec, spec_hash},
    docker::{BollardDockerClient, DockerClient, validate_volume},
    error::ControllerError,
    foundation::{Foundation, ImageArchive, RuntimeFoundation},
    ownership,
    readiness::{self, Components},
    resources::{self, Context as ResourceContext},
    status,
    tenant_client::{self, TenantClientError},
};

use super::{
    DEPENDENCY_INTERVAL, FOUNDATION_NAMESPACE, PROGRESS_INTERVAL, READY_INTERVAL, ReconcileError,
    STORAGE_CLASS, objects, progress, workers,
};

#[rustfmt::skip]
#[derive(Clone, Default)]
pub struct Assets { pub calico: Vec<u8>, pub cnpg: Vec<u8> }
#[rustfmt::skip]
impl Assets {
    pub fn load(directory: &Path) -> Result<Self, ControllerError> {
        Ok(Self {
            calico: std::fs::read(directory.join("calico.yaml"))
                .map_err(|_| ControllerError::Configuration("cannot read staged Calico asset".into()))?,
            cnpg: std::fs::read(directory.join("cnpg.yaml"))
                .map_err(|_| ControllerError::Configuration("cannot read staged CNPG asset".into()))?,
        })
    }
}
#[rustfmt::skip]
pub trait TenantAccess: Send + Sync {
    fn connect(&self, management: Client, control_plane: &DynamicObject, alternate_owner: Option<&DynamicObject>, tenant_name: &str, endpoint: &str)
        -> impl Future<Output = Result<Client, TenantClientError>> + Send;
}
pub struct LiveTenantAccess;
#[rustfmt::skip]
impl TenantAccess for LiveTenantAccess {
    async fn connect(&self, management: Client, control_plane: &DynamicObject, alternate_owner: Option<&DynamicObject>, tenant_name: &str, endpoint: &str) -> Result<Client, TenantClientError> {
        tenant_client::load_tenant_client_with_owner(management, control_plane, alternate_owner, tenant_name, tenant_name, endpoint)
            .await.map(|(client, _)| client)
    }
}
pub trait ProviderLifecycle: Send + Sync {
    fn supports(&self, provider: &TenantProviderSpec) -> bool;
    fn reconcile<'a>(
        &'a self,
        tenant: &'a Tenant,
        spec: &'a CanonicalSpec,
    ) -> impl Future<Output = Result<Action, ReconcileError>> + Send + 'a;
    fn finalize<'a>(
        &'a self,
        tenant: &'a Tenant,
        supported_version: &'a str,
    ) -> impl Future<Output = Result<Action, ReconcileError>> + Send + 'a;
}
#[rustfmt::skip]
pub struct LocalProvider<D = BollardDockerClient, A = LiveTenantAccess> {
    pub client: Client, pub docker: D, pub access: A, pub assets: Assets,
    pub foundation: Arc<RuntimeFoundation>,
}

#[rustfmt::skip]
impl LocalProvider {
    pub fn new(client: Client, docker: BollardDockerClient, assets: Assets, foundation: Arc<RuntimeFoundation>) -> Self {
        Self { client, docker, access: LiveTenantAccess, assets, foundation }
    }
}
impl<D: DockerClient + Clone, A: TenantAccess> ProviderLifecycle for LocalProvider<D, A> {
    fn supports(&self, provider: &TenantProviderSpec) -> bool {
        matches!(provider, TenantProviderSpec::Local { .. })
    }
    #[rustfmt::skip]
    async fn reconcile(&self, tenant: &Tenant, spec: &CanonicalSpec) -> Result<Action, ReconcileError> {
        let database_count = spec.local_databases().ok_or_else(|| ReconcileError::InvalidInput("azure provider is not supported by this controller".into()))?;
        let current_status = tenant.status.as_ref();
        let recorded_hash = current_status.and_then(|status| status.foundation_hash());
        let foundation = self.foundation.creation(recorded_hash)?;
        let foundation_hash = &self.foundation.hash;
        if status::set_finalizer(self.client.clone(), tenant, tenant, super::FINALIZER, true).await? {
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        if recorded_hash.is_none_or(str::is_empty) {
            status::update_status(self.client.clone(), tenant, |status| {
                let local = status.local_mut()?;
                if local
                    .foundation_hash
                    .as_deref()
                    .is_some_and(|hash| !hash.is_empty() && hash != foundation_hash)
                {
                    return Err(ControllerError::OwnershipInvalid(
                        "foundation binding changed".into(),
                    ));
                }
                local.foundation_hash = Some(foundation_hash.clone());
                readiness::progress_status(status, tenant);
                Ok(())
            })
            .await?;
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        let hash = spec_hash(spec);
        let name = tenant.name_any();
        let uid = tenant.uid().unwrap_or_default();
        let claim = allocation::allocate(
            self.client.clone(),
            &ClaimContext {
                namespace: FOUNDATION_NAMESPACE,
                ownership_label: &foundation.inputs.ownership_label,
                lab_prefix: &foundation.inputs.lab_prefix,
                tenant_name: &name,
                tenant_uid: &uid,
                spec_hash: &hash,
                foundation_hash,
                slots: &foundation.slots,
            },
            current_status.and_then(|status| status.allocation()),
        )
        .await?;
        if current_status.and_then(|status| status.allocation()).is_none() {
            status::update_status(self.client.clone(), tenant, |status| {
                let allocation = claim.status();
                let local = status.local_mut()?;
                if local
                    .allocation
                    .as_ref()
                    .is_some_and(|bound| bound != &allocation)
                {
                    return Err(ControllerError::OwnershipInvalid(
                        "allocation binding changed".into(),
                    ));
                }
                local.allocation = Some(allocation);
                readiness::progress_status(status, tenant);
                Ok(())
            })
            .await?;
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        let endpoint = format!("{}:{}", claim.slot.endpoint, foundation.inputs.api_port);
        let mut context = ResourceContext {
            tenant,
            spec,
            spec_hash: &hash,
            foundation_hash,
            endpoint: &endpoint,
            pod_cidr: &claim.slot.pod_cidr,
            service_cidr: &claim.slot.service_cidr,
            database_count,
            volume_path: "",
            worker_bootstrap_commands: &[],
            inputs: &foundation.inputs,
        };
        let namespace = resources::to_dynamic(&resources::namespace(&context))?;
        if objects::ensure_namespace(self.client.clone(), &namespace, context.identity())
            .await?
            .created
        {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        let mut inventory = Vec::new();
        let cluster = objects::ensure_management(
            self.client.clone(),
            &resources::cluster(&context)?,
            tenant,
            context.identity(),
            &mut inventory,
        )
        .await?;
        if cluster.created {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        if current_status.and_then(|status| status.cluster_uid()).is_none_or(str::is_empty) {
            let cluster_uid = cluster
                .object
                .uid()
                .ok_or_else(|| ReconcileError::OwnershipInvalid("Cluster UID is missing".into()))?;
            status::update_status(self.client.clone(), tenant, |status| {
                let local = status.local_mut()?;
                if local
                    .cluster_uid
                    .as_deref()
                    .is_some_and(|uid| !uid.is_empty() && uid != cluster_uid)
                {
                    return Err(ControllerError::OwnershipInvalid(
                        "Cluster UID binding changed".into(),
                    ));
                }
                local.cluster_uid = Some(cluster_uid.clone());
                readiness::progress_status(status, tenant);
                Ok(())
            })
            .await?;
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        let infrastructure = objects::ensure_management(
            self.client.clone(),
            &resources::dev_cluster(&context)?,
            tenant,
            context.identity(),
            &mut inventory,
        )
        .await?;
        if infrastructure.created {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        let control_plane = objects::ensure_management(
            self.client.clone(),
            &resources::kamaji_control_plane(&context)?,
            tenant,
            context.identity(),
            &mut inventory,
        )
        .await?;
        if control_plane.created {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        for root in [&infrastructure.object, &control_plane.object] {
            ownership::validate_provider_owner(root, &name, true, &inventory)?;
        }
        if !readiness::management_conditions_ready(
            &cluster.object,
            &["ControlPlaneReady", "ControlPlaneAvailable"],
        )? {
            return self.progress(tenant, DEPENDENCY_INTERVAL).await;
        }
        let tenant_client = self
            .access
            .connect(self.client.clone(), &control_plane.object, None, &name, &endpoint)
            .await?;
        tenant_client::ensure_bootstrap_rbac(tenant_client.clone()).await?;
        let volume_name = resources::storage_volume_name(&context);
        let labels = resources::storage_volume_labels(&context);
        let volume = match self.docker.inspect_volume(&volume_name).await? {
            Some(volume) => volume,
            None => self.docker.create_volume(&volume_name, &labels).await?,
        };
        validate_volume(&volume, &volume_name, &labels)?;
        let commands = resources::worker_bootstrap_commands(foundation.into(), database_count)?;
        context.volume_path = &volume.mountpoint;
        context.worker_bootstrap_commands = &commands;
        for desired in [
            resources::kubeadm_config_template(&context),
            resources::dev_machine_template(&context),
            resources::machine_deployment(&context),
        ] {
            if objects::ensure_management(
                self.client.clone(),
                &desired,
                tenant,
                context.identity(),
                &mut inventory,
            )
            .await?
            .created
            {
                return self.progress(tenant, PROGRESS_INTERVAL).await;
            }
        }
        for root in inventory.iter().filter(|object| {
            object.types.as_ref().is_some_and(|types| {
                matches!(
                    types.kind.as_str(),
                    "KubeadmConfigTemplate" | "DevMachineTemplate" | "MachineDeployment"
                )
            })
        }) {
            ownership::validate_provider_owner(root, &name, false, &inventory)?;
        }
        let network =
            resources::build_network(&context, &self.assets.calico, &network_images(foundation)?)?;
        // Network creation must not wait for Nodes that themselves require CNI.
        let network =
            objects::ensure_batch(tenant_client.clone(), &network.objects, context.identity())
                .await?;
        if network.created || network.pending {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        let deployment = inventory
            .iter()
            .find(|object| {
                object
                    .types
                    .as_ref()
                    .is_some_and(|types| types.kind == "MachineDeployment")
            })
            .ok_or_else(|| {
                ReconcileError::InvalidInput("MachineDeployment observation is missing".into())
            })?;
        let workers = workers::observe_workers(
            self.client.clone(),
            tenant_client.clone(),
            &self.docker,
            workers::WorkerInputs {
                identity: context.identity(),
                network_id: &foundation.network_id,
                desired_count: spec.workers,
                deployment,
                network_objects: &network.objects,
            },
        )
        .await?;
        if !workers.inventory_complete || !workers.all_ready || !workers.network_ready {
            return self.progress(tenant, DEPENDENCY_INTERVAL).await;
        }
        self.services(
            tenant_client,
            &context,
            foundation,
            &cluster.object,
            workers,
        )
        .await
    }
    #[rustfmt::skip]
    async fn finalize(&self, tenant: &Tenant, supported_version: &str) -> Result<Action, ReconcileError> { crate::finalize::Finalizer::with_docker(self.client.clone(), self.docker.clone(), supported_version, self.foundation.clone()).reconcile(tenant).await.map_err(Into::into) }
}
impl<D: DockerClient + Clone, A: TenantAccess> LocalProvider<D, A> {
    #[rustfmt::skip]
    async fn progress(&self, tenant: &Tenant, interval: std::time::Duration) -> Result<Action, ReconcileError> { progress(self.client.clone(), tenant, interval).await }
    async fn services(
        &self,
        client: Client,
        context: &ResourceContext<'_>,
        foundation: &Foundation,
        cluster: &DynamicObject,
        workers: workers::WorkerObservation,
    ) -> Result<Action, ReconcileError> {
        let storage = objects::ensure_static(
            client.clone(),
            &resources::to_dynamic(&resources::storage_class(context, STORAGE_CLASS))?,
            context.identity(),
        )
        .await?;
        if storage.created || storage.object.metadata.deletion_timestamp.is_some() {
            return self.progress(context.tenant, PROGRESS_INTERVAL).await;
        }
        let controller_image = image(foundation, "CNPG_CONTROLLER_IMAGE")?;
        let operator = resources::cnpg_operator(
            context,
            &self.assets.cnpg,
            &controller_image.tagged,
            &controller_image.reference,
        )?;
        let operator = objects::ensure_batch(client.clone(), &operator, context.identity()).await?;
        if operator.created || operator.pending {
            return self.progress(context.tenant, PROGRESS_INTERVAL).await;
        }
        let deployment = operator.objects.iter().find(|object| {
            object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "Deployment")
                && object.metadata.namespace.as_deref() == Some("cnpg-system")
                && object.name_any() == "cnpg-controller-manager"
        });
        if deployment.is_none_or(|deployment| !readiness::workload_available(deployment)) {
            return self.progress(context.tenant, DEPENDENCY_INTERVAL).await;
        }
        let mut database = resources::cnpg_objects(
            context,
            STORAGE_CLASS,
            &image(foundation, "POSTGRES_IMAGE")?.reference,
        )?;
        let desired = database
            .pop()
            .ok_or_else(|| ReconcileError::InvalidInput("CNPG Cluster is missing".into()))?;
        let static_objects =
            objects::ensure_batch(client.clone(), &database, context.identity()).await?;
        if static_objects.created || static_objects.pending {
            return self.progress(context.tenant, PROGRESS_INTERVAL).await;
        }
        let database = objects::ensure_dynamic(client, &desired, context.identity()).await?;
        if database.created {
            return self.progress(context.tenant, PROGRESS_INTERVAL).await;
        }
        let database_ready = readiness::database_ready(&database.object, context.database_count);
        if !database_ready {
            return self.progress(context.tenant, DEPENDENCY_INTERVAL).await;
        }
        let components = Components {
            control_plane: readiness::management_conditions_ready(cluster, &["Available"])?,
            workers: workers.inventory_complete && workers.all_ready,
            network: workers.network_ready,
            storage: storage
                .object
                .data
                .get("provisioner")
                .and_then(serde_json::Value::as_str)
                == Some("kubernetes.io/no-provisioner"),
            database: database_ready,
        };
        status::update_status(self.client.clone(), context.tenant, |status| {
            components.publish(status, context.tenant);
            Ok(())
        })
        .await?;
        Ok(Action::requeue(READY_INTERVAL))
    }
}
fn image<'a>(foundation: &'a Foundation, key: &str) -> Result<&'a ImageArchive, ReconcileError> {
    foundation
        .cache
        .image_archives
        .iter()
        .find(|image| image.key == key)
        .ok_or_else(|| ReconcileError::InvalidInput(format!("foundation image {key} is missing")))
}
fn network_images(foundation: &Foundation) -> Result<resources::NetworkImages, ReconcileError> {
    let cni = image(foundation, "CALICO_CNI_IMAGE")?;
    let node = image(foundation, "CALICO_NODE_IMAGE")?;
    let controllers = image(foundation, "CALICO_KUBE_CONTROLLERS_IMAGE")?;
    Ok(resources::NetworkImages {
        calico_cni: cni.reference.clone(),
        calico_cni_tagged: cni.tagged.clone(),
        calico_node: node.reference.clone(),
        calico_node_tagged: node.tagged.clone(),
        calico_controllers: controllers.reference.clone(),
        calico_controllers_tagged: controllers.tagged.clone(),
        kube_proxy: image(foundation, "KUBE_PROXY_IMAGE")?.reference.clone(),
    })
}
