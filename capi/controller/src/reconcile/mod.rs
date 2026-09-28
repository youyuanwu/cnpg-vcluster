//! Direct-read reconciliation with durable-write barriers and narrowly bound SSA.

mod azure;
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
        SUPPORTED_KUBERNETES_VERSION, Tenant, TenantPhase, TenantProviderStatus, canonical_spec,
    },
    error::ControllerError,
    foundation, management,
    readiness::{self, set_condition},
    runtime::{LeadershipGate, tenant_controller},
    sanitize, status,
};

pub const FOUNDATION_NAMESPACE: &str = "tenant-system";
pub const PROGRESS_INTERVAL: Duration = Duration::from_secs(1);
pub const DEPENDENCY_INTERVAL: Duration = Duration::from_secs(5);
pub const READY_INTERVAL: Duration = Duration::from_secs(300);
pub const STORAGE_CLASS: &str = "capi-hostpath";
#[derive(Clone, Debug)]
pub struct Config {
    pub supported_version: String,
    pub watch_management_resources: bool,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            supported_version: SUPPORTED_KUBERNETES_VERSION.into(),
            watch_management_resources: true,
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
            return self
                .failure(&tenant, ReconcileError::OwnershipInvalid(error.to_string()))
                .await;
        }
        if !self.provider.supports(&spec.provider) {
            return self.unsupported_provider(&tenant).await;
        }
        let deleting = tenant.metadata.deletion_timestamp.is_some();
        let result = if deleting {
            self.provider
                .finalize(&tenant, &self.config.supported_version)
                .await
        } else {
            self.provider.reconcile(&tenant, &spec).await
        };
        match result {
            Ok(action) => Ok(action),
            Err(error) if !deleting && error.pending() => {
                progress(self.client.clone(), &tenant, DEPENDENCY_INTERVAL).await
            }
            Err(error) => self.failure(&tenant, error).await,
        }
    }
    async fn unsupported_provider(&self, tenant: &Tenant) -> Result<Action, ReconcileError> {
        let has_finalizer = tenant.finalizers().iter().any(|value| value == FINALIZER);
        let (provider_status, provider_name) = match tenant.spec.provider {
            crate::api::TenantProviderSpec::Local { .. } => {
                (TenantProviderStatus::Local(Default::default()), "Local")
            }
            crate::api::TenantProviderSpec::Azure { .. } => (TenantProviderStatus::Azure, "Azure"),
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
        for definition in management::watched() {
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
