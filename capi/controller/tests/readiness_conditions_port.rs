//! Go parity: readiness_test.go, tenantresources_test.go, status_test.go,
//! reconcile_cnpg_test.go. No Pod/PVC readiness inventory is required.
mod creation_support;

use creation_support::*;
use serde_json::json;
use tenant_controller::{
    api::{TenantPhase, TenantStatus},
    readiness::*,
    reconcile::{READY_INTERVAL, update_status},
};

#[test]
fn aggregate_management_condition_matrix() {
    for (observed, condition_generation, expected, error) in [
        (json!(2), json!(2), true, false),
        (json!(1), json!(2), false, false),
        (json!(2), json!(1), false, false),
        (json!(2), serde_json::Value::Null, true, false),
        (serde_json::Value::Null, json!(2), true, false),
        (
            serde_json::Value::Null,
            serde_json::Value::Null,
            false,
            false,
        ),
        (json!("bad"), json!(2), false, true),
        (json!(2), json!("bad"), false, true),
    ] {
        let mut cluster = object(
            "cluster.x-k8s.io/v1beta2",
            "Cluster",
            "tenant-a",
            "tenant-a",
            "cluster",
        );
        cluster.data = json!({"status":{"conditions":[
            {"type":"ControlPlaneReady","status":"False","observedGeneration":2},
            {"type":"ControlPlaneAvailable","status":"True"}
        ]}});
        if !observed.is_null() {
            cluster.data["status"]["observedGeneration"] = observed;
        }
        if !condition_generation.is_null() {
            cluster.data["status"]["conditions"][1]["observedGeneration"] = condition_generation;
        }
        let actual =
            management_conditions_ready(&cluster, &["ControlPlaneReady", "ControlPlaneAvailable"]);
        if error {
            assert!(actual.is_err());
        } else {
            assert_eq!(actual.unwrap(), expected);
        }
        if !error {
            assert!(!management_conditions_ready(&cluster, &["Available"]).unwrap());
        }
    }
}

#[test]
fn workload_and_generic_ready_reject_stale_malformed_and_deleting_observations() {
    for kind in ["DaemonSet", "Deployment"] {
        let mut workload = object("apps/v1", kind, "kube-system", "workload", "network");
        workload.data = json!({"spec":{"replicas":1},"status":{"observedGeneration":2,"desiredNumberScheduled":1,"numberAvailable":1,"availableReplicas":1}});
        assert!(workload_available(&workload));
        for observed in [json!(1), json!("2"), serde_json::Value::Null] {
            workload.data["status"]["observedGeneration"] = observed;
            assert!(!workload_available(&workload));
        }
    }
    let mut node = object("v1", "Node", "", "worker-a", "machine");
    node.data = json!({"status":{"conditions":[{"type":"Ready","status":"True"}]}});
    assert!(
        node_ready(&node),
        "Node Ready does not require an absent generation"
    );
    node.data["status"]["conditions"][0]["observedGeneration"] = json!(1);
    assert!(!node_ready(&node));
    node.data["status"]["conditions"][0]["observedGeneration"] = json!("bad");
    assert!(!node_ready(&node));
}

#[test]
fn database_health_requires_only_exact_phase_and_instance_count() {
    for count in 1..=3 {
        for phase in ["", "Creating a new replica", "Cluster in healthy state"] {
            for ready in [
                json!(0),
                json!(count - 1),
                json!(count),
                json!(count + 1),
                json!(count.to_string()),
                serde_json::Value::Null,
            ] {
                let mut cluster = object(
                    "postgresql.cnpg.io/v1",
                    "Cluster",
                    "database",
                    "capi-postgres",
                    "cnpg",
                );
                cluster.data = json!({"status":{"phase":phase,"readyInstances":ready}});
                assert_eq!(
                    database_ready(&cluster, count),
                    phase == "Cluster in healthy state" && ready.as_i64() == Some(i64::from(count))
                );
            }
        }
    }
}

#[test]
fn provider_ready_rejects_stale_explicit_generation_but_allows_missing_evidence() {
    for (api, kind) in [
        ("cluster.x-k8s.io/v1beta2", "Machine"),
        ("infrastructure.cluster.x-k8s.io/v1beta2", "DevMachine"),
    ] {
        for (observed, condition_generation, expected) in [
            (None, None, true),
            (Some(json!(2)), None, true),
            (None, Some(json!(2)), true),
            (Some(json!(2)), Some(json!(2)), true),
            (Some(json!(1)), Some(json!(2)), false),
            (Some(json!(2)), Some(json!(1)), false),
            (Some(json!(-1)), None, false),
            (None, Some(json!(-1)), false),
            (Some(json!("2")), Some(json!(2)), false),
            (Some(json!(2)), Some(json!("2")), false),
            (Some(json!(null)), Some(json!(2)), false),
            (Some(json!(2)), Some(json!(null)), false),
            (Some(json!(2.5)), Some(json!(2)), false),
        ] {
            let mut object = object(api, kind, "tenant-a", "resource", "machine");
            object.data = json!({"status":{
                "phase":"Cluster in healthy state","readyInstances":1,
                "conditions":[{"type":"Ready","status":"True"}]
            }});
            if let Some(observed) = observed {
                object.data["status"]["observedGeneration"] = observed;
            }
            if let Some(generation) = condition_generation {
                object.data["status"]["conditions"][0]["observedGeneration"] = generation;
            }
            assert_eq!(object_ready(&object), expected, "{kind}: {}", object.data);
        }
        for mutation in [
            "missing-conditions",
            "malformed-conditions",
            "false",
            "unknown",
            "other-type",
            "deleting",
            "missing-generation",
        ] {
            let mut object = object(api, kind, "tenant-a", "resource", "machine");
            object.data = json!({"status":{
                "phase":"Cluster in healthy state","readyInstances":1,"observedGeneration":2,
                "conditions":[{"type":"Ready","status":"True","observedGeneration":2}]
            }});
            match mutation {
                "missing-conditions" => {
                    object.data["status"]
                        .as_object_mut()
                        .unwrap()
                        .remove("conditions");
                }
                "malformed-conditions" => object.data["status"]["conditions"] = json!({}),
                "false" => object.data["status"]["conditions"][0]["status"] = json!("False"),
                "unknown" => object.data["status"]["conditions"][0]["status"] = json!("Unknown"),
                "other-type" => object.data["status"]["conditions"][0]["type"] = json!("Available"),
                "deleting" => {
                    object.metadata.deletion_timestamp =
                        Some(serde_json::from_value(json!("2026-09-25T00:00:00Z")).unwrap())
                }
                "missing-generation" => object.metadata.generation = None,
                _ => unreachable!(),
            }
            assert_eq!(
                object_ready(&object),
                mutation == "missing-generation",
                "{kind}: {mutation}"
            );
        }
    }
    let mut node = object("v1", "Node", "", "node", "machine");
    node.metadata.generation = None;
    node.data = json!({"status":{"conditions":[{"type":"Ready","status":"True"}]}});
    assert!(node_ready(&node));
    node.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-25T00:00:00Z")).unwrap());
    assert!(!node_ready(&node));
}

