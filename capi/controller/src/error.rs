use std::time::Duration;

use kube::runtime::controller::Action;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorClass {
    Conflict,
    Retryable,
    AwaitChange,
    Terminal,
    LeadershipLost,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Requeue {
    AwaitChange,
    After(Duration),
    Stop,
}

impl Requeue {
    #[must_use]
    pub const fn action(self) -> Action {
        match self {
            Self::After(duration) => Action::requeue(duration),
            Self::AwaitChange | Self::Stop => Action::await_change(),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ControllerError {
    #[error("Kubernetes API request failed: {0}")]
    Kube(#[from] kube::Error),
    #[error("status update for {resource} remained conflicted after {attempts} attempts")]
    StatusConflict { resource: String, attempts: usize },
    #[error("resource ownership is invalid: {0}")]
    OwnershipInvalid(String),
    #[error("input is invalid: {0}")]
    InvalidInput(String),
    #[error("dependency is not ready: {0}")]
    DependencyPending(String),
    #[error("leader election failed: {0}")]
    LeaderElection(#[from] kube_lease_manager::LeaseManagerError),
    #[error("leadership was lost")]
    LeadershipLost,
    #[error("runtime configuration is invalid: {0}")]
    Configuration(String),
    #[error("health server failed: {0}")]
    Health(#[from] std::io::Error),
    #[error("runtime task failed: {0}")]
    Task(String),
}

impl ControllerError {
    #[must_use]
    pub fn class(&self) -> ErrorClass {
        match self {
            Self::StatusConflict { .. } => ErrorClass::Conflict,
            Self::OwnershipInvalid(_) | Self::InvalidInput(_) | Self::Configuration(_) => {
                ErrorClass::Terminal
            }
            Self::DependencyPending(_) => ErrorClass::AwaitChange,
            Self::LeadershipLost => ErrorClass::LeadershipLost,
            Self::LeaderElection(_) | Self::Health(_) | Self::Task(_) => ErrorClass::Retryable,
            Self::Kube(kube::Error::Api(status)) if status.is_conflict() => ErrorClass::Conflict,
            Self::Kube(kube::Error::Api(status)) if status.is_not_found() => {
                ErrorClass::AwaitChange
            }
            Self::Kube(kube::Error::Api(status))
                if matches!(status.code, 408 | 429 | 500 | 502 | 503 | 504) =>
            {
                ErrorClass::Retryable
            }
            Self::Kube(kube::Error::Api(_)) => ErrorClass::Terminal,
            Self::Kube(_) => ErrorClass::Retryable,
        }
    }

    #[must_use]
    pub fn requeue(&self, attempt: u32) -> Requeue {
        match self.class() {
            ErrorClass::Conflict => Requeue::After(Duration::from_secs(1)),
            ErrorClass::Retryable => {
                let exponent = attempt.min(6);
                Requeue::After(Duration::from_secs((1_u64 << exponent).min(60)))
            }
            ErrorClass::AwaitChange => Requeue::AwaitChange,
            ErrorClass::Terminal | ErrorClass::LeadershipLost => Requeue::Stop,
        }
    }
}

#[cfg(test)]
mod tests {
    use kube::core::Status;

    use super::*;

    fn api_error(code: u16, reason: &str) -> ControllerError {
        ControllerError::Kube(kube::Error::Api(
            Status::failure("request failed", reason)
                .with_code(code)
                .boxed(),
        ))
    }

    #[test]
    fn classifies_conflict_retryable_pending_and_terminal_errors() {
        assert_eq!(api_error(409, "Conflict").class(), ErrorClass::Conflict);
        assert_eq!(
            api_error(429, "TooManyRequests").class(),
            ErrorClass::Retryable
        );
        assert_eq!(
            api_error(503, "ServiceUnavailable").class(),
            ErrorClass::Retryable
        );
        assert_eq!(api_error(404, "NotFound").class(), ErrorClass::AwaitChange);
        assert_eq!(api_error(403, "Forbidden").class(), ErrorClass::Terminal);
        assert_eq!(
            ControllerError::OwnershipInvalid("foreign UID".into()).class(),
            ErrorClass::Terminal
        );
        assert_eq!(
            ControllerError::DependencyPending("Cluster".into()).class(),
            ErrorClass::AwaitChange
        );
        assert_eq!(
            ControllerError::LeadershipLost.class(),
            ErrorClass::LeadershipLost
        );
    }

    #[test]
    fn retry_backoff_is_bounded() {
        assert_eq!(
            ControllerError::Task("temporary".into()).requeue(0),
            Requeue::After(Duration::from_secs(1))
        );
        assert_eq!(
            ControllerError::Task("temporary".into()).requeue(20),
            Requeue::After(Duration::from_secs(60))
        );
        assert_eq!(
            ControllerError::InvalidInput("bad".into()).requeue(0),
            Requeue::Stop
        );
    }
}
