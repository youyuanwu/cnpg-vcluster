use std::{
    collections::BTreeMap,
    net::SocketAddr,
    sync::{Arc, Mutex},
};

use axum::{
    Json, Router,
    extract::{Query, State},
    http::StatusCode,
    routing::get,
};
use futures::StreamExt;
use k8s_openapi::api::core::v1::Namespace;
use kube::{
    Api, Client, ResourceExt,
    api::{ListParams, WatchParams},
    runtime::Controller,
    runtime::watcher,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tenant_database_controller::{api::TenantDatabaseCatalog, reconcile};

struct Context {
    client: Client,
    pod_uid: String,
    instance_id: String,
    observations: Mutex<BTreeMap<String, Snapshot>>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Snapshot {
    uid: String,
    generation: i64,
    resource_version: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ObservationQuery {
    namespace: String,
    name: String,
    #[serde(rename = "catalogUID")]
    catalog_uid: String,
    resource_version: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ReadyReceipt<'a> {
    #[serde(rename = "podUID")]
    pod_uid: &'a str,
    instance_id: &'a str,
}

fn key(namespace: &str, name: &str) -> String {
    format!("{namespace}/{name}")
}

fn snapshot(catalog: &TenantDatabaseCatalog) -> Option<Snapshot> {
    let uid = catalog.metadata.uid.as_ref()?.clone();
    let resource_version = catalog.metadata.resource_version.as_ref()?.clone();
    if uid.is_empty() || resource_version.is_empty() {
        return None;
    }
    Some(Snapshot {
        uid,
        generation: catalog.metadata.generation?,
        resource_version,
    })
}

fn matches_observation(
    catalog: &TenantDatabaseCatalog,
    expected: &Snapshot,
    pod_uid: &str,
    instance_id: &str,
) -> bool {
    snapshot(catalog).as_ref() == Some(expected)
        && catalog
            .status
            .as_ref()
            .and_then(|status| status.observer.as_ref())
            .is_some_and(|observer| {
                observer.catalog_uid == expected.uid
                    && observer.observed_generation == expected.generation
                    && !observer.observed_resource_version.is_empty()
                    && observer.pod_uid == pod_uid
                    && observer.instance_id == instance_id
            })
}

async fn readyz(State(context): State<Arc<Context>>) -> Result<Json<Value>, StatusCode> {
    let snapshots = context
        .observations
        .lock()
        .expect("observer state is intact")
        .clone();
    for (key, expected) in snapshots {
        if let Some((namespace, name)) = key.split_once('/')
            && let Ok(current) =
                Api::<TenantDatabaseCatalog>::namespaced(context.client.clone(), namespace)
                    .get(name)
                    .await
            && matches_observation(&current, &expected, &context.pod_uid, &context.instance_id)
            && let Ok(verified) = reconcile::verify_current(context.client.clone(), &current).await
            && matches_observation(&verified, &expected, &context.pod_uid, &context.instance_id)
            && context
                .observations
                .lock()
                .expect("observer state is intact")
                .get(&key)
                == Some(&expected)
        {
            return Ok(Json(json!(ReadyReceipt {
                pod_uid: &context.pod_uid,
                instance_id: &context.instance_id,
            })));
        }
    }
    Err(StatusCode::SERVICE_UNAVAILABLE)
}

async fn observation(
    State(context): State<Arc<Context>>,
    Query(query): Query<ObservationQuery>,
) -> Result<Json<Value>, StatusCode> {
    let key = key(&query.namespace, &query.name);
    let expected = context
        .observations
        .lock()
        .expect("observer state is intact")
        .get(&key)
        .cloned()
        .ok_or(StatusCode::SERVICE_UNAVAILABLE)?;
    if expected.uid != query.catalog_uid || expected.resource_version != query.resource_version {
        return Err(StatusCode::SERVICE_UNAVAILABLE);
    }
    let catalogs =
        Api::<TenantDatabaseCatalog>::namespaced(context.client.clone(), &query.namespace);
    let current = catalogs
        .get(&query.name)
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    if !matches_observation(&current, &expected, &context.pod_uid, &context.instance_id) {
        return Err(StatusCode::SERVICE_UNAVAILABLE);
    }
    let verified = reconcile::verify_current(context.client.clone(), &current)
        .await
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    if !matches_observation(&verified, &expected, &context.pod_uid, &context.instance_id)
        || context
            .observations
            .lock()
            .expect("observer state is intact")
            .get(&key)
            != Some(&expected)
    {
        return Err(StatusCode::SERVICE_UNAVAILABLE);
    }
    Ok(Json(json!({
        "observer": verified.status.as_ref().and_then(|status| status.observer.as_ref()),
        "resourceVersion": expected.resource_version,
    })))
}

async fn shutdown() {
    #[cfg(unix)]
    {
        let Ok(mut terminate) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        else {
            tracing::error!("SIGTERM handler unavailable");
            return;
        };
        tokio::select! {
            _ = terminate.recv() => {},
            result = tokio::signal::ctrl_c() => {
                if let Err(error) = result {
                    tracing::error!(%error, "SIGINT handler unavailable");
                }
            }
        }
    }
    #[cfg(not(unix))]
    if let Err(error) = tokio::signal::ctrl_c().await {
        tracing::error!(%error, "shutdown signal unavailable");
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "tenant_database_controller=info".into()),
        )
        .json()
        .try_init()?;
    let address: SocketAddr = std::env::var("HEALTH_ADDRESS")
        .unwrap_or_else(|_| "0.0.0.0:8082".into())
        .parse()?;
    let client = Client::try_default().await?;
    let catalogs = Api::<TenantDatabaseCatalog>::all(client.clone());
    catalogs.list(&ListParams::default().limit(1)).await?;
    let _ = catalogs
        .watch(&WatchParams::default().timeout(5), "0")
        .await?;
    reconcile::tenant_api(client.clone())
        .list(&ListParams::default().limit(1))
        .await?;
    Api::<Namespace>::all(client.clone())
        .list(&ListParams::default().limit(1))
        .await?;
    let pod_uid = std::env::var("POD_UID")?;
    if pod_uid.is_empty() {
        return Err("POD_UID must be set by the downward API".into());
    }
    let instance_id = std::fs::read_to_string("/proc/sys/kernel/random/uuid")?
        .trim()
        .to_string();
    if instance_id.is_empty() {
        return Err("observer instance identity unavailable".into());
    }
    let context = Arc::new(Context {
        client,
        pod_uid,
        instance_id,
        observations: Mutex::new(BTreeMap::new()),
    });
    let health = Router::new()
        .route("/healthz", get(|| async { StatusCode::OK }))
        .route("/readyz", get(readyz))
        .route("/observation", get(observation))
        .with_state(context.clone());
    let listener = tokio::net::TcpListener::bind(address).await?;
    let server = tokio::spawn(async move { axum::serve(listener, health).await });
    Controller::new(catalogs, watcher::Config::default())
        .graceful_shutdown_on(shutdown())
        .run(
            |catalog, context: Arc<Context>| async move {
                let result = reconcile::observe(
                    context.client.clone(),
                    &catalog,
                    &context.pod_uid,
                    &context.instance_id,
                )
                .await;
                let key = key(
                    &catalog.namespace().unwrap_or_default(),
                    &catalog.name_any(),
                );
                let mut observations = context
                    .observations
                    .lock()
                    .expect("observer state is intact");
                match &result {
                    Ok((_, verified)) => {
                        if let Some(value) = snapshot(verified) {
                            observations.insert(key, value);
                        } else {
                            observations.remove(&key);
                        }
                    }
                    Err(_) => {
                        observations.remove(&key);
                    }
                }
                result.map(|(action, _)| action)
            },
            |catalog, error, _| {
                tracing::warn!(
                    catalog = %catalog.name_any(),
                    namespace = ?catalog.namespace(),
                    %error,
                    "catalog observation blocked"
                );
                reconcile::error_policy()
            },
            context.clone(),
        )
        .for_each_concurrent(1, |result| {
            let context = context.clone();
            async move {
                if let Err(error) = result {
                    context
                        .observations
                        .lock()
                        .expect("observer state is intact")
                        .clear();
                    tracing::warn!(%error, "catalog watch error");
                }
            }
        })
        .await;
    context
        .observations
        .lock()
        .expect("observer state is intact")
        .clear();
    server.abort();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tenant_database_controller::api::{
        CatalogObservation, CatalogStatus, TenantDatabaseCatalogSpec,
    };

    #[test]
    fn receipt_requires_exact_live_identity_version_and_process() {
        let mut catalog = TenantDatabaseCatalog::new(
            "probe",
            TenantDatabaseCatalogSpec {
                tenant_name: "probe".into(),
                tenant_uid: "tenant-uid".into(),
                closed: false,
                entries: Default::default(),
            },
        );
        catalog.metadata.uid = Some("catalog-uid".into());
        catalog.metadata.generation = Some(1);
        catalog.metadata.resource_version = Some("12".into());
        catalog.status = Some(CatalogStatus {
            entries: Default::default(),
            observer: Some(CatalogObservation {
                catalog_uid: "catalog-uid".into(),
                observed_generation: 1,
                observed_resource_version: "11".into(),
                pod_uid: "pod-uid".into(),
                instance_id: "boot-a".into(),
            }),
        });
        let expected = snapshot(&catalog).unwrap();
        assert!(matches_observation(
            &catalog, &expected, "pod-uid", "boot-a"
        ));
        assert!(!matches_observation(
            &catalog, &expected, "pod-uid", "boot-b"
        ));
        assert!(!matches_observation(
            &catalog,
            &expected,
            "replacement-pod",
            "boot-a"
        ));
        catalog.metadata.resource_version = Some("13".into());
        assert!(!matches_observation(
            &catalog, &expected, "pod-uid", "boot-a"
        ));
        catalog.metadata.resource_version = Some("12".into());
        catalog.metadata.uid = Some("replacement".into());
        assert!(!matches_observation(
            &catalog, &expected, "pod-uid", "boot-a"
        ));
        catalog.metadata.uid = Some("catalog-uid".into());
        catalog.metadata.generation = Some(2);
        assert!(!matches_observation(
            &catalog, &expected, "pod-uid", "boot-a"
        ));
        catalog.metadata.generation = Some(1);
        catalog
            .status
            .as_mut()
            .unwrap()
            .observer
            .as_mut()
            .unwrap()
            .catalog_uid = "old".into();
        assert!(!matches_observation(
            &catalog, &expected, "pod-uid", "boot-a"
        ));
    }
}
