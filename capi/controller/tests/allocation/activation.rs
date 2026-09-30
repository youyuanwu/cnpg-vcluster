use crate::creation_support::{FakeDocker, tenant};
use crate::support::Server;
use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::ConfigMap;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use kube::{
    Api,
    api::{Patch, PatchParams, PostParams},
};
use serde_json::json;
use tenant_controller::{
    activation::{STATE_NAME, TICKET_NAME, admit},
    management::{InventoryPolicy, MANAGEMENT_RESOURCES},
};

const CONFIG_MAPS: &str = "/api/v1/namespaces/tenant-system/configmaps";
const STATE: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-controller-state";
const TICKET: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-controller-activation";
const TENANTS: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha3/tenants";
const NAMESPACES: &str = "/api/v1/namespaces";
const SECRETS: &str = "/api/v1/secrets";
const LEASES: &str = "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases";

fn config_map(name: &str, data: &[(&str, &str)]) -> ConfigMap {
    ConfigMap {
        metadata: ObjectMeta {
            name: Some(name.into()),
            namespace: Some("tenant-system".into()),
            uid: Some(format!("{name}-uid")),
            resource_version: Some("1".into()),
            ..Default::default()
        },
        data: Some(
            data.iter()
                .map(|(key, value)| ((*key).into(), (*value).into()))
                .collect(),
        ),
        ..Default::default()
    }
}

fn clean_server() -> Server {
    let server = Server::default();
    server.allow_list(TENANTS);
    for resource in MANAGEMENT_RESOURCES {
        let (group, version) = resource
            .api_version
            .split_once('/')
            .unwrap_or(("", resource.api_version));
        let base = if group.is_empty() {
            format!("/api/{version}")
        } else {
            format!("/apis/{group}/{version}")
        };
        let path = match resource.inventory_namespace {
            Some(namespace) => format!("{base}/namespaces/{namespace}/{}", resource.plural),
            None => format!("{base}/{}", resource.plural),
        };
        server.allow_typed_list(&path, resource.api_version, resource.kind);
    }
    server
}

fn ticket(hash: &str, token: &str) -> ConfigMap {
    ticket_with_previous(hash, token, "")
}

fn ticket_with_previous(hash: &str, token: &str, previous: &str) -> ConfigMap {
    config_map(
        TICKET_NAME,
        &[
            ("configurationHash", hash),
            ("previousConfigurationHash", previous),
            ("token", token),
            ("hostClean", "true"),
            ("createdAt", &chrono::Utc::now().to_rfc3339()),
        ],
    )
}

