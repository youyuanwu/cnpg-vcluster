use std::collections::VecDeque;
use std::convert::Infallible;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Body as ResponseBody;
use axum::http::{Request, Response};
use base64::{Engine, engine::general_purpose::STANDARD};
use http_body_util::BodyExt;
use k8s_openapi::api::core::v1::Secret;
use kube::Client;
use kube::client::Body;
use kube::core::{DynamicObject, Status};
use serde_json::{Value, json};
use tenant_controller::resources::bootstrap_rbac;
use tenant_controller::tenant_client::*;
use tower::service_fn;

// Self-signed, deliberately public test identity. Never used by live clients.
const CERTIFICATE: &str = include_str!("fixtures/adapters-test-only.crt");
const PRIVATE_KEY: &str = include_str!("fixtures/adapters-test-only.key");
const ENDPOINT: &str = "172.18.255.2:6443";

fn config() -> Value {
    json!({
        "apiVersion":"v1","kind":"Config","current-context":"tenant-context",
        "contexts":[{"name":"tenant-context","context":{"cluster":"tenant","user":"admin"}}],
        "clusters":[{"name":"tenant","cluster":{
            "server":format!("https://{ENDPOINT}"),
            "certificate-authority-data":STANDARD.encode(CERTIFICATE)
        }}],
        "users":[{"name":"admin","user":{
            "client-certificate-data":STANDARD.encode(CERTIFICATE),
            "client-key-data":STANDARD.encode(PRIVATE_KEY)
        }}]
    })
}

fn secret_bytes(bytes: &[u8]) -> Secret {
    serde_json::from_value(json!({
        "apiVersion":"v1","kind":"Secret",
        "metadata":{
            "name":"tenant-a-kubeconfig","namespace":"tenant-a","uid":"secret-uid",
            "ownerReferences":[{
                "apiVersion":"controlplane.cluster.x-k8s.io/v1alpha2",
                "kind":"KamajiControlPlane","name":"tenant-a","uid":"control-plane-uid"
            }]
        },
        "type":"cluster.x-k8s.io/secret",
        "data":{"value":STANDARD.encode(bytes)}
    }))
    .unwrap()
}

fn secret(value: &Value) -> Secret {
    secret_bytes(serde_yaml::to_string(value).unwrap().as_bytes())
}

fn control_plane() -> DynamicObject {
    serde_json::from_value(json!({
        "apiVersion":"controlplane.cluster.x-k8s.io/v1alpha2","kind":"KamajiControlPlane",
        "metadata":{"name":"tenant-a","namespace":"tenant-a","uid":"control-plane-uid"}
    }))
    .unwrap()
}

fn parse(secret: &Secret) -> Result<kube::config::Kubeconfig, TenantClientError> {
    parse_owned_kubeconfig(secret, &control_plane(), "tenant-a", "tenant-a", ENDPOINT)
}

#[tokio::test]
async fn exact_owned_secret_builds_certificate_authenticated_bounded_client() {
    let secret = secret(&config());
    let config =
        validated_tenant_config(&secret, &control_plane(), "tenant-a", "tenant-a", ENDPOINT)
            .await
            .unwrap();
    assert_eq!(
        config.cluster_url.to_string(),
        format!("https://{ENDPOINT}/")
    );
    assert_eq!(config.connect_timeout, Some(Duration::from_secs(30)));
    assert_eq!(config.read_timeout, Some(Duration::from_secs(30)));
    assert_eq!(config.write_timeout, Some(Duration::from_secs(30)));
    assert!(!config.accept_invalid_certs);
    assert!(!config.default_retry);
    assert!(config.proxy_url.is_none());
    assert!(config.root_cert_file.is_none());
    assert_eq!(config.root_cert.as_ref().unwrap().len(), 1);
    assert!(config.auth_info.exec.is_none());
    assert!(config.auth_info.client_key_data.is_some());
    tenant_client_from_secret(&secret, &control_plane(), "tenant-a", "tenant-a", ENDPOINT)
        .await
        .unwrap();
}

