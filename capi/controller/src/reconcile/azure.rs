use std::collections::{BTreeMap, BTreeSet};

use k8s_openapi::api::core::v1::{ConfigMap, Secret};
use kube::{
    Api, Client, ResourceExt,
    api::{ListParams, Patch, PatchParams},
    core::DynamicObject,
    runtime::controller::Action,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{
    allocation::AllocationError,
    api::{
        AzureBindingStatus, AzureKubeconfigStatus, AzureManagementStatus, AzureNodeIdentity,
        AzureProviderResourceIdentity, AzureProviderStatus, AzureVmssStatus, CanonicalSpec, Tenant,
        TenantPhase, TenantProviderSpec, spec_hash,
    },
    azure::{
        self, ANNOTATION_FOUNDATION, ANNOTATION_OPERATION, ANNOTATION_PROFILE, ANNOTATION_SPEC,
        AzureConfiguration, AzureContext, AzureOwnershipError, TENANT_ANNOTATION, desired_objects,
    },
    azure_allocation::{self, AzureAllocationCatalog, AzureClaimIdentity},
    error::ControllerError,
    management::{self, ResourceClass},
    readiness::{self, set_condition},
    status,
};

use super::{
    DEPENDENCY_INTERVAL, FINALIZER, FOUNDATION_NAMESPACE, PROGRESS_INTERVAL, ProviderLifecycle,
    READY_INTERVAL, ReconcileError, TenantAccess, local::LiveTenantAccess, objects,
};

pub use crate::azure::CONFIG_NAME;

#[derive(Clone)]
pub struct AzureProvider<A = LiveTenantAccess> {
    pub client: Client,
    pub configuration: AzureConfiguration,
    pub allocation: Option<AzureAllocationCatalog>,
    pub access: A,
}
#[rustfmt::skip]
enum AllocationStep { Ready(crate::api::AzureAllocationStatus), Stored, Exhausted }

impl AzureProvider {
    pub fn from_config_maps(
        client: Client,
        config: &ConfigMap,
        allocation: Option<&ConfigMap>,
    ) -> Result<Self, ControllerError> {
        Ok(Self {
            client,
            configuration: AzureConfiguration::from_config_map(config)
                .map_err(|error| ControllerError::Configuration(error.to_string()))?,
            allocation: allocation
                .and_then(|config| AzureAllocationCatalog::from_config_map(config).ok()),
            access: LiveTenantAccess,
        })
    }
}

impl<A: TenantAccess> ProviderLifecycle for AzureProvider<A> {
    fn supports(&self, provider: &TenantProviderSpec) -> bool {
        matches!(provider, TenantProviderSpec::Azure)
    }

    async fn validate_mutation(&self) -> Result<(), ReconcileError> {
        require_current_configuration(self.client.clone(), &self.configuration).await
    }

    async fn reconcile(
        &self,
        tenant: &Tenant,
        spec: &CanonicalSpec,
    ) -> Result<Action, ReconcileError> {
        self.validate_mutation().await?;
        let name = tenant.name_any();
        let tenant_uid = tenant
            .uid()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ownership("Tenant UID is missing"))?;
        let specification_sha256 = spec_hash(spec);
        let operation_id = operation_id(&tenant_uid, &specification_sha256);
        let binding = self
            .configuration
            .binding(&tenant_uid, &specification_sha256, &operation_id);
        let current = tenant.status.as_ref().and_then(|status| status.azure());
        if let Some(actual) = current.and_then(|status| status.binding.as_ref()) {
            azure::validate_binding(actual, &binding).map_err(azure_ownership)?;
        } else {
            if current.is_some_and(has_durable_state) {
                return Err(ownership("Azure durable status exists without its binding"));
            }
            self.update(tenant, |status| {
                status.binding = Some(binding.clone());
                Ok(())
            })
            .await?;
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        self.validate_mutation().await?;
        if status::set_finalizer(self.client.clone(), tenant, tenant, FINALIZER, true).await? {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        let identity = AzureClaimIdentity {
            tenant_name: &name,
            tenant_uid: &tenant_uid,
            spec_hash: &specification_sha256,
        };
        let allocation = match self
            .ensure_allocation(
                tenant,
                &binding,
                identity,
                current.and_then(|status| status.network_allocation.as_ref()),
            )
            .await?
        {
            AllocationStep::Ready(allocation) => allocation,
            AllocationStep::Stored => return self.progress(tenant, PROGRESS_INTERVAL).await,
            AllocationStep::Exhausted => {
                return self
                    .waiting(
                        tenant,
                        "AzureAllocationReady",
                        "Azure network allocation slots are exhausted",
                    )
                    .await;
            }
        };
        let mutations_allowed = self.require_current_allocation().await.is_ok();

        let context = AzureContext {
            tenant,
            spec,
            specification_sha256: &specification_sha256,
            foundation_sha256: &self.configuration.values.foundation_sha256,
            operation_id: &operation_id,
            configuration: &self.configuration.values,
            allocation: &allocation,
        };
        let desired = desired_objects(&context)
            .map_err(|error| ReconcileError::InvalidInput(error.to_string()))?;
        let mut live = Vec::new();
        for object in &desired[..8] {
            if self
                .write_barrier(tenant, &binding, object, &mut live, mutations_allowed)
                .await?
            {
                return self.progress(tenant, PROGRESS_INTERVAL).await;
            }
        }

        let cluster = find(&live, "Cluster")?.clone();
        let azure_cluster = find(&live, "AzureCluster")?.clone();
        let control_plane = find(&live, "KamajiControlPlane")?.clone();
        if !readiness::object_ready(&azure_cluster) {
            return self
                .waiting(
                    tenant,
                    "AzureControlPlaneReady",
                    "AzureCluster is not Ready",
                )
                .await;
        }
        let endpoint = control_plane_endpoint(&control_plane)?;
        if endpoint.is_none()
            || control_plane
                .data
                .pointer("/status/ready")
                .and_then(Value::as_bool)
                != Some(true)
        {
            return self
                .waiting(
                    tenant,
                    "AzureControlPlaneReady",
                    "Kamaji control plane endpoint is not Ready",
                )
                .await;
        }
        let endpoint = endpoint.expect("checked above");
        if cluster
            .data
            .pointer("/status/controlPlaneReady")
            .and_then(Value::as_bool)
            != Some(true)
            && !readiness::management_conditions_ready(
                &cluster,
                &["ControlPlaneReady", "ControlPlaneAvailable"],
            )?
        {
            return self
                .waiting(
                    tenant,
                    "AzureControlPlaneReady",
                    "CAPI Cluster control plane is not available",
                )
                .await;
        }
        self.validate_mutation().await?;
        if patch_cluster_bridge(
            self.client.clone(),
            &cluster,
            &azure_cluster,
            &endpoint,
            mutations_allowed,
        )
        .await?
        {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        self.record_endpoint(tenant, &binding, &endpoint).await?;
        if tenant
            .status
            .as_ref()
            .and_then(|status| status.azure())
            .and_then(|status| status.endpoint.as_deref())
            .is_none()
        {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        if self
            .record_kubeconfig(tenant, &binding, &cluster, &control_plane)
            .await?
        {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }

        for object in &desired[8..11] {
            if self
                .write_barrier(tenant, &binding, object, &mut live, mutations_allowed)
                .await?
            {
                return self.progress(tenant, PROGRESS_INTERVAL).await;
            }
        }
        let probe = find(&live, "Deployment")?;
        if !readiness::workload_available(probe) {
            return self
                .waiting(
                    tenant,
                    "AzureStatusProbeReady",
                    "status-probe Deployment is not available",
                )
                .await;
        }
        if self
            .write_barrier(tenant, &binding, &desired[11], &mut live, mutations_allowed)
            .await?
        {
            return self.progress(tenant, PROGRESS_INTERVAL).await;
        }
        let job = find(&live, "Job")?;
        if job_failed(job) {
            return Err(ReconcileError::Degraded {
                reason: "AzureAddonFailed",
                message: "Azure add-on Job failed".into(),
            });
        }
        if !job_complete(job) {
            return self
                .waiting(tenant, "AzureAddonsReady", "Azure add-on Job is incomplete")
                .await;
        }

        let tenant_client = self
            .access
            .connect(
                self.client.clone(),
                &control_plane,
                Some(&cluster),
                &name,
                &endpoint,
            )
            .await?;
        let workers = observe_workers(
            tenant_client.clone(),
            spec,
            find(&live, "MachinePool")?,
            find(&live, "AzureMachinePool")?,
            &self.configuration,
        )
        .await?;
        let Some(workers) = workers else {
            return self
                .waiting(
                    tenant,
                    "AzureWorkersReady",
                    "Azure worker identities are not Ready",
                )
                .await;
        };
        let components = observe_components(tenant_client).await?;
        let Some(components) = components else {
            return self
                .waiting(
                    tenant,
                    "AzureAddonsReady",
                    "Azure cloud-provider or Calico workloads are not Ready",
                )
                .await;
        };
        let provider_resources = observe_provider_resources(
            self.client.clone(),
            &binding,
            current
                .and_then(|status| status.management.as_ref())
                .ok_or_else(|| ownership("Azure management identity is incomplete"))?,
            &live,
            &name,
        )
        .await?;
        for kind in ["Machine", "AzureMachinePoolMachine"] {
            if provider_resources
                .iter()
                .filter(|resource| resource.kind == kind)
                .count()
                != spec.workers as usize
            {
                return self
                    .waiting(
                        tenant,
                        "AzureProviderResourcesReady",
                        "CAPZ worker resource inventory is incomplete",
                    )
                    .await;
            }
        }
        validate_durable_observations(
            current,
            &workers.vmss,
            &workers.nodes,
            &components,
            &provider_resources,
        )?;
        self.validate_mutation().await?;
        status::update_status(self.client.clone(), tenant, |status| {
            let azure = status.azure_mut()?;
            validate_status_binding(azure, &binding)?;
            azure.vmss = Some(workers.vmss.clone());
            azure.nodes = workers.nodes.clone();
            azure.addon_components = components.clone();
            azure.provider_resources = provider_resources.clone();
            readiness::initialize_status(status, tenant);
            for condition in [
                "AzureControlPlaneReady",
                "AzureKubeconfigReady",
                "AzureWorkersReady",
                "AzureStatusProbeReady",
                "AzureAddonsReady",
                "AzureProviderResourcesReady",
            ] {
                set_condition(
                    status,
                    tenant,
                    condition,
                    true,
                    condition,
                    "Azure contract is Ready",
                );
            }
            status.phase = Some(TenantPhase::Ready);
            set_condition(
                status,
                tenant,
                "Ready",
                true,
                "Ready",
                "Complete Azure tenant contract is Ready",
            );
            Ok(())
        })
        .await?;
        Ok(Action::requeue(READY_INTERVAL))
    }

    async fn finalize(
        &self,
        tenant: &Tenant,
        supported_version: &str,
    ) -> Result<Action, ReconcileError> {
        self.validate_mutation().await?;
        super::azure_finalize::finalize(
            self.client.clone(),
            &self.configuration,
            tenant,
            supported_version,
        )
        .await
    }
}

impl<A: TenantAccess> AzureProvider<A> {
    #[rustfmt::skip]
    async fn ensure_allocation(&self, tenant: &Tenant, binding: &AzureBindingStatus,
        identity: AzureClaimIdentity<'_>, recorded: Option<&crate::api::AzureAllocationStatus>)
        -> Result<AllocationStep, ReconcileError> {
        if let Some(recorded) = recorded {
            azure_allocation::validate_recorded(self.client.clone(), identity, recorded).await.map_err(allocation_error)?;
            return Ok(AllocationStep::Ready(recorded.clone()));
        }
        self.require_current_allocation().await?;
        let allocation = match azure_allocation::recover(self.client.clone(), identity).await.map_err(allocation_error)? {
            Some(allocation) => allocation,
            None => match azure_allocation::claim(self.client.clone(),
                self.allocation.as_ref().ok_or_else(|| catalog_block("new allocations"))?, identity).await {
                Ok(allocation) => allocation, Err(AllocationError::Exhausted) => return Ok(AllocationStep::Exhausted),
                Err(error) => return Err(allocation_error(error)),
            },
        };
        self.update(tenant, |status| {
            validate_status_binding(status, binding)?;
            if status.network_allocation.as_ref().is_some_and(|value| value != &allocation) {
                return Err(ControllerError::OwnershipInvalid("Azure network allocation identity changed".into()));
            }
            status.network_allocation = Some(allocation.clone()); Ok(())
        }).await?;
        Ok(AllocationStep::Stored)
    }

    #[rustfmt::skip]
    async fn progress(&self, tenant: &Tenant, interval: std::time::Duration) -> Result<Action, ReconcileError> {
        self.validate_mutation().await?;
        status::update_status(self.client.clone(), tenant, |status| {
            readiness::progress_status(status, tenant); Ok(())
        }).await?;
        Ok(Action::requeue(interval))
    }

    #[rustfmt::skip]
    async fn update(&self, tenant: &Tenant,
        mutate: impl Fn(&mut AzureProviderStatus) -> Result<(), ControllerError>) -> Result<(), ReconcileError> {
        self.validate_mutation().await?;
        status::update_status(self.client.clone(), tenant, |status| {
            mutate(status.azure_mut()?)?; readiness::progress_status(status, tenant); Ok(())
        }).await?;
        Ok(())
    }

    async fn write_barrier(
        &self,
        tenant: &Tenant,
        binding: &AzureBindingStatus,
        desired: &DynamicObject,
        live: &mut Vec<DynamicObject>,
        mutations_allowed: bool,
    ) -> Result<bool, ReconcileError> {
        let name = tenant.name_any();
        let kind = desired
            .types
            .as_ref()
            .map(|types| types.kind.as_str())
            .ok_or_else(|| ReconcileError::InvalidInput("desired Azure kind is missing".into()))?;
        let object_name = desired.name_any();
        let management = tenant
            .status
            .as_ref()
            .and_then(|status| status.azure())
            .and_then(|status| status.management.as_ref())
            .cloned()
            .unwrap_or_default();
        let recorded = management.uid_for(kind, &object_name, &name);
        self.validate_mutation().await?;
        if !mutations_allowed
            && objects::object_api(self.client.clone(), desired)?
                .get_opt(&object_name)
                .await?
                .is_none()
        {
            return Err(catalog_block("provider writes"));
        }
        let mut ensured =
            objects::read_or_create(self.client.clone(), desired, None, recorded.is_some()).await?;
        if !ensured.created {
            azure::validate_live_identity(desired, &ensured.object, recorded)
                .map_err(azure_ownership)?;
            validate_parent(&ensured.object, &management, &name)?;
            if let Err(error) = azure::validate_desired_object(desired, &ensured.object) {
                if !matches!(error, AzureOwnershipError::Desired(_)) {
                    return Err(azure_ownership(error));
                }
                if !mutations_allowed {
                    return Err(catalog_block("provider repair"));
                }
                self.validate_mutation().await?;
                ensured.object =
                    objects::apply_exact(self.client.clone(), desired, &ensured.object).await?;
                azure::validate_live_object(desired, &ensured.object, recorded)
                    .map_err(azure_ownership)?;
                validate_parent(&ensured.object, &management, &name)?;
            }
        }
        let uid = ensured
            .object
            .uid()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ownership("Azure management object UID is missing"))?;
        if recorded.is_none() {
            if !mutations_allowed {
                return Err(catalog_block("provider identity writes"));
            }
            let kind = kind.to_owned();
            let object_name = object_name.clone();
            self.update(tenant, |status| {
                validate_status_binding(status, binding)?;
                let management = status.management.get_or_insert_default();
                record_uid(management, &kind, &object_name, &name, &uid)
            })
            .await?;
            return Ok(true);
        }

        live.retain(|object| {
            object.types != ensured.object.types
                || object.metadata.name != ensured.object.metadata.name
        });
        live.push(ensured.object);
        Ok(false)
    }

    #[rustfmt::skip]
    async fn require_current_allocation(&self) -> Result<(), ReconcileError> {
        let live = Api::<ConfigMap>::namespaced(self.client.clone(), FOUNDATION_NAMESPACE)
            .get(azure_allocation::CONFIG_NAME).await.map_err(|error| ReconcileError::MutationGuard(
                format!("cannot read {FOUNDATION_NAMESPACE}/{}: {error}", azure_allocation::CONFIG_NAME)))?;
        let actual = AzureAllocationCatalog::from_config_map(&live).map_err(|error| {
            ReconcileError::MutationGuard(format!("{FOUNDATION_NAMESPACE}/{} is invalid: {error}", azure_allocation::CONFIG_NAME))
        })?;
        if self.allocation.as_ref() != Some(&actual) {
            return Err(ReconcileError::MutationGuard(format!("{FOUNDATION_NAMESPACE}/{} differs from the startup configuration", azure_allocation::CONFIG_NAME)));
        }
        Ok(())
    }

    #[rustfmt::skip]
    async fn record_endpoint(&self, tenant: &Tenant, binding: &AzureBindingStatus,
        endpoint: &str) -> Result<(), ReconcileError> {
        let recorded = tenant.status.as_ref().and_then(|status| status.azure())
            .and_then(|status| status.endpoint.as_deref());
        if recorded.is_some_and(|value| value != endpoint) {
            return Err(ownership("Azure endpoint identity changed"));
        }
        if recorded.is_none() {
            self.update(tenant, |status| {
                validate_status_binding(status, binding)?;
                if status.endpoint.as_deref().is_some_and(|value| value != endpoint) {
                    return Err(ControllerError::OwnershipInvalid("Azure endpoint identity changed".into()));
                }
                status.endpoint = Some(endpoint.into()); Ok(())
            }).await?;
        }
        Ok(())
    }

    #[rustfmt::skip]
    async fn record_kubeconfig(&self, tenant: &Tenant, binding: &AzureBindingStatus,
        cluster: &DynamicObject, control_plane: &DynamicObject) -> Result<bool, ReconcileError> {
        let name = tenant.name_any();
        let secret = Api::<Secret>::namespaced(self.client.clone(), &name)
            .get_opt(&format!("{name}-kubeconfig")).await?
            .ok_or_else(|| ReconcileError::Pending("tenant kubeconfig Secret is absent".into()))?;
        let uid = secret.uid().filter(|value| !value.is_empty())
            .ok_or_else(|| ownership("tenant kubeconfig Secret UID is missing"))?;
        let owners: Vec<_> = secret.owner_references().iter().filter(|owner| owner.controller == Some(true)).collect();
        if owners.len() != 1
            || ![cluster, control_plane].iter().any(|object| {
                owners[0].uid == object.uid().unwrap_or_default()
                    && owners[0].name == object.name_any()
                    && object.types.as_ref().is_some_and(|types| owners[0].api_version == types.api_version && owners[0].kind == types.kind)
            })
            || secret.type_.as_deref() != Some("cluster.x-k8s.io/secret")
        {
            return Err(ownership("tenant kubeconfig Secret ownership is invalid"));
        }
        let content = secret.data.as_ref().and_then(|data| data.get("value"))
            .filter(|value| !value.0.is_empty())
            .ok_or_else(|| ownership("tenant kubeconfig Secret content is incomplete"))?;
        let observed = AzureKubeconfigStatus { secret_uid: uid, content_sha256: hex::encode(Sha256::digest(&content.0)) };
        let recorded = tenant.status.as_ref().and_then(|status| status.azure())
            .and_then(|status| status.kubeconfig.as_ref());
        if recorded.is_some_and(|value| value != &observed) {
            return Err(ownership("tenant kubeconfig Secret identity changed"));
        }
        if recorded.is_none() {
            self.update(tenant, |status| {
                validate_status_binding(status, binding)?;
                if status.kubeconfig.as_ref().is_some_and(|value| value != &observed) {
                    return Err(ControllerError::OwnershipInvalid("tenant kubeconfig Secret identity changed".into()));
                }
                status.kubeconfig = Some(observed.clone()); Ok(())
            }).await?;
            return Ok(true);
        }
        Ok(false)
    }

    #[rustfmt::skip]
    async fn waiting(&self, tenant: &Tenant, condition: &str, message: &str) -> Result<Action, ReconcileError> {
        self.validate_mutation().await?;
        status::update_status(self.client.clone(), tenant, |status| {
            readiness::progress_status(status, tenant);
            set_condition(status, tenant, condition, false, "NotReady", message); Ok(())
        }).await?;
        Ok(Action::requeue(DEPENDENCY_INTERVAL))
    }
}

#[rustfmt::skip]
pub(super) async fn require_current_configuration(client: Client, expected: &AzureConfiguration) -> Result<(), ReconcileError> {
    let live = Api::<ConfigMap>::namespaced(client, FOUNDATION_NAMESPACE)
        .get(CONFIG_NAME).await.map_err(|error| ReconcileError::MutationGuard(
            format!("cannot read {FOUNDATION_NAMESPACE}/{CONFIG_NAME}: {error}")))?;
    let actual = AzureConfiguration::from_config_map(&live).map_err(|error| {
        ReconcileError::MutationGuard(format!("{FOUNDATION_NAMESPACE}/{CONFIG_NAME} is invalid: {error}"))
    })?;
    if &actual != expected {
        return Err(ReconcileError::MutationGuard(format!(
            "{FOUNDATION_NAMESPACE}/{CONFIG_NAME} differs from the startup configuration"
        )));
    }
    Ok(())
}

fn has_durable_state(status: &AzureProviderStatus) -> bool {
    status.network_allocation.is_some()
        || status.endpoint.is_some()
        || status.management.is_some()
        || status.kubeconfig.is_some()
        || status.vmss.is_some()
        || !status.nodes.is_empty()
        || !status.addon_components.is_empty()
        || !status.provider_resources.is_empty()
        || status.deletion.is_some()
}

pub(super) fn operation_id(tenant_uid: &str, specification_sha256: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(b"azure-tenant-operation-v1\0");
    digest.update(tenant_uid.as_bytes());
    digest.update(b"\0");
    digest.update(specification_sha256.as_bytes());
    format!("tenant-{}", hex::encode(digest.finalize()))
}

fn ownership(message: impl Into<String>) -> ReconcileError {
    ReconcileError::OwnershipInvalid(message.into())
}

fn catalog_block(action: &str) -> ReconcileError {
    ReconcileError::MutationGuard(format!(
        "Azure allocation catalog is invalid; {action} are blocked"
    ))
}

fn azure_ownership(error: AzureOwnershipError) -> ReconcileError {
    match error {
        AzureOwnershipError::Deleting => ReconcileError::Pending(error.to_string()),
        _ => ownership(error.to_string()),
    }
}

fn allocation_error(error: AllocationError) -> ReconcileError {
    match error {
        AllocationError::Api(error) => error.into(),
        AllocationError::Exhausted => ReconcileError::Pending(error.to_string()),
        _ => ownership(error.to_string()),
    }
}

fn validate_status_binding(
    status: &AzureProviderStatus,
    binding: &AzureBindingStatus,
) -> Result<(), ControllerError> {
    if status.binding.as_ref() == Some(binding) {
        Ok(())
    } else {
        Err(ControllerError::OwnershipInvalid(
            "Azure Tenant provider binding changed".into(),
        ))
    }
}

pub(super) fn record_uid(
    status: &mut AzureManagementStatus,
    kind: &str,
    object_name: &str,
    tenant: &str,
    uid: &str,
) -> Result<(), ControllerError> {
    let names = azure::AzureNames::new(tenant);
    let slot = match (kind, object_name) {
        ("Namespace", value) if value == names.namespace => &mut status.namespace_uid,
        ("AzureClusterIdentity", value) if value == names.identity => {
            &mut status.azure_cluster_identity_uid
        }
        ("Cluster", value) if value == names.cluster => &mut status.cluster_uid,
        ("AzureCluster", value) if value == names.azure_cluster => &mut status.azure_cluster_uid,
        ("KamajiControlPlane", value) if value == names.control_plane => {
            &mut status.kamaji_control_plane_uid
        }
        ("KubeadmConfig", value) if value == names.pool => &mut status.kubeadm_config_uid,
        ("AzureMachinePool", value) if value == names.pool => &mut status.azure_machine_pool_uid,
        ("MachinePool", value) if value == names.pool => &mut status.machine_pool_uid,
        ("ConfigMap", value) if value == names.cloud_values => {
            &mut status.cloud_values_config_map_uid
        }
        ("ConfigMap", value) if value == names.network_values => {
            &mut status.network_values_config_map_uid
        }
        ("Deployment", value) if value == names.status_probe => {
            &mut status.status_probe_deployment_uid
        }
        ("Job", value) if value == names.addon_job => &mut status.addon_job_uid,
        _ => {
            return Err(ControllerError::InvalidInput(
                "uncatalogued Azure management identity".into(),
            ));
        }
    };
    if slot.as_deref().is_some_and(|recorded| recorded != uid) {
        return Err(ControllerError::OwnershipInvalid(
            "Azure management object UID changed".into(),
        ));
    }
    *slot = Some(uid.into());
    Ok(())
}

pub(super) fn validate_parent(
    object: &DynamicObject,
    management: &AzureManagementStatus,
    tenant: &str,
) -> Result<(), ReconcileError> {
    let kind = object
        .types
        .as_ref()
        .map(|types| types.kind.as_str())
        .unwrap_or("");
    let Some(definition) = management::azure_by_kind(kind) else {
        return Err(ReconcileError::InvalidInput(format!(
            "{kind} is not in the Azure provider catalog"
        )));
    };
    let owners = object.owner_references();
    let Some(parent_kind) = definition.parent_kind else {
        return if owners.is_empty() {
            Ok(())
        } else {
            Err(ownership(format!(
                "{kind}/{} has an unexpected owner",
                object.name_any()
            )))
        };
    };
    let parent = management::azure_by_kind(parent_kind)
        .ok_or_else(|| ReconcileError::InvalidInput("Azure parent is not catalogued".into()))?;
    let parent_name = parent
        .expected_name(tenant)
        .ok_or_else(|| ReconcileError::InvalidInput("Azure parent has no exact name".into()))?;
    let parent_uid = management.uid_for(parent.kind, &parent_name, tenant);
    if parent_uid.is_none() {
        return if owners.is_empty() {
            Ok(())
        } else {
            Err(ownership(format!(
                "{kind}/{} has an owner before its durable parent",
                object.name_any()
            )))
        };
    }
    let [owner] = owners else {
        return if owners.is_empty() {
            Err(ReconcileError::Pending(format!(
                "{kind}/{} owner is pending",
                object.name_any()
            )))
        } else {
            Err(ownership(format!(
                "{kind}/{} has ambiguous owners",
                object.name_any()
            )))
        };
    };
    let controller_matches = if kind == "MachinePool" {
        owner.controller.is_none()
    } else {
        owner.controller == Some(true)
    };
    if !controller_matches
        || owner.api_version != parent.api_version
        || owner.kind != parent.kind
        || owner.name != parent_name
        || Some(owner.uid.as_str()) != parent_uid
    {
        return Err(ownership(format!(
            "{kind}/{} owner identity changed",
            object.name_any()
        )));
    }
    Ok(())
}

fn find<'a>(objects: &'a [DynamicObject], kind: &str) -> Result<&'a DynamicObject, ReconcileError> {
    objects
        .iter()
        .find(|object| {
            object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == kind)
        })
        .ok_or_else(|| ReconcileError::InvalidInput(format!("{kind} observation is missing")))
}

fn control_plane_endpoint(control_plane: &DynamicObject) -> Result<Option<String>, ReconcileError> {
    let endpoint = control_plane.data.pointer("/spec/controlPlaneEndpoint");
    let Some(endpoint) = endpoint else {
        return Ok(None);
    };
    let host = endpoint
        .get("host")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.trim() == *value);
    let port = endpoint
        .get("port")
        .and_then(Value::as_u64)
        .filter(|value| (1..=u64::from(u16::MAX)).contains(value));
    match (host, port) {
        (Some(host), Some(port)) => Ok(Some(format!("{host}:{port}"))),
        (None, None) => Ok(None),
        _ => Err(ReconcileError::InvalidInput(
            "Kamaji controlPlaneEndpoint is malformed".into(),
        )),
    }
}

