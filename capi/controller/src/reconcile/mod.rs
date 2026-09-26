//! Resumable, direct-read reconciliation. Every durable identity write is a
//! return barrier; only CAPI roots and the CNPG Cluster receive bound SSA.

mod error;
pub mod objects;
pub mod workers;

pub use error::ReconcileError;

use std::{future::Future, path::Path, sync::Arc, time::Duration};

use futures::StreamExt;
use k8s_openapi::api::{
    coordination::v1::Lease,
    core::v1::{ConfigMap, Namespace, Secret},
};
use kube::{
    Api, Client, ResourceExt,
    api::{Patch, PatchParams, PostParams},
    core::DynamicObject,
    runtime::{
        controller::{Action, Controller},
        reflector::ObjectRef,
        watcher,
    },
};
use serde_json::json;

use crate::{
    allocation::{self, ClaimContext},
    api::{
        CanonicalSpec, SUPPORTED_KUBERNETES_VERSION, Tenant, TenantPhase, TenantStatus,
        canonical_spec, spec_hash,
    },
    docker::{BollardDockerClient, DockerClient, validate_volume},
    error::ControllerError,
    foundation::{self, Foundation, ImageArchive, VerifiedFoundation},
    ownership,
    readiness::{self, Components, set_condition},
    resources::{self, Context as ResourceContext},
    runtime::{LeadershipGate, tenant_controller},
    sanitize,
    status::plan_status_update,
    tenant_client::{self, TenantClientError},
};

pub const FINALIZER: &str = "tenancy.cnpg-vcluster.io/finalizer";
pub const FOUNDATION_NAMESPACE: &str = "tenant-system";
pub const FOUNDATION_NAME: &str = "tenant-foundation";
pub const PROGRESS_INTERVAL: Duration = Duration::from_secs(1);
pub const DEPENDENCY_INTERVAL: Duration = Duration::from_secs(5);
pub const READY_INTERVAL: Duration = Duration::from_secs(300);
pub const STORAGE_CLASS: &str = "capi-hostpath";

#[derive(Clone, Debug)]
pub struct Config {
    pub mutation_enabled: bool,
    pub supported_version: String,
    pub controller_image: String,
    pub foundation_namespace: String,
    pub foundation_name: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mutation_enabled: false,
            supported_version: SUPPORTED_KUBERNETES_VERSION.into(),
            controller_image: String::new(),
            foundation_namespace: FOUNDATION_NAMESPACE.into(),
            foundation_name: FOUNDATION_NAME.into(),
        }
    }
}

#[derive(Clone, Default)]
pub struct Assets {
    pub calico: Vec<u8>,
    pub cnpg: Vec<u8>,
}

impl Assets {
    pub fn load(directory: &Path) -> Result<Self, ControllerError> {
        Ok(Self {
            calico: std::fs::read(directory.join("calico.yaml")).map_err(|_| {
                ControllerError::Configuration("cannot read staged Calico asset".into())
            })?,
            cnpg: std::fs::read(directory.join("cnpg.yaml")).map_err(|_| {
                ControllerError::Configuration("cannot read staged CNPG asset".into())
            })?,
        })
    }
}

pub trait TenantAccess: Send + Sync {
    fn connect(
        &self,
        management: Client,
        control_plane: &DynamicObject,
        tenant_name: &str,
        endpoint: &str,
    ) -> impl Future<Output = Result<Client, TenantClientError>> + Send;
}

pub struct LiveTenantAccess;

impl TenantAccess for LiveTenantAccess {
    async fn connect(
        &self,
        management: Client,
        control_plane: &DynamicObject,
        tenant_name: &str,
        endpoint: &str,
    ) -> Result<Client, TenantClientError> {
        tenant_client::load_tenant_client(
            management,
            control_plane,
            tenant_name,
            tenant_name,
            endpoint,
        )
        .await
        .map(|(client, _)| client)
    }
}

pub trait DeletionHandler: Send + Sync {
    fn reconcile(
        &self,
        tenant: &Tenant,
        supported_version: &str,
    ) -> impl Future<Output = Result<Action, ReconcileError>> + Send;
}

pub struct LiveDeletion {
    pub client: Client,
    pub docker: BollardDockerClient,
}

