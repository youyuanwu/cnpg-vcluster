mod creation_support;
mod support;

use creation_support::{FakeDocker, tenant};
use k8s_openapi::api::core::v1::ConfigMap;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use support::Server;
use tenant_controller::{
    activation::{STATE_NAME, TICKET_NAME, admit},
    management::ACTIVATION_RESOURCES,
};

const CONFIG_MAPS: &str = "/api/v1/namespaces/tenant-system/configmaps";
const STATE: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-controller-state";
const TICKET: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-controller-activation";
const TENANTS: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants";

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
    for resource in ACTIVATION_RESOURCES {
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
    assert_eq!(*docker.calls.lock().unwrap(), ["containers"]);
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