#[test]
fn secret_contract_and_exact_owner_are_checked_before_parsing() {
    for edit in 0..11 {
        let mut value = serde_json::to_value(secret(&config())).unwrap();
        match edit {
            0 => value["metadata"]["name"] = json!("other-kubeconfig"),
            1 => value["metadata"]["namespace"] = json!("foreign"),
            2 => value["metadata"]["uid"] = json!(""),
            3 => value["type"] = json!("Opaque"),
            4 => value["data"] = json!({}),
            5 => value["data"]["value"] = json!(""),
            6 => value["metadata"]["ownerReferences"][0]["uid"] = json!("foreign"),
            7 => value["metadata"]["ownerReferences"][0]["name"] = json!("foreign"),
            8 => value["metadata"]["ownerReferences"][0]["kind"] = json!("Cluster"),
            9 => value["metadata"]["ownerReferences"][0]["apiVersion"] = json!("foreign/v1"),
            _ => {
                let owner = value["metadata"]["ownerReferences"][0].clone();
                value["metadata"]["ownerReferences"]
                    .as_array_mut()
                    .unwrap()
                    .push(owner);
            }
        }
        assert!(
            parse(&serde_json::from_value(value).unwrap()).is_err(),
            "edit {edit}"
        );
    }
    for edit in 0..4 {
        let mut cp = control_plane();
        match edit {
            0 => cp.metadata.name = Some("foreign".into()),
            1 => cp.metadata.namespace = Some("foreign".into()),
            2 => cp.metadata.uid = None,
            _ => cp.types.as_mut().unwrap().kind = "Cluster".into(),
        }
        assert!(
            parse_owned_kubeconfig(&secret(&config()), &cp, "tenant-a", "tenant-a", ENDPOINT,)
                .is_err()
        );
    }
}

#[test]
fn kubeconfig_requires_utf8_yaml_unique_context_cluster_user_and_embedded_credentials() {
    assert_eq!(
        parse(&secret_bytes(&[0xff])).unwrap_err(),
        TenantClientError::Kubeconfig("invalid UTF-8")
    );
    assert_eq!(
        parse(&secret_bytes(b"clusters: [invalid YAML")).unwrap_err(),
        TenantClientError::Kubeconfig("invalid YAML")
    );
    for edit in 0..13 {
        let mut value = config();
        match edit {
            0 => value["current-context"] = json!("missing"),
            1 => value["contexts"] = json!([]),
            2 => value["contexts"][0]["context"]["user"] = json!(""),
            3 => value["contexts"][0]["context"]["cluster"] = json!("missing"),
            4 => value["users"] = json!([]),
            5 => value["clusters"][0]["cluster"]["certificate-authority-data"] = json!(""),
            6 => value["users"][0]["user"]["client-certificate-data"] = json!(""),
            7 => value["users"][0]["user"]["client-key-data"] = json!(""),
            8 => {
                value["clusters"][0]["cluster"]["certificate-authority-data"] =
                    json!("%%%not-base64")
            }
            9 => value["users"][0]["user"]["client-key-data"] = json!("%%%not-base64"),
            10 => value["users"][0]["user"]["client-certificate-data"] = json!("%%%not-base64"),
            11 => {
                let entry = value["contexts"][0].clone();
                value["contexts"].as_array_mut().unwrap().push(entry);
            }
            _ => {
                let entry = value["users"][0].clone();
                value["users"].as_array_mut().unwrap().push(entry);
            }
        }
        assert!(parse(&secret(&value)).is_err(), "edit {edit}");
    }
}

