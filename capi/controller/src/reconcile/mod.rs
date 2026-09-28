//! Direct-read reconciliation with durable-write barriers and narrowly bound SSA.

mod error;
mod local;
pub mod objects;
pub mod workers;

pub use crate::api::FINALIZER;
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
    docker::{BollardDockerClient, DockerClient},
    error::{ControllerError, ErrorClass},
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
#[rustfmt::skip]
#[derive(Clone, Debug)]
pub struct Config { pub supported_version: String }
#[rustfmt::skip]
impl Default for Config {
    fn default() -> Self { Self { supported_version: SUPPORTED_KUBERNETES_VERSION.into() } }
}
#[rustfmt::skip]
pub struct Reconciler<D = BollardDockerClient, A = local::LiveTenantAccess> {
    pub client: Client, pub provider: LocalProvider<D, A>, pub config: Config,
}
#[rustfmt::skip]
impl Reconciler { pub fn new(client: Client, config: Config, provider: LocalProvider) -> Self { Self { client, provider, config } } }

impl<D: DockerClient + Clone, A: TenantAccess> Reconciler<D, A> {
    #[rustfmt::skip]
    pub async fn reconcile_name(&self, name: &str) -> Result<Action, ReconcileError> {
        let Some(tenant) = Api::<Tenant>::all(self.client.clone()).get_opt(name).await? else { return Ok(Action::await_change()); };
        let spec = match canonical_spec(name, &tenant.spec, &self.config.supported_version)
            .and_then(|spec| crate::api::validate_provider_status(&spec, tenant.status.as_ref()).map(|()| spec))
        {
            Ok(spec) => spec,
            Err(error) => {
                status::update_status(self.client.clone(), &tenant, |status| {
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
        if !self.provider.supports(&spec.provider) { status::update_status(self.client.clone(), &tenant, |status| { status.provider = Some(TenantProviderStatus::Azure); status.phase = Some(TenantPhase::Failed); for condition in ["Accepted", "Ready"] { set_condition(status, &tenant, condition, false, "ProviderUnsupported", "Azure provider reconciliation is not implemented"); } Ok(()) }).await?; return Ok(Action::await_change()); }
        if tenant.metadata.deletion_timestamp.is_some() {
            if !tenant.finalizers().iter().any(|value| value == FINALIZER) {
                return Ok(Action::await_change());
            }
            return match crate::finalize::Finalizer::with_docker(
                self.client.clone(),
                self.provider.docker.clone(),
                &self.config.supported_version,
                self.provider.foundation.clone(),
            )
            .reconcile(&tenant)
            .await
            {
                Ok(action) => Ok(action),
                Err(error) if error.class() == ErrorClass::Conflict => {
                    Ok(Action::requeue(PROGRESS_INTERVAL))
                }
                Err(error) => self.failure(&tenant, error.into()).await,
            };
        }
        match self
            .provider
            .reconcile(&tenant, &spec)
            .await
        {
            Ok(action) => Ok(action),
            Err(error) if error.pending() => {
                progress(self.client.clone(), &tenant, DEPENDENCY_INTERVAL).await
            }
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
        } else if matches!(&error, ReconcileError::Foundation(_)) {
            (
                if deleting {
                    TenantPhase::Deleting
                } else {
                    TenantPhase::Failed
                },
                "FoundationInvalid",
            )
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
        if error.conflict() {
            return Ok(Action::requeue(PROGRESS_INTERVAL));
        }
        status::update_status(self.client.clone(), tenant, |status| {
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

pub fn controller(client: Client, _config: &Config) -> Controller<Tenant> {
    let mut controller = tenant_controller(client.clone());
    for definition in management::watched() {
        let resource = definition.api_resource();
        controller = controller.watches_with(
            Api::<DynamicObject>::all_with(client.clone(), &resource),
            resource,
            watcher::Config::default(),
            move |object| map_management_to_tenant(definition, &object),
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