impl DeletionHandler for LiveDeletion {
    async fn reconcile(
        &self,
        tenant: &Tenant,
        supported_version: &str,
    ) -> Result<Action, ReconcileError> {
        finalize(
            self.client.clone(),
            self.docker.clone(),
            tenant,
            supported_version,
        )
        .await
    }
}

async fn finalize(
    client: Client,
    docker: BollardDockerClient,
    tenant: &Tenant,
    supported_version: &str,
) -> Result<Action, ReconcileError> {
    crate::finalize::Finalizer::new(client, docker, supported_version)
        .reconcile(tenant)
        .await
        .map_err(Into::into)
}

pub struct Reconciler<D = BollardDockerClient, A = LiveTenantAccess, H = LiveDeletion> {
    pub client: Client,
    pub docker: D,
    pub access: A,
    pub deletion: H,
    pub config: Config,
    pub assets: Assets,
}

impl Reconciler {
    pub fn new(
        client: Client,
        docker: BollardDockerClient,
        config: Config,
        assets: Assets,
    ) -> Self {
        Self {
            deletion: LiveDeletion {
                client: client.clone(),
                docker: docker.clone(),
            },
            client,
            docker,
            access: LiveTenantAccess,
            config,
            assets,
        }
    }
}

impl<D: DockerClient, A: TenantAccess, H: DeletionHandler> Reconciler<D, A, H> {
    pub async fn reconcile_name(&self, name: &str) -> Result<Action, ReconcileError> {
        let Some(tenant) = Api::<Tenant>::all(self.client.clone())
            .get_opt(name)
            .await?
        else {
            return Ok(Action::await_change());
        };
        let spec = match canonical_spec(name, &tenant.spec, &self.config.supported_version) {
            Ok(spec) => spec,
            Err(error) => {
                update_status(self.client.clone(), &tenant, |status| {
                    status.phase = Some(TenantPhase::Failed);
                    set_condition(
                        status,
                        &tenant,
                        "Accepted",
                        false,
                        "InvalidSpec",
                        &error.to_string(),
                    );
                    set_condition(
                        status,
                        &tenant,
                        "Ready",
                        false,
                        "InvalidSpec",
                        "Tenant specification is invalid",
                    );
                    Ok(())
                })
                .await?;
                return Ok(Action::await_change());
            }
        };
        if tenant.metadata.deletion_timestamp.is_some() {
            if !tenant.finalizers().iter().any(|value| value == FINALIZER) {
                return Ok(Action::await_change());
            }
            return match self
                .deletion
                .reconcile(&tenant, &self.config.supported_version)
                .await
            {
                Ok(action) => Ok(action),
                Err(error) => self.failure(&tenant, error).await,
            };
        }
        if !self.config.mutation_enabled {
            update_status(self.client.clone(), &tenant, |status| {
                status.phase = Some(TenantPhase::Progressing);
                set_condition(
                    status,
                    &tenant,
                    "Accepted",
                    true,
                    "Accepted",
                    "Tenant specification is accepted",
                );
                set_condition(
                    status,
                    &tenant,
                    "Ready",
                    false,
                    "MutationDisabled",
                    "Tenant controller mutation is disabled until clean cutover",
                );
                Ok(())
            })
            .await?;
            return Ok(Action::await_change());
        }
        match self.create(&tenant, &spec).await {
            Ok(action) => Ok(action),
            Err(error) if error.pending() => self.progress(&tenant, DEPENDENCY_INTERVAL).await,
            Err(error) => self.failure(&tenant, error).await,
        }
    }

