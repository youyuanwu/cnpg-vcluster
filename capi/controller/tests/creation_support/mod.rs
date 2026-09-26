#![allow(dead_code)]

use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    convert::Infallible,
    sync::{Arc, Mutex},
};

use axum::http::{Request, Response};
use http_body_util::BodyExt;
use kube::{Client, ResourceExt, client::Body, core::DynamicObject, runtime::controller::Action};
use serde_json::{Value, json};
use tenant_controller::{
    api::{Tenant, TenantSpec},
    docker::{DockerClient, DockerContainer, DockerError, DockerNetwork, DockerVolume},
    ownership::Identity,
    reconcile::{DeletionHandler, ReconcileError, TenantAccess},
    tenant_client::TenantClientError,
};
use tower::service_fn;

#[derive(Clone, Debug)]
pub struct Call {
    pub method: String,
    pub path: String,
    pub query: String,
    pub content_type: String,
    pub body: Value,
}

#[derive(Default)]
pub struct State {
    pub objects: BTreeMap<String, Value>,
    pub lists: BTreeSet<String>,
    pub calls: Vec<Call>,
    pub responses: BTreeMap<(String, String), VecDeque<(u16, Value)>>,
    pub revision: u32,
}

#[derive(Clone, Default)]
pub struct Server(pub Arc<Mutex<State>>);

impl Server {
    pub fn client(&self) -> Client {
        let state = self.0.clone();
        Client::new(
            service_fn(move |request: Request<Body>| {
                let state = state.clone();
                async move {
                    let (parts, body) = request.into_parts();
                    let bytes = body.collect().await.unwrap().to_bytes();
                    let body = if bytes.is_empty() {
                        Value::Null
                    } else {
                        serde_yaml::from_slice::<Value>(&bytes).unwrap()
                    };
                    let path = parts.uri.path().to_owned();
                    let method = parts.method.to_string();
                    let mut state = state.lock().unwrap();
                    state.calls.push(Call {
                        method: method.clone(),
                        path: path.clone(),
                        query: parts.uri.query().unwrap_or("").into(),
                        content_type: parts
                            .headers
                            .get("content-type")
                            .map(|value| value.to_str().unwrap_or(""))
                            .unwrap_or("")
                            .into(),
                        body: body.clone(),
                    });
                    if let Some(response) = state
                        .responses
                        .get_mut(&(method.clone(), path.clone()))
                        .and_then(VecDeque::pop_front)
                    {
                        return Ok::<_, Infallible>(response_body(response.0, response.1));
                    }
                    let (status, result) = match method.as_str() {
                        "GET" if state.lists.contains(&path) => {
                            assert!(
                                parts
                                    .uri
                                    .query()
                                    .is_none_or(|query| !query.contains("labelSelector")
                                        && !query.contains("fieldSelector")),
                                "filtered safety inventory"
                            );
                            let prefix = format!("{path}/");
                            let items: Vec<_> = state
                                .objects
                                .iter()
                                .filter(|(key, _)| {
                                    key.starts_with(&prefix) && !key[prefix.len()..].contains('/')
                                })
                                .map(|(_, value)| value.clone())
                                .collect();
                            (
                                200,
                                json!({"apiVersion":"v1","kind":"List","metadata":{"resourceVersion":"1"},"items":items}),
                            )
                        }
                        "GET" => match state.objects.get(&path) {
                            Some(value) => (200, value.clone()),
                            None => (404, status(404, "NotFound")),
                        },
                        "POST" => {
                            let name = body["metadata"]["name"].as_str().unwrap();
                            let key = format!("{path}/{name}");
                            if state.objects.contains_key(&key) {
                                (409, status(409, "AlreadyExists"))
                            } else {
                                state.revision += 1;
                                let mut value = body;
                                value["metadata"]["uid"] =
                                    json!(format!("created-{}", state.revision));
                                value["metadata"]["resourceVersion"] =
                                    json!(state.revision.to_string());
                                value["metadata"]["generation"] = json!(1);
                                state.objects.insert(key, value.clone());
                                (201, value)
                            }
                        }
                        "PATCH" => {
                            let key = path.strip_suffix("/status").unwrap_or(&path);
                            match state.objects.get(key).cloned() {
                                None => (404, status(404, "NotFound")),
                                Some(mut current) => {
                                    if body["metadata"]["uid"] != current["metadata"]["uid"]
                                        || body["metadata"]["resourceVersion"]
                                            != current["metadata"]["resourceVersion"]
                                    {
                                        (409, status(409, "Conflict"))
                                    } else {
                                        merge(&mut current, &body);
                                        state.revision += 1;
                                        current["metadata"]["resourceVersion"] =
                                            json!(state.revision.to_string());
                                        state.objects.insert(key.into(), current.clone());
                                        (200, current)
                                    }
                                }
                            }
                        }
                        "PUT" => {
                            let current = state.objects.get(&path).unwrap();
                            assert_eq!(body["metadata"]["uid"], current["metadata"]["uid"]);
                            assert_eq!(
                                body["metadata"]["resourceVersion"],
                                current["metadata"]["resourceVersion"]
                            );
                            state.revision += 1;
                            let mut body = body;
                            body["metadata"]["resourceVersion"] = json!(state.revision.to_string());
                            state.objects.insert(path, body.clone());
                            (200, body)
                        }
                        _ => panic!("unexpected request {method} {path}"),
                    };
                    Ok(response_body(status, result))
                }
            }),
            "default",
        )
    }

    pub fn insert(&self, path: &str, value: impl serde::Serialize) {
        self.0
            .lock()
            .unwrap()
            .objects
            .insert(path.into(), serde_json::to_value(value).unwrap());
    }

    pub fn allow_list(&self, path: &str) {
        self.0.lock().unwrap().lists.insert(path.into());
    }