async fn patch_cluster_bridge(
    client: Client,
    cluster: &DynamicObject,
    azure_cluster: &DynamicObject,
    endpoint: &str,
    mutations_allowed: bool,
) -> Result<bool, ReconcileError> {
    let (host, port) = endpoint
        .rsplit_once(':')
        .ok_or_else(|| ReconcileError::InvalidInput("Azure endpoint is malformed".into()))?;
    let port: u16 = port
        .parse()
        .map_err(|_| ReconcileError::InvalidInput("Azure endpoint port is malformed".into()))?;
    let api = objects::object_api(client, cluster)?;
    let uid = cluster
        .uid()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| ownership("Cluster UID is missing"))?;
    let rv = cluster
        .resource_version()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| ownership("Cluster resourceVersion is missing"))?;
    if cluster
        .data
        .pointer("/spec/controlPlaneEndpoint/host")
        .and_then(Value::as_str)
        != Some(host)
        || cluster
            .data
            .pointer("/spec/controlPlaneEndpoint/port")
            .and_then(Value::as_u64)
            != Some(u64::from(port))
    {
        if !mutations_allowed {
            return Err(catalog_block("endpoint repair"));
        }
        let updated = api
            .patch(
                &cluster.name_any(),
                &PatchParams::default(),
                &Patch::Merge(&json!({"metadata":{"uid":uid,"resourceVersion":rv},
                    "spec":{"controlPlaneEndpoint":{"host":host,"port":port}}})),
            )
            .await?;
        if updated.uid().as_deref() != Some(&uid) {
            return Err(ownership("Cluster identity changed during endpoint bridge"));
        }
        return Ok(true);
    }
    let infrastructure_ready = cluster
        .data
        .pointer("/status/infrastructureReady")
        .and_then(Value::as_bool)
        == Some(true);
    if !infrastructure_ready && readiness::object_ready(azure_cluster) {
        if !mutations_allowed {
            return Err(catalog_block("status repair"));
        }
        let updated = api
            .patch_status(
                &cluster.name_any(),
                &PatchParams::default(),
                &Patch::Merge(&json!({"metadata":{"uid":uid,"resourceVersion":rv},
                    "status":{"infrastructureReady":true}})),
            )
            .await?;
        if updated.uid().as_deref() != Some(&uid) {
            return Err(ownership(
                "Cluster identity changed during infrastructure readiness bridge",
            ));
        }
        return Ok(true);
    }
    Ok(false)
}

