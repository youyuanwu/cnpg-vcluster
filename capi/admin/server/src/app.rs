use std::{
    convert::Infallible,
    net::IpAddr,
    path::{Path as FilePath, PathBuf},
    sync::Arc,
};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, Path, State, rejection::BytesRejection},
    http::{HeaderMap, Request, StatusCode, header, uri::Authority},
    middleware::{self, Next},
    response::IntoResponse,
    routing::{get, post},
};
use tenant_admin_shared::{
    ApiEnvelope,
    catalog::{
        CatalogQueryRequest, CatalogQueryResponse, CatalogView, DatabaseAddRequest,
        DatabaseDeleteRequest,
    },
    lifecycle::{
        TenantCreateRequest, TenantCreateResponse, TenantDeleteRequest, TenantDeleteResponse,
        TenantDeleteState, TenantField, TenantFieldError, TenantMutationIdentity,
    },
    query::{
        DatabaseObservation, DatabaseObservationFreshness, DatabaseQueryRequest,
        DatabaseQueryResponse, DatabaseUnavailableReason, ManagementComponentView,
        ManagementOverview, OverviewSnapshot, ProviderMode, TenantClassification, TenantCounts,
        TenantSnapshot, TenantSnapshotIdentity, TenantSummary, TopologyGraph,
    },
    routes::{
        API_DATABASE_PATH, API_DATABASE_QUERY_PATH, API_DATABASES_PATH, API_OVERVIEW_PATH,
        API_TENANT_DATABASE_QUERY_PATH, API_TENANT_PATH, API_TENANT_TOPOLOGY_PATH,
        API_TENANTS_PATH, READINESS_PATH, TENANT_ADMIN_UNSAFE_REQUEST_HEADER,
        TENANT_ADMIN_UNSAFE_REQUEST_VALUE,
    },
};
use tenant_controller::api::{Tenant, TenantProviderSpec, TenantSpec, canonical_spec};
use tower::{ServiceExt, service_fn};
use tower_http::{
    services::{ServeDir, ServeFile},
    trace::TraceLayer,
};

use crate::projection::merge_catalog_topology;
use crate::{AppError, DataSource, SourceError, TenantProjection, project_summary};

const MAX_DATABASE_QUERY_BODY_BYTES: usize = 128 * 1_024;
const MAX_TENANT_MUTATION_BODY_BYTES: usize = 16 * 1_024;

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
    let index_file = ServeFile::new(index);
    let static_files =
        ServeDir::new(web_directory).fallback(service_fn(move |request: Request<Body>| {
            let index_file = index_file.clone();
            async move {
                let path = request.uri().path();
                let has_extension = path
                    .rsplit('/')
                    .next()
                    .is_some_and(|segment| FilePath::new(segment).extension().is_some());
                if has_extension {
                    return Ok::<_, Infallible>(StatusCode::NOT_FOUND.into_response());
                }
                let response = match index_file.oneshot(request).await {
                    Ok(response) => response.map(Body::new),
                    Err(infallible) => match infallible {},
                };
                Ok(response)
            }
        }));
    Router::new()
        .route("/healthz", get(healthz))
        .route(READINESS_PATH, get(readyz))
        .route(API_OVERVIEW_PATH, get(overview))
        .route(
            API_TENANTS_PATH,
            get(tenants)
                .post(tenant_create)
                .layer(DefaultBodyLimit::max(MAX_TENANT_MUTATION_BODY_BYTES)),
        )
        .route(
            API_TENANT_PATH,
            get(tenant_detail)
                .delete(tenant_delete)
                .layer(DefaultBodyLimit::max(MAX_TENANT_MUTATION_BODY_BYTES)),
        )
        .route(API_TENANT_TOPOLOGY_PATH, get(tenant_topology))
        .route(
            API_DATABASES_PATH,
            get(database_list)
                .post(database_add)
                .layer(DefaultBodyLimit::max(MAX_TENANT_MUTATION_BODY_BYTES)),
        )
        .route(
            API_DATABASE_PATH,
            axum::routing::delete(database_delete)
                .layer(DefaultBodyLimit::max(MAX_TENANT_MUTATION_BODY_BYTES)),
        )
        .route(
            API_DATABASE_QUERY_PATH,
            post(catalog_query).layer(DefaultBodyLimit::max(MAX_DATABASE_QUERY_BODY_BYTES)),
        )
        .route(
            API_TENANT_DATABASE_QUERY_PATH,
            post(database_query).layer(DefaultBodyLimit::max(MAX_DATABASE_QUERY_BODY_BYTES)),
        )
        .route("/api/{*path}", get(api_not_found))
        .fallback_service(static_files)
        .layer(middleware::from_fn(no_store_html))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

async fn catalog_tenant(state: &AppState, name: &str) -> Result<Tenant, AppError> {
    validate_tenant_name(name)?;
    let tenant = state
        .source
        .get_tenant(name)
        .await?
        .ok_or_else(|| AppError::not_found(format!("Tenant {name} was not found")))?;
    let matches = matches!(
        (state.provider, &tenant.spec.provider),
        (ProviderMode::Local, TenantProviderSpec::Local)
            | (ProviderMode::Azure, TenantProviderSpec::Azure)
    );
    if !matches {
        return Err(AppError::database_unavailable(
            "Tenant provider does not match Admin",
            false,
        ));
    }
    Ok(tenant)
}