    pub fn respond(&self, method: &str, path: &str, status: u16, body: Value) {
        self.0
            .lock()
            .unwrap()
            .responses
            .entry((method.into(), path.into()))
            .or_default()
            .push_back((status, body));
    }

    pub fn calls(&self) -> Vec<Call> {
        self.0.lock().unwrap().calls.clone()
    }

    pub fn take_calls(&self) -> Vec<Call> {
        std::mem::take(&mut self.0.lock().unwrap().calls)
    }

    pub fn get(&self, path: &str) -> Value {
        self.0.lock().unwrap().objects[path].clone()
    }
}

fn response_body(status: u16, value: Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&value).unwrap()))
        .unwrap()
}

pub fn status(code: u16, reason: &str) -> Value {
    json!({"apiVersion":"v1","kind":"Status","status":"Failure","code":code,"reason":reason,"message":"test request failed"})
}

fn merge(target: &mut Value, patch: &Value) {
    if let (Some(target), Some(patch)) = (target.as_object_mut(), patch.as_object()) {
        for (key, value) in patch {
            if value.is_null() {
                target.remove(key);
            } else {
                merge(target.entry(key.clone()).or_insert(Value::Null), value);
            }
        }
    } else {
        *target = patch.clone();
    }
}

pub fn tenant() -> Tenant {
    let mut value = Tenant::new(
        "tenant-a",
        TenantSpec {
            kubernetes_version: "1.36.4".into(),
            workers: 1,
            databases: 1,
        },
    );
    value.metadata.uid = Some("tenant-uid".into());
    value.metadata.resource_version = Some("1".into());
    value.metadata.generation = Some(2);
    value
}

pub fn identity() -> Identity<'static> {
    Identity {
        tenant_name: "tenant-a",
        tenant_uid: "tenant-uid",
        spec_hash: "spec-hash",
        foundation_hash: "foundation-hash",
        ownership_label: "example.io/owned",
        lab_prefix: "example",
    }
}

pub fn object(api: &str, kind: &str, namespace: &str, name: &str, role: &str) -> DynamicObject {
    let mut value: DynamicObject = serde_json::from_value(json!({
        "apiVersion":api,"kind":kind,
        "metadata":{"name":name,"uid":format!("{name}-uid"),"resourceVersion":"1","generation":2,
            "annotations":identity().annotations(role),"labels":identity().labels()}
    }))
    .unwrap();
    if !namespace.is_empty() {
        value.metadata.namespace = Some(namespace.into());
    }
    value
}

pub fn path(object: &DynamicObject) -> String {
    let types = object.types.as_ref().unwrap();
    let resource = tenant_controller::reconcile::objects::resource(&types.api_version, &types.kind);
    let mut path = if resource.group.is_empty() {
        format!("/api/{}", resource.version)
    } else {
        format!("/apis/{}/{}", resource.group, resource.version)
    };
    if let Some(namespace) = &object.metadata.namespace {
        path.push_str(&format!("/namespaces/{namespace}"));
    }
    path.push_str(&format!("/{}/{}", resource.plural, object.name_any()));
    path
}

#[derive(Clone, Default)]
pub struct FakeDocker {
    pub containers: Arc<Mutex<Vec<DockerContainer>>>,
    pub volumes: Arc<Mutex<BTreeMap<String, DockerVolume>>>,
    pub calls: Arc<Mutex<Vec<String>>>,
}

impl DockerClient for FakeDocker {
    async fn inspect_container(&self, _: &str) -> Result<Option<DockerContainer>, DockerError> {
        panic!("unexpected single container inspection")
    }
    async fn inspect_network(&self, _: &str) -> Result<DockerNetwork, DockerError> {
        panic!("unexpected network inspection")
    }
    async fn inspect_volume(&self, name: &str) -> Result<Option<DockerVolume>, DockerError> {
        self.calls.lock().unwrap().push(format!("inspect {name}"));
        Ok(self.volumes.lock().unwrap().get(name).cloned())
    }
    async fn create_volume(
        &self,
        name: &str,
        labels: &BTreeMap<String, String>,
    ) -> Result<DockerVolume, DockerError> {
        self.calls.lock().unwrap().push(format!("create {name}"));
        let volume = DockerVolume {
            name: name.into(),
            created_at: "2026-09-25".into(),
            mountpoint: "/var/lib/docker/owned/_data".into(),
            labels: labels.clone(),
        };
        self.volumes
            .lock()
            .unwrap()
            .insert(name.into(), volume.clone());
        Ok(volume)
    }
    async fn remove_volume(&self, _: &str) -> Result<(), DockerError> {
        panic!("creation cannot remove a volume")
    }
    async fn list_containers(&self) -> Result<Vec<DockerContainer>, DockerError> {
        self.calls.lock().unwrap().push("containers".into());
        Ok(self.containers.lock().unwrap().clone())
    }
}

pub struct FakeAccess(pub Client);
impl TenantAccess for FakeAccess {
    async fn connect(
        &self,
        _: Client,
        _: &DynamicObject,
        _: &str,
        endpoint: &str,
    ) -> Result<Client, TenantClientError> {
        assert!(endpoint.ends_with(":6443"));
        Ok(self.0.clone())
    }
}

#[derive(Clone, Default)]
pub struct FakeDeletion(pub Arc<Mutex<Vec<(String, String)>>>);
impl DeletionHandler for FakeDeletion {
    async fn reconcile(
        &self,
        tenant: &Tenant,
        supported_version: &str,
    ) -> Result<Action, ReconcileError> {
        self.0
            .lock()
            .unwrap()
            .push((tenant.name_any(), supported_version.into()));
        Ok(Action::requeue(std::time::Duration::from_secs(5)))
    }
}
