use std::{path::PathBuf, sync::Arc};

use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::get,
};
use tenant_admin_shared::{
    ApiEnvelope,
    query::{
        ManagementComponentView, ManagementOverview, OverviewSnapshot, ProviderMode,
        TenantClassification, TenantCounts, TenantSnapshot, TenantSnapshotIdentity, TenantSummary,
        TopologyGraph,
    },
    routes::{
        API_OVERVIEW_PATH, API_TENANT_PATH, API_TENANT_TOPOLOGY_PATH, API_TENANTS_PATH,
        READINESS_PATH,
    },
};
use tower_http::{
    services::{ServeDir, ServeFile},
    trace::TraceLayer,
};

use crate::{AppError, DataSource, TenantProjection, project_summary};

#[derive(Clone)]
pub struct AppState {
    source: Arc<dyn DataSource>,
    provider: ProviderMode,
}

impl AppState {
    pub fn new(source: Arc<dyn DataSource>, provider: ProviderMode) -> Self {
        Self { source, provider }
    }
}

pub fn router(state: AppState, web_directory: PathBuf) -> Router {
    let index = web_directory.join("index.html");
    let static_files = ServeDir::new(web_directory).fallback(ServeFile::new(index));
    Router::new()
        .route("/healthz", get(healthz))
        .route(READINESS_PATH, get(readyz))
        .route(API_OVERVIEW_PATH, get(overview))
        .route(API_TENANTS_PATH, get(tenants))
        .route(API_TENANT_PATH, get(tenant_detail))
        .route(API_TENANT_TOPOLOGY_PATH, get(tenant_topology))
        .route("/api/{*path}", get(api_not_found))
        .fallback_service(static_files)
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn healthz() -> StatusCode {
    StatusCode::OK
}

async fn readyz(State(state): State<AppState>) -> StatusCode {
    match state.source.check_ready().await {
        Ok(()) => StatusCode::OK,
        Err(_) => StatusCode::SERVICE_UNAVAILABLE,
    }
}

async fn overview(
    State(state): State<AppState>,
) -> Result<Json<ApiEnvelope<OverviewSnapshot>>, AppError> {
    let tenants = state.source.list_tenants().await?;
    let summaries = sorted_summaries(state.provider, tenants);
    let overview = ManagementOverview {
        provider_mode: state.provider,
        tenants: counts(&summaries),
        components: vec![ManagementComponentView {
            name: "kubernetes-api".into(),
            ready: true,
            identity: None,
            message: None,
        }],
    };
    Ok(Json(ApiEnvelope::new(OverviewSnapshot {
        overview,
        tenants: summaries,
    })))
}

async fn tenants(
    State(state): State<AppState>,
) -> Result<Json<ApiEnvelope<Vec<TenantSummary>>>, AppError> {
    let tenants = state.source.list_tenants().await?;
    Ok(Json(ApiEnvelope::new(sorted_summaries(
        state.provider,
        tenants,
    ))))
}

async fn tenant_detail(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<ApiEnvelope<TenantSnapshot>>, AppError> {
    validate_tenant_name(&name)?;
    let tenant = state
        .source
        .get_tenant(&name)
        .await?
        .ok_or_else(|| AppError::not_found(format!("Tenant {name} was not found")))?;
    let resources = state
        .source
        .list_management_resources(state.provider, &name)
        .await?;
    let projection = TenantProjection::new(state.provider, tenant, resources);
    let identity = TenantSnapshotIdentity {
        uid: projection.detail.uid.clone(),
        generation: projection.detail.generation,
        observed_generation: projection.detail.observed_generation,
    };
    Ok(Json(ApiEnvelope::new(TenantSnapshot {
        identity,
        detail: projection.detail,
        topology: projection.topology,
    })))
}

async fn tenant_topology(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<ApiEnvelope<TopologyGraph>>, AppError> {
    validate_tenant_name(&name)?;
    let tenant = state
        .source
        .get_tenant(&name)
        .await?
        .ok_or_else(|| AppError::not_found(format!("Tenant {name} was not found")))?;
    let resources = state
        .source
        .list_management_resources(state.provider, &name)
        .await?;
    Ok(Json(ApiEnvelope::new(
        TenantProjection::new(state.provider, tenant, resources).topology,
    )))
}

async fn api_not_found() -> impl IntoResponse {
    AppError::not_found("API route was not found")
}

fn sorted_summaries(
    provider: ProviderMode,
    tenants: Vec<tenant_controller::api::Tenant>,
) -> Vec<TenantSummary> {
    let mut summaries: Vec<_> = tenants
        .iter()
        .map(|tenant| project_summary(provider, tenant))
        .collect();
    summaries.sort_by(|left, right| left.name.cmp(&right.name));
    summaries
}

fn counts(summaries: &[TenantSummary]) -> TenantCounts {
    let mut counts = TenantCounts {
        total: usize_u32(summaries.len()),
        ..TenantCounts::default()
    };
    for summary in summaries {
        match summary.classification {
            TenantClassification::Ready => counts.ready = counts.ready.saturating_add(1),
            TenantClassification::Progressing => {
                counts.progressing = counts.progressing.saturating_add(1);
            }
            TenantClassification::Degraded | TenantClassification::OwnershipInvalid => {
                counts.degraded = counts.degraded.saturating_add(1);
            }
            TenantClassification::Failed => counts.failed = counts.failed.saturating_add(1),
            TenantClassification::Deleting => {
                counts.deleting = counts.deleting.saturating_add(1);
            }
        }
    }
    counts
}

fn validate_tenant_name(name: &str) -> Result<(), AppError> {
    let bytes = name.as_bytes();
    if !(1..=30).contains(&bytes.len())
        || !bytes
            .first()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        || !bytes
            .last()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        || !bytes
            .iter()
            .all(|value| value.is_ascii_lowercase() || value.is_ascii_digit() || *value == b'-')
    {
        return Err(AppError::invalid_request(
            "Tenant name must be a lowercase DNS label of at most 30 characters",
        ));
    }
    Ok(())
}

fn usize_u32(value: usize) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        future::ready,
        path::PathBuf,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        },
    };

    use axum::{
        body::Body,
        http::{Request, StatusCode},
    };
    use http_body_util::BodyExt;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::{Condition, Time};
    use kube::core::DynamicObject;
    use tenant_admin_shared::{
        ApiErrorCode, ApiErrorEnvelope,
        query::{OverviewSnapshot, TenantSnapshot, TenantSummary, TopologyGraph},
    };
    use tenant_controller::api::{
        LocalProviderStatus, Tenant, TenantPhase, TenantProviderStatus, TenantSpec, TenantStatus,
    };
    use tower::ServiceExt;

    use super::*;
    use crate::{SourceError, SourceFuture};

    #[derive(Clone)]
    struct MockSource {
        tenants: Result<Vec<Tenant>, SourceError>,
        tenant: Result<Option<Tenant>, SourceError>,
        resources: Result<Vec<DynamicObject>, SourceError>,
        ready: Result<(), SourceError>,
        calls: Arc<SourceCalls>,
    }

    #[derive(Default)]
    struct SourceCalls {
        tenant_lists: AtomicUsize,
        tenant_gets: AtomicUsize,
        resource_lists: AtomicUsize,
    }

    impl DataSource for MockSource {
        fn list_tenants(&self) -> SourceFuture<'_, Vec<Tenant>> {
            self.calls.tenant_lists.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.tenants.clone()))
        }

        fn get_tenant(&self, _name: &str) -> SourceFuture<'_, Option<Tenant>> {
            self.calls.tenant_gets.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.tenant.clone()))
        }

        fn list_management_resources(
            &self,
            _provider: ProviderMode,
            _tenant_name: &str,
        ) -> SourceFuture<'_, Vec<DynamicObject>> {
            self.calls.resource_lists.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.resources.clone()))
        }

        fn check_ready(&self) -> SourceFuture<'_, ()> {
            Box::pin(ready(self.ready.clone()))
        }
    }

    fn ready_tenant() -> Tenant {
        let mut tenant = Tenant::new("tenant-a", TenantSpec::local("1.36.4", 1, 1));
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.generation = Some(1);
        tenant.status = Some(TenantStatus {
            observed_generation: Some(1),
            phase: Some(TenantPhase::Ready),
            conditions: vec![Condition {
                type_: "Ready".into(),
                status: "True".into(),
                reason: "Ready".into(),
                message: "ready".into(),
                observed_generation: Some(1),
                last_transition_time: serde_json::from_str::<Time>(r#""2026-01-01T00:00:00Z""#)
                    .expect("time"),
            }],
            provider: Some(TenantProviderStatus::Local(LocalProviderStatus::default())),
        });
        tenant
    }

    fn test_router(source: MockSource) -> Router {
        router(
            AppState::new(Arc::new(source), ProviderMode::Local),
            PathBuf::from("capi/admin/server/tests/fixtures/web"),
        )
    }

    async fn response_json<T: serde::de::DeserializeOwned>(
        response: axum::response::Response,
    ) -> T {
        let body = response
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        serde_json::from_slice(&body).expect("json")
    }

    #[tokio::test]
    async fn empty_routes_and_readiness_are_successful() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);
        for path in ["/healthz", "/readyz"] {
            let response = app
                .clone()
                .oneshot(Request::get(path).body(Body::empty()).expect("request"))
                .await
                .expect("response");
            assert_eq!(response.status(), StatusCode::OK);
        }
        let response = app
            .clone()
            .oneshot(
                Request::get(API_OVERVIEW_PATH)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::OK);
        let envelope: ApiEnvelope<OverviewSnapshot> = response_json(response).await;
        assert_eq!(envelope.data.overview.tenants.total, 0);
        assert!(envelope.data.tenants.is_empty());
        assert_eq!(calls.tenant_lists.load(Ordering::Relaxed), 1);
        let response = app
            .oneshot(
                Request::get(API_TENANTS_PATH)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        let envelope: ApiEnvelope<Vec<TenantSummary>> = response_json(response).await;
        assert!(envelope.data.is_empty());
        assert_eq!(calls.tenant_lists.load(Ordering::Relaxed), 2);
    }

    #[tokio::test]
    async fn overview_snapshot_sorts_summaries_from_one_tenant_scan() {
        let mut tenant_b = ready_tenant();
        tenant_b.metadata.name = Some("tenant-b".into());
        let mut tenant_a = ready_tenant();
        tenant_a.metadata.name = Some("tenant-a".into());
        let source = MockSource {
            tenants: Ok(vec![tenant_b, tenant_a]),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let response = test_router(source)
            .oneshot(
                Request::get(API_OVERVIEW_PATH)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");

        let envelope: ApiEnvelope<OverviewSnapshot> = response_json(response).await;
        assert_eq!(envelope.data.overview.tenants.total, 2);
        assert_eq!(
            envelope
                .data
                .tenants
                .iter()
                .map(|summary| summary.name.as_str())
                .collect::<Vec<_>>(),
            ["tenant-a", "tenant-b"]
        );
        assert_eq!(calls.tenant_lists.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn detail_snapshot_and_topology_return_shared_envelopes() {
        let tenant = ready_tenant();
        let source = MockSource {
            tenants: Ok(vec![tenant.clone()]),
            tenant: Ok(Some(tenant)),
            resources: Ok(Vec::new()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);
        let detail = app
            .clone()
            .oneshot(
                Request::get("/api/v1/tenants/tenant-a")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        let detail: ApiEnvelope<TenantSnapshot> = response_json(detail).await;
        assert_eq!(
            detail.data.detail.summary.classification,
            TenantClassification::Ready
        );
        assert_eq!(detail.data.identity.uid, "tenant-uid");
        assert_eq!(detail.data.identity.generation, 1);
        assert_eq!(detail.data.identity.observed_generation, Some(1));
        assert_eq!(detail.data.topology.tenant_name, "tenant-a");
        assert!(!detail.data.topology.nodes.is_empty());
        assert_eq!(calls.tenant_gets.load(Ordering::Relaxed), 1);
        assert_eq!(calls.resource_lists.load(Ordering::Relaxed), 1);
        let topology = app
            .oneshot(
                Request::get("/api/v1/tenants/tenant-a/topology")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        let topology: ApiEnvelope<TopologyGraph> = response_json(topology).await;
        assert_eq!(topology.data.tenant_name, "tenant-a");
        assert!(!topology.data.nodes.is_empty());
        assert_eq!(calls.tenant_gets.load(Ordering::Relaxed), 2);
        assert_eq!(calls.resource_lists.load(Ordering::Relaxed), 2);
    }

    #[tokio::test]
    async fn not_found_invalid_and_unavailable_are_typed() {
        let app = test_router(MockSource {
            tenants: Err(SourceError::KubernetesUnavailable),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            ready: Err(SourceError::KubernetesUnavailable),
            calls: Arc::default(),
        });
        let missing = app
            .clone()
            .oneshot(
                Request::get("/api/v1/tenants/missing")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(missing.status(), StatusCode::NOT_FOUND);
        let missing: ApiErrorEnvelope = response_json(missing).await;
        assert_eq!(missing.error.code, ApiErrorCode::NotFound);
        let invalid = app
            .clone()
            .oneshot(
                Request::get("/api/v1/tenants/INVALID")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(invalid.status(), StatusCode::BAD_REQUEST);
        let unavailable = app
            .clone()
            .oneshot(
                Request::get(API_TENANTS_PATH)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(unavailable.status(), StatusCode::SERVICE_UNAVAILABLE);
        let unavailable: ApiErrorEnvelope = response_json(unavailable).await;
        assert_eq!(unavailable.error.code, ApiErrorCode::KubernetesUnavailable);
        let readiness = app
            .oneshot(
                Request::get(READINESS_PATH)
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(readiness.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn static_files_use_mime_nested_fallback_and_no_traversal() {
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        let directory = PathBuf::from("target").join(format!(
            "admin-server-web-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(directory.join("assets")).expect("create web fixture");
        fs::write(
            directory.join("index.html"),
            "<main>tenant-admin-shell</main>",
        )
        .expect("write index");
        fs::write(directory.join("assets/app.css"), "body{color:black}").expect("write css");
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let app = router(
            AppState::new(Arc::new(source), ProviderMode::Local),
            directory.clone(),
        );
        let css = app
            .clone()
            .oneshot(
                Request::get("/assets/app.css")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(css.status(), StatusCode::OK);
        assert_eq!(
            css.headers()
                .get("content-type")
                .and_then(|value| value.to_str().ok()),
            Some("text/css")
        );
        let nested = app
            .clone()
            .oneshot(
                Request::get("/tenants/tenant-a")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(nested.status(), StatusCode::OK);
        let nested_body = nested.into_body().collect().await.expect("body").to_bytes();
        assert!(
            nested_body
                .windows(18)
                .any(|value| value == b"tenant-admin-shell")
        );
        let traversal = app
            .oneshot(
                Request::get("/%2e%2e/Cargo.toml")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        let traversal_body = traversal
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        assert!(!traversal_body.windows(9).any(|value| value == b"[package]"));
        fs::remove_dir_all(directory).expect("remove web fixture");
    }
}
