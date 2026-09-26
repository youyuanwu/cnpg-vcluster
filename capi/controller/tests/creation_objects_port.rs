//! Go parity: tenantresources_test.go and tenantresource_batch_test.go.
mod creation_support;
mod support;

use creation_support::*;
use kube::ResourceExt;
use serde_json::json;
use tenant_controller::{
    ownership::{RESOURCE_ANNOTATION, TENANT_UID_ANNOTATION},
    reconcile::{ReconcileError, objects::*},
};

#[tokio::test]
async fn static_existing_drift_is_never_patched_and_missing_objects_are_created_without_status() {
    let server = Server::default();
    let mut desired = object("v1", "ConfigMap", "default", "network", "network");
    desired.data = json!({"data":{"value":"desired"},"status":{"ignored":true}});
    let mut current = desired.clone();
    current.data = json!({"data":{"value":"drifted"}});
    server.insert(&path(&current), &current);
    let result = ensure_static(server.client(), &desired, identity())
        .await
        .unwrap();
    assert!(!result.created);
    assert_eq!(result.object.data["data"]["value"], "drifted");
    assert_eq!(server.calls().len(), 1);
    assert_eq!(server.calls()[0].method, "GET");
    server.0.lock().unwrap().objects.clear();
    let result = ensure_static(server.client(), &desired, identity())
        .await
        .unwrap();
    assert!(result.created);
    let calls = server.calls();
    assert_eq!(calls.last().unwrap().method, "POST");
    assert!(calls.last().unwrap().body.get("status").is_none());
}

#[tokio::test]
async fn foreign_static_marker_variants_stop_without_mutation() {
    for marker in identity().annotations("network").keys() {
        let server = Server::default();
        let desired = object("v1", "ConfigMap", "default", "network", "network");
        let mut current = desired.clone();
        current
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .insert(marker.clone(), "foreign".into());
        server.insert(&path(&current), current);
        let error = ensure_static(server.client(), &desired, identity())
            .await
            .unwrap_err();
        assert!(error.ownership_invalid(), "{marker}: {error}");
        assert_eq!(server.calls().len(), 1);
    }
}

#[tokio::test]
async fn already_exists_rereads_then_validates_owned_and_foreign_races() {
    for foreign in [false, true] {
        let server = Server::default();
        let desired = object("v1", "ConfigMap", "default", "network", "network");
        let mut current = desired.clone();
        if foreign {
            current
                .metadata
                .annotations
                .as_mut()
                .unwrap()
                .insert(TENANT_UID_ANNOTATION.into(), "successor".into());
        }
        server.insert(&path(&current), &current);
        server.respond("GET", &path(&current), 404, status(404, "NotFound"));
        let result = ensure_static(server.client(), &desired, identity()).await;
        if foreign {
            assert!(result.unwrap_err().ownership_invalid());
        } else {
            assert!(!result.unwrap().created);
        }
        let calls = server.calls();
        assert_eq!(
            calls
                .iter()
                .map(|call| call.method.as_str())
                .collect::<Vec<_>>(),
            ["GET", "POST", "GET"]
        );
        assert_eq!(
            server.get(&path(&current)),
            serde_json::to_value(current).unwrap()
        );
    }
}

#[tokio::test]
async fn dynamic_apply_is_uid_rv_bound_strips_status_and_uses_response_without_extra_get() {
    let server = Server::default();
    let mut desired = object(
        "postgresql.cnpg.io/v1",
        "Cluster",
        "database",
        "capi-postgres",
        "cnpg",
    );
    desired.data = json!({"spec":{"instances":3},"status":{"mustNotApply":true}});
    let mut current = desired.clone();
    current.data = json!({"spec":{"instances":1},"status":{"phase":"Cluster in healthy state","readyInstances":3}});
    server.insert(&path(&current), &current);
    let result = ensure_dynamic(server.client(), &desired, identity())
        .await
        .unwrap();
    assert!(!result.created);
    assert_eq!(result.object.data["spec"]["instances"], 3);
    assert_eq!(result.object.data["status"]["readyInstances"], 3);
    let calls = server.calls();
    assert_eq!(calls.len(), 2);
    let apply = &calls[1];
    assert_eq!(apply.method, "PATCH");
    assert!(apply.content_type.starts_with("application/apply-patch+"));
    assert!(apply.query.contains("force=true"));
    assert!(apply.query.contains(FIELD_MANAGER));
    assert_eq!(apply.body["metadata"]["uid"], current.uid().unwrap());
    assert_eq!(apply.body["metadata"]["resourceVersion"], "1");
    assert!(apply.body.get("status").is_none());
}

