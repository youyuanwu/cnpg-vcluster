//! Direct-read reconciliation with durable-write barriers and narrowly bound SSA.

mod azure;
mod azure_finalize;
mod error;
mod local;
pub mod objects;
pub mod workers;

pub use crate::api::FINALIZER;
pub use azure::{AzureProvider, CONFIG_NAME as AZURE_CONFIG_NAME};
pub use error::ReconcileError;
pub use local::{Assets, LocalProvider, ProviderLifecycle, TenantAccess};

use std::{future::Future, sync::Arc, time::Duration};

use futures::StreamExt;
use kube::{
    Api, Client, ResourceExt,
    core::DynamicObject,
    runtime::{
        controller::{Action, Controller},
        reflector::ObjectRef,
        watcher,
    },
};

use crate::{
    api::{
        CatalogCreateIntent, DatabaseCapability, SUPPORTED_KUBERNETES_VERSION, Tenant, TenantPhase,
        TenantProviderStatus, canonical_spec,
    },
    error::ControllerError,
    foundation, management,
    readiness::{self, set_condition},
    runtime::{LeadershipGate, tenant_controller},
    sanitize, status,
};
use tenant_database_runtime::catalog_runtime::{self, CatalogIdentity, CatalogRuntimeError};

pub const FOUNDATION_NAMESPACE: &str = "tenant-system";
pub const PROGRESS_INTERVAL: Duration = Duration::from_secs(1);
pub const DEPENDENCY_INTERVAL: Duration = Duration::from_secs(5);
pub const READY_INTERVAL: Duration = Duration::from_secs(300);
pub const STORAGE_CLASS: &str = "capi-hostpath";
fn create_intent(tenant: &Tenant) -> Result<CatalogCreateIntent, ReconcileError> {
    let name = tenant.name_any();
    Ok(CatalogCreateIntent {
        namespace: tenant_database_runtime::database_namespace(&name),
        name,
        tenant_uid: tenant
            .uid()
            .ok_or_else(|| ReconcileError::OwnershipInvalid("Tenant UID is missing".into()))?,
    })
}

fn recorded_catalog(tenant: &Tenant) -> Option<CatalogIdentity> {
    tenant
        .status
        .as_ref()?
        .database_capability
        .as_ref()
        .and_then(|value| {
            (!value.catalog_uid.is_empty() && !value.namespace_uid.is_empty()).then(|| {
                CatalogIdentity {
                    namespace_uid: value.namespace_uid.clone(),
                    catalog_uid: value.catalog_uid.clone(),
                    storage_namespace_uid: value.storage_namespace_uid.clone(),
                }
            })
        })
}

fn catalog_error(error: CatalogRuntimeError) -> ReconcileError {
    ReconcileError::OwnershipInvalid(error.to_string())
}
async fn record_catalog_identity(
    client: Client,
    tenant: &Tenant,
    intent: &CatalogCreateIntent,
    identity: &CatalogIdentity,
) -> Result<(), ReconcileError> {
    status::update_status(client, tenant, |status| {
        if status.catalog_create_intent.as_ref() != Some(intent) {
            return Err(ControllerError::OwnershipInvalid(
                "catalog intent changed".into(),
            ));
        }
        let capability = status
            .database_capability
            .get_or_insert_with(Default::default);
        if (!capability.namespace_uid.is_empty()
            && capability.namespace_uid != identity.namespace_uid)
            || (capability.storage_namespace_uid.is_some()
                && capability.storage_namespace_uid != identity.storage_namespace_uid)
            || (!capability.catalog_uid.is_empty()
                && capability.catalog_uid != identity.catalog_uid)
        {
            return Err(ControllerError::OwnershipInvalid(
                "catalog identity changed".into(),
            ));
        }
        capability.namespace = intent.namespace.clone();
        capability.namespace_uid = identity.namespace_uid.clone();
        capability.catalog_uid = identity.catalog_uid.clone();
        capability.storage_namespace_uid = identity.storage_namespace_uid.clone();
        capability.available = false;
        capability.reason = "CatalogObserved".into();
        crate::readiness::set_condition(
            status,
            tenant,
            status::CATALOG_CREATE_CONDITION,
            true,
            "Observed",
            "Catalog identity persisted",
        );
        Ok(())
    })
    .await?;
    Ok(())
}

