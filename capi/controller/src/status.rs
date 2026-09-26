use serde_json::{Value, json};

use crate::api::{Tenant, TenantStatus};
use crate::error::ControllerError;

#[derive(Clone, Debug, PartialEq)]
pub enum StatusUpdatePlan {
    Noop,
    Replace {
        resource_version: String,
        status: Box<TenantStatus>,
    },
}

impl StatusUpdatePlan {
    #[must_use]
    pub fn merge_patch(&self) -> Option<Value> {
        match self {
            Self::Noop => None,
            Self::Replace {
                resource_version,
                status,
            } => Some(json!({
                "metadata": {"resourceVersion": resource_version},
                "status": status,
            })),
        }
    }
}

pub fn plan_status_update<F>(
    current: &Tenant,
    mutate: F,
) -> Result<StatusUpdatePlan, ControllerError>
where
    F: FnOnce(&mut TenantStatus) -> Result<(), ControllerError>,
{
    let mut status = current.status.clone().unwrap_or_default();
    mutate(&mut status)?;
    if let Some(generation) = current.metadata.generation {
        status.observed_generation = Some(generation);
    }
    if current
        .status
        .as_ref()
        .is_some_and(|current| current == &status)
        || (current.status.is_none() && status == TenantStatus::default())
    {
        return Ok(StatusUpdatePlan::Noop);
    }
    let resource_version = current
        .metadata
        .resource_version
        .clone()
        .ok_or_else(|| ControllerError::InvalidInput("Tenant has no resourceVersion".into()))?;
    Ok(StatusUpdatePlan::Replace {
        resource_version,
        status: Box::new(status),
    })
}

#[cfg(test)]
mod tests {
    use kube::ResourceExt;

    use crate::api::{AllocationStatus, TenantPhase, TenantSpec};

    use super::*;

    fn tenant() -> Tenant {
        let mut tenant = Tenant::new(
            "tenant-a",
            TenantSpec {
                kubernetes_version: "1.36.4".into(),
                workers: 1,
                databases: 1,
            },
        );
        tenant.metadata.generation = Some(7);
        tenant.metadata.resource_version = Some("42".into());
        tenant.status = Some(TenantStatus {
            phase: Some(TenantPhase::Progressing),
            allocation: Some(AllocationStatus {
                slot_id: "slot-a".into(),
                endpoint: "10.0.0.8".into(),
                pod_cidr: "10.73.0.0/16".into(),
                service_cidr: "10.143.0.0/16".into(),
            }),
            foundation_hash: Some("foundation".into()),
            ..Default::default()
        });
        tenant
    }

    #[test]
    fn plan_preserves_unmodified_fields_and_sets_generation() {
        let tenant = tenant();
        let plan = plan_status_update(&tenant, |status| {
            status.phase = Some(TenantPhase::Ready);
            Ok(())
        })
        .unwrap();
        let StatusUpdatePlan::Replace {
            resource_version,
            status,
        } = plan
        else {
            panic!("expected replacement");
        };
        assert_eq!(resource_version, "42");
        assert_eq!(status.phase, Some(TenantPhase::Ready));
        assert_eq!(status.observed_generation, Some(7));
        assert_eq!(status.allocation.as_ref().unwrap().endpoint, "10.0.0.8");
        assert_eq!(status.foundation_hash.as_deref(), Some("foundation"));
    }

    #[test]
    fn unchanged_current_generation_is_a_noop() {
        let mut tenant = tenant();
        tenant.status.as_mut().unwrap().observed_generation = Some(7);
        assert_eq!(
            plan_status_update(&tenant, |_| Ok(())).unwrap(),
            StatusUpdatePlan::Noop
        );
    }

    #[test]
    fn stale_observed_generation_requires_an_update() {
        let mut tenant = tenant();
        tenant.status.as_mut().unwrap().observed_generation = Some(6);
        let plan = plan_status_update(&tenant, |_| Ok(())).unwrap();
        assert!(matches!(plan, StatusUpdatePlan::Replace { .. }));
        assert_eq!(
            plan.merge_patch().unwrap()["status"]["observedGeneration"],
            7
        );
        assert_eq!(tenant.name_any(), "tenant-a");
    }
}