#[tokio::test]
async fn dynamic_apply_conflicts_invalid_fields_and_identity_replacements_are_distinct() {
    for code in [409, 422, 403, 500, 200] {
        let server = Server::default();
        let desired = object(
            "postgresql.cnpg.io/v1",
            "Cluster",
            "database",
            "capi-postgres",
            "cnpg",
        );
        server.insert(&path(&desired), &desired);
        let mut replacement = serde_json::to_value(&desired).unwrap();
        replacement["metadata"]["uid"] = json!("successor");
        server.respond(
            "PATCH",
            &path(&desired),
            code,
            if code == 200 {
                replacement
            } else {
                status(code, if code == 409 { "Conflict" } else { "Invalid" })
            },
        );
        let error = ensure_dynamic(server.client(), &desired, identity())
            .await
            .unwrap_err();
        match code {
            409 => assert_eq!(
                error.action(),
                kube::runtime::controller::Action::requeue(std::time::Duration::from_secs(1))
            ),
            422 => assert_eq!(error.degraded_reason(), Some("ImmutableDrift")),
            200 => assert!(error.ownership_invalid()),
            _ => assert!(matches!(error, ReconcileError::Kube(_))),
        }
        assert_eq!(server.calls().len(), 2);
    }
}

#[tokio::test]
async fn dynamic_objects_without_uid_or_resource_version_never_receive_apply() {
    for uid in [true, false] {
        let server = Server::default();
        let desired = object(
            "postgresql.cnpg.io/v1",
            "Cluster",
            "database",
            "capi-postgres",
            "cnpg",
        );
        let mut current = desired.clone();
        if uid {
            current.metadata.uid = None;
        } else {
            current.metadata.resource_version = None;
        }
        server.insert(&path(&current), &current);
        assert!(
            ensure_dynamic(server.client(), &desired, identity())
                .await
                .unwrap_err()
                .ownership_invalid()
        );
        assert_eq!(server.calls().len(), 1);
    }
}

#[tokio::test]
async fn management_cluster_never_recreates_a_bound_root_and_refuses_uid_or_owner_changes() {
    let mut tenant = tenant();
    tenant.status = Some(tenant_controller::api::TenantStatus {
        cluster_uid: Some("tenant-a-uid".into()),
        ..Default::default()
    });
    let desired = object(
        "cluster.x-k8s.io/v1beta2",
        "Cluster",
        "tenant-a",
        "tenant-a",
        "cluster",
    );
    let server = Server::default();
    assert_eq!(
        ensure_management(server.client(), &desired, &tenant, identity(), &mut vec![])
            .await
            .unwrap_err()
            .degraded_reason(),
        Some("RootClusterMissing")
    );
    assert_eq!(server.calls().len(), 1);
    for field in ["uid", "ownerReferences", "labels"] {
        let server = Server::default();
        let mut current = serde_json::to_value(&desired).unwrap();
        match field {
            "uid" => current["metadata"]["uid"] = json!("successor"),
            "labels" => current["metadata"]["labels"] = json!({}),
            _ => {
                current["metadata"]["ownerReferences"] =
                    json!([{"apiVersion":"v1","kind":"Namespace","name":"foreign","uid":"foreign"}])
            }
        }
        server.insert(&path(&desired), current);
        assert!(
            ensure_management(server.client(), &desired, &tenant, identity(), &mut vec![])
                .await
                .unwrap_err()
                .ownership_invalid()
        );
        assert_eq!(server.calls().len(), 1);
    }
}

#[tokio::test]
async fn namespace_owned_content_is_not_repaired_and_deleting_namespace_blocks() {
    let server = Server::default();
    let mut desired = object("v1", "Namespace", "", "tenant-a", "namespace");
    desired.data = json!({"spec":{"finalizers":["kubernetes"]}});
    server.insert(&path(&desired), &desired);
    assert!(
        !ensure_namespace(server.client(), &desired, identity())
            .await
            .unwrap()
            .created
    );
    assert_eq!(server.calls().len(), 1);
    let mut deleting = serde_json::to_value(&desired).unwrap();
    deleting["metadata"]["deletionTimestamp"] = json!("2026-09-25T00:00:00Z");
    server.insert(&path(&desired), deleting);
    assert!(
        ensure_namespace(server.client(), &desired, identity())
            .await
            .unwrap_err()
            .pending()
    );
}

#[tokio::test]
async fn whole_static_batch_creates_all_objects_then_only_reads() {
    let server = Server::default();
    let objects = (0..43)
        .map(|index| {
            object(
                "v1",
                "ConfigMap",
                "default",
                &format!("config-{index}"),
                "network",
            )
        })
        .collect::<Vec<_>>();
    let first = ensure_batch(server.client(), &objects, identity())
        .await
        .unwrap();
    assert!(first.created && !first.pending);
    assert_eq!(server.calls().len(), 86);
    server.take_calls();
    let second = ensure_batch(server.client(), &objects, identity())
        .await
        .unwrap();
    assert!(!second.created && !second.pending);
    assert_eq!(server.calls().len(), 43);
    assert!(server.calls().iter().all(|call| call.method == "GET"));
}