#[test]
fn endpoint_comparison_requires_https_and_exact_allocated_authority_without_url_extras() {
    for endpoint in [
        "http://172.18.255.2:6443",
        "https://172.18.255.3:6443",
        "https://172.18.255.2:443",
        "https://172.18.255.2",
        "https://172.18.255.2:6443/",
        "https://172.18.255.2:6443/api",
        "https://172.18.255.2:6443?secret=sentinel",
        "https://172.18.255.2:6443#sentinel",
        "https://sentinel@172.18.255.2:6443",
        "https://172.18.255.2:6443\n",
    ] {
        let mut value = config();
        value["clusters"][0]["cluster"]["server"] = json!(endpoint);
        let error = parse(&secret(&value)).unwrap_err();
        assert_eq!(
            error,
            TenantClientError::Kubeconfig("endpoint does not match the allocation")
        );
        assert!(!format!("{error:?} {error}").contains("sentinel"));
    }
    for endpoint in ["[2001:db8::1]:6443", "tenant-api.example:443"] {
        let mut value = config();
        value["clusters"][0]["cluster"]["server"] = json!(format!("https://{endpoint}"));
        parse_owned_kubeconfig(
            &secret(&value),
            &control_plane(),
            "tenant-a",
            "tenant-a",
            endpoint,
        )
        .unwrap();
    }
}

#[test]
fn kubeconfig_cannot_execute_plugins_read_host_files_or_override_tls_and_authentication() {
    for (key, extra) in [
        (
            "exec",
            json!({"command":"never-run-this","apiVersion":"client.authentication.k8s.io/v1"}),
        ),
        ("auth-provider", json!({"name":"never-run-this"})),
        ("client-key", json!("/must-not-be-read")),
        ("client-certificate", json!("/must-not-be-read")),
        ("tokenFile", json!("/must-not-be-read")),
        ("token", json!("private-sentinel")),
        ("username", json!("private-sentinel")),
        ("password", json!("private-sentinel")),
        ("as", json!("system:admin")),
        ("as-groups", json!(["system:masters"])),
    ] {
        let mut value = config();
        value["users"][0]["user"][key] = extra;
        assert!(parse(&secret(&value)).is_err(), "{key}");
    }
    for (key, extra) in [
        ("insecure-skip-tls-verify", json!(true)),
        ("certificate-authority", json!("/must-not-be-read")),
        ("proxy-url", json!("https://private-sentinel")),
        ("tls-server-name", json!("private-sentinel")),
    ] {
        let mut value = config();
        value["clusters"][0]["cluster"][key] = extra;
        assert!(parse(&secret(&value)).is_err(), "{key}");
    }
}

#[tokio::test]
async fn tls_build_errors_and_yaml_errors_never_retain_secret_material() {
    for path in [
        "/clusters/0/cluster/certificate-authority-data",
        "/users/0/user/client-certificate-data",
        "/users/0/user/client-key-data",
    ] {
        let mut value = config();
        *value.pointer_mut(path).unwrap() = json!(STANDARD.encode("private-sentinel"));
        let error = tenant_client_from_secret(
            &secret(&value),
            &control_plane(),
            "tenant-a",
            "tenant-a",
            ENDPOINT,
        )
        .await
        .err()
        .expect("invalid TLS data was accepted");
        assert_eq!(error, TenantClientError::ClientConfiguration);
        assert!(!format!("{error:?} {error}").contains("private-sentinel"));
        assert!(std::error::Error::source(&error).is_none());
    }
    let error = parse(&secret_bytes(b"current-context: [private-sentinel]")).unwrap_err();
    assert!(!format!("{error:?} {error}").contains("private-sentinel"));
}

#[derive(Debug)]
struct Recorded {
    method: String,
    path: String,
    body: Value,
}

#[derive(Clone)]
struct KubeFixture {
    responses: Arc<Mutex<VecDeque<(u16, Value)>>>,
    requests: Arc<Mutex<Vec<Recorded>>>,
}