fn job_complete(job: &DynamicObject) -> bool {
    job.data
        .pointer("/status/conditions")
        .and_then(Value::as_array)
        .is_some_and(|conditions| {
            conditions
                .iter()
                .any(|condition| condition["type"] == "Complete" && condition["status"] == "True")
        })
}

fn job_failed(job: &DynamicObject) -> bool {
    job.data
        .pointer("/status/conditions")
        .and_then(Value::as_array)
        .is_some_and(|conditions| {
            conditions
                .iter()
                .any(|condition| condition["type"] == "Failed" && condition["status"] == "True")
        })
}

struct WorkerObservation {
    vmss: AzureVmssStatus,
    nodes: Vec<AzureNodeIdentity>,
}

async fn observe_workers(
    client: Client,
    spec: &CanonicalSpec,
    pool: &DynamicObject,
    azure_pool: &DynamicObject,
    configuration: &AzureConfiguration,
) -> Result<Option<WorkerObservation>, ReconcileError> {
    let workers = i64::from(spec.workers);
    if pool.data.pointer("/spec/replicas").and_then(Value::as_i64) != Some(workers)
        || pool
            .data
            .pointer("/status/replicas")
            .and_then(Value::as_i64)
            != Some(workers)
        || pool
            .data
            .pointer("/status/readyReplicas")
            .and_then(Value::as_i64)
            != Some(workers)
        || azure_pool
            .data
            .pointer("/status/replicas")
            .and_then(Value::as_i64)
            != Some(workers)
        || (azure_pool
            .data
            .pointer("/status/ready")
            .and_then(Value::as_bool)
            != Some(true)
            && !readiness::object_ready(azure_pool))
    {
        return Ok(None);
    }
    let refs = pool
        .data
        .pointer("/status/nodeRefs")
        .and_then(Value::as_array)
        .ok_or_else(|| ReconcileError::Pending("MachinePool nodeRefs are pending".into()))?;
    let names: BTreeSet<_> = refs
        .iter()
        .filter_map(|item| item.get("name").and_then(Value::as_str))
        .collect();
    if names.len() != spec.workers as usize || refs.len() != spec.workers as usize {
        return Ok(None);
    }
    let nodes_api = Api::<DynamicObject>::all_with(client, &objects::resource("v1", "Node"));
    let nodes = nodes_api.list(&ListParams::default()).await?.items;
    if nodes.len() != spec.workers as usize {
        return Ok(None);
    }
    let expected_prefix = format!(
        "/subscriptions/{}/resourceGroups/{}/providers/Microsoft.Compute/virtualMachineScaleSets/{}-worker",
        configuration.values.subscription_id,
        configuration.values.resource_group_name,
        pool.metadata.namespace.as_deref().unwrap_or("")
    );
    let mut identities = Vec::new();
    let mut vmss_id = None;
    let mut instance_ids = Vec::new();
    for node in nodes {
        let node_name = node.name_any();
        if !names.contains(node_name.as_str()) || !readiness::node_ready(&node) {
            return Ok(None);
        }
        let uid = node
            .uid()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ownership("Node UID is missing"))?;
        let provider_id = node
            .data
            .pointer("/spec/providerID")
            .and_then(Value::as_str)
            .filter(|value| value.to_ascii_lowercase().starts_with("azure:///"))
            .ok_or_else(|| ownership("Node Azure providerID is invalid"))?;
        let resource_id = provider_id
            .strip_prefix("azure://")
            .or_else(|| provider_id.strip_prefix("AZURE://"))
            .unwrap_or(&provider_id["azure://".len()..]);
        let (observed_vmss, instance) = resource_id
            .rsplit_once("/virtualMachines/")
            .ok_or_else(|| ownership("Node VMSS providerID is invalid"))?;
        if !observed_vmss.eq_ignore_ascii_case(&expected_prefix)
            || instance.is_empty()
            || instance.contains('/')
        {
            return Err(ownership("Node VMSS providerID changed"));
        }
        if vmss_id
            .as_deref()
            .is_some_and(|value: &str| !value.eq_ignore_ascii_case(observed_vmss))
        {
            return Err(ownership("Nodes reference different VMSS identities"));
        }
        vmss_id.get_or_insert_with(|| observed_vmss.to_owned());
        instance_ids.push(resource_id.to_owned());
        let internal_ips: Vec<_> = node
            .data
            .pointer("/status/addresses")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter(|address| address["type"] == "InternalIP")
            .filter_map(|address| address["address"].as_str())
            .collect();
        if internal_ips.len() != 1 || internal_ips[0].parse::<std::net::IpAddr>().is_err() {
            return Ok(None);
        }
        identities.push(AzureNodeIdentity {
            name: node_name,
            uid,
            provider_id: provider_id.into(),
            internal_ip: internal_ips[0].into(),
        });
    }
    identities.sort_by(|left, right| left.name.cmp(&right.name));
    instance_ids.sort_by_key(|value| value.to_ascii_lowercase());
    if instance_ids
        .iter()
        .map(|value| value.to_ascii_lowercase())
        .collect::<BTreeSet<_>>()
        .len()
        != spec.workers as usize
    {
        return Err(ownership("VMSS instance identities are duplicated"));
    }
    Ok(Some(WorkerObservation {
        vmss: AzureVmssStatus {
            id: vmss_id,
            instance_ids,
        },
        nodes: identities,
    }))
}