#[tokio::test]
async fn batch_still_validates_foreign_objects_after_an_earlier_creation() {
    let server = Server::default();
    let objects = ["first", "foreign", "last"]
        .map(|name| object("v1", "ConfigMap", "default", name, "network"));
    let mut foreign = objects[1].clone();
    foreign
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .insert(RESOURCE_ANNOTATION.into(), "other".into());
    server.insert(&path(&foreign), foreign);
    assert!(
        ensure_batch(server.client(), &objects, identity())
            .await
            .unwrap_err()
            .ownership_invalid()
    );
    assert_eq!(
        server
            .calls()
            .iter()
            .map(|call| call.method.as_str())
            .collect::<Vec<_>>(),
        ["GET", "POST", "GET"]
    );
}

fn crd() -> kube::core::DynamicObject {
    let mut crd = object(
        "apiextensions.k8s.io/v1",
        "CustomResourceDefinition",
        "",
        "widgets.example.io",
        "network",
    );
    crd.data = json!({"spec":{"group":"example.io","names":{"kind":"Widget","plural":"widgets"},"scope":"Namespaced",
        "versions":[{"name":"v1","served":true},{"name":"v2","served":true}]}});
    crd
}

#[tokio::test]
async fn crd_establishment_all_served_discovery_and_dependency_order_form_one_barrier() {
    let server = Server::default();
    let crd = crd();
    let namespace = object("v1", "Namespace", "", "workload", "network");
    let config = object("v1", "ConfigMap", "workload", "config", "network");
    let workload = object("apps/v1", "Deployment", "workload", "workload", "network");
    let objects = vec![
        workload.clone(),
        config.clone(),
        namespace.clone(),
        crd.clone(),
    ];
    let first = ensure_batch(server.client(), &objects, identity())
        .await
        .unwrap();
    assert!(first.created && first.pending);
    assert_eq!(
        server
            .calls()
            .iter()
            .filter(|call| call.method == "POST")
            .count(),
        2
    );
    let mut current = server.get(&path(&crd));
    current["status"] = json!({"conditions":[{"type":"Established","status":"True"}]});
    server.insert(&path(&crd), current);
    server.take_calls();
    assert!(
        ensure_batch(server.client(), &objects, identity())
            .await
            .unwrap()
            .pending
    );
    assert!(server.calls().iter().all(|call| call.method == "GET"));
    for version in ["v1", "v2"] {
        server.insert(&format!("/apis/example.io/{version}"), json!({"groupVersion":format!("example.io/{version}"),
            "resources":[{"name":"widgets","kind":"Widget","namespaced":true,"verbs":["get","create"]}]}));
        if version == "v1" {
            assert!(
                ensure_batch(server.client(), &objects, identity())
                    .await
                    .unwrap()
                    .pending
            );
        }
    }
    server.take_calls();
    let result = ensure_batch(server.client(), &objects, identity())
        .await
        .unwrap();
    assert!(result.created && !result.pending);
    let creates: Vec<_> = server
        .calls()
        .into_iter()
        .filter(|call| call.method == "POST")
        .map(|call| call.body["metadata"]["name"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(creates, ["config", "workload"]);
    assert_eq!(
        server
            .calls()
            .iter()
            .filter(|call| call.path == path(&crd))
            .count(),
        1,
        "CRD is not reread"
    );
}

#[tokio::test]
async fn foreign_crd_is_rejected_before_any_dependent_write() {
    let server = Server::default();
    let crd = crd();
    let mut foreign = crd.clone();
    foreign.metadata.annotations = None;
    server.insert(&path(&foreign), foreign);
    let objects = vec![
        object("v1", "ConfigMap", "default", "config", "network"),
        crd,
    ];
    assert!(
        ensure_batch(server.client(), &objects, identity())
            .await
            .unwrap_err()
            .ownership_invalid()
    );
    assert_eq!(server.calls().len(), 1);
}

#[tokio::test]
async fn shared_server_can_replace_an_object_before_a_live_read() {
    let server = Server::default();
    let desired = object("v1", "ConfigMap", "default", "network", "network");
    server.insert(&path(&desired), &desired);
    let mut replacement = desired.clone();
    replacement.metadata.uid = Some("replacement-uid".into());
    replacement.metadata.annotations = None;
    server.replace_on(
        "GET",
        &path(&desired),
        Some(serde_json::to_value(replacement).unwrap()),
    );
    assert!(
        ensure_static(server.client(), &desired, identity())
            .await
            .unwrap_err()
            .ownership_invalid()
    );
    assert_eq!(server.calls().len(), 1);
}