impl KubeFixture {
    fn new(responses: Vec<(u16, Value)>) -> Self {
        Self {
            responses: Arc::new(Mutex::new(responses.into())),
            requests: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn client(&self) -> Client {
        let fixture = self.clone();
        Client::new(
            service_fn(move |request: Request<Body>| {
                let fixture = fixture.clone();
                async move {
                    let (parts, body) = request.into_parts();
                    let bytes = body.collect().await.unwrap().to_bytes();
                    fixture.requests.lock().unwrap().push(Recorded {
                        method: parts.method.to_string(),
                        path: parts.uri.path().to_owned(),
                        body: if bytes.is_empty() {
                            Value::Null
                        } else {
                            serde_json::from_slice(&bytes).unwrap()
                        },
                    });
                    let (status, body) = fixture
                        .responses
                        .lock()
                        .unwrap()
                        .pop_front()
                        .expect("unexpected API request");
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "application/json")
                            .body(ResponseBody::from(body.to_string()))
                            .unwrap(),
                    )
                }
            }),
            "default",
        )
    }

    fn assert_exhausted(&self) {
        assert!(self.responses.lock().unwrap().is_empty());
    }
}

fn api_status(code: u16) -> Value {
    json!({
        "apiVersion":"v1","kind":"Status","status":"Failure","code":code,
        "message":"private-sentinel", "reason": match code {
            403 => "Forbidden", 404 => "NotFound", 409 => "AlreadyExists",
            503 => "ServiceUnavailable", _ => "Unauthorized"
        }
    })
}

#[tokio::test]
async fn management_reads_exact_secret_and_never_falls_back_to_environment_credentials() {
    let fixture = KubeFixture::new(vec![(
        200,
        serde_json::to_value(secret(&config())).unwrap(),
    )]);
    let (_, secret) = load_tenant_client(
        fixture.client(),
        &control_plane(),
        "tenant-a",
        "tenant-a",
        ENDPOINT,
    )
    .await
    .unwrap();
    assert_eq!(secret.metadata.uid.as_deref(), Some("secret-uid"));
    {
        let requests = fixture.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].method, "GET");
        assert_eq!(
            requests[0].path,
            "/api/v1/namespaces/tenant-a/secrets/tenant-a-kubeconfig"
        );
        assert!(requests[0].body.is_null());
    }
    fixture.assert_exhausted();
    for status in [401, 403, 404, 503] {
        let fixture = KubeFixture::new(vec![(status, api_status(status))]);
        let error = load_tenant_client(
            fixture.client(),
            &control_plane(),
            "tenant-a",
            "tenant-a",
            ENDPOINT,
        )
        .await
        .err()
        .unwrap();
        if status == 404 {
            assert_eq!(error, TenantClientError::SecretPending);
        } else {
            assert!(
                matches!(error, TenantClientError::Api { status: actual, .. } if actual == status)
            );
        }
        assert!(!format!("{error:?} {error}").contains("private-sentinel"));
        assert_eq!(fixture.requests.lock().unwrap().len(), 1);
        fixture.assert_exhausted();
    }
}

#[test]
fn pure_bootstrap_content_checks_are_order_insensitive_only_for_subjects() {
    let desired = bootstrap_rbac().remove(0);
    let mut role = desired.role.clone();
    role.metadata.resource_version = Some("observed-rv".into());
    validate_bootstrap_role(&role, &desired.role).unwrap();
    role.rules.as_mut().unwrap()[0].verbs.push("list".into());
    assert_eq!(
        validate_bootstrap_role(&role, &desired.role),
        Err(TenantClientError::BootstrapMismatch("Role"))
    );
    let mut binding = desired.binding.clone();
    binding.subjects.as_mut().unwrap().reverse();
    validate_bootstrap_binding(&binding, &desired.binding).unwrap();
    for edit in 0..4 {
        let mut binding = desired.binding.clone();
        match edit {
            0 => binding.role_ref.name = "foreign".into(),
            1 => binding.subjects.as_mut().unwrap()[0].name = "foreign".into(),
            2 => {
                binding.subjects.as_mut().unwrap().remove(0);
            }
            _ => {
                let duplicate = binding.subjects.as_ref().unwrap()[0].clone();
                binding.subjects.as_mut().unwrap().push(duplicate);
            }
        }
        assert_eq!(
            validate_bootstrap_binding(&binding, &desired.binding),
            Err(TenantClientError::BootstrapMismatch("RoleBinding"))
        );
    }
}