async fn observe_components(
    client: Client,
) -> Result<Option<BTreeMap<String, String>>, ReconcileError> {
    let definitions = [
        (
            "cloudController",
            "apps/v1",
            "Deployment",
            "kube-system",
            "cloud-controller-manager",
        ),
        (
            "cloudNode",
            "apps/v1",
            "DaemonSet",
            "kube-system",
            "cloud-node-manager",
        ),
        (
            "calicoNode",
            "apps/v1",
            "DaemonSet",
            "calico-system",
            "calico-node",
        ),
        (
            "calicoControllers",
            "apps/v1",
            "Deployment",
            "calico-system",
            "calico-kube-controllers",
        ),
    ];
    let mut result = BTreeMap::new();
    for (key, version, kind, namespace, name) in definitions {
        let api = Api::<DynamicObject>::namespaced_with(
            client.clone(),
            namespace,
            &objects::resource(version, kind),
        );
        let Some(object) = api.get_opt(name).await? else {
            return Ok(None);
        };
        if !azure_workload_ready(&object) {
            return Ok(None);
        }
        let uid = object
            .uid()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| ownership(format!("{kind}/{name} UID is missing")))?;
        result.insert(key.into(), uid);
    }
    Ok(Some(result))
}

fn azure_workload_ready(object: &DynamicObject) -> bool {
    if !readiness::workload_available(object) {
        return false;
    }
    if object
        .types
        .as_ref()
        .is_some_and(|types| types.kind == "DaemonSet")
    {
        let desired = object
            .data
            .pointer("/status/desiredNumberScheduled")
            .and_then(Value::as_i64);
        return desired.is_some_and(|desired| {
            desired > 0
                && object
                    .data
                    .pointer("/status/numberReady")
                    .and_then(Value::as_i64)
                    == Some(desired)
                && object
                    .data
                    .pointer("/status/updatedNumberScheduled")
                    .and_then(Value::as_i64)
                    == Some(desired)
        });
    }
    let desired = object
        .data
        .pointer("/spec/replicas")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    object
        .data
        .pointer("/status/updatedReplicas")
        .and_then(Value::as_i64)
        == Some(desired)
}