pub(crate) async fn ensure_database_catalog(
    client: Client,
    tenant: &Tenant,
    azure: bool,
) -> Result<DatabaseCapability, ReconcileError> {
    let intent = create_intent(tenant)?;
    let prior = tenant
        .status
        .as_ref()
        .and_then(|status| status.catalog_create_intent.as_ref());
    if prior.is_none() {
        status::record_catalog_create_intent(client, tenant, &intent).await?;
        return Err(ReconcileError::Pending(
            "catalog creation intent recorded".into(),
        ));
    }
    if prior != Some(&intent) {
        return Err(ReconcileError::OwnershipInvalid(
            "catalog creation intent changed".into(),
        ));
    }
    let outcome = tenant
        .status
        .as_ref()
        .and_then(status::catalog_create_outcome);
    let recorded = recorded_catalog(tenant);
    if let Some(identity) = catalog_runtime::observe_catalog(
        client.clone(),
        &intent.name,
        &intent.tenant_uid,
        recorded.as_ref(),
    )
    .await
    .map_err(catalog_error)?
    {
        if recorded.as_ref() != Some(&identity) || outcome != Some("Observed") {
            record_catalog_identity(client, tenant, &intent, &identity).await?;
            return Err(ReconcileError::Pending("catalog identity recorded".into()));
        }
        return Ok(DatabaseCapability {
            available: false,
            reason: "RuntimeNotReady".into(),
            namespace: intent.namespace,
            namespace_uid: identity.namespace_uid,
            catalog_uid: identity.catalog_uid,
            storage_namespace_uid: identity.storage_namespace_uid,
        });
    }
    if recorded.is_some() {
        return Err(ReconcileError::OwnershipInvalid(
            "catalog identity disappeared".into(),
        ));
    }
    if outcome == Some("PreparationRejected") {
        status::set_catalog_create_outcome(
            client,
            tenant,
            &intent,
            "PreparationRejected",
            "Prepared",
        )
        .await?;
        return Err(ReconcileError::Pending(
            "namespace CREATE retry prepared".into(),
        ));
    }
    if !matches!(
        outcome,
        Some("Prepared" | "Preparing" | "Namespaced" | "Rejected")
    ) {
        return Err(ReconcileError::Pending(
            "catalog CREATE outcome is unresolved".into(),
        ));
    }
    if outcome == Some("Prepared") {
        status::set_catalog_create_outcome(
            client.clone(),
            tenant,
            &intent,
            "Prepared",
            "Preparing",
        )
        .await?;
        let namespace_uid = catalog_runtime::ensure_namespace(
            client.clone(),
            &intent.namespace,
            &intent.tenant_uid,
            None,
        )
        .await;
        let namespace_uid = match namespace_uid {
            Ok(uid) => uid,
            Err(CatalogRuntimeError::Rejected) => {
                let latest = Api::<Tenant>::all(client.clone()).get(&intent.name).await?;
                status::set_catalog_create_outcome(
                    client,
                    &latest,
                    &intent,
                    "Preparing",
                    "PreparationRejected",
                )
                .await?;
                return Err(ReconcileError::Pending("namespace CREATE rejected".into()));
            }
            Err(error) => return Err(catalog_error(error)),
        };
        let storage_uid = if azure {
            Some(
                match catalog_runtime::ensure_namespace(
                    client.clone(),
                    &catalog_runtime::storage_namespace(&intent.name),
                    &intent.tenant_uid,
                    None,
                )
                .await
                {
                    Ok(uid) => uid,
                    Err(CatalogRuntimeError::Rejected) => {
                        let latest = Api::<Tenant>::all(client.clone()).get(&intent.name).await?;
                        status::set_catalog_create_outcome(
                            client,
                            &latest,
                            &intent,
                            "Preparing",
                            "PreparationRejected",
                        )
                        .await?;
                        return Err(ReconcileError::Pending(
                            "storage namespace CREATE rejected".into(),
                        ));
                    }
                    Err(error) => return Err(catalog_error(error)),
                },
            )
        } else {
            None
        };
        let latest = Api::<Tenant>::all(client.clone()).get(&intent.name).await?;
        status::record_catalog_namespaces(
            client,
            &latest,
            &intent,
            &namespace_uid,
            storage_uid.as_deref(),
        )
        .await?;
        return Err(ReconcileError::Pending(
            "catalog namespace identities recorded".into(),
        ));
    }
    if outcome == Some("Preparing") {
        let namespace_uid = catalog_runtime::observe_namespace(
            client.clone(),
            &intent.namespace,
            &intent.tenant_uid,
            None,
        )
        .await
        .map_err(catalog_error)?;
        let storage_uid = if azure {
            catalog_runtime::observe_namespace(
                client.clone(),
                &catalog_runtime::storage_namespace(&intent.name),
                &intent.tenant_uid,
                None,
            )
            .await
            .map_err(catalog_error)?
        } else {
            None
        };
        if let Some(namespace_uid) = namespace_uid
            && (!azure || storage_uid.is_some())
        {
            status::record_catalog_namespaces(
                client,
                tenant,
                &intent,
                &namespace_uid,
                storage_uid.as_deref(),
            )
            .await?;
        }
        return Err(ReconcileError::Pending(
            "catalog namespace preparation is pending".into(),
        ));
    }
    let capability = tenant
        .status
        .as_ref()
        .and_then(|status| status.database_capability.as_ref());
    let namespace_uid = capability
        .map(|capability| capability.namespace_uid.as_str())
        .filter(|uid| !uid.is_empty())
        .ok_or_else(|| {
            ReconcileError::OwnershipInvalid("catalog namespace UID is missing".into())
        })?;
    let storage_uid = capability.and_then(|capability| capability.storage_namespace_uid.as_deref());
    if azure && storage_uid.is_none() {
        return Err(ReconcileError::OwnershipInvalid(
            "storage namespace UID is missing".into(),
        ));
    }
    catalog_runtime::ensure_namespace(
        client.clone(),
        &intent.namespace,
        &intent.tenant_uid,
        Some(namespace_uid),
    )
    .await
    .map_err(catalog_error)?;
    if let Some(storage_uid) = storage_uid {
        catalog_runtime::ensure_namespace(
            client.clone(),
            &catalog_runtime::storage_namespace(&intent.name),
            &intent.tenant_uid,
            Some(storage_uid),
        )
        .await
        .map_err(catalog_error)?;
    }
    let current = Api::<Tenant>::all(client.clone()).get(&intent.name).await?;
    if current.uid() != tenant.uid()
        || current.resource_version() != tenant.resource_version()
        || current.metadata.deletion_timestamp.is_some()
    {
        return Err(ReconcileError::Pending(
            "Tenant changed before catalog CREATE".into(),
        ));
    }
    status::set_catalog_create_outcome(
        client.clone(),
        &current,
        &intent,
        outcome.expect("checked above"),
        "Unknown",
    )
    .await?;
    match catalog_runtime::ensure_catalog(
        client.clone(),
        &intent.name,
        &intent.tenant_uid,
        Some(namespace_uid),
        None,
        storage_uid,
        azure,
    )
    .await
    {
        Ok(identity) => {
            let latest = Api::<Tenant>::all(client.clone()).get(&intent.name).await?;
            record_catalog_identity(client, &latest, &intent, &identity).await?;
        }
        Err(CatalogRuntimeError::Rejected) => {
            let latest = Api::<Tenant>::all(client.clone()).get(&intent.name).await?;
            status::set_catalog_create_outcome(client, &latest, &intent, "Unknown", "Rejected")
                .await?;
        }
        Err(error) => return Err(catalog_error(error)),
    }
    Err(ReconcileError::Pending(
        "catalog CREATE outcome recorded".into(),
    ))
}

