mod creation_support;
mod support;

use creation_support::{FakeDocker, tenant};
use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::ConfigMap;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use serde_json::json;
use support::Server;
use tenant_controller::{
    activation::{STATE_NAME, TICKET_NAME, admit},
    management::{ACTIVATION_RESOURCES, InventoryPolicy},
};

const CONFIG_MAPS: &str = "/api/v1/namespaces/tenant-system/configmaps";
const STATE: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-controller-state";
const TICKET: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-controller-activation";
const TENANTS: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants";
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
    for path in [TENANTS, NAMESPACES, SECRETS, LEASES] {
        server.allow_list(path);
    }
    for resource in ACTIVATION_RESOURCES
        .iter()
        .filter(|resource| resource.inventory_policy == InventoryPolicy::BlockAnyInstance)
    {
        let (group, version) = resource.api_version.split_once('/').unwrap();
        server.allow_list(&format!("/apis/{group}/{version}/{}", resource.plural));
    }
    server
}

fn ticket(hash: &str, token: &str) -> ConfigMap {
    config_map(
        TICKET_NAME,
        &[
            ("configurationHash", hash),
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
    admit(server.client(), &docker, "hash-a", "").await.unwrap();
    assert_eq!(server.calls().len(), 1);
    assert!(docker.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn changed_identity_requires_ticket_and_atomically_accepts_clean_state() {
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    let docker = FakeDocker::default();
    admit(server.client(), &docker, "hash-b", "token-b")
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
        let error = admit(server.client(), &FakeDocker::default(), "hash-b", "token-b")
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
async fn stale_or_consumed_ticket_cannot_be_replayed() {
    for field in ["createdAt", "consumed"] {
        let server = clean_server();
        let mut value = ticket("hash-b", "token-b");
        value.data.as_mut().unwrap().insert(
            field.into(),
            if field == "createdAt" {
                "2020-01-01T00:00:00Z".into()
            } else {
                "true".into()
            },
        );
        server.insert(TICKET, value);
        assert!(
            admit(server.client(), &FakeDocker::default(), "hash-b", "token-b")
                .await
                .is_err()
        );
    }
}

#[tokio::test]
async fn namespace_secret_lease_and_volume_residue_block_activation() {
    for residue in ["namespace", "secret", "lease", "volume"] {
        let server = clean_server();
        server.insert(TICKET, ticket("hash-b", "token-b"));
        let docker = FakeDocker::default();
        match residue {
            "namespace" => server.insert(
                &format!("{NAMESPACES}/tenant-a"),
                json!({"apiVersion":"v1","kind":"Namespace","metadata":{
                    "name":"tenant-a","annotations":{"tenancy.cnpg-vcluster.io/tenant":"tenant-a"}}}),
            ),
            "secret" => server.insert(
                &format!("{SECRETS}/tenant-a-kubeconfig"),
                json!({"apiVersion":"v1","kind":"Secret","metadata":{
                    "name":"tenant-a-kubeconfig","namespace":"tenant-a",
                    "ownerReferences":[{"apiVersion":"controlplane.cluster.x-k8s.io/v1alpha2",
                        "kind":"KamajiControlPlane","name":"tenant-a","uid":"cp"}]}}),
            ),
            "lease" => server.insert(
                &format!("{LEASES}/slot-a"),
                Lease {
                    metadata: ObjectMeta {
                        name: Some("slot-a".into()),
                        namespace: Some("tenant-system".into()),
                        labels: Some([(
                            "tenancy.cnpg-vcluster.io/slot-id".into(),
                            "slot-a".into(),
                        )].into()),
                        ..Default::default()
                    },
                    ..Default::default()
                },
            ),
            "volume" => {
                docker.volumes.lock().unwrap().insert(
                    "tenant-a-storage".into(),
                    tenant_controller::docker::DockerVolume {
                        name: "tenant-a-storage".into(),
                        created_at: "now".into(),
                        mountpoint: "/volume".into(),
                        labels: Default::default(),
                    },
                );
            }
            _ => unreachable!(),
        }
        assert!(
            admit(server.client(), &docker, "hash-b", "token-b")
                .await
                .is_err(),
            "{residue}"
        );
    }
}

#[tokio::test]
async fn accepted_state_survives_ticket_delete_failure_without_replay() {
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    server.respond(
        "DELETE",
        TICKET,
        503,
        support::kube::status(503, "Unavailable"),
    );
    let docker = FakeDocker::default();
    assert!(
        admit(server.client(), &docker, "hash-b", "token-b")
            .await
            .is_err()
    );
    let ticket: ConfigMap = serde_json::from_value(server.get(TICKET)).unwrap();
    assert_eq!(ticket.data.unwrap()["consumed"], "true");
    admit(server.client(), &docker, "hash-b", "").await.unwrap();
}

#[tokio::test]
async fn provider_discovery_uncertainty_is_not_absence() {
    let server = clean_server();
    server.insert(TICKET, ticket("hash-b", "token-b"));
    let first = ACTIVATION_RESOURCES
        .iter()
        .find(|resource| resource.inventory_policy == InventoryPolicy::BlockAnyInstance)
        .unwrap();
    let (group, version) = first.api_version.split_once('/').unwrap();
    let path = format!("/apis/{group}/{version}/{}", first.plural);
    server.respond("GET", &path, 404, support::kube::status(404, "NotFound"));
    assert!(
        admit(server.client(), &FakeDocker::default(), "hash-b", "token-b")
            .await
            .is_err()
    );
}