async fn observe_provider_resources(
    client: Client,
    binding: &AzureBindingStatus,
    management_status: &AzureManagementStatus,
    roots: &[DynamicObject],
    tenant: &str,
) -> Result<Vec<AzureProviderResourceIdentity>, ReconcileError> {
    let mut inventory = Vec::new();
    let mut seen = BTreeSet::new();
    for definition in management::AZURE_MANAGEMENT_RESOURCES
        .iter()
        .filter(|definition| {
            definition.class == ResourceClass::Descendant
                || matches!(definition.kind, "ConfigMap" | "Deployment" | "Secret")
        })
    {
        if !seen.insert((definition.api_version, definition.plural)) {
            continue;
        }
        let api = Api::<DynamicObject>::namespaced_with(
            client.clone(),
            tenant,
            &definition.api_resource(),
        );
        let mut items = api.list(&ListParams::default()).await?.items;
        for item in &mut items {
            item.types.get_or_insert(kube::core::TypeMeta {
                api_version: definition.api_version.into(),
                kind: definition.kind.into(),
            });
        }
        inventory.extend(items);
    }
    let explicit_uids: BTreeSet<_> = management_status
        .recorded_uids()
        .into_iter()
        .map(str::to_owned)
        .collect();
    inventory.retain(|object| {
        !object.uid().is_some_and(|uid| explicit_uids.contains(&uid))
            && !(object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "ConfigMap")
                && object.name_any() == "kube-root-ca.crt")
            && !(object
                .types
                .as_ref()
                .is_some_and(|types| types.kind == "Secret")
                && object.name_any() == format!("{tenant}-kubeconfig"))
    });
    let mut owned: BTreeSet<String> = management_status
        .recorded_uids()
        .into_iter()
        .map(Into::into)
        .collect();
    owned.extend(roots.iter().filter_map(ResourceExt::uid));
    let mut selected = BTreeSet::new();
    loop {
        let before = selected.len();
        for (index, object) in inventory.iter().enumerate() {
            let exact_markers = markers_match(object, binding);
            let owner_match = object
                .owner_references()
                .iter()
                .any(|owner| owned.contains(&owner.uid));
            if exact_markers || owner_match {
                let uid = object
                    .uid()
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| ownership("Azure provider resource UID is missing"))?;
                owned.insert(uid);
                selected.insert(index);
            }
        }
        if selected.len() == before {
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
        return Err(ownership(format!(
            "foreign or ownerless Azure provider resource is present: {unknown}"
        )));
    }
    let mut result = Vec::new();
    for object in inventory {
        let types = object
            .types
            .as_ref()
            .ok_or_else(|| ownership("Azure provider resource GVK is missing"))?;
        if matches!(types.kind.as_str(), "Pod" | "ReplicaSet" | "EndpointSlice") {
            continue;
        }
        let uid = object
            .uid()
            .ok_or_else(|| ownership("provider UID is missing"))?;
        let mut owner_uids: Vec<_> = object
            .owner_references()
            .iter()
            .map(|owner| owner.uid.clone())
            .collect();
        owner_uids.sort();
        owner_uids.dedup();
        if owner_uids.is_empty() || !owner_uids.iter().any(|uid| owned.contains(uid)) {
            return Err(ownership(format!(
                "Azure provider owner chain is incomplete for {}/{}",
                types.kind,
                object.name_any()
            )));
        }
        let resource_id = [
            "/status/id",
            "/status/resourceId",
            "/status/providerID",
            "/spec/providerID",
        ]
        .into_iter()
        .find_map(|pointer| object.data.pointer(pointer).and_then(Value::as_str))
        .map(Into::into);
        if types.api_version.contains(".azure.com/")
            && resource_id
                .as_deref()
                .is_none_or(|value: &str| !value.starts_with('/'))
        {
            return Err(ReconcileError::Pending(format!(
                "{}/{} Azure resource ID is pending",
                types.kind,
                object.name_any()
            )));
        }
        result.push(AzureProviderResourceIdentity {
            api_version: types.api_version.clone(),
            kind: types.kind.clone(),
            namespace: object.namespace(),
            name: object.name_any(),
            uid,
            resource_id,
            owner_uids,
        });
    }
    result.sort_by(|left, right| {
        (
            &left.api_version,
            &left.kind,
            &left.namespace,
            &left.name,
            &left.uid,
            &left.resource_id,
        )
            .cmp(&(
                &right.api_version,
                &right.kind,
                &right.namespace,
                &right.name,
                &right.uid,
                &right.resource_id,
            ))
    });
    Ok(result)
}

