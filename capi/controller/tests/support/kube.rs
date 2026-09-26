use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    convert::Infallible,
    sync::{Arc, Mutex},
};

use axum::http::{Request, Response};
use http_body_util::BodyExt;
use kube::{Client, client::Body};
use serde_json::{Value, json};
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
    pub replacements: BTreeMap<(String, String), VecDeque<Option<Value>>>,
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
                    let query = parts.uri.query().unwrap_or("").to_owned();
                    let mut state = state.lock().unwrap();
                    state.calls.push(Call {
                        method: method.clone(),
                        path: path.clone(),
                        query: query.clone(),
                        content_type: parts
                            .headers
                            .get("content-type")
                            .map(|value| value.to_str().unwrap_or(""))
                            .unwrap_or("")
                            .into(),
                        body: body.clone(),
                    });
                    if let Some(replacement) = state
                        .replacements
                        .get_mut(&(method.clone(), path.clone()))
                        .and_then(VecDeque::pop_front)
                    {
                        match replacement {
                            Some(value) => {
                                state.objects.insert(path.clone(), value);
                            }
                            None => {
                                state.objects.remove(&path);
                            }
                        }
                    }
                    if let Some(response) = state
                        .responses
                        .get_mut(&(method.clone(), path.clone()))
                        .and_then(VecDeque::pop_front)
                    {
                        return Ok::<_, Infallible>(response_body(response.0, response.1));
                    }
                    let (code, result) = match method.as_str() {
                        "GET" if state.lists.contains(&path) => {
                            assert!(
                                query.is_empty()
                                    || (!query.contains("labelSelector")
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
                                json!({
                                    "apiVersion":"v1",
                                    "kind":"List",
                                    "metadata":{"resourceVersion":"1"},
                                    "items":items
                                }),
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
                                    let metadata = body.get("metadata").unwrap_or(&Value::Null);
                                    let uid_matches = metadata["uid"].is_null()
                                        || metadata["uid"] == current["metadata"]["uid"];
                                    let version_matches = metadata["resourceVersion"].is_null()
                                        || metadata["resourceVersion"]
                                            == current["metadata"]["resourceVersion"];
                                    if !uid_matches || !version_matches {
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
                        "DELETE" => match state.objects.get(&path).cloned() {
                            None => (404, status(404, "NotFound")),
                            Some(current) => {
                                let expected = &body["preconditions"];
                                if current["metadata"]["uid"] != expected["uid"]
                                    || current["metadata"]["resourceVersion"]
                                        != expected["resourceVersion"]
                                {
                                    (409, status(409, "Conflict"))
                                } else {
                                    state.objects.remove(&path);
                                    (
                                        200,
                                        json!({
                                            "apiVersion":"v1",
                                            "kind":"Status",
                                            "status":"Success",
                                            "code":200
                                        }),
                                    )
                                }
                            }
                        },
                        _ => panic!("unexpected request {method} {path}"),
                    };
                    Ok(response_body(code, result))
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

    pub fn respond(&self, method: &str, path: &str, code: u16, body: Value) {
        self.0
            .lock()
            .unwrap()
            .responses
            .entry((method.into(), path.into()))
            .or_default()
            .push_back((code, body));
    }

    pub fn replace_on(&self, method: &str, path: &str, replacement: Option<Value>) {
        self.0
            .lock()
            .unwrap()
            .replacements
            .entry((method.into(), path.into()))
            .or_default()
            .push_back(replacement);
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

fn response_body(code: u16, value: Value) -> Response<Body> {
    Response::builder()
        .status(code)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&value).unwrap()))
        .unwrap()
}

pub fn status(code: u16, reason: &str) -> Value {
    json!({
        "apiVersion":"v1",
        "kind":"Status",
        "status":"Failure",
        "code":code,
        "reason":reason,
        "message":"test request failed"
    })
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