#[tokio::test]
async fn bootstrap_creates_only_missing_roles_and_bindings_with_exact_content() {
    let expected: Vec<_> = bootstrap_rbac()
        .into_iter()
        .flat_map(|desired| {
            [
                serde_json::to_value(desired.role).unwrap(),
                serde_json::to_value(desired.binding).unwrap(),
            ]
        })
        .collect();
    let responses = expected
        .iter()
        .flat_map(|object| [(404, api_status(404)), (201, object.clone())])
        .collect();
    let fixture = KubeFixture::new(responses);
    ensure_bootstrap_rbac(fixture.client()).await.unwrap();
    fixture.assert_exhausted();
    let requests = fixture.requests.lock().unwrap();
    assert_eq!(requests.len(), 8);
    for (index, desired) in expected.iter().enumerate() {
        let resource = if desired["kind"] == "Role" {
            "roles"
        } else {
            "rolebindings"
        };
        let collection =
            format!("/apis/rbac.authorization.k8s.io/v1/namespaces/kube-system/{resource}");
        let read = &requests[2 * index];
        let create = &requests[2 * index + 1];
        assert_eq!(read.method, "GET");
        assert_eq!(
            read.path,
            format!(
                "{collection}/{}",
                desired["metadata"]["name"].as_str().unwrap()
            )
        );
        assert_eq!(create.method, "POST");
        assert_eq!(create.path, collection);
        assert_eq!(create.body, *desired);
    }
}

#[tokio::test]
async fn bootstrap_existing_content_is_never_patched_and_subject_order_is_ignored() {
    let responses = bootstrap_rbac()
        .into_iter()
        .flat_map(|mut desired| {
            desired.binding.subjects.as_mut().unwrap().reverse();
            [
                (200, serde_json::to_value(desired.role).unwrap()),
                (200, serde_json::to_value(desired.binding).unwrap()),
            ]
        })
        .collect();
    let fixture = KubeFixture::new(responses);
    ensure_bootstrap_rbac(fixture.client()).await.unwrap();
    fixture.assert_exhausted();
    assert!(
        fixture
            .requests
            .lock()
            .unwrap()
            .iter()
            .all(|request| request.method == "GET")
    );
}

#[tokio::test]
async fn bootstrap_foreign_existing_role_or_binding_blocks_without_repair() {
    let desired = bootstrap_rbac().remove(0);
    for binding in [false, true] {
        let mut object = if binding {
            serde_json::to_value(&desired.binding).unwrap()
        } else {
            serde_json::to_value(&desired.role).unwrap()
        };
        let responses = if binding {
            object["subjects"][0]["name"] = json!("foreign");
            vec![
                (200, serde_json::to_value(&desired.role).unwrap()),
                (200, object),
            ]
        } else {
            object["rules"][0]["verbs"] = json!(["*"]);
            vec![(200, object)]
        };
        let fixture = KubeFixture::new(responses);
        assert!(matches!(
            ensure_bootstrap_rbac(fixture.client()).await,
            Err(TenantClientError::BootstrapMismatch(_))
        ));
        fixture.assert_exhausted();
        assert!(
            fixture
                .requests
                .lock()
                .unwrap()
                .iter()
                .all(|request| request.method == "GET")
        );
    }
}

#[tokio::test]
async fn bootstrap_forbidden_at_every_read_and_create_is_pending_not_an_ownership_failure() {
    let desired: Vec<_> = bootstrap_rbac()
        .into_iter()
        .flat_map(|desired| {
            [
                serde_json::to_value(desired.role).unwrap(),
                serde_json::to_value(desired.binding).unwrap(),
            ]
        })
        .collect();
    for object_index in 0..4 {
        for create in [false, true] {
            let mut responses: Vec<_> = desired[..object_index]
                .iter()
                .map(|object| (200, object.clone()))
                .collect();
            if create {
                responses.push((404, api_status(404)));
            }
            responses.push((403, api_status(403)));
            let fixture = KubeFixture::new(responses);
            let error = ensure_bootstrap_rbac(fixture.client()).await.unwrap_err();
            assert_eq!(error, TenantClientError::AdministrativeAccessPending);
            assert_eq!(error.class(), TenantApiErrorClass::Pending);
            fixture.assert_exhausted();
        }
    }
}