async fn database_list(
    State(state): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<ApiEnvelope<CatalogView>>, AppError> {
    let tenant = catalog_tenant(&state, &name).await?;
    Ok(Json(ApiEnvelope::new(
        state.source.read_catalog(&tenant).await?,
    )))
}

async fn database_add(
    State(state): State<AppState>,
    Path(name): Path<String>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<(StatusCode, Json<ApiEnvelope<CatalogView>>), AppError> {
    validate_request_origin(&headers)?;
    let tenant = catalog_tenant(&state, &name).await?;
    let body = body.map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    let request: DatabaseAddRequest = serde_json::from_slice(&body)
        .map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    if !tenant_database_controller::api::valid_logical_uid(&request.catalog_uid)
        || !tenant_database_controller::api::valid_name(&request.name)
        || !(1..=3).contains(&request.instances)
    {
        return Err(AppError::invalid_request(
            "Catalog UID, database name or instance count is invalid",
        ));
    }
    let result = state.source.add_database(&tenant, &request).await?;
    Ok((StatusCode::CREATED, Json(ApiEnvelope::new(result))))
}

async fn database_delete(
    State(state): State<AppState>,
    Path((name, uid)): Path<(String, String)>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<(StatusCode, Json<ApiEnvelope<CatalogView>>), AppError> {
    validate_request_origin(&headers)?;
    let tenant = catalog_tenant(&state, &name).await?;
    let body = body.map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    let request: DatabaseDeleteRequest = serde_json::from_slice(&body)
        .map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    if !tenant_database_controller::api::valid_logical_uid(&uid)
        || !tenant_database_controller::api::valid_logical_uid(&request.catalog_uid)
        || request.logical_uid != uid
        || !tenant_database_controller::api::valid_name(&request.confirmation)
    {
        return Err(AppError::invalid_request(
            "Exact database identity and name confirmation are required",
        ));
    }
    let result = state.source.delete_database(&tenant, &request).await?;
    Ok((StatusCode::ACCEPTED, Json(ApiEnvelope::new(result))))
}

async fn catalog_query(
    State(state): State<AppState>,
    Path((name, uid)): Path<(String, String)>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<Json<ApiEnvelope<CatalogQueryResponse>>, AppError> {
    validate_request_origin(&headers)?;
    let tenant = catalog_tenant(&state, &name).await?;
    let body = body.map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    let request: CatalogQueryRequest = serde_json::from_slice(&body)
        .map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    if !tenant_database_controller::api::valid_logical_uid(&uid)
        || uid != request.logical_uid
        || !tenant_database_controller::api::valid_logical_uid(&request.catalog_uid)
        || request.instance_uid.is_empty()
        || request.instance_uid.len() > 128
        || request.instance_uid.chars().any(char::is_control)
    {
        return Err(AppError::invalid_request(
            "Exact catalog, database and instance identities are required",
        ));
    }
    validate_query_request(&DatabaseQueryRequest {
        instance: request.instance.clone(),
        database: request.database.clone(),
        sql: request.sql.clone(),
    })?;
    Ok(Json(ApiEnvelope::new(
        state.source.query_catalog(&tenant, &request).await?,
    )))
}

async fn no_store_html(request: Request<Body>, next: Next) -> impl IntoResponse {
    let mut response = next.run(request).await;
    if response
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.starts_with("text/html"))
    {
        response.headers_mut().insert(
            header::CACHE_CONTROL,
            header::HeaderValue::from_static("no-store"),
        );
    }
    response
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
    let creation = state.source.creation_capability(state.provider).await?;
    let summaries = sorted_summaries(state.provider, tenants);
    let overview = ManagementOverview {
        provider_mode: state.provider,
        creation,
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

async fn tenant_create(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<(StatusCode, Json<ApiEnvelope<TenantCreateResponse>>), AppError> {
    validate_request_origin(&headers)?;
    let body = body.map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    let request: TenantCreateRequest = serde_json::from_slice(&body)
        .map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    let field_errors = validate_create_request(&request);
    if !field_errors.is_empty() {
        return Err(AppError::invalid_fields(
            "Tenant creation fields are invalid",
            field_errors,
        ));
    }
    let capability = state.source.creation_capability(state.provider).await?;
    if !capability.available {
        return Err(SourceError::CreationUnavailable.into());
    }
    let version = capability
        .supported_kubernetes_version
        .ok_or(SourceError::CreationUnavailable)?;
    let provider = match state.provider {
        ProviderMode::Local => TenantProviderSpec::Local,
        ProviderMode::Azure => TenantProviderSpec::Azure,
    };
    let spec = TenantSpec {
        kubernetes_version: version.clone(),
        workers: i32::try_from(request.workers)
            .map_err(|_| AppError::invalid_request("workers is invalid"))?,
        provider,
    };
    let canonical = canonical_spec(&request.name, &spec, &version)
        .map_err(|error| AppError::invalid_request(error.to_string()))?;
    let created = state
        .source
        .create_tenant(Tenant::new(&request.name, canonical))
        .await?;
    let response = TenantCreateResponse {
        identity: mutation_identity(&created)?,
        provider: match state.provider {
            ProviderMode::Local => tenant_admin_shared::query::TenantProvider::Local,
            ProviderMode::Azure => tenant_admin_shared::query::TenantProvider::Azure,
        },
        kubernetes_version: version,
    };
    Ok((StatusCode::CREATED, Json(ApiEnvelope::new(response))))
}

async fn tenant_delete(
    State(state): State<AppState>,
    Path(name): Path<String>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<(StatusCode, Json<ApiEnvelope<TenantDeleteResponse>>), AppError> {
    validate_tenant_name(&name)?;
    validate_request_origin(&headers)?;
    let body = body.map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    let request: TenantDeleteRequest = serde_json::from_slice(&body)
        .map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    if request.confirmation != name {
        return Err(AppError::invalid_request(
            "Deletion confirmation must exactly match the Tenant name",
        ));
    }
    if request.uid.is_empty()
        || request.uid.len() > 128
        || request.uid.chars().any(char::is_control)
    {
        return Err(AppError::invalid_request("Tenant UID is invalid"));
    }
    let response = state.source.delete_tenant(&name, &request.uid).await?;
    let status = match response.state {
        TenantDeleteState::Accepted => StatusCode::ACCEPTED,
        TenantDeleteState::Completed => StatusCode::OK,
    };
    Ok((status, Json(ApiEnvelope::new(response))))
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
    let projection = projected_tenant(&state, tenant, resources).await?;
    let identity = TenantSnapshotIdentity {
        uid: projection.detail.uid.clone(),
        generation: projection.detail.generation,
        observed_generation: projection.detail.observed_generation,
    };
    Ok(Json(ApiEnvelope::new(TenantSnapshot {
        identity,
        detail: projection.detail,
        database: projection.database,
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
        projected_tenant(&state, tenant, resources).await?.topology,
    )))
}

async fn projected_tenant(
    state: &AppState,
    tenant: Tenant,
    resources: Vec<kube::core::DynamicObject>,
) -> Result<TenantProjection, AppError> {
    let catalog = if tenant
        .status
        .as_ref()
        .and_then(|status| status.database_capability.as_ref())
        .is_some()
    {
        Some(state.source.read_catalog(&tenant).await?)
    } else {
        None
    };
    let database = if catalog.is_some() {
        DatabaseObservation::Unavailable {
            observed_at: chrono::Utc::now().to_rfc3339(),
            freshness: DatabaseObservationFreshness::Live,
            reason: DatabaseUnavailableReason::Pending,
            message: "Use the catalog database endpoint for per-cluster observations".into(),
            retryable: false,
        }
    } else {
        state
            .source
            .database_observation(state.provider, &tenant, &resources)
            .await?
    };
    let mut projection = TenantProjection::new(state.provider, tenant, resources, database);
    if let Some(catalog) = catalog {
        merge_catalog_topology(&mut projection.topology, &catalog);
    }
    Ok(projection)
}

async fn database_query(
    State(state): State<AppState>,
    Path(name): Path<String>,
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<Json<ApiEnvelope<DatabaseQueryResponse>>, AppError> {
    validate_tenant_name(&name)?;
    validate_request_origin(&headers)?;
    let body = body.map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    let request: DatabaseQueryRequest = serde_json::from_slice(&body)
        .map_err(|_| AppError::invalid_request("Request body must be valid JSON"))?;
    validate_query_request(&request)?;
    if state.provider != ProviderMode::Local {
        return Err(AppError::database_unavailable(
            "Database queries are available only for local Tenants",
            false,
        ));
    }
    let tenant = state
        .source
        .get_tenant(&name)
        .await?
        .ok_or_else(|| AppError::not_found(format!("Tenant {name} was not found")))?;
    if tenant
        .status
        .as_ref()
        .and_then(|status| status.database_capability.as_ref())
        .is_some()
    {
        return Err(AppError::database_unavailable(
            "Select an exact catalog database and instance for queries",
            false,
        ));
    }
    if !matches!(tenant.spec.provider, TenantProviderSpec::Local) {
        return Err(AppError::database_unavailable(
            "Database queries are available only for local Tenants",
            false,
        ));
    }
    let resources = state
        .source
        .list_management_resources(state.provider, &name)
        .await?;
    let response = state
        .source
        .database_query(state.provider, &tenant, &resources, &request)
        .await?;
    Ok(Json(ApiEnvelope::new(response)))
}

fn validate_request_origin(headers: &HeaderMap) -> Result<(), AppError> {
    let mut origins = headers.get_all(header::ORIGIN).iter();
    let Some(origin) = origins.next() else {
        return Ok(());
    };
    if origins.next().is_some() {
        return Err(AppError::invalid_request("Request Origin is not allowed"));
    }
    let mut unsafe_headers = headers.get_all(TENANT_ADMIN_UNSAFE_REQUEST_HEADER).iter();
    if unsafe_headers
        .next()
        .filter(|value| value.as_bytes() == TENANT_ADMIN_UNSAFE_REQUEST_VALUE.as_bytes())
        .filter(|_| unsafe_headers.next().is_none())
        .is_none()
    {
        return Err(AppError::invalid_request(
            "Unsafe browser request header is required",
        ));
    }
    let origin = origin
        .to_str()
        .map_err(|_| AppError::invalid_request("Request Origin is not allowed"))?;
    let (_scheme, origin_authority) = origin
        .split_once("://")
        .filter(|(scheme, authority)| {
            matches!(*scheme, "http" | "https")
                && !authority.is_empty()
                && !authority
                    .chars()
                    .any(|character| character.is_whitespace() || "/@?#".contains(character))
        })
        .ok_or_else(|| AppError::invalid_request("Request Origin is not allowed"))?;
    let origin_authority = origin_authority
        .parse::<Authority>()
        .map_err(|_| AppError::invalid_request("Request Origin is not allowed"))?;

    let mut hosts = headers.get_all(header::HOST).iter();
    let host = hosts
        .next()
        .filter(|_| hosts.next().is_none())
        .and_then(|host| host.to_str().ok())
        .ok_or_else(|| AppError::invalid_request("Request Origin is not allowed"))?;
    if host.contains('@') {
        return Err(AppError::invalid_request("Request Origin is not allowed"));
    }
    let host_authority = host
        .parse::<Authority>()
        .map_err(|_| AppError::invalid_request("Request Origin is not allowed"))?;
    if !origin_authority
        .host()
        .eq_ignore_ascii_case(host_authority.host())
        || origin_authority.port_u16() != host_authority.port_u16()
    {
        return Err(AppError::invalid_request("Request Origin is not allowed"));
    }
    let hostname = host_authority.host();
    let ip_candidate = hostname
        .strip_prefix('[')
        .and_then(|hostname| hostname.strip_suffix(']'))
        .unwrap_or(hostname);
    if !hostname.eq_ignore_ascii_case("localhost") && ip_candidate.parse::<IpAddr>().is_err() {
        return Err(AppError::invalid_request(
            "Request Host is not allowed for browser mutation requests",
        ));
    }
    Ok(())
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

fn validate_create_request(request: &TenantCreateRequest) -> Vec<TenantFieldError> {
    let mut errors = Vec::new();
    if !valid_tenant_name(&request.name) {
        errors.push(TenantFieldError {
            field: TenantField::Name,
            code: "invalid-name".into(),
            message: "Use a 1 to 30 character lowercase DNS label.".into(),
        });
    }
    if !(1..=3).contains(&request.workers) {
        errors.push(TenantFieldError {
            field: TenantField::Workers,
            code: "invalid-count".into(),
            message: "Workers must be from 1 through 3.".into(),
        });
    }
    errors.sort_by_key(|error| error.field);
    errors
}

fn mutation_identity(tenant: &Tenant) -> Result<TenantMutationIdentity, AppError> {
    Ok(TenantMutationIdentity {
        name: tenant
            .metadata
            .name
            .clone()
            .ok_or_else(|| AppError::internal("Kubernetes returned a Tenant without a name"))?,
        uid: tenant
            .metadata
            .uid
            .clone()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| AppError::internal("Kubernetes returned a Tenant without a UID"))?,
        generation: tenant.metadata.generation,
    })
}

fn valid_tenant_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=30).contains(&bytes.len())
        && bytes
            .first()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        && bytes
            .last()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        && bytes
            .iter()
            .all(|value| value.is_ascii_lowercase() || value.is_ascii_digit() || *value == b'-')
}

fn validate_tenant_name(name: &str) -> Result<(), AppError> {
    if !valid_tenant_name(name) {
        return Err(AppError::invalid_request(
            "Tenant name must be a lowercase DNS label of at most 30 characters",
        ));
    }
    Ok(())
}

fn validate_query_request(request: &DatabaseQueryRequest) -> Result<(), AppError> {
    if request.sql.trim().is_empty() || request.sql.len() > 64 * 1_024 {
        return Err(AppError::invalid_request(
            "SQL must be nonempty and at most 64 KiB",
        ));
    }
    if request.database.is_empty()
        || request.database.len() > 63
        || request
            .database
            .chars()
            .any(|character| character == '\0' || character.is_control())
    {
        return Err(AppError::invalid_request(
            "Database name must be 1 to 63 bytes without control characters",
        ));
    }
    if !is_dns_label(&request.instance, 63) {
        return Err(AppError::invalid_request(
            "Database instance must be a valid DNS label of at most 63 bytes",
        ));
    }
    Ok(())
}

fn is_dns_label(value: &str, maximum: usize) -> bool {
    let bytes = value.as_bytes();
    (1..=maximum).contains(&bytes.len())
        && bytes
            .first()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        && bytes
            .last()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        && bytes
            .iter()
            .all(|value| value.is_ascii_lowercase() || value.is_ascii_digit() || *value == b'-')
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
            Arc, Mutex,
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
        lifecycle::{
            CreationCapability, TenantDeleteResponse, TenantDeleteState, TenantMutationIdentity,
        },
        query::{
            ConditionStatus, DatabaseClusterIdentity, DatabaseClusterObservation,
            DatabaseCondition, DatabaseInstanceObservation, DatabaseInstanceRole,
            DatabaseObservation, DatabaseObservationFreshness, DatabasePvcHealth,
            DatabaseQueryRequest, DatabaseQueryResponse, DatabaseQueryResult, DatabaseServices,
            OverviewSnapshot, TenantSnapshot, TenantSummary, TopologyGraph,
        },
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
        database: Result<DatabaseObservation, SourceError>,
        query: Result<DatabaseQueryResponse, SourceError>,
        ready: Result<(), SourceError>,
        calls: Arc<SourceCalls>,
    }

    #[derive(Default)]
    struct SourceCalls {
        tenant_lists: AtomicUsize,
        tenant_gets: AtomicUsize,
        resource_lists: AtomicUsize,
        database_observations: AtomicUsize,
        database_queries: AtomicUsize,
        tenant_creates: AtomicUsize,
        tenant_deletes: AtomicUsize,
        created_tenants: Mutex<Vec<Tenant>>,
        catalog_reads: AtomicUsize,
        catalog_adds: AtomicUsize,
        catalog_deletes: AtomicUsize,
        catalog_queries: AtomicUsize,
    }

    impl DataSource for MockSource {
        fn read_catalog<'a>(&'a self, tenant: &'a Tenant) -> SourceFuture<'a, CatalogView> {
            self.calls.catalog_reads.fetch_add(1, Ordering::Relaxed);
            let name = tenant.metadata.name.clone().unwrap();
            Box::pin(ready(Ok(CatalogView {
                tenant: name,
                tenant_uid: tenant.metadata.uid.clone().unwrap(),
                catalog_uid: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into(),
                resource_version: "10".into(),
                closed: false,
                capability_available: true,
                databases: vec![],
            })))
        }

        fn add_database<'a>(
            &'a self,
            tenant: &'a Tenant,
            request: &'a DatabaseAddRequest,
        ) -> SourceFuture<'a, CatalogView> {
            self.calls.catalog_adds.fetch_add(1, Ordering::Relaxed);
            let mut response = self.read_catalog(tenant);
            let request = request.clone();
            Box::pin(async move {
                let mut catalog = response.as_mut().await?;
                catalog.resource_version = request.instances.to_string();
                Ok(catalog)
            })
        }

        fn delete_database<'a>(
            &'a self,
            tenant: &'a Tenant,
            request: &'a DatabaseDeleteRequest,
        ) -> SourceFuture<'a, CatalogView> {
            self.calls.catalog_deletes.fetch_add(1, Ordering::Relaxed);
            let mut response = self.read_catalog(tenant);
            let request = request.clone();
            Box::pin(async move {
                let mut catalog = response.as_mut().await?;
                catalog.resource_version = request.logical_uid;
                Ok(catalog)
            })
        }

        fn query_catalog<'a>(
            &'a self,
            _tenant: &'a Tenant,
            request: &'a CatalogQueryRequest,
        ) -> SourceFuture<'a, CatalogQueryResponse> {
            self.calls.catalog_queries.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(Ok(CatalogQueryResponse {
                catalog_uid: request.catalog_uid.clone(),
                logical_uid: request.logical_uid.clone(),
                instance: request.instance.clone(),
                instance_uid: request.instance_uid.clone(),
                executed_at: "2026-01-01T00:00:00Z".into(),
                duration_ms: 1,
                truncated: false,
                results: vec![],
            })))
        }
        fn list_tenants(&self) -> SourceFuture<'_, Vec<Tenant>> {
            self.calls.tenant_lists.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.tenants.clone()))
        }

        fn get_tenant(&self, _name: &str) -> SourceFuture<'_, Option<Tenant>> {
            self.calls.tenant_gets.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.tenant.clone()))
        }

        fn creation_capability(
            &self,
            _provider: ProviderMode,
        ) -> SourceFuture<'_, CreationCapability> {
            Box::pin(ready(Ok(CreationCapability::available("1.36.4"))))
        }

        fn create_tenant(&self, mut tenant: Tenant) -> SourceFuture<'_, Tenant> {
            self.calls.tenant_creates.fetch_add(1, Ordering::Relaxed);
            self.calls
                .created_tenants
                .lock()
                .unwrap()
                .push(tenant.clone());
            tenant.metadata.uid = Some("created-uid".into());
            tenant.metadata.generation = Some(1);
            Box::pin(ready(Ok(tenant)))
        }

        fn delete_tenant<'a>(
            &'a self,
            name: &'a str,
            uid: &'a str,
        ) -> SourceFuture<'a, TenantDeleteResponse> {
            self.calls.tenant_deletes.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(Ok(TenantDeleteResponse {
                identity: TenantMutationIdentity {
                    name: name.into(),
                    uid: uid.into(),
                    generation: Some(1),
                },
                state: TenantDeleteState::Accepted,
            })))
        }

        fn list_management_resources(
            &self,
            _provider: ProviderMode,
            _tenant_name: &str,
        ) -> SourceFuture<'_, Vec<DynamicObject>> {
            self.calls.resource_lists.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.resources.clone()))
        }

        fn database_observation<'a>(
            &'a self,
            _provider: ProviderMode,
            _tenant: &'a Tenant,
            _management_resources: &'a [DynamicObject],
        ) -> SourceFuture<'a, DatabaseObservation> {
            self.calls
                .database_observations
                .fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.database.clone()))
        }

        fn database_query<'a>(
            &'a self,
            _provider: ProviderMode,
            _tenant: &'a Tenant,
            _management_resources: &'a [DynamicObject],
            _request: &'a DatabaseQueryRequest,
        ) -> SourceFuture<'a, DatabaseQueryResponse> {
            self.calls.database_queries.fetch_add(1, Ordering::Relaxed);
            Box::pin(ready(self.query.clone()))
        }

        fn check_ready(&self) -> SourceFuture<'_, ()> {
            Box::pin(ready(self.ready.clone()))
        }
    }

    fn ready_tenant() -> Tenant {
        let mut tenant = Tenant::new("tenant-a", TenantSpec::local("1.36.4", 1));
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.generation = Some(1);
        tenant.status = Some(TenantStatus {
            observed_generation: Some(1),
            phase: Some(TenantPhase::Ready),
            database_capability: None,
            catalog_create_intent: None,
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

    fn database_observation() -> DatabaseObservation {
        DatabaseObservation::Available {
            observed_at: "2026-09-29T20:50:16Z".into(),
            freshness: DatabaseObservationFreshness::Live,
            cluster: Box::new(DatabaseClusterObservation {
                identity: DatabaseClusterIdentity {
                    api_version: "postgresql.cnpg.io/v1".into(),
                    kind: "Cluster".into(),
                    namespace: "database".into(),
                    name: "capi-postgres".into(),
                    uid: Some("database-uid".into()),
                    generation: 1,
                },
                phase: Some("Cluster in healthy state".into()),
                reason: Some("ClusterIsReady".into()),
                desired_instances: 1,
                observed_instances: 1,
                ready_instances: 1,
                current_primary: Some("capi-postgres-1".into()),
                target_primary: Some("capi-postgres-1".into()),
                current_primary_since: None,
                target_primary_requested_at: None,
                current_primary_failing_since: None,
                image: None,
                timeline: Some(1),
                services: DatabaseServices {
                    read: None,
                    write: None,
                },
                topology_available: true,
                nodes_used: Some(1),
                instances: vec![DatabaseInstanceObservation {
                    name: "capi-postgres-1".into(),
                    role: DatabaseInstanceRole::Primary,
                    status: Some("healthy".into()),
                    timeline: Some(1),
                    node: Some("worker-a".into()),
                    zone: Some("local-a".into()),
                }],
                storage: DatabasePvcHealth {
                    total: 1,
                    healthy: 1,
                    ..DatabasePvcHealth::default()
                },
                conditions: vec![DatabaseCondition {
                    condition_type: "Ready".into(),
                    status: ConditionStatus::True,
                    reason: Some("ClusterIsReady".into()),
                    message: None,
                    observed_generation: Some(1),
                    last_transition_time: None,
                }],
            }),
        }
    }

    fn query_response() -> DatabaseQueryResponse {
        DatabaseQueryResponse {
            tenant: "tenant-a".into(),
            cluster: "capi-postgres".into(),
            instance: "capi-postgres-1".into(),
            database: "postgres".into(),
            executed_at: "2026-09-29T22:00:00Z".into(),
            duration_ms: 12,
            truncated: false,
            results: vec![
                DatabaseQueryResult {
                    columns: vec!["value".into(), "nullable".into()],
                    rows: vec![vec![Some("42".into()), None]],
                    affected_rows: 1,
                    truncated: false,
                },
                DatabaseQueryResult {
                    columns: Vec::new(),
                    rows: Vec::new(),
                    affected_rows: 3,
                    truncated: false,
                },
            ],
        }
    }

    fn test_router(source: MockSource) -> Router {
        test_router_with_provider(source, ProviderMode::Local)
    }

    fn test_router_with_provider(source: MockSource, provider: ProviderMode) -> Router {
        router(
            AppState::new(Arc::new(source), provider),
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
            database: Ok(database_observation()),
            query: Ok(query_response()),
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
        assert!(envelope.data.overview.creation.available);
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
    async fn tenant_create_and_delete_are_guarded_provider_aware_and_uid_bound() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(ready_tenant())),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);
        let create = Request::post(API_TENANTS_PATH)
            .body(Body::from(r#"{"name":"tenant-b","workers":2}"#))
            .unwrap();
        let response = app.clone().oneshot(create).await.unwrap();
        assert_eq!(response.status(), StatusCode::CREATED);
        let envelope: ApiEnvelope<TenantCreateResponse> = response_json(response).await;
        assert_eq!(envelope.data.identity.name, "tenant-b");
        assert_eq!(envelope.data.identity.uid, "created-uid");
        assert_eq!(calls.tenant_creates.load(Ordering::Relaxed), 1);

        let invalid = Request::post(API_TENANTS_PATH)
            .header("origin", "http://127.0.0.1:8080")
            .header("host", "127.0.0.1:8080")
            .header(TENANT_ADMIN_UNSAFE_REQUEST_HEADER, "1")
            .body(Body::from(r#"{"name":"Invalid","workers":0}"#))
            .unwrap();
        let response = app.clone().oneshot(invalid).await.unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let envelope: ApiErrorEnvelope = response_json(response).await;
        assert_eq!(envelope.error.field_errors.len(), 2);
        assert_eq!(calls.tenant_creates.load(Ordering::Relaxed), 1);

        let mismatched = Request::delete("/api/v1/tenants/tenant-a")
            .header("origin", "http://127.0.0.1:8080")
            .header("host", "127.0.0.1:8080")
            .header(TENANT_ADMIN_UNSAFE_REQUEST_HEADER, "1")
            .body(Body::from(r#"{"uid":"tenant-uid","confirmation":"other"}"#))
            .unwrap();
        assert_eq!(
            app.clone().oneshot(mismatched).await.unwrap().status(),
            StatusCode::BAD_REQUEST
        );
        assert_eq!(calls.tenant_deletes.load(Ordering::Relaxed), 0);

        let delete = Request::delete("/api/v1/tenants/tenant-a")
            .body(Body::from(
                r#"{"uid":"tenant-uid","confirmation":"tenant-a"}"#,
            ))
            .unwrap();
        let response = app.oneshot(delete).await.unwrap();
        assert_eq!(response.status(), StatusCode::ACCEPTED);
        assert_eq!(calls.tenant_deletes.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn tenant_create_rejects_provider_inapplicable_fields() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router_with_provider(source, ProviderMode::Azure);
        let request = Request::post(API_TENANTS_PATH)
            .body(Body::from(
                r#"{"name":"tenant-b","workers":1,"databases":1}"#,
            ))
            .unwrap();
        let response = app.oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let envelope: ApiErrorEnvelope = response_json(response).await;
        assert_eq!(envelope.error.code, ApiErrorCode::InvalidRequest);
        assert_eq!(calls.tenant_creates.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn azure_tenant_create_uses_server_provider_and_supported_version() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router_with_provider(source, ProviderMode::Azure);
        let request = Request::post(API_TENANTS_PATH)
            .body(Body::from(r#"{"name":"tenant-b","workers":2}"#))
            .unwrap();
        let response = app.oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::CREATED);
        let created = calls.created_tenants.lock().unwrap();
        assert_eq!(created.len(), 1);
        assert_eq!(created[0].spec.kubernetes_version, "1.36.4");
        assert_eq!(created[0].spec.workers, 2);
        assert!(matches!(
            created[0].spec.provider,
            TenantProviderSpec::Azure
        ));
    }

    #[tokio::test]
    async fn every_browser_tenant_mutation_requires_the_unsafe_request_proof() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);
        for request in [
            Request::post(API_TENANTS_PATH)
                .header("origin", "http://127.0.0.1:8080")
                .header("host", "127.0.0.1:8080")
                .body(Body::from(
                    r#"{"name":"tenant-b","workers":1,"databases":1}"#,
                ))
                .unwrap(),
            Request::delete("/api/v1/tenants/tenant-a")
                .header("origin", "http://127.0.0.1:8080")
                .header("host", "127.0.0.1:8080")
                .body(Body::from(
                    r#"{"uid":"tenant-uid","confirmation":"tenant-a"}"#,
                ))
                .unwrap(),
        ] {
            assert_eq!(
                app.clone().oneshot(request).await.unwrap().status(),
                StatusCode::BAD_REQUEST
            );
        }
        assert_eq!(calls.tenant_creates.load(Ordering::Relaxed), 0);
        assert_eq!(calls.tenant_deletes.load(Ordering::Relaxed), 0);
        assert_eq!(calls.database_observations.load(Ordering::Relaxed), 0);
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
            database: Ok(database_observation()),
            query: Ok(query_response()),
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
        assert_eq!(calls.database_observations.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn detail_snapshot_and_topology_return_shared_envelopes() {
        let tenant = ready_tenant();
        let source = MockSource {
            tenants: Ok(vec![tenant.clone()]),
            tenant: Ok(Some(tenant)),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
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
        assert!(matches!(
            detail.data.database,
            DatabaseObservation::Available { .. }
        ));
        assert_eq!(detail.data.topology.tenant_name, "tenant-a");
        assert!(!detail.data.topology.nodes.is_empty());
        assert_eq!(calls.tenant_gets.load(Ordering::Relaxed), 1);
        assert_eq!(calls.resource_lists.load(Ordering::Relaxed), 1);
        assert_eq!(calls.database_observations.load(Ordering::Relaxed), 1);
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
        assert_eq!(calls.database_observations.load(Ordering::Relaxed), 2);
    }

    #[tokio::test]
    async fn not_found_invalid_and_unavailable_are_typed() {
        let app = test_router(MockSource {
            tenants: Err(SourceError::KubernetesUnavailable),
            tenant: Ok(None),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
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

    fn query_request(sql: &str) -> Request<Body> {
        query_request_with_content_type(sql, Some("application/json"))
    }

    fn query_request_with_content_type(sql: &str, content_type: Option<&str>) -> Request<Body> {
        let mut request = Request::post("/api/v1/tenants/tenant-a/database/query");
        if let Some(content_type) = content_type {
            request = request.header("content-type", content_type);
        }
        request
            .body(Body::from(
                serde_json::to_vec(&DatabaseQueryRequest {
                    instance: "capi-postgres-1".into(),
                    database: "postgres".into(),
                    sql: sql.into(),
                })
                .expect("query body"),
            ))
            .expect("request")
    }

    fn origin_query_request(
        host: &str,
        origin: &str,
        include_unsafe_header: bool,
    ) -> Request<Body> {
        let mut request = query_request_with_content_type("select 1", Some("text/plain"));
        request
            .headers_mut()
            .insert(header::HOST, host.parse().expect("host"));
        request
            .headers_mut()
            .insert(header::ORIGIN, origin.parse().expect("origin"));
        if include_unsafe_header {
            request.headers_mut().insert(
                TENANT_ADMIN_UNSAFE_REQUEST_HEADER,
                TENANT_ADMIN_UNSAFE_REQUEST_VALUE
                    .parse()
                    .expect("unsafe request header"),
            );
        }
        request
    }

    #[tokio::test]
    async fn database_query_route_requires_post_json_and_valid_fields() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(ready_tenant())),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);
        let method = app
            .clone()
            .oneshot(
                Request::get("/api/v1/tenants/tenant-a/database/query")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(method.status(), StatusCode::METHOD_NOT_ALLOWED);
        let malformed = app
            .clone()
            .oneshot(
                Request::post("/api/v1/tenants/tenant-a/database/query")
                    .header("content-type", "application/json")
                    .body(Body::from("{"))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(malformed.status(), StatusCode::BAD_REQUEST);
        let malformed: ApiErrorEnvelope = response_json(malformed).await;
        assert_eq!(malformed.error.code, ApiErrorCode::InvalidRequest);
        let malformed_without_content_type = app
            .clone()
            .oneshot(
                Request::post("/api/v1/tenants/tenant-a/database/query")
                    .body(Body::from("{"))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(
            malformed_without_content_type.status(),
            StatusCode::BAD_REQUEST
        );
        let malformed_without_content_type: ApiErrorEnvelope =
            response_json(malformed_without_content_type).await;
        assert_eq!(
            malformed_without_content_type.error.code,
            ApiErrorCode::InvalidRequest
        );
        let oversized = app
            .clone()
            .oneshot(
                Request::post("/api/v1/tenants/tenant-a/database/query")
                    .body(Body::from(vec![b'x'; MAX_DATABASE_QUERY_BODY_BYTES + 1]))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(oversized.status(), StatusCode::BAD_REQUEST);
        let oversized: ApiErrorEnvelope = response_json(oversized).await;
        assert_eq!(oversized.error.code, ApiErrorCode::InvalidRequest);

        for request in [
            DatabaseQueryRequest {
                instance: "INVALID".into(),
                database: "postgres".into(),
                sql: "select 1".into(),
            },
            DatabaseQueryRequest {
                instance: "capi-postgres-1".into(),
                database: "bad\ndatabase".into(),
                sql: "select 1".into(),
            },
            DatabaseQueryRequest {
                instance: "capi-postgres-1".into(),
                database: "postgres".into(),
                sql: " \n\t".into(),
            },
            DatabaseQueryRequest {
                instance: "capi-postgres-1".into(),
                database: "postgres".into(),
                sql: "x".repeat(64 * 1_024 + 1),
            },
        ] {
            let response = app
                .clone()
                .oneshot(
                    Request::post("/api/v1/tenants/tenant-a/database/query")
                        .header("content-type", "application/json")
                        .body(Body::from(
                            serde_json::to_vec(&request).expect("request body"),
                        ))
                        .expect("request"),
                )
                .await
                .expect("response");
            assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        }
        assert_eq!(calls.tenant_gets.load(Ordering::Relaxed), 0);
        assert_eq!(calls.resource_lists.load(Ordering::Relaxed), 0);
        assert_eq!(calls.database_queries.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn database_query_accepts_json_without_required_content_type() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(ready_tenant())),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);

        for content_type in [None, Some("application/octet-stream"), Some("text/plain")] {
            let response = app
                .clone()
                .oneshot(query_request_with_content_type("select 1", content_type))
                .await
                .expect("response");
            assert_eq!(response.status(), StatusCode::OK);
            let response: ApiEnvelope<DatabaseQueryResponse> = response_json(response).await;
            assert_eq!(response.data.results[0].rows[0][0].as_deref(), Some("42"));
        }
        assert_eq!(calls.database_queries.load(Ordering::Relaxed), 3);
    }

    #[tokio::test]
    async fn database_query_enforces_same_origin_when_origin_is_present() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(ready_tenant())),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);

        let response = app
            .clone()
            .oneshot(origin_query_request(
                "localhost:8080",
                "https://localhost:8080",
                false,
            ))
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);

        for (host, origin) in [
            ("localhost:8080", "https://LOCALHOST:8080"),
            ("127.0.0.1:8080", "http://127.0.0.1:8080"),
            ("[::1]:8080", "http://[::1]:8080"),
        ] {
            let response = app
                .clone()
                .oneshot(origin_query_request(host, origin, true))
                .await
                .expect("response");
            assert_eq!(response.status(), StatusCode::OK, "{origin}");
        }

        for (host, origin) in [
            ("attacker.example:8080", "https://attacker.example:8080"),
            ("localhost:8080", "https://foreign.example:8080"),
            ("localhost:8080", "null"),
            ("localhost:8080", "not-an-origin"),
            ("localhost:8080", "http://[::1"),
        ] {
            let response = app
                .clone()
                .oneshot(origin_query_request(host, origin, true))
                .await
                .expect("response");
            assert_eq!(response.status(), StatusCode::BAD_REQUEST, "{origin}");
            let response: ApiErrorEnvelope = response_json(response).await;
            assert_eq!(response.error.code, ApiErrorCode::InvalidRequest);
        }
        assert_eq!(calls.database_queries.load(Ordering::Relaxed), 3);
    }

    #[tokio::test]
    async fn database_query_returns_structured_results_and_preserves_detail_reads() {
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(ready_tenant())),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: Arc::default(),
        };
        let calls = source.calls.clone();
        let app = test_router(source);
        let response = app
            .clone()
            .oneshot(query_request(
                "select 42, null; update values set active = true",
            ))
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::OK);
        let response: ApiEnvelope<DatabaseQueryResponse> = response_json(response).await;
        assert_eq!(response.data.results.len(), 2);
        assert_eq!(
            response.data.results[0].rows,
            vec![vec![Some("42".into()), None]]
        );
        assert_eq!(response.data.results[1].affected_rows, 3);
        assert_eq!(calls.tenant_gets.load(Ordering::Relaxed), 1);
        assert_eq!(calls.resource_lists.load(Ordering::Relaxed), 1);
        assert_eq!(calls.database_queries.load(Ordering::Relaxed), 1);
        assert_eq!(calls.database_observations.load(Ordering::Relaxed), 0);

        let detail = app
            .oneshot(
                Request::get("/api/v1/tenants/tenant-a")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(detail.status(), StatusCode::OK);
        assert_eq!(calls.database_observations.load(Ordering::Relaxed), 1);
        assert_eq!(calls.database_queries.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn database_query_maps_provider_query_error_and_timeout() {
        for (provider, error, status, code) in [
            (
                ProviderMode::Azure,
                SourceError::DatabaseUnavailable {
                    message: "Database queries are available only for local Tenants".into(),
                    retryable: false,
                },
                StatusCode::SERVICE_UNAVAILABLE,
                ApiErrorCode::DatabaseUnavailable,
            ),
            (
                ProviderMode::Local,
                SourceError::QueryFailed {
                    sqlstate: Some("42601".into()),
                    message: "syntax error".into(),
                },
                StatusCode::UNPROCESSABLE_ENTITY,
                ApiErrorCode::QueryFailed,
            ),
            (
                ProviderMode::Local,
                SourceError::QueryResponseTooLarge,
                StatusCode::UNPROCESSABLE_ENTITY,
                ApiErrorCode::QueryResponseTooLarge,
            ),
            (
                ProviderMode::Local,
                SourceError::QueryTimedOut,
                StatusCode::GATEWAY_TIMEOUT,
                ApiErrorCode::QueryTimedOut,
            ),
            (
                ProviderMode::Local,
                SourceError::QueryOutcomeUnknown,
                StatusCode::GATEWAY_TIMEOUT,
                ApiErrorCode::QueryOutcomeUnknown,
            ),
        ] {
            let source = MockSource {
                tenants: Ok(Vec::new()),
                tenant: Ok(Some(ready_tenant())),
                resources: Ok(Vec::new()),
                database: Ok(database_observation()),
                query: Err(error),
                ready: Ok(()),
                calls: Arc::default(),
            };
            let response = test_router_with_provider(source, provider)
                .oneshot(query_request("private-sql-must-not-appear"))
                .await
                .expect("response");
            assert_eq!(response.status(), status);
            let response: ApiErrorEnvelope = response_json(response).await;
            assert_eq!(response.error.code, code);
            assert!(!response.error.message.contains("private-sql"));
            if code == ApiErrorCode::QueryFailed {
                assert!(response.error.message.contains("SQLSTATE 42601"));
                assert!(response.error.message.contains("syntax error"));
            }
        }
    }

    #[tokio::test]
    async fn nested_catalog_routes_validate_exact_identities_origin_and_bounds() {
        const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        const LOGICAL: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
        let calls = Arc::new(SourceCalls::default());
        let source = MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(ready_tenant())),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: calls.clone(),
        };
        let router = test_router_with_provider(source, ProviderMode::Azure);
        let path = "/api/v1/tenants/tenant-a/databases";
        let response = router
            .clone()
            .oneshot(Request::get(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        let router = test_router(MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(ready_tenant())),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: calls.clone(),
        });
        let response = router
            .clone()
            .oneshot(Request::get(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body: ApiEnvelope<CatalogView> = response_json(response).await;
        assert_eq!(body.schema_version, 5);
        assert_eq!(body.data.catalog_uid, CATALOG);
        let add = format!(r#"{{"catalogUid":"{CATALOG}","name":"alpha","instances":2}}"#);
        let send = |path: &str, body: String| {
            Request::post(path)
                .header("host", "localhost:8080")
                .header("origin", "http://localhost:8080")
                .header(
                    TENANT_ADMIN_UNSAFE_REQUEST_HEADER,
                    TENANT_ADMIN_UNSAFE_REQUEST_VALUE,
                )
                .body(Body::from(body))
                .unwrap()
        };
        let response = router
            .clone()
            .oneshot(send(path, add.clone()))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::CREATED);
        let response = router
            .clone()
            .oneshot(
                Request::post(path)
                    .header("host", "localhost:8080")
                    .header("origin", "http://localhost:8080")
                    .body(Body::from(add))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let delete_path = format!("{path}/{LOGICAL}");
        let deletion = format!(
            r#"{{"catalogUid":"{CATALOG}","logicalUid":"{LOGICAL}","confirmation":"alpha"}}"#
        );
        let response = router
            .clone()
            .oneshot(
                Request::delete(&delete_path)
                    .header("host", "localhost:8080")
                    .header("origin", "http://localhost:8080")
                    .header(
                        TENANT_ADMIN_UNSAFE_REQUEST_HEADER,
                        TENANT_ADMIN_UNSAFE_REQUEST_VALUE,
                    )
                    .body(Body::from(deletion))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::ACCEPTED);
        let invalid = format!(
            r#"{{"catalogUid":"{CATALOG}","logicalUid":"{CATALOG}","confirmation":"alpha"}}"#
        );
        let response = router
            .clone()
            .oneshot(
                Request::delete(&delete_path)
                    .body(Body::from(invalid))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let query_path = format!("{delete_path}/query");
        let query = format!(
            r#"{{"catalogUid":"{CATALOG}","logicalUid":"{LOGICAL}","instance":"pg-1","instanceUid":"pod-uid","database":"postgres","sql":"select 1"}}"#
        );
        let response = router
            .clone()
            .oneshot(send(&query_path, query))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let wrong = format!(
            r#"{{"catalogUid":"{CATALOG}","logicalUid":"{CATALOG}","instance":"pg-1","instanceUid":"pod-uid","database":"postgres","sql":"select 1"}}"#
        );
        let response = router
            .clone()
            .oneshot(send(&query_path, wrong))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let response = router
            .clone()
            .oneshot(send(&query_path, "x".repeat(128 * 1024 + 1)))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        assert_eq!(calls.catalog_adds.load(Ordering::Relaxed), 1);
        assert_eq!(calls.catalog_deletes.load(Ordering::Relaxed), 1);
        assert_eq!(calls.catalog_queries.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn catalog_capable_tenant_cannot_use_legacy_unbound_query_route() {
        let mut tenant = ready_tenant();
        tenant.status.as_mut().unwrap().database_capability =
            Some(tenant_controller::api::DatabaseCapability {
                available: true,
                reason: "Ready".into(),
                namespace: "tenant-db-tenant-a".into(),
                namespace_uid: "namespace-uid".into(),
                catalog_uid: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into(),
                storage_namespace_uid: None,
            });
        let calls = Arc::new(SourceCalls::default());
        let app = test_router(MockSource {
            tenants: Ok(Vec::new()),
            tenant: Ok(Some(tenant)),
            resources: Ok(Vec::new()),
            database: Ok(database_observation()),
            query: Ok(query_response()),
            ready: Ok(()),
            calls: calls.clone(),
        });
        let response = app.oneshot(query_request("select 1")).await.unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(calls.resource_lists.load(Ordering::Relaxed), 0);
        assert_eq!(calls.database_queries.load(Ordering::Relaxed), 0);
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
            database: Ok(database_observation()),
            query: Ok(query_response()),
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
        assert_eq!(
            nested
                .headers()
                .get("cache-control")
                .and_then(|value| value.to_str().ok()),
            Some("no-store")
        );
        let nested_body = nested.into_body().collect().await.expect("body").to_bytes();
        assert!(
            nested_body
                .windows(18)
                .any(|value| value == b"tenant-admin-shell")
        );
        let missing_asset = app
            .clone()
            .oneshot(
                Request::get("/assets/missing.js")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(missing_asset.status(), StatusCode::NOT_FOUND);
        let missing_asset_body = missing_asset
            .into_body()
            .collect()
            .await
            .expect("body")
            .to_bytes();
        assert!(
            !missing_asset_body
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