    async fn failure(
        &self,
        tenant: &Tenant,
        error: ReconcileError,
    ) -> Result<Action, ReconcileError> {
        let deleting = tenant.metadata.deletion_timestamp.is_some();
        let (phase, reason) = if error.ownership_invalid() {
            (TenantPhase::OwnershipInvalid, "OwnershipInvalid")
        } else if let Some(reason) = error.degraded_reason() {
            (TenantPhase::Degraded, reason)
        } else if matches!(
            &error,
            ReconcileError::Foundation(foundation::FoundationError::Identity)
        ) {
            (
                if deleting {
                    TenantPhase::Deleting
                } else {
                    TenantPhase::Failed
                },
                "FoundationMismatch",
            )
        } else if matches!(
            &error,
            ReconcileError::Foundation(_) | ReconcileError::FoundationRead(_)
        ) {
            (
                if deleting {
                    TenantPhase::Deleting
                } else {
                    TenantPhase::Failed
                },
                "FoundationInvalid",
            )
        } else if matches!(&error, ReconcileError::FoundationMutationDisabled) {
            (TenantPhase::Failed, "FoundationMutationDisabled")
        } else {
            (
                if deleting {
                    TenantPhase::Deleting
                } else {
                    TenantPhase::Failed
                },
                if deleting {
                    "DeletionBlocked"
                } else {
                    "ReconcileFailed"
                },
            )
        };
        if matches!(&error, ReconcileError::Kube(kube::Error::Api(status)) if status.code == 409) {
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        update_status(self.client.clone(), tenant, |status| {
            status.phase = Some(phase);
            set_condition(status, tenant, "Ready", false, reason, &error.to_string());
            if phase == TenantPhase::OwnershipInvalid {
                set_condition(
                    status,
                    tenant,
                    "OwnershipValid",
                    false,
                    reason,
                    &error.to_string(),
                );
            }
            if matches!(reason, "FoundationMismatch" | "FoundationInvalid") {
                set_condition(
                    status,
                    tenant,
                    "FoundationReady",
                    false,
                    reason,
                    &error.to_string(),
                );
            }
            Ok(())
        })
        .await?;
        tracing::warn!(tenant = %tenant.name_any(), error = %sanitize::text(&error.to_string()), "Tenant reconciliation blocked");
        Ok(error.action())
    }

    async fn progress(
        &self,
        tenant: &Tenant,
        interval: Duration,
    ) -> Result<Action, ReconcileError> {
        update_status(self.client.clone(), tenant, |status| {
            readiness::progress_status(status, tenant);
            Ok(())
        })
        .await?;
        Ok(Action::requeue(interval))
    }

    pub async fn load_foundation(
        &self,
        tenant: &Tenant,
    ) -> Result<VerifiedFoundation<Foundation>, ReconcileError> {
        let config_map =
            Api::<ConfigMap>::namespaced(self.client.clone(), &self.config.foundation_namespace)
                .get(&self.config.foundation_name)
                .await
                .map_err(ReconcileError::FoundationRead)?;
        let data = config_map.data.as_ref().ok_or_else(|| {
            foundation::FoundationError::Invalid("ConfigMap data is missing".into())
        })?;
        Ok(foundation::parse_for_creation(
            data.get("foundation.json")
                .map(String::as_str)
                .unwrap_or(""),
            data.get("foundation.sha256")
                .map(String::as_str)
                .unwrap_or(""),
            tenant
                .status
                .as_ref()
                .and_then(|status| status.foundation_hash.as_deref()),
            &self.config.supported_version,
            &self.config.controller_image,
        )?)
    }

    async fn create(
        &self,
        tenant: &Tenant,
        spec: &CanonicalSpec,
    ) -> Result<Action, ReconcileError> {
        let verified = self.load_foundation(tenant).await?;
        let foundation = &verified.value;
        if !foundation.mutation_enabled {
            return Err(ReconcileError::FoundationMutationDisabled);
        }
        if tenant.uid().is_none_or(|uid| uid.is_empty())
            || tenant
                .resource_version()
                .is_none_or(|version| version.is_empty())
        {
            return Err(ReconcileError::OwnershipInvalid(
                "Tenant UID/resourceVersion is missing".into(),
            ));
        }
        if !tenant.finalizers().iter().any(|value| value == FINALIZER) {
            let mut updated = tenant.clone();
            updated
                .metadata
                .finalizers
                .get_or_insert_default()
                .push(FINALIZER.into());
            Api::<Tenant>::all(self.client.clone())
                .replace(&tenant.name_any(), &PostParams::default(), &updated)
                .await?;
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        if tenant
            .status
            .as_ref()
            .and_then(|status| status.foundation_hash.as_deref())
            .is_none_or(str::is_empty)
        {
            update_status(self.client.clone(), tenant, |status| {
                if status
                    .foundation_hash
                    .as_deref()
                    .is_some_and(|hash| !hash.is_empty() && hash != verified.hash)
                {
                    return Err(ControllerError::OwnershipInvalid(
                        "foundation binding changed".into(),
                    ));
                }
                status.foundation_hash = Some(verified.hash.clone());
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
                namespace: &self.config.foundation_namespace,
                ownership_label: &foundation.inputs.ownership_label,
                lab_prefix: &foundation.inputs.lab_prefix,
                tenant_name: &name,
                tenant_uid: &uid,
                spec_hash: &hash,
                foundation_hash: &verified.hash,
                slots: &foundation.slots,
            },
            tenant
                .status
                .as_ref()
                .and_then(|status| status.allocation.as_ref()),
        )
        .await?;
        if tenant
            .status
            .as_ref()
            .and_then(|status| status.allocation.as_ref())
            .is_none()
        {
            update_status(self.client.clone(), tenant, |status| {
                let allocation = claim.status();
                if status
                    .allocation
                    .as_ref()
                    .is_some_and(|bound| bound != &allocation)
                {
                    return Err(ControllerError::OwnershipInvalid(
                        "allocation binding changed".into(),
                    ));
                }
                status.allocation = Some(allocation);
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
            foundation_hash: &verified.hash,
            endpoint: &endpoint,
            pod_cidr: &claim.slot.pod_cidr,
            service_cidr: &claim.slot.service_cidr,
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
        if tenant
            .status
            .as_ref()
            .and_then(|status| status.cluster_uid.as_deref())
            .is_none_or(str::is_empty)
        {
            let cluster_uid = cluster
                .object
                .uid()
                .ok_or_else(|| ReconcileError::OwnershipInvalid("Cluster UID is missing".into()))?;
            update_status(self.client.clone(), tenant, |status| {
                if status
                    .cluster_uid
                    .as_deref()
                    .is_some_and(|uid| !uid.is_empty() && uid != cluster_uid)
                {
                    return Err(ControllerError::OwnershipInvalid(
                        "Cluster UID binding changed".into(),
                    ));
                }
                status.cluster_uid = Some(cluster_uid.clone());
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
            .connect(self.client.clone(), &control_plane.object, &name, &endpoint)
            .await?;
        tenant_client::ensure_bootstrap_rbac(tenant_client.clone()).await?;
        let volume_name = resources::storage_volume_name(&context);
        let labels = resources::storage_volume_labels(&context);
        let volume = match self.docker.inspect_volume(&volume_name).await? {
            Some(volume) => volume,
            None => self.docker.create_volume(&volume_name, &labels).await?,
        };
        validate_volume(&volume, &volume_name, &labels)?;
        let commands = resources::worker_bootstrap_commands(foundation.into(), spec.databases)?;
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
        let database_ready = readiness::database_ready(&database.object, context.spec.databases);
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
        update_status(self.client.clone(), context.tenant, |status| {
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

/// Retry against a fresh direct read only after a conflict. Never transfer
/// bindings or observations to a same-name replacement or changed generation.
pub async fn update_status<F>(
    client: Client,
    tenant: &Tenant,
    mutate: F,
) -> Result<(), ReconcileError>
where
    F: Fn(&mut TenantStatus) -> Result<(), ControllerError>,
{
    let api = Api::<Tenant>::all(client);
    let mut current = tenant.clone();
    for attempt in 0..=4 {
        if current.uid() != tenant.uid()
            || current.metadata.generation != tenant.metadata.generation
            || current.spec != tenant.spec
            || current.metadata.deletion_timestamp != tenant.metadata.deletion_timestamp
        {
            return Err(ReconcileError::OwnershipInvalid(
                "Tenant identity or lifecycle changed during status update".into(),
            ));
        }
        let plan = plan_status_update(&current, &mutate)?;
        let Some(mut patch) = plan.merge_patch() else {
            return Ok(());
        };
        patch["metadata"]["uid"] = json!(tenant.uid());
        match api
            .patch_status(
                &tenant.name_any(),
                &PatchParams::default(),
                &Patch::Merge(&patch),
            )
            .await
        {
            Ok(updated) => {
                if updated.uid() != tenant.uid() {
                    return Err(ReconcileError::OwnershipInvalid(
                        "Tenant changed during status update".into(),
                    ));
                }
                return Ok(());
            }
            Err(kube::Error::Api(status)) if status.code == 409 && attempt < 4 => {
                current = api.get(&tenant.name_any()).await?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    unreachable!("bounded status retries return")
}

pub fn controller(client: Client, config: &Config) -> Controller<Tenant> {
    let controller = tenant_controller(client.clone());
    let store = controller.store();
    let foundation_namespace = config.foundation_namespace.clone();
    let foundation_name = config.foundation_name.clone();
    let mut controller = controller
        .watches(
            Api::<ConfigMap>::all(client.clone()),
            watcher::Config::default(),
            move |object| {
                if object.namespace().as_deref() == Some(&foundation_namespace)
                    && object.name_any() == foundation_name
                {
                    store
                        .state()
                        .iter()
                        .map(|tenant| ObjectRef::new(&tenant.name_any()))
                        .collect()
                } else {
                    crate::runtime::map_dependent_to_tenant(&object)
                }
            },
        )
        .watches(
            Api::<Namespace>::all(client.clone()),
            watcher::Config::default(),
            |object| crate::runtime::map_dependent_to_tenant(&object),
        )
        .watches(
            Api::<Lease>::all(client.clone()),
            watcher::Config::default(),
            |object| crate::runtime::map_dependent_to_tenant(&object),
        )
        .watches(
            Api::<Secret>::all(client.clone()),
            watcher::Config::default(),
            |object| {
                let mapped = crate::runtime::map_dependent_to_tenant(&object);
                if !mapped.is_empty() {
                    return mapped;
                }
                object
                    .namespace()
                    .filter(|namespace| object.name_any() == format!("{namespace}-kubeconfig"))
                    .map(|namespace| vec![ObjectRef::new(&namespace)])
                    .unwrap_or_default()
            },
        );
    for (version, kind) in [
        ("cluster.x-k8s.io/v1beta2", "Cluster"),
        ("cluster.x-k8s.io/v1beta2", "MachineDeployment"),
        ("cluster.x-k8s.io/v1beta2", "MachineSet"),
        ("cluster.x-k8s.io/v1beta2", "Machine"),
        ("infrastructure.cluster.x-k8s.io/v1beta2", "DevCluster"),
        (
            "infrastructure.cluster.x-k8s.io/v1beta2",
            "DevMachineTemplate",
        ),
        ("infrastructure.cluster.x-k8s.io/v1beta2", "DevMachine"),
        (
            "bootstrap.cluster.x-k8s.io/v1beta2",
            "KubeadmConfigTemplate",
        ),
        (
            "controlplane.cluster.x-k8s.io/v1alpha2",
            "KamajiControlPlane",
        ),
    ] {
        let resource = objects::resource(version, kind);
        controller = controller.watches_with(
            Api::<DynamicObject>::all_with(client.clone(), &resource),
            resource,
            watcher::Config::default(),
            |object| {
                let mapped = crate::runtime::map_dependent_to_tenant(&object);
                if !mapped.is_empty() {
                    return mapped;
                }
                object
                    .labels()
                    .get("cluster.x-k8s.io/cluster-name")
                    .filter(|name| !name.is_empty())
                    .map(|name| vec![ObjectRef::new(name)])
                    .unwrap_or_default()
            },
        );
    }
    controller
}

pub struct ControllerContext {
    pub reconciler: Reconciler,
    pub gate: LeadershipGate,
}

pub async fn run_controller(
    reconciler: Reconciler,
    gate: LeadershipGate,
    stop: impl Future<Output = ()> + Send + Sync + 'static,
) -> Result<(), ControllerError> {
    let controller =
        controller(reconciler.client.clone(), &reconciler.config).graceful_shutdown_on(stop);
    let context = Arc::new(ControllerContext { reconciler, gate });
    controller.run(
        |tenant, context: Arc<ControllerContext>| async move {
            let Some(_permit) = context.gate.try_enter() else { return Ok(Action::await_change()); };
            context.reconciler.reconcile_name(&tenant.name_any()).await
        },
        |tenant, error: &ReconcileError, _context| {
            tracing::warn!(tenant = %tenant.name_any(), error = %sanitize::text(&error.to_string()), "reconciliation failed");
            error.action()
        },
        context,
    ).for_each(|result| async move {
        if let Err(error) = result {
            tracing::warn!(error = %sanitize::text(&error.to_string()), "controller stream error");
        }
    }).await;
    Ok(())
}