#[tokio::test]
async fn bootstrap_create_race_rereads_and_refuses_foreign_winner() {
    let desired = bootstrap_rbac().remove(0);
    for binding in [false, true] {
        let mut object = if binding {
            serde_json::to_value(&desired.binding).unwrap()
        } else {
            serde_json::to_value(&desired.role).unwrap()
        };
        let mut responses = Vec::new();
        if binding {
            responses.push((200, serde_json::to_value(&desired.role).unwrap()));
            object["subjects"][0]["name"] = json!("foreign");
        } else {
            object["rules"][0]["verbs"] = json!(["*"]);
        }
        responses.extend([
            (404, api_status(404)),
            (409, api_status(409)),
            (200, object),
        ]);
        let fixture = KubeFixture::new(responses);
        assert!(matches!(
            ensure_bootstrap_rbac(fixture.client()).await,
            Err(TenantClientError::BootstrapMismatch(_))
        ));
        fixture.assert_exhausted();
        let requests = fixture.requests.lock().unwrap();
        assert_eq!(requests[requests.len() - 1].method, "GET");
        assert_eq!(
            requests
                .iter()
                .filter(|request| request.method == "POST")
                .count(),
            1
        );
    }
}

#[tokio::test]
async fn bootstrap_create_race_accepts_only_matching_winners() {
    let responses = bootstrap_rbac()
        .into_iter()
        .flat_map(|desired| {
            [
                serde_json::to_value(desired.role).unwrap(),
                serde_json::to_value(desired.binding).unwrap(),
            ]
        })
        .flat_map(|object| {
            [
                (404, api_status(404)),
                (409, api_status(409)),
                (200, object),
            ]
        })
        .collect();
    let fixture = KubeFixture::new(responses);
    ensure_bootstrap_rbac(fixture.client()).await.unwrap();
    fixture.assert_exhausted();
    let requests = fixture.requests.lock().unwrap();
    assert_eq!(requests.len(), 12);
    assert_eq!(
        requests
            .iter()
            .map(|request| request.method.as_str())
            .collect::<Vec<_>>(),
        ["GET", "POST", "GET"].repeat(4)
    );
}

#[tokio::test]
async fn bootstrap_disappearing_race_winner_is_pending_without_an_unbounded_create_loop() {
    let fixture = KubeFixture::new(vec![
        (404, api_status(404)),
        (409, api_status(409)),
        (404, api_status(404)),
    ]);
    let error = ensure_bootstrap_rbac(fixture.client()).await.unwrap_err();
    assert_eq!(error.class(), TenantApiErrorClass::Pending);
    assert_eq!(fixture.requests.lock().unwrap().len(), 3);
    fixture.assert_exhausted();
}

#[tokio::test]
async fn other_tenant_api_failures_propagate_with_redacted_bodies_and_bounded_classification() {
    for (code, class) in [
        (401, TenantApiErrorClass::Terminal),
        (403, TenantApiErrorClass::Pending),
        (404, TenantApiErrorClass::Pending),
        (409, TenantApiErrorClass::Conflict),
        (429, TenantApiErrorClass::Retryable),
        (503, TenantApiErrorClass::Retryable),
    ] {
        let error = kube::Error::Api(
            Status::failure("private-sentinel", "fixture")
                .with_code(code)
                .boxed(),
        );
        assert_eq!(classify_tenant_api_error(&error), class);
        if matches!(code, 403 | 404) {
            continue;
        }
        let fixture = KubeFixture::new(vec![(code, api_status(code))]);
        let error = ensure_bootstrap_rbac(fixture.client()).await.unwrap_err();
        assert_eq!(error.class(), class);
        assert!(!format!("{error:?} {error}").contains("private-sentinel"));
        fixture.assert_exhausted();
    }
}