pub(super) fn markers_match(object: &DynamicObject, binding: &AzureBindingStatus) -> bool {
    let annotations = object.annotations();
    annotations.get(TENANT_ANNOTATION) == Some(&object.namespace().unwrap_or_default())
        && annotations.get(ANNOTATION_PROFILE).map(String::as_str) == Some("azure")
        && annotations.get(ANNOTATION_SPEC) == Some(&binding.specification_sha256)
        && annotations.get(ANNOTATION_FOUNDATION) == Some(&binding.foundation_sha256)
        && annotations.get(ANNOTATION_OPERATION) == Some(&binding.operation_id)
}

fn validate_durable_observations(
    current: Option<&AzureProviderStatus>,
    vmss: &AzureVmssStatus,
    nodes: &[AzureNodeIdentity],
    components: &BTreeMap<String, String>,
    resources: &[AzureProviderResourceIdentity],
) -> Result<(), ReconcileError> {
    let Some(current) = current else {
        return Ok(());
    };
    if let Some(recorded) = &current.vmss
        && (recorded
            .id
            .as_deref()
            .zip(vmss.id.as_deref())
            .is_some_and(|(left, right)| !left.eq_ignore_ascii_case(right))
            || !bounded_replacement(&recorded.instance_ids, &vmss.instance_ids))
    {
        return Err(ownership(
            "VMSS durable identity changed outside one replacement",
        ));
    }
    if !current.nodes.is_empty() && !bounded_node_replacement(&current.nodes, nodes) {
        return Err(ownership(
            "Node durable identity changed outside one replacement",
        ));
    }
    if !current.addon_components.is_empty() && current.addon_components != *components {
        return Err(ownership("Azure add-on workload identity changed"));
    }
    if !current.provider_resources.is_empty()
        && !bounded_provider_replacement(&current.provider_resources, resources)
    {
        return Err(ownership(
            "Azure provider resource identity changed outside one CAPZ replacement",
        ));
    }
    Ok(())
}