async fn drain_databases(client: Client, tenant: &Tenant) -> Result<bool, ReconcileError> {
    let intent = create_intent(tenant)?;
    let prior = tenant
        .status
        .as_ref()
        .and_then(|status| status.catalog_create_intent.as_ref());
    if prior.is_some_and(|prior| prior != &intent) {
        return Err(ReconcileError::OwnershipInvalid(
            "catalog creation intent changed".into(),
        ));
    }
    let recorded = recorded_catalog(tenant);
    let outcome = tenant
        .status
        .as_ref()
        .and_then(status::catalog_create_outcome);
    if prior.is_none() && recorded.is_none() && outcome != Some("Closed") {
        status::close_catalog_creation_without_intent(client, tenant).await?;
        return Ok(false);
    }
    if prior.is_some() && (recorded.is_none() || outcome != Some("Observed")) {
        let observed = catalog_runtime::observe_catalog(
            client.clone(),
            &intent.name,
            &intent.tenant_uid,
            recorded.as_ref(),
        )
        .await
        .map_err(catalog_error)?;
        if let Some(identity) = observed {
            record_catalog_identity(client, tenant, &intent, &identity).await?;
            return Ok(false);
        }
        if !matches!(
            outcome,
            Some("Prepared" | "Namespaced" | "Rejected" | "PreparationRejected")
        ) {
            return Ok(false);
        }
    }
    let expected = recorded.or_else(|| {
        tenant
            .status
            .as_ref()?
            .database_capability
            .as_ref()
            .map(|value| CatalogIdentity {
                namespace_uid: value.namespace_uid.clone(),
                catalog_uid: String::new(),
                storage_namespace_uid: value.storage_namespace_uid.clone(),
            })
    });
    catalog_runtime::drain_catalog(client, &intent.name, &intent.tenant_uid, expected.as_ref())
        .await
        .map_err(catalog_error)
}
#[derive(Clone, Debug)]
pub struct Config {
    pub supported_version: String,
    pub watch_management_resources: bool,
    pub management_resources: &'static [management::ManagementResource],
}
impl Default for Config {
    fn default() -> Self {
        Self {
            supported_version: SUPPORTED_KUBERNETES_VERSION.into(),
            watch_management_resources: true,
            management_resources: management::MANAGEMENT_RESOURCES,
        }
    }
}
pub struct Reconciler<P = LocalProvider> {
    pub client: Client,
    pub provider: P,
    pub config: Config,
}
impl<P> Reconciler<P> {
    pub fn new(client: Client, config: Config, provider: P) -> Self {
        Self {
            client,
            provider,
            config,
        }
    }
}