#[tokio::test]
async fn same_identity_restart_needs_no_ticket_or_inventory() {
    let server = Server::default();
    server.insert(
        STATE,
        config_map(STATE_NAME, &[("configurationHash", "hash-a")]),
    );
    let docker = FakeDocker::default();
    admit(server.client(), &docker, "hash-a", "", false)
        .await
        .unwrap();
    assert_eq!(server.calls().len(), 1);
    assert!(docker.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn rollback_lock_blocks_same_and_changed_identity_admission() {
    for hash in ["hash-a", "hash-b"] {
        let server = clean_server();
        server.insert(
            STATE,
            config_map(
                STATE_NAME,
                &[
                    ("configurationHash", "hash-a"),
                    ("rollbackToken", "rollback-a"),
                ],
            ),
        );
        if hash == "hash-b" {
            server.insert(TICKET, ticket("hash-b", "token-b"));
        }
        let error = admit(
            server.client(),
            &FakeDocker::default(),
            hash,
            if hash == "hash-b" { "token-b" } else { "" },
            true,
        )
        .await
        .unwrap_err();
        assert!(matches!(
            error,
            tenant_controller::error::ControllerError::Configuration(_)
        ));
    }
}

#[test]
fn typed_catalog_policies_match_activation_contract() {
    for (kind, namespaced, role, policy, exemption) in [
        (
            "Namespace",
            false,
            "namespace",
            InventoryPolicy::TenantMarkers,
            "management-infrastructure",
        ),
        (
            "Secret",
            true,
            "tenant-kubeconfig",
            InventoryPolicy::TenantMarkersOrKamajiOwner,
            "controller-installation-secrets",
        ),
        (
            "Lease",
            true,
            "allocation-lease",
            InventoryPolicy::AllocationMarkers,
            "controller-leader-election",
        ),
    ] {
        let resource = MANAGEMENT_RESOURCES
            .iter()
            .find(|resource| resource.kind == kind)
            .unwrap();
        assert_eq!(resource.namespaced, namespaced);
        assert_eq!(resource.role, role);
        assert_eq!(resource.inventory_policy, policy);
        assert_eq!(resource.exemptions, [exemption]);
    }
}

#[tokio::test]
async fn changed_identity_requires_ticket_and_atomically_accepts_clean_state() {
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    let docker = FakeDocker::default();
    docker
        .containers
        .lock()
        .unwrap()
        .push(tenant_controller::docker::DockerContainer {
            id: "registry".into(),
            name: "offline-registry".into(),
            state: "running".into(),
            labels: [("cnpg-vcluster.capi/role".into(), "offline-registry".into())].into(),
            networks: Default::default(),
            network_addresses: Default::default(),
        });
    admit(server.client(), &docker, "hash-b", "token-b", true)
        .await
        .unwrap();
    let state: ConfigMap = serde_json::from_value(server.get(STATE)).unwrap();
    assert_eq!(state.data.unwrap()["configurationHash"], "hash-b");
    assert!(!server.0.lock().unwrap().objects.contains_key(TICKET));
    assert_eq!(*docker.calls.lock().unwrap(), ["containers", "volumes"]);
    assert!(
        server
            .calls()
            .iter()
            .any(|call| call.method == "POST" && call.path == CONFIG_MAPS)
    );
}

#[tokio::test]
async fn existing_state_is_replaced_with_uid_preserved_and_conflicts_fail_closed() {
    for conflict in [false, true] {
        let server = clean_server();
        server.insert(
            STATE,
            config_map(STATE_NAME, &[("configurationHash", "hash-a")]),
        );
        server.insert(TICKET, ticket_with_previous("hash-b", "token-b", "hash-a"));
        if conflict {
            server.respond(
                "PUT",
                STATE,
                409,
                crate::support::kube::status(409, "Conflict"),
            );
        }
        let result = admit(
            server.client(),
            &FakeDocker::default(),
            "hash-b",
            "token-b",
            true,
        )
        .await;
        let state: ConfigMap = serde_json::from_value(server.get(STATE)).unwrap();
        if conflict {
            assert!(result.is_err());
            assert_eq!(state.data.unwrap()["configurationHash"], "hash-a");
        } else {
            result.unwrap();
            assert_eq!(state.data.unwrap()["configurationHash"], "hash-b");
            assert_eq!(
                state.metadata.uid.as_deref(),
                Some("tenant-controller-state-uid")
            );
        }
    }
}

#[tokio::test]
async fn rollback_patch_advances_revision_and_rejects_stale_state_put() {
    let server = Server::default();
    server.insert(
        STATE,
        config_map(STATE_NAME, &[("configurationHash", "hash-a")]),
    );
    let api = Api::<ConfigMap>::namespaced(server.client(), "tenant-system");
    let stale = api.get(STATE_NAME).await.unwrap();
    api.patch(
        STATE_NAME,
        &PatchParams::default(),
        &Patch::Merge(json!({
            "metadata":{"resourceVersion":"1"},
            "data":{"rollbackToken":"rollback-a"}
        })),
    )
    .await
    .unwrap();
    let mut replacement = stale;
    replacement
        .data
        .get_or_insert_default()
        .insert("configurationHash".into(), "hash-b".into());
    assert!(
        api.replace(STATE_NAME, &PostParams::default(), &replacement)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn creation_invalid_configuration_cannot_replace_accepted_identity() {
    let server = clean_server();
    server.insert(
        STATE,
        config_map(STATE_NAME, &[("configurationHash", "hash-a")]),
    );
    server.insert(TICKET, ticket_with_previous("hash-b", "token-b", "hash-a"));
    let error = admit(
        server.client(),
        &FakeDocker::default(),
        "hash-b",
        "token-b",
        false,
    )
    .await
    .unwrap_err();
    assert!(error.to_string().contains("invalid for Tenant creation"));
    let state: ConfigMap = serde_json::from_value(server.get(STATE)).unwrap();
    assert_eq!(state.data.unwrap()["configurationHash"], "hash-a");
    let observed_ticket: ConfigMap = serde_json::from_value(server.get(TICKET)).unwrap();
    assert!(!observed_ticket.data.unwrap().contains_key("consumed"));

    let first = clean_server();
    first.insert(TICKET, ticket("hash-b", "token-b"));
    assert!(
        admit(
            first.client(),
            &FakeDocker::default(),
            "hash-b",
            "token-b",
            false,
        )
        .await
        .unwrap_err()
        .to_string()
        .contains("invalid for Tenant creation")
    );
    assert!(!first.0.lock().unwrap().objects.contains_key(STATE));
}

#[tokio::test]
async fn consumed_ticket_resumes_only_from_its_recorded_previous_identity() {
    for current in ["hash-a", "hash-c"] {
        let server = clean_server();
        server.insert(
            STATE,
            config_map(STATE_NAME, &[("configurationHash", current)]),
        );
        let mut value = ticket_with_previous("hash-b", "token-b", "hash-a");
        value
            .data
            .get_or_insert_default()
            .insert("consumed".into(), "true".into());
        server.insert(TICKET, value);
        let result = admit(
            server.client(),
            &FakeDocker::default(),
            "hash-b",
            "token-b",
            true,
        )
        .await;
        if current == "hash-a" {
            result.unwrap();
            let state: ConfigMap = serde_json::from_value(server.get(STATE)).unwrap();
            assert_eq!(state.data.unwrap()["configurationHash"], "hash-b");
        } else {
            assert!(result.is_err());
        }
    }
}

#[tokio::test]
async fn active_tenant_or_invalid_ticket_blocks_identity_change() {
    for invalid_ticket in [false, true] {
        let server = clean_server();
        server.insert(
            TICKET,
            ticket("hash-b", if invalid_ticket { "wrong" } else { "token-b" }),
        );
        if !invalid_ticket {
            server.insert(&format!("{TENANTS}/tenant-a"), tenant());
        }
        let error = admit(
            server.client(),
            &FakeDocker::default(),
            "hash-b",
            "token-b",
            true,
        )
        .await
        .unwrap_err();
        assert!(matches!(
            error,
            tenant_controller::error::ControllerError::Configuration(_)
        ));
        assert!(!server.0.lock().unwrap().objects.contains_key(STATE));
        assert!(server.0.lock().unwrap().objects.contains_key(TICKET));
    }
}

#[tokio::test]
async fn stale_or_invalid_consumption_ticket_is_rejected() {
    for field in ["createdAt", "consumed"] {
        let server = clean_server();
        let mut value = ticket("hash-b", "token-b");
        value.data.as_mut().unwrap().insert(
            field.into(),
            if field == "createdAt" {
                "2020-01-01T00:00:00Z".into()
            } else {
                "invalid".into()
            },
        );
        server.insert(TICKET, value);
        assert!(
            admit(
                server.client(),
                &FakeDocker::default(),
                "hash-b",
                "token-b",
                true,
            )
            .await
            .is_err()
        );
    }
}

#[tokio::test]
async fn namespace_secret_lease_and_volume_residue_block_activation() {
    for residue in [
        "namespace",
        "namespace-allocation",
        "secret",
        "secret-allocation",
        "lease",
        "volume",
    ] {
        let server = clean_server();
        server.insert(TICKET, ticket("hash-b", "token-b"));
        let docker = FakeDocker::default();
        match residue {
            "namespace" => server.insert(
                &format!("{NAMESPACES}/tenant-a"),
                json!({"apiVersion":"v1","kind":"Namespace","metadata":{
                    "name":"tenant-a","uid":"namespace-uid",
                    "annotations":{"tenancy.cnpg-vcluster.io/tenant":"tenant-a"}}}),
            ),
            "namespace-allocation" => server.insert(
                &format!("{NAMESPACES}/tenant-a"),
                json!({"apiVersion":"v1","kind":"Namespace","metadata":{
                    "name":"tenant-a","uid":"namespace-uid",
                    "labels":{"tenancy.cnpg-vcluster.io/slot-id":"slot-a"}}}),
            ),
            "secret" => server.insert(
                &format!("{SECRETS}/tenant-a-kubeconfig"),
                json!({"apiVersion":"v1","kind":"Secret","metadata":{
                    "name":"tenant-a-kubeconfig","namespace":"tenant-a",
                    "uid":"secret-uid",
                    "ownerReferences":[{"apiVersion":"controlplane.cluster.x-k8s.io/v1alpha2",
                        "kind":"KamajiControlPlane","name":"tenant-a","uid":"cp"}]}}),
            ),
            "secret-allocation" => server.insert(
                &format!("{SECRETS}/tenant-a-kubeconfig"),
                json!({"apiVersion":"v1","kind":"Secret","metadata":{
                    "name":"tenant-a-kubeconfig","namespace":"tenant-a",
                    "uid":"secret-uid",
                    "annotations":{"tenancy.cnpg-vcluster.io/resource":"allocation-lease"}}}),
            ),
            "lease" => server.insert(
                &format!("{LEASES}/slot-a"),
                Lease {
                    metadata: ObjectMeta {
                        name: Some("slot-a".into()),
                        namespace: Some("tenant-system".into()),
                        uid: Some("lease-uid".into()),
                        labels: Some(
                            [("tenancy.cnpg-vcluster.io/slot-id".into(), "slot-a".into())].into(),
                        ),
                        ..Default::default()
                    },
                    ..Default::default()
                },
            ),
            "volume" => {
                docker.volumes.lock().unwrap().insert(
                    "project-volume".into(),
                    tenant_controller::docker::DockerVolume {
                        name: "project-volume".into(),
                        created_at: "now".into(),
                        mountpoint: "/volume".into(),
                        labels: [("cnpg-vcluster.capi/role".into(), "unexpected-role".into())]
                            .into(),
                    },
                );
            }
            _ => unreachable!(),
        }
        assert!(
            admit(server.client(), &docker, "hash-b", "token-b", true)
                .await
                .is_err(),
            "{residue}"
        );
    }
}

#[tokio::test]
async fn malformed_catalog_inventory_identity_blocks_activation() {
    for (path, item) in [
        (
            format!("{NAMESPACES}/tenant-a"),
            json!({"apiVersion":"v1","kind":"Namespace","metadata":{"name":"tenant-a"}}),
        ),
        (
            format!("{SECRETS}/tenant-a"),
            json!({"apiVersion":"v1","kind":"Secret","metadata":{
                "name":"tenant-a","namespace":"tenant-a","uid":"uid",
                "ownerReferences":[{}]}}),
        ),
        (
            format!("{SECRETS}/tenant-b"),
            json!({"apiVersion":"v1","kind":"Secret","metadata":{
                "name":"tenant-b","namespace":"tenant-a","uid":"uid",
                "annotations":null}}),
        ),
    ] {
        let server = clean_server();
        server.insert(TICKET, ticket("hash-b", "token-b"));
        server.insert(&path, item);
        assert!(
            admit(
                server.client(),
                &FakeDocker::default(),
                "hash-b",
                "token-b",
                true,
            )
            .await
            .is_err()
        );
    }
}

#[tokio::test]
async fn malformed_catalog_inventory_list_blocks_activation() {
    let resource = MANAGEMENT_RESOURCES
        .iter()
        .find(|resource| resource.kind == "Cluster")
        .unwrap();
    let path = format!("/apis/cluster.x-k8s.io/v1beta2/{}", resource.plural);
    for payload in [
        json!({"apiVersion":"v1","kind":"ClusterList","items":[]}),
        json!({"apiVersion":resource.api_version,"kind":"WrongList","items":[]}),
        json!({"apiVersion":resource.api_version,"kind":"ClusterList","items":null}),
        json!({"apiVersion":resource.api_version,"kind":"ClusterList"}),
    ] {
        let server = clean_server();
        server.insert(TICKET, ticket("hash-b", "token-b"));
        server.respond("GET", &path, 200, payload);
        assert!(
            admit(
                server.client(),
                &FakeDocker::default(),
                "hash-b",
                "token-b",
                true,
            )
            .await
            .is_err()
        );
    }
}

#[tokio::test]
async fn unmarked_typed_infrastructure_is_exempt_from_activation_inventory() {
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    server.insert(
        &format!("{NAMESPACES}/management"),
        json!({"apiVersion":"v1","kind":"Namespace","metadata":{
            "name":"management","uid":"namespace-uid"}}),
    );
    server.insert(
        &format!("{SECRETS}/controller-secret"),
        json!({"apiVersion":"v1","kind":"Secret","metadata":{
            "name":"controller-secret","namespace":"tenant-system","uid":"secret-uid"}}),
    );
    server.insert(
        &format!("{LEASES}/tenant-controller.tenancy.cnpg-vcluster.io"),
        Lease {
            metadata: ObjectMeta {
                name: Some("tenant-controller.tenancy.cnpg-vcluster.io".into()),
                namespace: Some("tenant-system".into()),
                uid: Some("leader-uid".into()),
                ..Default::default()
            },
            ..Default::default()
        },
    );
    admit(
        server.client(),
        &FakeDocker::default(),
        "hash-b",
        "token-b",
        true,
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn every_allocation_identity_marker_blocks_but_leader_lease_does_not() {
    for marker in [
        "slot-label",
        "tenant-label",
        "resource",
        "slot-annotation",
        "tenant",
        "tenant-uid",
    ] {
        let server = clean_server();
        server.insert(TICKET, ticket("hash-b", "token-b"));
        let mut lease = Lease {
            metadata: ObjectMeta {
                name: Some("claim".into()),
                namespace: Some("tenant-system".into()),
                uid: Some("claim-uid".into()),
                ..Default::default()
            },
            ..Default::default()
        };
        match marker {
            "slot-label" => {
                lease.metadata.labels =
                    Some([("tenancy.cnpg-vcluster.io/slot-id".into(), "slot-a".into())].into())
            }
            "tenant-label" => {
                lease.metadata.labels =
                    Some([("tenancy.cnpg-vcluster.io/tenant".into(), "tenant-a".into())].into())
            }
            "resource" => {
                lease.metadata.annotations = Some(
                    [(
                        "tenancy.cnpg-vcluster.io/resource".into(),
                        "allocation-lease".into(),
                    )]
                    .into(),
                )
            }
            "slot-annotation" => {
                lease.metadata.annotations =
                    Some([("tenancy.cnpg-vcluster.io/slot-id".into(), "slot-a".into())].into())
            }
            "tenant" => {
                lease.metadata.annotations =
                    Some([("tenancy.cnpg-vcluster.io/tenant".into(), "tenant-a".into())].into())
            }
            "tenant-uid" => {
                lease.metadata.annotations =
                    Some([("tenancy.cnpg-vcluster.io/tenant-uid".into(), "uid-a".into())].into())
            }
            _ => unreachable!(),
        }
        server.insert(&format!("{LEASES}/claim"), lease);
        assert!(
            admit(
                server.client(),
                &FakeDocker::default(),
                "hash-b",
                "token-b",
                true,
            )
            .await
            .is_err(),
            "{marker}"
        );
    }
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    server.insert(
        &format!("{LEASES}/tenant-controller.tenancy.cnpg-vcluster.io"),
        Lease {
            metadata: ObjectMeta {
                name: Some("tenant-controller.tenancy.cnpg-vcluster.io".into()),
                namespace: Some("tenant-system".into()),
                uid: Some("leader-uid".into()),
                ..Default::default()
            },
            ..Default::default()
        },
    );
    admit(
        server.client(),
        &FakeDocker::default(),
        "hash-b",
        "token-b",
        true,
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn accepted_state_survives_ticket_delete_failure_without_replay() {
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    server.respond(
        "DELETE",
        TICKET,
        503,
        crate::support::kube::status(503, "Unavailable"),
    );
    let docker = FakeDocker::default();
    assert!(
        admit(server.client(), &docker, "hash-b", "token-b", true)
            .await
            .is_err()
    );
    let ticket: ConfigMap = serde_json::from_value(server.get(TICKET)).unwrap();
    assert_eq!(ticket.data.unwrap()["consumed"], "true");
    admit(server.client(), &docker, "hash-b", "", true)
        .await
        .unwrap();
}

#[tokio::test]
async fn provider_discovery_uncertainty_is_not_absence() {
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    let first = MANAGEMENT_RESOURCES
        .iter()
        .find(|resource| resource.inventory_policy == InventoryPolicy::BlockAnyInstance)
        .unwrap();
    let (group, version) = first.api_version.split_once('/').unwrap();
    let path = format!("/apis/{group}/{version}/{}", first.plural);
    server.respond(
        "GET",
        &path,
        404,
        crate::support::kube::status(404, "NotFound"),
    );
    assert!(
        admit(
            server.client(),
            &FakeDocker::default(),
            "hash-b",
            "token-b",
            true,
        )
        .await
        .is_err()
    );
}