fn bounded_replacement(before: &[String], after: &[String]) -> bool {
    let before: BTreeSet<_> = before
        .iter()
        .map(|value| value.to_ascii_lowercase())
        .collect();
    let after: BTreeSet<_> = after
        .iter()
        .map(|value| value.to_ascii_lowercase())
        .collect();
    before == after
        || (before.len() == after.len()
            && before.difference(&after).count() == 1
            && after.difference(&before).count() == 1)
}

fn bounded_node_replacement(before: &[AzureNodeIdentity], after: &[AzureNodeIdentity]) -> bool {
    let before: BTreeSet<_> = before
        .iter()
        .map(|node| (&node.name, &node.uid, &node.provider_id, &node.internal_ip))
        .collect();
    let after: BTreeSet<_> = after
        .iter()
        .map(|node| (&node.name, &node.uid, &node.provider_id, &node.internal_ip))
        .collect();
    before == after
        || (before.len() == after.len()
            && before.difference(&after).count() == 1
            && after.difference(&before).count() == 1)
}

fn bounded_provider_replacement(
    before: &[AzureProviderResourceIdentity],
    after: &[AzureProviderResourceIdentity],
) -> bool {
    if before == after {
        return true;
    }
    let stable = |items: &[AzureProviderResourceIdentity]| {
        items
            .iter()
            .filter(|item| !matches!(item.kind.as_str(), "Machine" | "AzureMachinePoolMachine"))
            .cloned()
            .collect::<Vec<_>>()
    };
    if stable(before) != stable(after) {
        return false;
    }
    let replaceable = |items: &[AzureProviderResourceIdentity]| {
        items
            .iter()
            .filter(|item| matches!(item.kind.as_str(), "Machine" | "AzureMachinePoolMachine"))
            .map(|item| {
                (
                    item.kind.clone(),
                    item.name.clone(),
                    item.uid.clone(),
                    item.resource_id.clone(),
                )
            })
            .collect::<BTreeSet<_>>()
    };
    bounded_set(replaceable(before), replaceable(after), 2)
}

