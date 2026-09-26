use std::time::Duration;

use kube::runtime::controller::Action;

use crate::{
    allocation::AllocationError,
    docker::DockerError,
    error::ControllerError,
    foundation::FoundationError,
    ownership::OwnershipError,
    resources::BuildError,
    tenant_client::{TenantApiErrorClass, TenantClientError},
};

#[derive(Debug, thiserror::Error)]
pub enum ReconcileError {
    #[error(transparent)]
    Kube(#[from] kube::Error),
    #[error(transparent)]
    Runtime(#[from] ControllerError),
    #[error(transparent)]
    Allocation(#[from] AllocationError),
    #[error(transparent)]
    Docker(#[from] DockerError),
    #[error(transparent)]
    Ownership(#[from] OwnershipError),
    #[error(transparent)]
    Foundation(#[from] FoundationError),
    #[error("foundation API read failed: {0}")]
    FoundationRead(kube::Error),
    #[error("Tenant foundation mutation is disabled")]
    FoundationMutationDisabled,
    #[error(transparent)]
    TenantClient(#[from] TenantClientError),
    #[error(transparent)]
    Build(#[from] BuildError),
    #[error("ownership is invalid: {0}")]
    OwnershipInvalid(String),
    #[error("input is invalid: {0}")]
    InvalidInput(String),
    #[error("dependency is pending: {0}")]
    Pending(String),
    #[error("{reason}: {message}")]
    Degraded {
        reason: &'static str,
        message: String,
    },
}

impl ReconcileError {
    pub fn ownership_invalid(&self) -> bool {
        matches!(
            self,
            Self::OwnershipInvalid(_)
                | Self::Ownership(
                    OwnershipError::MissingUid(_)
                        | OwnershipError::Markers(_)
                        | OwnershipError::TenantOwner(_)
                        | OwnershipError::ProviderOwner(_)
                        | OwnershipError::OwnerChain(_)
                        | OwnershipError::Cycle
                        | OwnershipError::ApiVersion
                        | OwnershipError::MissingOwner(_)
                        | OwnershipError::DuplicateOwner(_)
                        | OwnershipError::OwnerUid
                        | OwnershipError::ClusterUid { .. }
                        | OwnershipError::SecretContract
                        | OwnershipError::SecretOwner
                )
                | Self::Docker(DockerError::Identity(_))
                | Self::Allocation(
                    AllocationError::Duplicate
                        | AllocationError::Claim(_)
                        | AllocationError::NameHeld
                        | AllocationError::StatusMismatch
                        | AllocationError::Missing
                )
                | Self::TenantClient(
                    TenantClientError::SecretOwnership | TenantClientError::SecretContract
                )
                | Self::Runtime(ControllerError::OwnershipInvalid(_))
        )
    }

    pub fn pending(&self) -> bool {
        matches!(
            self,
            Self::Pending(_)
                | Self::Ownership(OwnershipError::ProviderOwnerPending)
                | Self::Allocation(AllocationError::Exhausted)
        ) || matches!(self, Self::TenantClient(error) if error.class() == TenantApiErrorClass::Pending)
    }

    pub fn degraded_reason(&self) -> Option<&'static str> {
        match self {
            Self::Degraded { reason, .. } => Some(reason),
            Self::TenantClient(TenantClientError::BootstrapMismatch(_)) => {
                Some("BootstrapAccessMismatch")
            }
            _ => None,
        }
    }

    pub fn action(&self) -> Action {
        if self.degraded_reason().is_some() || self.ownership_invalid() {
            return Action::requeue(super::READY_INTERVAL);
        }
        if self.pending() {
            return Action::requeue(super::DEPENDENCY_INTERVAL);
        }
        match self {
            Self::Kube(error)
            | Self::FoundationRead(error)
            | Self::Allocation(AllocationError::Api(error)) => match error {
                kube::Error::Api(status) if status.code == 409 => {
                    Action::requeue(super::PROGRESS_INTERVAL)
                }
                kube::Error::Api(status) if status.code == 404 => {
                    Action::requeue(super::DEPENDENCY_INTERVAL)
                }
                _ => Action::requeue(Duration::from_secs(30)),
            },
            Self::Runtime(error) => error.requeue(5).action(),
            Self::TenantClient(error) if error.class() == TenantApiErrorClass::Conflict => {
                Action::requeue(super::PROGRESS_INTERVAL)
            }
            Self::Foundation(_)
            | Self::FoundationMutationDisabled
            | Self::Build(_)
            | Self::InvalidInput(_) => Action::requeue(super::READY_INTERVAL),
            _ => Action::requeue(Duration::from_secs(30)),
        }
    }
}