#[test]
fn aggregate_conditions_are_current_and_established_recovery_remains_degraded() {
    let mut tenant = tenant();
    let mut status = TenantStatus::default();
    let healthy = Components {
        control_plane: true,
        workers: true,
        network: true,
        storage: true,
        database: true,
    };
    healthy.publish(&mut status, &tenant);
    assert_eq!(status.phase, Some(TenantPhase::Ready));
    assert!(status.conditions.iter().all(|condition| condition.observed_generation == Some(2) && condition.status == "True"));
    let transition = status
        .conditions
        .iter()
        .find(|condition| condition.type_ == "Ready")
        .unwrap()
        .last_transition_time
        .clone();
    healthy.publish(&mut status, &tenant);
    assert_eq!(
        status
            .conditions
            .iter()
            .find(|condition| condition.type_ == "Ready")
            .unwrap()
            .last_transition_time,
        transition
    );
    tenant.status = Some(status.clone());
    progress_status(&mut status, &tenant);
    assert_eq!(status.phase, Some(TenantPhase::Degraded));
    assert_eq!(
        status
            .conditions
            .iter()
            .find(|condition| condition.type_ == "Ready")
            .unwrap()
            .reason,
        "Recovering"
    );
    for index in 0..5 {
        let mut components = healthy;
        match index {
            0 => components.control_plane = false,
            1 => components.workers = false,
            2 => components.network = false,
            3 => components.storage = false,
            _ => components.database = false,
        }
        components.publish(&mut status, &tenant);
        assert_eq!(status.phase, Some(TenantPhase::Degraded));
        assert!(!components.ready());
    }
    assert_eq!(READY_INTERVAL.as_secs(), 300);
}

const TENANT: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants/tenant-a";

#[tokio::test]
async fn status_conflict_preserves_concurrent_fields_without_unneeded_initial_get() {
    let server = Server::default();
    let tenant = tenant();
    let mut concurrent = serde_json::to_value(&tenant).unwrap();
    concurrent["metadata"]["resourceVersion"] = json!("2");
    concurrent["status"] = json!({"foundationHash":"concurrent","conditions":[{
        "type":"External","status":"True","reason":"External","message":"preserve",
        "lastTransitionTime":"2026-09-25T00:00:00Z","observedGeneration":2
    }]});
    server.insert(TENANT, concurrent);
    update_status(server.client(), &tenant, |status| {
        status.phase = Some(TenantPhase::Progressing);
        Ok(())
    })
    .await
    .unwrap();
    let calls = server.calls();
    assert_eq!(
        calls
            .iter()
            .map(|call| call.method.as_str())
            .collect::<Vec<_>>(),
        ["PATCH", "GET", "PATCH"]
    );
    let status = server.get(TENANT)["status"].clone();
    assert_eq!(status["foundationHash"], "concurrent");
    assert_eq!(status["conditions"][0]["type"], "External");
    assert_eq!(status["observedGeneration"], 2);
}

#[tokio::test]
async fn status_conflict_never_writes_a_same_name_replacement_or_new_generation() {
    for replacement in [true, false] {
        let server = Server::default();
        let tenant = tenant();
        let mut current = serde_json::to_value(&tenant).unwrap();
        current["metadata"]["resourceVersion"] = json!("2");
        if replacement {
            current["metadata"]["uid"] = json!("successor");
        } else {
            current["metadata"]["generation"] = json!(3);
        }
        server.insert(TENANT, current);
        let error = update_status(server.client(), &tenant, |status| {
            status.phase = Some(TenantPhase::Ready);
            Ok(())
        })
        .await
        .unwrap_err();
        assert!(error.ownership_invalid());
        assert_eq!(server.calls().len(), 2);
    }
}

#[tokio::test]
async fn unchanged_status_is_a_true_zero_request_noop_and_messages_are_sanitized() {
    let server = Server::default();
    let mut tenant = tenant();
    tenant.status = Some(TenantStatus {
        observed_generation: Some(2),
        ..Default::default()
    });
    update_status(server.client(), &tenant, |_| Ok(()))
        .await
        .unwrap();
    assert!(server.calls().is_empty());
    let mut status = TenantStatus::default();
    set_condition(
        &mut status,
        &tenant,
        "Ready",
        false,
        "Failure",
        "password=super-secret",
    );
    assert!(!status.conditions[0].message.contains("super-secret"));
}