fn bounded_set<T: Ord>(before: BTreeSet<T>, after: BTreeSet<T>, limit: usize) -> bool {
    before.len() == after.len()
        && (1..=limit).contains(&before.difference(&after).count())
        && before.difference(&after).count() == after.difference(&before).count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn operation_identity_is_stable_and_uid_bound() {
        let first = operation_id("uid-a", "spec");
        assert_eq!(first, operation_id("uid-a", "spec"));
        assert_ne!(first, operation_id("uid-b", "spec"));
        assert_ne!(first, operation_id("uid-a", "other"));
    }

    #[test]
    fn bounded_replacement_accepts_exactly_one_delta() {
        let before = vec!["one".into(), "two".into(), "three".into()];
        assert!(bounded_replacement(&before, &before));
        assert!(bounded_replacement(
            &before,
            &["one".into(), "two".into(), "four".into()]
        ));
        assert!(!bounded_replacement(
            &before,
            &["one".into(), "four".into(), "five".into()]
        ));
    }

    #[test]
    fn bounded_node_and_capz_resource_replacement_preserves_survivors() {
        let node = |name: &str| AzureNodeIdentity {
            name: name.into(),
            uid: format!("{name}-uid"),
            provider_id: format!("azure:///{name}"),
            internal_ip: format!("10.0.0.{}", name.len()),
        };
        let before = vec![node("one"), node("two"), node("three")];
        let after = vec![node("one"), node("two"), node("four")];
        assert!(bounded_node_replacement(&before, &after));
        assert!(!bounded_node_replacement(
            &before,
            &[node("one"), node("four"), node("five")]
        ));

        let resource = |kind: &str, name: &str| AzureProviderResourceIdentity {
            api_version: "example/v1".into(),
            kind: kind.into(),
            namespace: Some("tenant".into()),
            name: name.into(),
            uid: format!("{name}-uid"),
            resource_id: None,
            owner_uids: vec!["pool".into()],
        };
        let stable = resource("Service", "stable");
        let old = vec![
            stable.clone(),
            resource("Machine", "machine-old"),
            resource("AzureMachinePoolMachine", "azure-old"),
        ];
        let replacement = vec![
            stable.clone(),
            resource("Machine", "machine-new"),
            resource("AzureMachinePoolMachine", "azure-new"),
        ];
        assert!(bounded_provider_replacement(&old, &replacement));
        let mut changed_stable = replacement;
        changed_stable[0] = resource("Service", "foreign");
        assert!(!bounded_provider_replacement(&old, &changed_stable));
    }
}