impl<P: ProviderLifecycle> Reconciler<P> {
    #[rustfmt::skip]
    pub async fn reconcile_name(&self, name: &str) -> Result<Action, ReconcileError> {
        let Some(tenant) = Api::<Tenant>::all(self.client.clone())
            .get_opt(name)
            .await?
        else {
            return Ok(Action::await_change());
        };
        let has_finalizer = tenant.finalizers().iter().any(|value| value == FINALIZER);
        if tenant.metadata.deletion_timestamp.is_some() && !has_finalizer {
            return Ok(Action::await_change());
        }
        let provider_supported = self.provider.supports(&tenant.spec.provider);
        if provider_supported && let Err(guard) = self.provider.validate_mutation().await {
            return Ok(guard.action());
        }
        let spec = match canonical_spec(name, &tenant.spec, &self.config.supported_version) {
            Ok(spec) => spec,
            Err(error) => {
                status::update_status(self.client.clone(), &tenant, |status| {
                    status.phase = Some(TenantPhase::Failed);
                    let error = error.to_string();
                    for (condition, message) in [
                        ("Accepted", error.as_str()),
                        ("Ready", "Tenant specification is invalid"),
                    ] {
                        set_condition(status, &tenant, condition, false, "InvalidSpec", message);
                    }
                    Ok(())
                })
                .await?;
                return Ok(Action::await_change());
            }
        };
        if let Err(error) = crate::api::validate_provider_status(&spec, tenant.status.as_ref()) {
            if let Err(guard) = self.provider.validate_mutation().await {
                return Ok(guard.action());
            }
            return self
                .failure(&tenant, ReconcileError::OwnershipInvalid(error.to_string()))
                .await;
        }
        if !provider_supported {
            return self.unsupported_provider(&tenant).await;
        }
        let deleting = tenant.metadata.deletion_timestamp.is_some();
        let result = if deleting {
            match drain_databases(self.client.clone(), &tenant).await {
                Ok(true) => self.provider.finalize(&tenant, &self.config.supported_version).await,
                Ok(false) => Err(ReconcileError::Pending("catalog drain is pending".into())),
                Err(error) => Err(error),
            }
        } else {
            self.provider.reconcile(&tenant, &spec).await
        };
        match result {
            Ok(action) => Ok(action),
            Err(error) if !deleting && error.pending() => {
                if let Err(guard) = self.provider.validate_mutation().await {
                    return Ok(guard.action());
                }
                progress(self.client.clone(), &tenant, DEPENDENCY_INTERVAL).await
            }
            Err(error) => {
                if matches!(error, ReconcileError::MutationGuard(_)) {
                    return Ok(error.action());
                }
                if let Err(guard) = self.provider.validate_mutation().await {
                    return Ok(guard.action());
                }
                self.failure(&tenant, error).await
            }
        }
    }
    async fn unsupported_provider(&self, tenant: &Tenant) -> Result<Action, ReconcileError> {
        let has_finalizer = tenant.finalizers().iter().any(|value| value == FINALIZER);
        let (provider_status, provider_name) = match tenant.spec.provider {
            crate::api::TenantProviderSpec::Local => {
                (TenantProviderStatus::Local(Default::default()), "Local")
            }
            crate::api::TenantProviderSpec::Azure => {
                (TenantProviderStatus::Azure(Default::default()), "Azure")
            }
        };
        let (reason, detail) = if has_finalizer {
            (
                "ProviderFinalizerUnsupported",
                "carries the controller finalizer; lifecycle is not implemented",
            )
        } else {
            ("ProviderUnsupported", "reconciliation is not implemented")
        };
        let message = format!("{provider_name} provider {detail}");
        status::update_status(self.client.clone(), tenant, |status| {
            status
                .provider
                .get_or_insert_with(|| provider_status.clone());
            status.phase = Some(if tenant.metadata.deletion_timestamp.is_some() {
                TenantPhase::Deleting
            } else {
                TenantPhase::Failed
            });
            for condition in ["Accepted", "Ready"] {
                set_condition(status, tenant, condition, false, reason, &message);
            }
            Ok(())
        })
        .await?;
        Ok(Action::await_change())
    }
    async fn failure(
        &self,
        tenant: &Tenant,
        error: ReconcileError,
    ) -> Result<Action, ReconcileError> {
        let deleting = tenant.metadata.deletion_timestamp.is_some();
        let failed = if deleting {
            TenantPhase::Deleting
        } else {
            TenantPhase::Failed
        };
        let (phase, reason) = if error.ownership_invalid() {
            (TenantPhase::OwnershipInvalid, "OwnershipInvalid")
        } else if let Some(reason) = error.degraded_reason() {
            (TenantPhase::Degraded, reason)
        } else if matches!(
            &error,
            ReconcileError::Foundation(foundation::FoundationError::Identity)
        ) {
            (failed, "FoundationMismatch")
        } else if matches!(&error, ReconcileError::Foundation(_)) {
            (failed, "FoundationInvalid")
        } else {
            (
                failed,
                if deleting {
                    "DeletionBlocked"
                } else {
                    "ReconcileFailed"
                },
            )
        };
        if error.conflict() {
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        let message = error.to_string();
        status::update_status(self.client.clone(), tenant, |status| {
            status.phase = Some(phase);
            set_condition(status, tenant, "Ready", false, reason, &message);
            if phase == TenantPhase::OwnershipInvalid {
                set_condition(status, tenant, "OwnershipValid", false, reason, &message);
            }
            if matches!(reason, "FoundationMismatch" | "FoundationInvalid") {
                set_condition(status, tenant, "FoundationReady", false, reason, &message);
            }
            Ok(())
        })
        .await?;
        tracing::warn!(tenant = %tenant.name_any(), error = %sanitize::text(&error.to_string()), "Tenant reconciliation blocked");
        Ok(error.action())
    }
}

async fn progress(
    client: Client,
    tenant: &Tenant,
    interval: Duration,
) -> Result<Action, ReconcileError> {
    status::update_status(client, tenant, |status| {
        readiness::progress_status(status, tenant);
        Ok(())
    })
    .await?;
    Ok(Action::requeue(interval))
}

pub fn map_management_to_tenant(
    definition: management::ManagementResource,
    object: &DynamicObject,
) -> Vec<ObjectRef<Tenant>> {
    let mapped = crate::runtime::map_dependent_to_tenant(object);
    if !mapped.is_empty() {
        return mapped;
    }
    if let Some(name) = object
        .annotations()
        .get(crate::azure::TENANT_ANNOTATION)
        .or_else(|| object.labels().get(crate::azure::TENANT_LABEL))
        .filter(|name| !name.is_empty())
    {
        return vec![ObjectRef::new(name)];
    }
    if let Some(suffix) = definition.watch_name_suffix {
        object
            .namespace()
            .filter(|namespace| object.name_any() == format!("{namespace}{suffix}"))
            .map(|namespace| vec![ObjectRef::new(&namespace)])
            .unwrap_or_default()
    } else if let Some(label) = definition.watch_cluster_label {
        object
            .labels()
            .get(label)
            .filter(|name| !name.is_empty())
            .map(|name| vec![ObjectRef::new(name)])
            .unwrap_or_default()
    } else {
        Vec::new()
    }
}

pub fn controller(client: Client, config: &Config) -> Controller<Tenant> {
    let mut controller = tenant_controller(client.clone());
    if config.watch_management_resources {
        for definition in config
            .management_resources
            .iter()
            .copied()
            .filter(|resource| resource.watched)
        {
            let resource = definition.api_resource();
            controller = controller.watches_with(
                Api::<DynamicObject>::all_with(client.clone(), &resource),
                resource,
                watcher::Config::default(),
                move |object| map_management_to_tenant(definition, &object),
            );
        }
    }
    controller
}

pub struct ControllerContext<P> {
    pub reconciler: Reconciler<P>,
    pub gate: LeadershipGate,
}

pub async fn run_controller<P: ProviderLifecycle + 'static>(
    reconciler: Reconciler<P>,
    gate: LeadershipGate,
    stop: impl Future<Output = ()> + Send + Sync + 'static,
) -> Result<(), ControllerError> {
    let controller =
        controller(reconciler.client.clone(), &reconciler.config).graceful_shutdown_on(stop);
    let context = Arc::new(ControllerContext { reconciler, gate });
    controller.run(
        |tenant, context: Arc<ControllerContext<P>>| async move {
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
