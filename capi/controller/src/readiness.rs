use k8s_openapi::apimachinery::pkg::apis::meta::v1::{Condition, Time};
use kube::core::DynamicObject;
use serde_json::Value;

use crate::{
    api::{Tenant, TenantPhase, TenantStatus},
    reconcile::ReconcileError,
    sanitize,
};

fn integer(value: Option<&Value>) -> Result<Option<i64>, ReconcileError> {
    value
        .map(|value| {
            value
                .as_i64()
                .ok_or_else(|| ReconcileError::InvalidInput("malformed observedGeneration".into()))
        })
        .transpose()
}

/// ControlPlaneReady and ControlPlaneAvailable are alternatives; Available is
/// deliberately a separate final availability gate.
pub fn management_conditions_ready(
    object: &DynamicObject,
    condition_types: &[&str],
) -> Result<bool, ReconcileError> {
    if object.metadata.deletion_timestamp.is_some() {
        return Ok(false);
    }
    let generation = object.metadata.generation.unwrap_or_default();
    let observed = integer(object.data.pointer("/status/observedGeneration"))?;
    if observed.is_some_and(|observed| observed < generation) {
        return Ok(false);
    }
    let Some(conditions) = object.data.pointer("/status/conditions") else {
        return Ok(false);
    };
    let conditions = conditions
        .as_array()
        .ok_or_else(|| ReconcileError::InvalidInput("malformed management conditions".into()))?;
    for condition in conditions {
        if condition
            .get("type")
            .and_then(Value::as_str)
            .is_none_or(|kind| !condition_types.contains(&kind))
            || condition.get("status").and_then(Value::as_str) != Some("True")
        {
            continue;
        }
        let current = integer(condition.get("observedGeneration"))?;
        if current
            .or(observed)
            .is_some_and(|value| value >= generation)
        {
            return Ok(true);
        }
    }
    Ok(false)
}

pub fn object_ready(object: &DynamicObject) -> bool {
    if object.metadata.deletion_timestamp.is_some() {
        return false;
    }
    let generation = object.metadata.generation.unwrap_or_default();
    let Ok(observed) = integer(object.data.pointer("/status/observedGeneration")) else {
        return false;
    };
    if observed.is_some_and(|value| value < generation) {
        return false;
    }
    object
        .data
        .pointer("/status/conditions")
        .and_then(Value::as_array)
        .is_some_and(|conditions| {
            conditions.iter().any(|condition| {
                condition["type"] == "Ready"
                    && condition["status"] == "True"
                    && integer(condition.get("observedGeneration"))
                        .is_ok_and(|value| value.is_none_or(|value| value >= generation))
            })
        })
}

pub fn node_ready(object: &DynamicObject) -> bool {
    object_ready(object)
}

pub fn workload_available(object: &DynamicObject) -> bool {
    if object.metadata.deletion_timestamp.is_some()
        || object
            .data
            .pointer("/status/observedGeneration")
            .and_then(Value::as_i64)
            .is_none_or(|value| value < object.metadata.generation.unwrap_or_default())
    {
        return false;
    }
    let daemon_set = object
        .types
        .as_ref()
        .is_some_and(|types| types.kind == "DaemonSet");
    let desired = object
        .data
        .pointer(if daemon_set {
            "/status/desiredNumberScheduled"
        } else {
            "/spec/replicas"
        })
        .and_then(Value::as_i64);
    let available = object
        .data
        .pointer(if daemon_set {
            "/status/numberAvailable"
        } else {
            "/status/availableReplicas"
        })
        .and_then(Value::as_i64);
    desired.is_some_and(|desired| desired > 0 && Some(desired) == available)
}

pub fn database_ready(object: &DynamicObject, instances: i32) -> bool {
    object.metadata.deletion_timestamp.is_none()
        && object.data.pointer("/status/phase").and_then(Value::as_str)
            == Some("Cluster in healthy state")
        && object
            .data
            .pointer("/status/readyInstances")
            .and_then(Value::as_i64)
            == Some(i64::from(instances))
}

pub fn set_condition(
    status: &mut TenantStatus,
    tenant: &Tenant,
    kind: &str,
    ready: bool,
    reason: &str,
    message: &str,
) {
    let value = if ready { "True" } else { "False" };
    let previous = status
        .conditions
        .iter()
        .position(|condition| condition.type_ == kind);
    let transition = previous
        .filter(|index| status.conditions[*index].status == value)
        .map(|index| status.conditions[index].last_transition_time.clone())
        .unwrap_or_else(|| Time(k8s_openapi::jiff::Timestamp::now()));
    let condition = Condition {
        type_: kind.into(),
        status: value.into(),
        reason: reason.into(),
        message: sanitize::text(message),
        observed_generation: tenant.metadata.generation,
        last_transition_time: transition,
    };
    if let Some(index) = previous {
        status.conditions[index] = condition;
    } else {
        status.conditions.push(condition);
    }
}

pub fn initialize_status(status: &mut TenantStatus, tenant: &Tenant) {
    status.observed_generation = tenant.metadata.generation;
    if matches!(status.phase, None | Some(TenantPhase::Pending)) {
        status.phase = Some(TenantPhase::Progressing);
    }
    for (kind, message) in [
        ("Accepted", "Tenant specification is accepted"),
        ("FoundationReady", "Tenant foundation identity is current"),
        ("OwnershipValid", "Observed Tenant ownership is valid"),
    ] {
        set_condition(status, tenant, kind, true, kind, message);
    }
}

pub fn progress_status(status: &mut TenantStatus, tenant: &Tenant) {
    initialize_status(status, tenant);
    let established = tenant.status.as_ref().is_some_and(|status| {
        status.conditions.iter().any(|condition| {
            condition.type_ == "DatabaseReady"
                && condition.observed_generation == tenant.metadata.generation
        })
    });
    let reason = if established {
        "Recovering"
    } else {
        "Progressing"
    };
    status.phase = Some(if established {
        TenantPhase::Degraded
    } else {
        TenantPhase::Progressing
    });
    set_condition(
        status,
        tenant,
        "Ready",
        false,
        reason,
        "Tenant reconciliation is progressing",
    );
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Components {
    pub control_plane: bool,
    pub workers: bool,
    pub network: bool,
    pub storage: bool,
    pub database: bool,
}

impl Components {
    pub fn ready(self) -> bool {
        self.control_plane && self.workers && self.network && self.storage && self.database
    }

    pub fn publish(self, status: &mut TenantStatus, tenant: &Tenant) {
        initialize_status(status, tenant);
        for (kind, ready) in [
            ("ControlPlaneReady", self.control_plane),
            ("WorkersReady", self.workers),
            ("NetworkReady", self.network),
            ("StorageReady", self.storage),
            ("DatabaseReady", self.database),
        ] {
            set_condition(
                status,
                tenant,
                kind,
                ready,
                if ready { kind } else { "NotReady" },
                &format!("{kind} is {ready}"),
            );
        }
        status.phase = Some(if self.ready() {
            TenantPhase::Ready
        } else {
            TenantPhase::Degraded
        });
        set_condition(
            status,
            tenant,
            "Ready",
            self.ready(),
            if self.ready() {
                "Ready"
            } else {
                "ComponentsNotReady"
            },
            if self.ready() {
                "Tenant components are ready"
            } else {
                "One or more Tenant components are not ready"
            },
        );
    }
}
