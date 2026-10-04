use kube::core::DynamicObject;
use serde_json::Value;

fn current(value: Option<&Value>, generation: i64) -> bool {
    match value {
        None => true,
        Some(value) => value
            .as_i64()
            .is_some_and(|observed| observed >= generation),
    }
}

pub fn object_ready(object: &DynamicObject) -> bool {
    if object.metadata.deletion_timestamp.is_some() {
        return false;
    }
    let generation = object.metadata.generation.unwrap_or_default();
    if !current(
        object.data.pointer("/status/observedGeneration"),
        generation,
    ) {
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
                    && current(condition.get("observedGeneration"), generation)
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
