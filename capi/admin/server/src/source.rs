use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    net::Ipv6Addr,
    pin::Pin,
    sync::Arc,
};

use axum::http::uri::Authority;
use chrono::{SecondsFormat, Utc};
use futures::{StreamExt, stream};
use k8s_openapi::api::{
    apps::v1::Deployment,
    core::v1::{Pod, Secret},
};
use kube::{
    Api, Client, ResourceExt,
    api::{DeleteParams, ListParams, ObjectList, PostParams, Preconditions, PropagationPolicy},
    core::{ApiResource, DynamicObject, GroupVersionKind},
};
use serde::Deserialize;
use tenant_admin_shared::lifecycle::{
    CreationCapability, TenantDeleteResponse, TenantDeleteState, TenantMutationIdentity,
};
use tenant_admin_shared::query::{
    ConditionStatus, DatabaseClusterIdentity, DatabaseClusterObservation, DatabaseCondition,
    DatabaseInstanceObservation, DatabaseInstanceRole, DatabaseNotApplicableReason,
    DatabaseObservation, DatabaseObservationFreshness, DatabasePvcHealth, DatabaseQueryRequest,
    DatabaseQueryResponse, DatabaseServices, DatabaseUnavailableReason, ProviderMode,
};
use tenant_controller::{
    api::{Tenant, TenantProviderSpec},
    management::{AZURE_MANAGEMENT_RESOURCES, MANAGEMENT_RESOURCES, ManagementResource},
    ownership::validate_provider_owner,
    resources::{
        CNPG_CLUSTER_API_VERSION, CNPG_CLUSTER_KIND, CNPG_CLUSTER_PLURAL,
        MANAGED_DATABASE_CLUSTER_NAME, MANAGED_DATABASE_NAMESPACE,
    },
    tenant_client::{TenantApiErrorClass, TenantClientError, load_tenant_client},
};

use crate::{
    SourceError,
    database::{
        PodBindingError, PostgresQueryExecutor, QueryClusterBinding, QueryConnection,
        QueryExecutionError, QueryExecutor, validate_pod_binding,
    },
    projection::is_accepted_local_management_resource,
};

const MAX_TENANTS: u32 = 500;
const MAX_RESOURCES_PER_KIND: u32 = 500;
const MAX_RESOURCES_TOTAL: usize = 2_000;
const RESOURCE_LIST_CONCURRENCY: usize = 8;
const MAX_DATABASE_INSTANCES: usize = 64;
const MAX_DATABASE_CONDITIONS: usize = 32;
const MAX_DATABASE_COUNT: u32 = 10_000;
const MAX_IDENTITY: usize = 253;
const MAX_TEXT: usize = 512;
const MAX_IMAGE: usize = 1_024;
const MAX_TIMESTAMP: usize = 64;
const MAX_SQL_BYTES: usize = 64 * 1_024;
const MAX_DATABASE_BYTES: usize = 63;
const MAX_INSTANCE_BYTES: usize = 63;
const MAX_DATABASE_USERNAME_BYTES: usize = 1_024;
const MAX_DATABASE_PASSWORD_BYTES: usize = 16 * 1_024;
const DATABASE_SUPERUSER_SECRET_NAME: &str = "capi-postgres-superuser";
const CONTROLLER_NAMESPACE: &str = "tenant-system";
const CONTROLLER_DEPLOYMENT: &str = "tenant-controller";

pub type SourceFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, SourceError>> + Send + 'a>>;
type TenantClientFuture<'a> =
    Pin<Box<dyn Future<Output = Result<Client, TenantClientError>> + Send + 'a>>;

trait TenantClientLoader: Send + Sync {
    fn load<'a>(
        &'a self,
        management: Client,
        control_plane: &'a DynamicObject,
        namespace: &'a str,
        tenant_name: &'a str,
        endpoint: &'a str,
    ) -> TenantClientFuture<'a>;
}

struct ValidatedTenantClientLoader;

impl TenantClientLoader for ValidatedTenantClientLoader {
    fn load<'a>(
        &'a self,
        management: Client,
        control_plane: &'a DynamicObject,
        namespace: &'a str,
        tenant_name: &'a str,
        endpoint: &'a str,
    ) -> TenantClientFuture<'a> {
        Box::pin(async move {
            let (client, _secret) =
                load_tenant_client(management, control_plane, namespace, tenant_name, endpoint)
                    .await?;
            Ok(client)
        })
    }
}

pub trait DataSource: Send + Sync {
    fn list_tenants(&self) -> SourceFuture<'_, Vec<Tenant>>;
    fn get_tenant(&self, name: &str) -> SourceFuture<'_, Option<Tenant>>;
    fn creation_capability(&self, provider: ProviderMode) -> SourceFuture<'_, CreationCapability> {
        let _ = provider;
        Box::pin(async {
            Ok(CreationCapability::unavailable(
                "Tenant creation capability is unavailable",
            ))
        })
    }
    fn create_tenant(&self, tenant: Tenant) -> SourceFuture<'_, Tenant> {
        let _ = tenant;
        Box::pin(async { Err(SourceError::CreationUnavailable) })
    }
    fn delete_tenant<'a>(
        &'a self,
        name: &'a str,
        uid: &'a str,
    ) -> SourceFuture<'a, TenantDeleteResponse> {
        let _ = (name, uid);
        Box::pin(async { Err(SourceError::KubernetesUnavailable) })
    }
    fn list_management_resources(
        &self,
        provider: ProviderMode,
        tenant_name: &str,
    ) -> SourceFuture<'_, Vec<DynamicObject>>;
    fn database_observation<'a>(
        &'a self,
        provider: ProviderMode,
        tenant: &'a Tenant,
        management_resources: &'a [DynamicObject],
    ) -> SourceFuture<'a, DatabaseObservation> {
        let _ = (tenant, management_resources);
        Box::pin(async move {
            Ok(match provider {
                ProviderMode::Azure => not_applicable(
                    observed_at(),
                    DatabaseNotApplicableReason::ProviderUnsupported,
                ),
                ProviderMode::Local => unavailable(
                    observed_at(),
                    DatabaseUnavailableReason::TenantApiUnavailable,
                    "Live database source is unavailable",
                    true,
                ),
            })
        })
    }
    fn database_query<'a>(
        &'a self,
        provider: ProviderMode,
        tenant: &'a Tenant,
        management_resources: &'a [DynamicObject],
        request: &'a DatabaseQueryRequest,
    ) -> SourceFuture<'a, DatabaseQueryResponse> {
        let _ = (provider, tenant, management_resources, request);
        Box::pin(async move {
            Err(SourceError::DatabaseUnavailable {
                message: "Live database query source is unavailable".into(),
                retryable: true,
            })
        })
    }
    fn check_ready(&self) -> SourceFuture<'_, ()>;
}

#[derive(Clone)]
pub struct KubeDataSource {
    client: Client,
    tenant_clients: Arc<dyn TenantClientLoader>,
    query_executor: Arc<dyn QueryExecutor>,
}

impl KubeDataSource {
    pub fn new(client: Client) -> Self {
        Self {
            client,
            tenant_clients: Arc::new(ValidatedTenantClientLoader),
            query_executor: Arc::new(PostgresQueryExecutor),
        }
    }

    async fn tenant_list(&self, limit: u32) -> Result<ObjectList<Tenant>, SourceError> {
        Api::<Tenant>::all(self.client.clone())
            .list(&ListParams::default().limit(limit))
            .await
            .map_err(|error| {
                tracing::warn!(error = %tenant_controller::sanitize::text(&error.to_string()), "Tenant list failed");
                SourceError::KubernetesUnavailable
            })
    }

    async fn load_creation_capability(&self, provider: ProviderMode) -> CreationCapability {
        let api = Api::<Deployment>::namespaced(self.client.clone(), CONTROLLER_NAMESPACE);
        let deployment = match api.get_opt(CONTROLLER_DEPLOYMENT).await {
            Ok(Some(deployment)) => deployment,
            Ok(None) => {
                return CreationCapability::unavailable(
                    "Tenant controller Deployment is unavailable",
                );
            }
            Err(error) => {
                tracing::warn!(
                    error = %tenant_controller::sanitize::text(&error.to_string()),
                    "Tenant creation capability read failed"
                );
                return CreationCapability::unavailable(
                    "Tenant controller capability is unavailable",
                );
            }
        };
        let Some(spec) = deployment.spec.as_ref() else {
            return CreationCapability::unavailable("Tenant controller capability is malformed");
        };
        let replicas = spec.replicas.unwrap_or(1);
        let status = deployment.status.as_ref();
        let generation = deployment.metadata.generation;
        if replicas < 1
            || generation.is_none()
            || deployment.metadata.deletion_timestamp.is_some()
            || generation != status.and_then(|status| status.observed_generation)
            || status.and_then(|status| status.updated_replicas) != Some(replicas)
            || status.and_then(|status| status.ready_replicas) != Some(replicas)
            || status.and_then(|status| status.available_replicas) != Some(replicas)
        {
            return CreationCapability::unavailable("Tenant controller rollout is not complete");
        }
        let mut managers = spec
            .template
            .spec
            .as_ref()
            .into_iter()
            .flat_map(|pod| pod.containers.iter())
            .filter(|container| container.name == "manager");
        let Some(manager) = managers.next().filter(|_| managers.next().is_none()) else {
            return CreationCapability::unavailable("Tenant controller capability is malformed");
        };
        let args = manager.args.as_deref().unwrap_or_default();
        let Some((configured_provider, configured_version)) = controller_arguments(args) else {
            return CreationCapability::unavailable("Tenant controller arguments are malformed");
        };
        let expected_provider = match provider {
            ProviderMode::Local => "local",
            ProviderMode::Azure => "azure",
        };
        if configured_provider != expected_provider {
            return CreationCapability::unavailable(
                "Tenant controller provider does not match Tenant Admin",
            );
        }
        let Some(version) =
            Some(configured_version.trim_start_matches('v')).filter(|value| valid_version(value))
        else {
            return CreationCapability::unavailable(
                "Tenant controller supported version is unavailable",
            );
        };
        CreationCapability::available(version)
    }

    async fn create_exact_tenant(&self, tenant: Tenant) -> Result<Tenant, SourceError> {
        Api::<Tenant>::all(self.client.clone())
            .create(&PostParams::default(), &tenant)
            .await
            .map_err(mutation_error)
    }

    async fn delete_exact_tenant(
        &self,
        name: &str,
        expected_uid: &str,
    ) -> Result<TenantDeleteResponse, SourceError> {
        let api = Api::<Tenant>::all(self.client.clone());
        for attempt in 0..3 {
            let Some(tenant) = api.get_opt(name).await.map_err(mutation_error)? else {
                return Ok(delete_response(
                    name,
                    expected_uid,
                    None,
                    TenantDeleteState::Completed,
                ));
            };
            let uid = tenant
                .uid()
                .filter(|value| !value.is_empty())
                .ok_or(SourceError::StaleIdentity)?;
            if uid != expected_uid {
                return Err(SourceError::StaleIdentity);
            }
            let generation = tenant.metadata.generation;
            if tenant.metadata.deletion_timestamp.is_some() {
                return Ok(delete_response(
                    name,
                    &uid,
                    generation,
                    TenantDeleteState::Accepted,
                ));
            }
            let params = DeleteParams {
                preconditions: Some(Preconditions {
                    uid: Some(uid.clone()),
                    resource_version: tenant.resource_version(),
                }),
                propagation_policy: Some(PropagationPolicy::Background),
                ..DeleteParams::default()
            };
            match api.delete(name, &params).await {
                Ok(_) => {
                    return Ok(delete_response(
                        name,
                        &uid,
                        generation,
                        TenantDeleteState::Accepted,
                    ));
                }
                Err(kube::Error::Api(status)) if status.code == 404 => {
                    return Ok(delete_response(
                        name,
                        &uid,
                        generation,
                        TenantDeleteState::Completed,
                    ));
                }
                Err(kube::Error::Api(status)) if status.code == 409 && attempt < 2 => continue,
                Err(error) => return Err(mutation_error(error)),
            }
        }
        Err(SourceError::Conflict)
    }

    async fn list_definition(
        client: Client,
        definition: ManagementResource,
        tenant_name: Arc<str>,
    ) -> Result<Vec<DynamicObject>, SourceError> {
        let api_resource = definition.api_resource();
        let namespace = match definition.inventory_namespace {
            Some(namespace) => namespace,
            None => tenant_name.as_ref(),
        };
        let api = if definition.namespaced {
            Api::<DynamicObject>::namespaced_with(client, namespace, &api_resource)
        } else {
            Api::<DynamicObject>::all_with(client, &api_resource)
        };
        if !definition.namespaced
            && let Some(expected_name) = definition.expected_name(&tenant_name)
        {
            return match api.get_opt(&expected_name).await {
                Ok(Some(mut object)) => {
                    object.types.get_or_insert_with(|| kube::core::TypeMeta {
                        api_version: definition.api_version.into(),
                        kind: definition.kind.into(),
                    });
                    Ok(vec![object])
                }

                Ok(None) => Ok(Vec::new()),
                Err(error) => {
                    tracing::warn!(
                        api_version = definition.api_version,
                        kind = definition.kind,
                        name = expected_name,
                        error = %tenant_controller::sanitize::text(&error.to_string()),
                        "management resource read failed"
                    );
                    Err(SourceError::KubernetesUnavailable)
                }
            };
        }
        match api
            .list(&ListParams::default().limit(MAX_RESOURCES_PER_KIND + 1))
            .await
        {
            Ok(mut list) => {
                if list.items.len() > MAX_RESOURCES_PER_KIND as usize
                    || list
                        .metadata
                        .continue_
                        .as_deref()
                        .is_some_and(|value| !value.is_empty())
                {
                    return Err(SourceError::ResponseTooLarge);
                }
                for item in &mut list.items {
                    item.types.get_or_insert_with(|| kube::core::TypeMeta {
                        api_version: definition.api_version.into(),
                        kind: definition.kind.into(),
                    });
                }
                Ok(list.items)
            }
            Err(kube::Error::Api(response)) if response.code == 404 => Ok(Vec::new()),
            Err(error) => {
                tracing::warn!(
                    api_version = definition.api_version,
                    kind = definition.kind,
                    error = %tenant_controller::sanitize::text(&error.to_string()),
                    "management resource list failed"
                );
                Err(SourceError::KubernetesUnavailable)
            }
        }
    }

    async fn observe_database(
        &self,
        provider: ProviderMode,
        tenant: &Tenant,
        management_resources: &[DynamicObject],
    ) -> DatabaseObservation {
        let observed_at = observed_at();
        if provider == ProviderMode::Azure
            || matches!(tenant.spec.provider, TenantProviderSpec::Azure)
        {
            return not_applicable(
                observed_at,
                DatabaseNotApplicableReason::ProviderUnsupported,
            );
        }
        let access = match self
            .tenant_database_access(tenant, management_resources)
            .await
        {
            Ok(access) => access,
            Err(error) => {
                return unavailable(observed_at, error.reason, error.message, error.retryable);
            }
        };
        match project_database_cluster(access.cluster) {
            Ok(Some(cluster)) => DatabaseObservation::Available {
                observed_at,
                freshness: DatabaseObservationFreshness::Live,
                cluster: Box::new(cluster),
            },
            Ok(None) => unavailable(
                observed_at,
                DatabaseUnavailableReason::Pending,
                "Managed database status is pending",
                true,
            ),
            Err(()) => unavailable(
                observed_at,
                DatabaseUnavailableReason::Malformed,
                "Managed database metadata is malformed",
                true,
            ),
        }
    }

    async fn query_database(
        &self,
        provider: ProviderMode,
        tenant: &Tenant,
        management_resources: &[DynamicObject],
        request: &DatabaseQueryRequest,
    ) -> Result<DatabaseQueryResponse, SourceError> {
        validate_query_request(request)?;
        if provider != ProviderMode::Local
            || !matches!(tenant.spec.provider, TenantProviderSpec::Local)
        {
            return Err(database_unavailable(
                "Database queries are available only for local Tenants",
                false,
            ));
        }
        let tenant_name = tenant
            .metadata
            .name
            .as_deref()
            .filter(|name| !name.is_empty())
            .ok_or_else(|| database_unavailable("Tenant identity is malformed", false))?;
        let access = self
            .tenant_database_access(tenant, management_resources)
            .await
            .map_err(|error| database_unavailable(error.message, error.retryable))?;
        let cluster_identity = validated_query_cluster(&access.cluster, &request.instance)?;
        let pod = read_database_pod(access.client.clone(), &request.instance).await?;
        let pod_binding = validate_pod_binding(&pod, &request.instance, &cluster_identity)
            .map_err(pod_binding_error)?;
        let secret = read_database_secret(access.client.clone()).await?;
        let credentials = validate_database_secret(&secret, &cluster_identity.uid)?;
        let execution = self
            .query_executor
            .execute(QueryConnection {
                client: access.client,
                pod: &pod_binding,
                database: &request.database,
                username: &credentials.username,
                password: &credentials.password,
                sql: &request.sql,
            })
            .await
            .map_err(query_execution_error)?;
        Ok(DatabaseQueryResponse {
            tenant: tenant_name.to_owned(),
            cluster: MANAGED_DATABASE_CLUSTER_NAME.into(),
            instance: request.instance.clone(),
            database: request.database.clone(),
            executed_at: observed_at(),
            duration_ms: execution.duration_ms,
            truncated: execution.truncated,
            results: execution.results,
        })
    }

    async fn tenant_database_access(
        &self,
        tenant: &Tenant,
        management_resources: &[DynamicObject],
    ) -> Result<TenantDatabaseAccess, DatabaseAccessError> {
        let tenant_name = tenant
            .metadata
            .name
            .as_deref()
            .filter(|name| !name.is_empty())
            .ok_or(DatabaseAccessError::malformed(
                "Tenant identity is malformed",
            ))?;
        let local_status = tenant
            .status
            .as_ref()
            .and_then(|status| status.local())
            .ok_or(DatabaseAccessError::pending(
                "Tenant API endpoint is pending",
            ))?;
        let allocation_host = local_status
            .allocation
            .as_ref()
            .map(|allocation| allocation.endpoint.as_str())
            .filter(|endpoint| !endpoint.is_empty())
            .ok_or(DatabaseAccessError::pending(
                "Tenant API endpoint is pending",
            ))?;
        let cluster_uid = local_status
            .cluster_uid
            .as_deref()
            .filter(|uid| !uid.is_empty())
            .ok_or(DatabaseAccessError::pending(
                "Tenant management Cluster identity is pending",
            ))?;
        let cluster = expected_management_cluster(management_resources, tenant_name).ok_or(
            DatabaseAccessError::management_missing("Expected management Cluster is unavailable"),
        )?;
        if !is_accepted_local_management_resource(tenant, management_resources, cluster) {
            return Err(DatabaseAccessError::invalid(
                "Tenant management-cluster ownership is invalid",
            ));
        }
        let endpoint =
            trusted_endpoint_authority(cluster, cluster_uid, allocation_host).map_err(|()| {
                tracing::warn!(
                    tenant = %tenant_controller::sanitize::text(tenant_name),
                    "trusted Tenant API endpoint metadata is invalid"
                );
                DatabaseAccessError::malformed("Trusted Tenant API endpoint metadata is invalid")
            })?;
        let control_plane = expected_control_plane(management_resources, tenant_name).ok_or(
            DatabaseAccessError::management_missing("Expected Tenant control plane is unavailable"),
        )?;
        if !is_accepted_local_management_resource(tenant, management_resources, control_plane)
            || validate_provider_owner(control_plane, tenant_name, true, management_resources)
                .is_err()
        {
            return Err(DatabaseAccessError::invalid(
                "Tenant control-plane ownership is invalid",
            ));
        }
        let client = self
            .tenant_clients
            .load(
                self.client.clone(),
                control_plane,
                tenant_name,
                tenant_name,
                &endpoint,
            )
            .await
            .map_err(|error| {
                let access_error = match error.class() {
                    TenantApiErrorClass::Pending => {
                        DatabaseAccessError::pending("Tenant administrative access is pending")
                    }
                    TenantApiErrorClass::Conflict | TenantApiErrorClass::Retryable => {
                        DatabaseAccessError::tenant_api("Tenant API is unavailable")
                    }
                    TenantApiErrorClass::Terminal => {
                        DatabaseAccessError::invalid("Tenant administrative access is invalid")
                    }
                };
                tracing::warn!(
                    tenant = %tenant_controller::sanitize::text(tenant_name),
                    reason = ?access_error.reason,
                    "live database access failed"
                );
                access_error
            })?;
        let cluster = match read_database_cluster(client.clone()).await {
            Ok(Some(cluster)) => cluster,
            Ok(None) => {
                return Err(DatabaseAccessError::cluster_missing(
                    "Managed database Cluster was not found",
                ));
            }
            Err(error) => {
                tracing::warn!(
                    tenant = %tenant_controller::sanitize::text(tenant_name),
                    status = database_error_status(&error),
                    "live database read failed"
                );
                return Err(DatabaseAccessError::tenant_api(
                    "Tenant API database read failed",
                ));
            }
        };
        Ok(TenantDatabaseAccess { client, cluster })
    }
}

struct TenantDatabaseAccess {
    client: Client,
    cluster: DynamicObject,
}

struct DatabaseAccessError {
    reason: DatabaseUnavailableReason,
    message: &'static str,
    retryable: bool,
}

impl DatabaseAccessError {
    const fn pending(message: &'static str) -> Self {
        Self {
            reason: DatabaseUnavailableReason::Pending,
            message,
            retryable: true,
        }
    }

    const fn management_missing(message: &'static str) -> Self {
        Self {
            reason: DatabaseUnavailableReason::ManagementResourceMissing,
            message,
            retryable: true,
        }
    }

    const fn invalid(message: &'static str) -> Self {
        Self {
            reason: DatabaseUnavailableReason::TenantAccessInvalid,
            message,
            retryable: false,
        }
    }

    const fn tenant_api(message: &'static str) -> Self {
        Self {
            reason: DatabaseUnavailableReason::TenantApiUnavailable,
            message,
            retryable: true,
        }
    }

    const fn cluster_missing(message: &'static str) -> Self {
        Self {
            reason: DatabaseUnavailableReason::ClusterMissing,
            message,
            retryable: true,
        }
    }

    const fn malformed(message: &'static str) -> Self {
        Self {
            reason: DatabaseUnavailableReason::Malformed,
            message,
            retryable: false,
        }
    }
}

fn observed_at() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn not_applicable(observed_at: String, reason: DatabaseNotApplicableReason) -> DatabaseObservation {
    DatabaseObservation::NotApplicable {
        observed_at,
        freshness: DatabaseObservationFreshness::Live,
        reason,
    }
}

fn unavailable(
    observed_at: String,
    reason: DatabaseUnavailableReason,
    message: &str,
    retryable: bool,
) -> DatabaseObservation {
    DatabaseObservation::Unavailable {
        observed_at,
        freshness: DatabaseObservationFreshness::Live,
        reason,
        message: bounded(message, MAX_TEXT),
        retryable,
    }
}

fn expected_management_cluster<'a>(
    resources: &'a [DynamicObject],
    tenant_name: &str,
) -> Option<&'a DynamicObject> {
    expected_management_resource(resources, tenant_name, "Cluster")
}

fn expected_control_plane<'a>(
    resources: &'a [DynamicObject],
    tenant_name: &str,
) -> Option<&'a DynamicObject> {
    expected_management_resource(resources, tenant_name, "KamajiControlPlane")
}

fn expected_management_resource<'a>(
    resources: &'a [DynamicObject],
    tenant_name: &str,
    kind: &str,
) -> Option<&'a DynamicObject> {
    let definition = MANAGEMENT_RESOURCES
        .iter()
        .find(|resource| resource.kind == kind)?;
    let expected_name = definition.expected_name(tenant_name)?;
    resources.iter().find(|resource| {
        resource.types.as_ref().is_some_and(|types| {
            types.api_version == definition.api_version && types.kind == definition.kind
        }) && resource.metadata.namespace.as_deref() == Some(tenant_name)
            && resource.metadata.name.as_deref() == Some(expected_name.as_str())
    })
}

fn trusted_endpoint_authority(
    cluster: &DynamicObject,
    expected_uid: &str,
    allocation_host: &str,
) -> Result<String, ()> {
    if cluster.metadata.uid.as_deref() != Some(expected_uid) {
        return Err(());
    }
    let endpoint = cluster
        .data
        .pointer("/spec/controlPlaneEndpoint")
        .and_then(serde_json::Value::as_object)
        .ok_or(())?;
    let host = endpoint
        .get("host")
        .and_then(serde_json::Value::as_str)
        .filter(|host| !host.is_empty() && *host == allocation_host)
        .ok_or(())?;
    let port = endpoint
        .get("port")
        .and_then(serde_json::Value::as_u64)
        .filter(|port| (1..=65_535).contains(port))
        .ok_or(())?;
    if host
        .chars()
        .any(|character| character.is_whitespace() || "/@?#[]".contains(character))
    {
        return Err(());
    }
    let authority = if host.contains(':') {
        host.parse::<Ipv6Addr>().map_err(|_| ())?;
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    };
    let parsed = authority.parse::<Authority>().map_err(|_| ())?;
    if parsed.port_u16() != u16::try_from(port).ok() {
        return Err(());
    }
    Ok(authority)
}

async fn read_database_cluster(client: Client) -> Result<Option<DynamicObject>, kube::Error> {
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
        "postgresql.cnpg.io",
        "v1",
        CNPG_CLUSTER_KIND,
    ));
    resource.plural = CNPG_CLUSTER_PLURAL.into();
    Api::<DynamicObject>::namespaced_with(client, MANAGED_DATABASE_NAMESPACE, &resource)
        .get_opt(MANAGED_DATABASE_CLUSTER_NAME)
        .await
}

async fn read_database_pod(client: Client, name: &str) -> Result<Pod, SourceError> {
    match Api::<Pod>::namespaced(client, MANAGED_DATABASE_NAMESPACE)
        .get_opt(name)
        .await
    {
        Ok(Some(pod)) => Ok(pod),
        Ok(None) => Err(database_unavailable(
            "Requested database instance is unavailable",
            true,
        )),
        Err(error) => {
            tracing::warn!(
                instance = %tenant_controller::sanitize::text(name),
                status = database_error_status(&error),
                "database instance Pod read failed"
            );
            Err(database_unavailable(
                "Tenant API database Pod read failed",
                true,
            ))
        }
    }
}

async fn read_database_secret(client: Client) -> Result<Secret, SourceError> {
    match Api::<Secret>::namespaced(client, MANAGED_DATABASE_NAMESPACE)
        .get_opt(DATABASE_SUPERUSER_SECRET_NAME)
        .await
    {
        Ok(Some(secret)) => Ok(secret),
        Ok(None) => Err(database_unavailable(
            "Database credentials are unavailable",
            true,
        )),
        Err(error) => {
            tracing::warn!(
                status = database_error_status(&error),
                "database credential Secret read failed"
            );
            Err(database_unavailable(
                "Tenant API database credential read failed",
                true,
            ))
        }
    }
}

fn database_error_status(error: &kube::Error) -> u16 {
    match error {
        kube::Error::Api(response) => response.code,
        _ => 0,
    }
}

fn validated_query_cluster(
    cluster: &DynamicObject,
    requested_instance: &str,
) -> Result<QueryClusterBinding, SourceError> {
    let types = cluster
        .types
        .as_ref()
        .filter(|types| {
            types.api_version == CNPG_CLUSTER_API_VERSION && types.kind == CNPG_CLUSTER_KIND
        })
        .ok_or_else(|| database_unavailable("Managed database identity is invalid", false))?;
    let _ = types;
    if cluster.metadata.namespace.as_deref() != Some(MANAGED_DATABASE_NAMESPACE)
        || cluster.metadata.name.as_deref() != Some(MANAGED_DATABASE_CLUSTER_NAME)
    {
        return Err(database_unavailable(
            "Managed database identity is invalid",
            false,
        ));
    }
    let uid = cluster
        .metadata
        .uid
        .as_deref()
        .filter(|uid| !uid.is_empty())
        .ok_or_else(|| database_unavailable("Managed database identity is pending", true))?
        .to_owned();
    let body: CnpgClusterData = serde_json::from_value(cluster.data.clone())
        .map_err(|_| database_unavailable("Managed database metadata is malformed", false))?;
    let status = body
        .status
        .ok_or_else(|| database_unavailable("Managed database status is pending", true))?;
    let is_current_instance = requested_instance != "pending"
        && projected_instances(&status)
            .iter()
            .any(|instance| instance.name == requested_instance);
    if !is_current_instance {
        return Err(database_unavailable(
            "Requested database instance is not current",
            false,
        ));
    }
    Ok(QueryClusterBinding {
        api_version: CNPG_CLUSTER_API_VERSION.into(),
        kind: CNPG_CLUSTER_KIND.into(),
        name: MANAGED_DATABASE_CLUSTER_NAME.into(),
        uid,
    })
}

struct DatabaseCredentials {
    username: String,
    password: String,
}

fn validate_database_secret(
    secret: &Secret,
    cluster_uid: &str,
) -> Result<DatabaseCredentials, SourceError> {
    if secret.metadata.name.as_deref() != Some(DATABASE_SUPERUSER_SECRET_NAME)
        || secret.metadata.namespace.as_deref() != Some(MANAGED_DATABASE_NAMESPACE)
        || secret.metadata.uid.as_deref().is_none_or(str::is_empty)
        || secret.metadata.deletion_timestamp.is_some()
        || secret.type_.as_deref() != Some("kubernetes.io/basic-auth")
        || !has_exact_controlling_cluster_owner(&secret.metadata.owner_references, cluster_uid)
    {
        return Err(database_unavailable(
            "Database credentials are invalid",
            false,
        ));
    }
    let data = secret
        .data
        .as_ref()
        .ok_or_else(|| database_unavailable("Database credentials are invalid", false))?;
    let username = credential_text(
        data.get("username").map(|value| value.0.as_slice()),
        MAX_DATABASE_USERNAME_BYTES,
    )?;
    let password = credential_text(
        data.get("password").map(|value| value.0.as_slice()),
        MAX_DATABASE_PASSWORD_BYTES,
    )?;
    Ok(DatabaseCredentials { username, password })
}

fn credential_text(value: Option<&[u8]>, maximum: usize) -> Result<String, SourceError> {
    let value =
        value.ok_or_else(|| database_unavailable("Database credentials are invalid", false))?;
    if value.is_empty() || value.len() > maximum || value.contains(&0) {
        return Err(database_unavailable(
            "Database credentials are invalid",
            false,
        ));
    }
    String::from_utf8(value.to_vec())
        .map_err(|_| database_unavailable("Database credentials are invalid", false))
}

fn has_exact_controlling_cluster_owner(
    owners: &Option<Vec<k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference>>,
    cluster_uid: &str,
) -> bool {
    owners.as_ref().is_some_and(|owners| {
        owners.iter().any(|owner| {
            owner.api_version == CNPG_CLUSTER_API_VERSION
                && owner.kind == CNPG_CLUSTER_KIND
                && owner.name == MANAGED_DATABASE_CLUSTER_NAME
                && owner.uid == cluster_uid
                && owner.controller == Some(true)
        })
    })
}

fn validate_query_request(request: &DatabaseQueryRequest) -> Result<(), SourceError> {
    if request.sql.trim().is_empty() || request.sql.len() > MAX_SQL_BYTES {
        return Err(SourceError::DatabaseUnavailable {
            message: "SQL must be nonempty and at most 64 KiB".into(),
            retryable: false,
        });
    }
    if request.database.is_empty()
        || request.database.len() > MAX_DATABASE_BYTES
        || request
            .database
            .chars()
            .any(|character| character == '\0' || character.is_control())
    {
        return Err(SourceError::DatabaseUnavailable {
            message: "Database name must be 1 to 63 bytes without control characters".into(),
            retryable: false,
        });
    }
    if !is_dns_label(&request.instance, MAX_INSTANCE_BYTES) {
        return Err(SourceError::DatabaseUnavailable {
            message: "Database instance must be a valid DNS label of at most 63 bytes".into(),
            retryable: false,
        });
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

fn database_unavailable(message: impl Into<String>, retryable: bool) -> SourceError {
    SourceError::DatabaseUnavailable {
        message: message.into(),
        retryable,
    }
}

fn query_execution_error(error: QueryExecutionError) -> SourceError {
    match error {
        QueryExecutionError::DatabaseUnavailable => {
            database_unavailable("Database connection is unavailable", true)
        }
        QueryExecutionError::QueryFailed { sqlstate, message } => {
            SourceError::QueryFailed { sqlstate, message }
        }
        QueryExecutionError::ResponseLimitExceeded => SourceError::QueryResponseTooLarge,
        QueryExecutionError::TimedOut => SourceError::QueryTimedOut,
        QueryExecutionError::OutcomeUnknown => SourceError::QueryOutcomeUnknown,
    }
}

fn pod_binding_error(error: PodBindingError) -> SourceError {
    match error {
        PodBindingError::InvalidIdentity => {
            database_unavailable("Requested database instance Pod identity is invalid", false)
        }
        PodBindingError::NotReady => {
            database_unavailable("Requested database instance Pod is not ready", true)
        }
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CnpgClusterData {
    spec: CnpgClusterSpec,
    status: Option<CnpgClusterStatus>,
}

#[derive(Deserialize)]
struct CnpgClusterSpec {
    instances: i64,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CnpgClusterStatus {
    phase: Option<String>,
    phase_reason: Option<String>,
    instances: Option<i64>,
    ready_instances: Option<i64>,
    current_primary: Option<String>,
    target_primary: Option<String>,
    current_primary_timestamp: Option<String>,
    target_primary_timestamp: Option<String>,
    current_primary_failing_since_timestamp: Option<String>,
    image: Option<String>,
    #[serde(rename = "timelineID")]
    timeline_id: Option<i64>,
    read_service: Option<String>,
    write_service: Option<String>,
    #[serde(default)]
    instance_names: Vec<String>,
    #[serde(default)]
    instances_status: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    instances_reported_state: BTreeMap<String, CnpgInstanceReportedState>,
    topology: Option<CnpgTopology>,
    pvc_count: Option<i64>,
    #[serde(default, rename = "healthyPVC")]
    healthy_pvc: Vec<String>,
    #[serde(default, rename = "danglingPVC")]
    dangling_pvc: Vec<String>,
    #[serde(default, rename = "initializingPVC")]
    initializing_pvc: Vec<String>,
    #[serde(default, rename = "resizingPVC")]
    resizing_pvc: Vec<String>,
    #[serde(default, rename = "unusablePVC")]
    unusable_pvc: Vec<String>,
    #[serde(default)]
    conditions: Vec<CnpgCondition>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CnpgInstanceReportedState {
    is_primary: bool,
    #[serde(rename = "timeLineID")]
    timeline_id: Option<i64>,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CnpgTopology {
    #[serde(default)]
    instances: BTreeMap<String, BTreeMap<String, String>>,
    nodes_used: Option<i64>,
    successfully_extracted: Option<bool>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CnpgCondition {
    #[serde(rename = "type")]
    condition_type: String,
    status: String,
    reason: String,
    message: String,
    observed_generation: Option<i64>,
    last_transition_time: String,
}

fn project_database_cluster(
    cluster: DynamicObject,
) -> Result<Option<DatabaseClusterObservation>, ()> {
    let types = cluster.types.as_ref().ok_or(())?;
    if types.api_version != CNPG_CLUSTER_API_VERSION
        || types.kind != CNPG_CLUSTER_KIND
        || cluster.metadata.namespace.as_deref() != Some(MANAGED_DATABASE_NAMESPACE)
        || cluster.metadata.name.as_deref() != Some(MANAGED_DATABASE_CLUSTER_NAME)
    {
        return Err(());
    }
    let generation = cluster.metadata.generation.ok_or(())?;
    if generation < 0 {
        return Err(());
    }
    let body: CnpgClusterData = serde_json::from_value(cluster.data).map_err(|_| ())?;
    let desired_instances = count(body.spec.instances)?;
    let Some(status) = body.status else {
        return Ok(None);
    };
    if status.phase.is_none()
        && status.instances.is_none()
        && status.ready_instances.is_none()
        && status.instance_names.is_empty()
    {
        return Ok(None);
    }
    let observed_instances = count(status.instances.unwrap_or_default())?;
    let ready_instances = count(status.ready_instances.unwrap_or_default())?;
    let instances = projected_instances(&status);
    let conditions = projected_database_conditions(&status.conditions)?;
    let topology_available = status
        .topology
        .as_ref()
        .and_then(|topology| topology.successfully_extracted)
        .unwrap_or(false);
    let nodes_used = status
        .topology
        .as_ref()
        .and_then(|topology| topology.nodes_used)
        .map(count)
        .transpose()?;
    Ok(Some(DatabaseClusterObservation {
        identity: DatabaseClusterIdentity {
            api_version: CNPG_CLUSTER_API_VERSION.into(),
            kind: CNPG_CLUSTER_KIND.into(),
            namespace: MANAGED_DATABASE_NAMESPACE.into(),
            name: MANAGED_DATABASE_CLUSTER_NAME.into(),
            uid: cluster
                .metadata
                .uid
                .as_deref()
                .filter(|value| !value.is_empty())
                .map(|value| bounded(value, MAX_IDENTITY)),
            generation,
        },
        phase: bounded_option(status.phase.as_deref(), MAX_TEXT),
        reason: bounded_option(status.phase_reason.as_deref(), MAX_TEXT),
        desired_instances,
        observed_instances,
        ready_instances,
        current_primary: bounded_option(status.current_primary.as_deref(), MAX_IDENTITY),
        target_primary: bounded_option(status.target_primary.as_deref(), MAX_IDENTITY),
        current_primary_since: bounded_option(
            status.current_primary_timestamp.as_deref(),
            MAX_TIMESTAMP,
        ),
        target_primary_requested_at: bounded_option(
            status.target_primary_timestamp.as_deref(),
            MAX_TIMESTAMP,
        ),
        current_primary_failing_since: bounded_option(
            status.current_primary_failing_since_timestamp.as_deref(),
            MAX_TIMESTAMP,
        ),
        image: bounded_option(status.image.as_deref(), MAX_IMAGE),
        timeline: status.timeline_id.filter(|value| *value >= 0),
        services: DatabaseServices {
            read: bounded_option(status.read_service.as_deref(), MAX_IDENTITY),
            write: bounded_option(status.write_service.as_deref(), MAX_IDENTITY),
        },
        topology_available,
        nodes_used,
        instances,
        storage: DatabasePvcHealth {
            total: count(status.pvc_count.unwrap_or_default())?,
            healthy: bounded_len(status.healthy_pvc.len()),
            dangling: bounded_len(status.dangling_pvc.len()),
            initializing: bounded_len(status.initializing_pvc.len()),
            resizing: bounded_len(status.resizing_pvc.len()),
            unusable: bounded_len(status.unusable_pvc.len()),
        },
        conditions,
    }))
}

fn projected_instances(status: &CnpgClusterStatus) -> Vec<DatabaseInstanceObservation> {
    let mut names = BTreeSet::new();
    names.extend(
        status
            .current_primary
            .iter()
            .filter(|name| !name.is_empty()),
    );
    names.extend(status.instance_names.iter().filter(|name| !name.is_empty()));
    names.extend(
        status
            .instances_reported_state
            .keys()
            .filter(|name| !name.is_empty()),
    );
    for instances in status.instances_status.values() {
        names.extend(instances.iter().filter(|name| !name.is_empty()));
    }
    if let Some(topology) = &status.topology {
        names.extend(topology.instances.keys().filter(|name| !name.is_empty()));
    }
    let mut instance_status = BTreeMap::new();
    for (state, instances) in &status.instances_status {
        for name in instances {
            instance_status
                .entry(name.as_str())
                .or_insert(state.as_str());
        }
    }
    names
        .into_iter()
        .take(MAX_DATABASE_INSTANCES)
        .map(|name| {
            let reported = status.instances_reported_state.get(name);
            let role = if status.current_primary.as_deref() == Some(name)
                || reported.is_some_and(|state| state.is_primary)
            {
                DatabaseInstanceRole::Primary
            } else if reported.is_some()
                || status.current_primary.is_some()
                || instance_status.contains_key(name.as_str())
            {
                DatabaseInstanceRole::Standby
            } else {
                DatabaseInstanceRole::Unknown
            };
            let placement = status
                .topology
                .as_ref()
                .and_then(|topology| topology.instances.get(name));
            DatabaseInstanceObservation {
                name: bounded(name, MAX_IDENTITY),
                role,
                status: instance_status
                    .get(name.as_str())
                    .map(|value| bounded(value, MAX_TEXT)),
                timeline: reported.and_then(|state| state.timeline_id.filter(|value| *value >= 0)),
                node: placement
                    .and_then(|labels| labels.get("kubernetes.io/hostname"))
                    .map(|value| bounded(value, MAX_IDENTITY)),
                zone: placement
                    .and_then(|labels| labels.get("topology.kubernetes.io/zone"))
                    .map(|value| bounded(value, MAX_IDENTITY)),
            }
        })
        .collect()
}

fn projected_database_conditions(
    conditions: &[CnpgCondition],
) -> Result<Vec<DatabaseCondition>, ()> {
    conditions
        .iter()
        .take(MAX_DATABASE_CONDITIONS)
        .map(|condition| {
            let status = match condition.status.as_str() {
                "True" => ConditionStatus::True,
                "False" => ConditionStatus::False,
                "Unknown" => ConditionStatus::Unknown,
                _ => return Err(()),
            };
            Ok(DatabaseCondition {
                condition_type: bounded(&condition.condition_type, MAX_IDENTITY),
                status,
                reason: (!condition.reason.is_empty())
                    .then(|| bounded(&condition.reason, MAX_TEXT)),
                message: (!condition.message.is_empty())
                    .then(|| bounded(&condition.message, MAX_TEXT)),
                observed_generation: condition.observed_generation.filter(|value| *value >= 0),
                last_transition_time: (!condition.last_transition_time.is_empty())
                    .then(|| bounded(&condition.last_transition_time, MAX_TIMESTAMP)),
            })
        })
        .collect()
}

fn count(value: i64) -> Result<u32, ()> {
    u32::try_from(value)
        .map(|value| value.min(MAX_DATABASE_COUNT))
        .map_err(|_| ())
}

fn bounded_len(value: usize) -> u32 {
    u32::try_from(value)
        .unwrap_or(u32::MAX)
        .min(MAX_DATABASE_COUNT)
}

fn bounded_option(value: Option<&str>, max: usize) -> Option<String> {
    value
        .filter(|value| !value.is_empty())
        .map(|value| bounded(value, max))
}

fn bounded(value: &str, max: usize) -> String {
    tenant_controller::sanitize::text(value)
        .chars()
        .take(max)
        .collect()
}

fn controller_arguments(arguments: &[String]) -> Option<(&str, &str)> {
    let mut provider = None;
    let mut version = None;
    let mut index = 0;
    while index < arguments.len() {
        let argument = arguments.get(index)?;
        if argument == "--probe-in-cluster" {
            index += 1;
            continue;
        }
        let (flag, value) = match argument.split_once('=') {
            Some((flag, value)) => (flag, value),
            None => {
                index += 1;
                (argument.as_str(), arguments.get(index)?.as_str())
            }
        };
        match flag {
            "--provider" => provider = Some(value),
            "--supported-kubernetes-version" => version = Some(value),
            "--leader-elect"
            | "--health-probe-bind-address"
            | "--leader-election-id"
            | "--leader-election-namespace"
            | "--leader-election-identity"
            | "--leader-lease-duration-seconds"
            | "--leader-renew-grace-seconds"
            | "--controller-image"
            | "--activation-token"
            | "--metrics-bind-address" => {}
            _ => return None,
        }
        index += 1;
    }
    Some((provider?, version?))
}

fn valid_version(value: &str) -> bool {
    value.split('.').count() == 3
        && value
            .split('.')
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}

fn delete_response(
    name: &str,
    uid: &str,
    generation: Option<i64>,
    state: TenantDeleteState,
) -> TenantDeleteResponse {
    TenantDeleteResponse {
        identity: TenantMutationIdentity {
            name: name.into(),
            uid: uid.into(),
            generation,
        },
        state,
    }
}

fn mutation_error(error: kube::Error) -> SourceError {
    match error {
        kube::Error::Api(status) if status.code == 409 => SourceError::Conflict,
        kube::Error::Api(status) if status.code == 403 => SourceError::Forbidden,
        kube::Error::Api(status) if matches!(status.code, 400 | 422) => SourceError::Rejected,
        error => {
            tracing::warn!(
                error = %tenant_controller::sanitize::text(&error.to_string()),
                "Tenant lifecycle mutation failed"
            );
            SourceError::KubernetesUnavailable
        }
    }
}

impl DataSource for KubeDataSource {
    fn list_tenants(&self) -> SourceFuture<'_, Vec<Tenant>> {
        Box::pin(async move {
            let list = self.tenant_list(MAX_TENANTS + 1).await?;
            if list.items.len() > MAX_TENANTS as usize
                || list
                    .metadata
                    .continue_
                    .as_deref()
                    .is_some_and(|value| !value.is_empty())
            {
                return Err(SourceError::ResponseTooLarge);
            }
            Ok(list.items)
        })
    }

    fn get_tenant(&self, name: &str) -> SourceFuture<'_, Option<Tenant>> {
        let name = name.to_owned();
        Box::pin(async move {
            Api::<Tenant>::all(self.client.clone())
                .get_opt(&name)
                .await
                .map_err(|error| {
                    tracing::warn!(
                        tenant = name,
                        error = %tenant_controller::sanitize::text(&error.to_string()),
                        "Tenant read failed"
                    );
                    SourceError::KubernetesUnavailable
                })
        })
    }

    fn creation_capability(&self, provider: ProviderMode) -> SourceFuture<'_, CreationCapability> {
        Box::pin(async move { Ok(self.load_creation_capability(provider).await) })
    }

    fn create_tenant(&self, tenant: Tenant) -> SourceFuture<'_, Tenant> {
        Box::pin(async move { self.create_exact_tenant(tenant).await })
    }

    fn delete_tenant<'a>(
        &'a self,
        name: &'a str,
        uid: &'a str,
    ) -> SourceFuture<'a, TenantDeleteResponse> {
        Box::pin(async move { self.delete_exact_tenant(name, uid).await })
    }

    fn list_management_resources(
        &self,
        provider: ProviderMode,
        tenant_name: &str,
    ) -> SourceFuture<'_, Vec<DynamicObject>> {
        let client = self.client.clone();
        let tenant_name: Arc<str> = Arc::from(tenant_name);
        Box::pin(async move {
            let catalog = match provider {
                ProviderMode::Local => MANAGEMENT_RESOURCES,
                ProviderMode::Azure => AZURE_MANAGEMENT_RESOURCES,
            };
            let mut unique = BTreeSet::new();
            let definitions: Vec<_> = catalog
                .iter()
                .copied()
                .filter(|definition| !matches!(definition.kind, "Lease" | "Secret"))
                .filter(|definition| {
                    unique.insert((
                        definition.api_version,
                        definition.plural,
                        definition.namespaced,
                        definition.inventory_namespace,
                    ))
                })
                .collect();
            let chunks = stream::iter(definitions.into_iter().map(|definition| {
                Self::list_definition(client.clone(), definition, tenant_name.clone())
            }))
            .buffer_unordered(RESOURCE_LIST_CONCURRENCY)
            .collect::<Vec<_>>()
            .await;
            let mut resources = Vec::new();
            for chunk in chunks {
                resources.extend(chunk?);
                if resources.len() > MAX_RESOURCES_TOTAL {
                    return Err(SourceError::ResponseTooLarge);
                }
            }

            Ok(resources)
        })
    }

    fn database_observation<'a>(
        &'a self,
        provider: ProviderMode,
        tenant: &'a Tenant,
        management_resources: &'a [DynamicObject],
    ) -> SourceFuture<'a, DatabaseObservation> {
        Box::pin(async move {
            Ok(self
                .observe_database(provider, tenant, management_resources)
                .await)
        })
    }

    fn database_query<'a>(
        &'a self,
        provider: ProviderMode,
        tenant: &'a Tenant,
        management_resources: &'a [DynamicObject],
        request: &'a DatabaseQueryRequest,
    ) -> SourceFuture<'a, DatabaseQueryResponse> {
        Box::pin(async move {
            self.query_database(provider, tenant, management_resources, request)
                .await
        })
    }

    fn check_ready(&self) -> SourceFuture<'_, ()> {
        Box::pin(async move {
            self.tenant_list(1).await?;
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use std::{
        collections::VecDeque,
        convert::Infallible,
        future::ready,
        sync::{Arc, Mutex},
    };

    use axum::http::{Method, Request, Response};
    use http_body_util::BodyExt;
    use kube::client::Body;
    use serde_json::{Value, json};
    use tenant_controller::{
        api::{SUPPORTED_KUBERNETES_VERSION, TenantSpec, canonical_spec, spec_hash},
        ownership::{
            FOUNDATION_ANNOTATION, RESOURCE_ANNOTATION, SPEC_HASH_ANNOTATION, TENANT_ANNOTATION,
            TENANT_UID_ANNOTATION,
        },
    };
    use tower::service_fn;

    use super::*;

    #[derive(Clone)]
    struct StaticTenantClientLoader {
        client: Client,
        endpoints: Arc<Mutex<Vec<String>>>,
    }

    #[derive(Clone)]
    struct StaticQueryExecutor {
        result: Result<crate::database::QueryExecution, QueryExecutionError>,
        calls: Arc<Mutex<Vec<QueryExecutorCall>>>,
    }

    #[derive(Clone, Debug, Eq, PartialEq)]
    struct QueryExecutorCall {
        pod: String,
        pod_uid: String,
        cluster_uid: String,
        database: String,
        username: String,
        password: String,
        sql: String,
    }

    impl QueryExecutor for StaticQueryExecutor {
        fn execute<'a>(&'a self, request: QueryConnection<'a>) -> crate::database::QueryFuture<'a> {
            self.calls
                .lock()
                .expect("query calls lock")
                .push(QueryExecutorCall {
                    pod: request.pod.pod_name.clone(),
                    pod_uid: request.pod.pod_uid.clone(),
                    cluster_uid: request.pod.cluster.uid.clone(),
                    database: request.database.into(),
                    username: request.username.into(),
                    password: request.password.into(),
                    sql: request.sql.into(),
                });
            Box::pin(ready(self.result.clone()))
        }
    }

    impl TenantClientLoader for StaticTenantClientLoader {
        fn load<'a>(
            &'a self,
            _management: Client,
            _control_plane: &'a DynamicObject,
            _namespace: &'a str,
            _tenant_name: &'a str,
            endpoint: &'a str,
        ) -> TenantClientFuture<'a> {
            self.endpoints
                .lock()
                .expect("endpoint calls lock")
                .push(endpoint.to_owned());
            let client = self.client.clone();
            Box::pin(async move { Ok(client) })
        }
    }

    type Calls = Arc<Mutex<Vec<(Method, String)>>>;

    fn fixture_client(responses: Vec<(u16, Value)>) -> (Client, Calls) {
        let responses = Arc::new(Mutex::new(VecDeque::from(responses)));
        let calls = Arc::new(Mutex::new(Vec::new()));
        let service_responses = responses.clone();
        let service_calls = calls.clone();
        let client = Client::new(
            service_fn(move |request: Request<Body>| {
                let responses = service_responses.clone();
                let calls = service_calls.clone();
                async move {
                    calls
                        .lock()
                        .expect("call log lock")
                        .push((request.method().clone(), request.uri().path().to_owned()));
                    let (status, body) = responses
                        .lock()
                        .expect("responses lock")
                        .pop_front()
                        .unwrap_or_else(|| {
                            (
                                500,
                                json!({
                                    "apiVersion":"v1",
                                    "kind":"Status",
                                    "status":"Failure",
                                    "code":500,
                                    "reason":"UnexpectedRequest",
                                    "message":"unexpected request"
                                }),
                            )
                        });
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "application/json")
                            .body(Body::from(
                                serde_json::to_vec(&body).expect("response body"),
                            ))
                            .expect("response"),
                    )
                }
            }),
            "default",
        );
        (client, calls)
    }

    fn source_with_tenant_client(management: Client, tenant: Client) -> KubeDataSource {
        source_with_recorded_tenant_client(management, tenant).0
    }

    fn source_with_recorded_tenant_client(
        management: Client,
        tenant: Client,
    ) -> (KubeDataSource, Arc<Mutex<Vec<String>>>) {
        let endpoints = Arc::new(Mutex::new(Vec::new()));
        let source = KubeDataSource {
            client: management,
            tenant_clients: Arc::new(StaticTenantClientLoader {
                client: tenant,
                endpoints: endpoints.clone(),
            }),
            query_executor: Arc::new(PostgresQueryExecutor),
        };
        (source, endpoints)
    }

    fn source_with_query_executor(
        management: Client,
        tenant: Client,
        result: Result<crate::database::QueryExecution, QueryExecutionError>,
    ) -> (KubeDataSource, Arc<Mutex<Vec<QueryExecutorCall>>>) {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let source = KubeDataSource {
            client: management,
            tenant_clients: Arc::new(StaticTenantClientLoader {
                client: tenant,
                endpoints: Arc::new(Mutex::new(Vec::new())),
            }),
            query_executor: Arc::new(StaticQueryExecutor {
                result,
                calls: calls.clone(),
            }),
        };
        (source, calls)
    }

    fn local_tenant() -> Tenant {
        serde_json::from_value(json!({
            "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha4",
            "kind":"Tenant",
            "metadata":{"name":"tenant-a","uid":"tenant-uid","generation":4},
            "spec":{
                "kubernetesVersion":"1.36.4",
                "workers":3,
                "provider":{"type":"local","databases":3}
            },
            "status":{
                "observedGeneration":4,
                "phase":"Ready",
                "provider":{
                    "type":"local",
                    "clusterUID":"cluster-uid",
                    "foundationHash":"foundation-hash",
                    "allocation":{
                        "slotId":"1",
                        "endpoint":"172.18.255.2",
                        "podCIDR":"10.244.0.0/24",
                        "serviceCIDR":"10.96.0.0/24"
                    }
                }
            }
        }))
        .expect("local Tenant")
    }

    fn azure_tenant() -> Tenant {
        serde_json::from_value(json!({
            "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha4",
            "kind":"Tenant",
            "metadata":{"name":"tenant-a","uid":"tenant-uid","generation":4},
            "spec":{
                "kubernetesVersion":"1.36.4",
                "workers":3,
                "provider":{
                    "type":"azure",
                    "podCIDR":"10.244.0.0/24",
                    "serviceCIDR":"10.96.0.0/24"
                }
            }
        }))
        .expect("Azure Tenant")
    }

    fn local_annotations(role: &str) -> Value {
        let tenant = local_tenant();
        let canonical = canonical_spec("tenant-a", &tenant.spec, SUPPORTED_KUBERNETES_VERSION)
            .expect("canonical local Tenant");
        json!({
            (TENANT_ANNOTATION):"tenant-a",
            (TENANT_UID_ANNOTATION):"tenant-uid",
            (SPEC_HASH_ANNOTATION):spec_hash(&canonical),
            (FOUNDATION_ANNOTATION):"foundation-hash",
            (RESOURCE_ANNOTATION):role
        })
    }

    fn control_plane() -> DynamicObject {
        serde_json::from_value(json!({
            "apiVersion":"controlplane.cluster.x-k8s.io/v1alpha2",
            "kind":"KamajiControlPlane",
            "metadata":{
                "name":"tenant-a",
                "namespace":"tenant-a",
                "uid":"control-plane-uid",
                "annotations":local_annotations("kamaji-control-plane"),
                "ownerReferences":[{
                    "apiVersion":"cluster.x-k8s.io/v1beta2",
                    "kind":"Cluster",
                    "name":"tenant-a",
                    "uid":"cluster-uid"
                }]
            }
        }))
        .expect("control plane")
    }

    fn management_cluster() -> DynamicObject {
        serde_json::from_value(json!({
            "apiVersion":"cluster.x-k8s.io/v1beta2",
            "kind":"Cluster",
            "metadata":{
                "name":"tenant-a",
                "namespace":"tenant-a",
                "uid":"cluster-uid",
                "annotations":local_annotations("cluster")
            },
            "spec":{
                "controlPlaneEndpoint":{
                    "host":"172.18.255.2",
                    "port":6443
                }
            }
        }))
        .expect("management Cluster")
    }

    fn local_resources() -> Vec<DynamicObject> {
        vec![management_cluster(), control_plane()]
    }

    fn cnpg_cluster() -> Value {
        json!({
            "apiVersion":"postgresql.cnpg.io/v1",
            "kind":"Cluster",
            "metadata":{
                "name":"capi-postgres",
                "namespace":"database",
                "uid":"database-uid",
                "generation":8
            },
            "spec":{"instances":3},
            "status":{
                "phase":"Cluster in healthy state",
                "phaseReason":"ClusterIsReady",
                "instances":3,
                "readyInstances":3,
                "currentPrimary":"capi-postgres-1",
                "targetPrimary":"capi-postgres-1",
                "currentPrimaryTimestamp":"2026-09-29T20:40:00Z",
                "targetPrimaryTimestamp":"2026-09-29T20:39:59Z",
                "image":"ghcr.io/cloudnative-pg/postgresql:18",
                "timelineID":4,
                "readService":"capi-postgres-r",
                "writeService":"capi-postgres-rw",
                "instanceNames":["capi-postgres-1","capi-postgres-2","capi-postgres-3"],
                "instancesStatus":{
                    "healthy":["capi-postgres-1","capi-postgres-2","capi-postgres-3"]
                },
                "instancesReportedState":{
                    "capi-postgres-1":{"isPrimary":true,"timeLineID":4,"ip":"10.244.0.9"},
                    "capi-postgres-2":{"isPrimary":false,"timeLineID":4,"ip":"10.244.0.10"},
                    "capi-postgres-3":{"isPrimary":false,"timeLineID":4,"ip":"10.244.0.11"}
                },
                "topology":{
                    "successfullyExtracted":true,
                    "nodesUsed":3,
                    "instances":{
                        "capi-postgres-1":{
                            "kubernetes.io/hostname":"worker-a",
                            "topology.kubernetes.io/zone":"local-a",
                            "arbitrary.example/private":"private-label"
                        },
                        "capi-postgres-2":{"kubernetes.io/hostname":"worker-b"},
                        "capi-postgres-3":{"kubernetes.io/hostname":"worker-c"}
                    }
                },
                "pvcCount":3,
                "healthyPVC":["capi-postgres-1","capi-postgres-2","capi-postgres-3"],
                "danglingPVC":[],
                "initializingPVC":[],
                "resizingPVC":[],
                "unusablePVC":[],
                "conditions":[{
                    "type":"Ready",
                    "status":"True",
                    "reason":"ClusterIsReady",
                    "message":"Cluster is ready",
                    "observedGeneration":8,
                    "lastTransitionTime":"2026-09-29T20:40:00Z"
                }],
                "systemID":"private-system-id",
                "managedRolesStatus":{"private-role":{"status":"ok"}}
            }
        })
    }

    fn database_pod() -> Value {
        json!({
            "apiVersion":"v1",
            "kind":"Pod",
            "metadata":{
                "name":"capi-postgres-1",
                "namespace":"database",
                "uid":"pod-uid",
                "ownerReferences":[{
                    "apiVersion":"postgresql.cnpg.io/v1",
                    "kind":"Cluster",
                    "name":"capi-postgres",
                    "uid":"database-uid",
                    "controller":true,
                    "blockOwnerDeletion":true
                }]
            },
            "spec":{"containers":[{"name":"postgres","image":"postgres:18"}]},
            "status":{
                "phase":"Running",
                "conditions":[{
                    "type":"Ready",
                    "status":"True",
                    "lastTransitionTime":"2026-09-29T20:40:00Z"
                }]
            }
        })
    }

    fn database_secret() -> Value {
        json!({
            "apiVersion":"v1",
            "kind":"Secret",
            "metadata":{
                "name":"capi-postgres-superuser",
                "namespace":"database",
                "uid":"secret-uid",
                "ownerReferences":[{
                    "apiVersion":"postgresql.cnpg.io/v1",
                    "kind":"Cluster",
                    "name":"capi-postgres",
                    "uid":"database-uid",
                    "controller":true
                }]
            },
            "type":"kubernetes.io/basic-auth",
            "data":{
                "username":"cG9zdGdyZXM=",
                "password":"cHJpdmF0ZS1wYXNzd29yZA=="
            }
        })
    }

    fn query_request() -> DatabaseQueryRequest {
        DatabaseQueryRequest {
            instance: "capi-postgres-1".into(),
            database: "postgres".into(),
            sql: "select 1; update values set active = true".into(),
        }
    }

    fn query_execution() -> crate::database::QueryExecution {
        crate::database::QueryExecution {
            duration_ms: 17,
            truncated: false,
            results: vec![
                tenant_admin_shared::query::DatabaseQueryResult {
                    columns: vec!["value".into()],
                    rows: vec![vec![Some("1".into())]],
                    affected_rows: 1,
                    truncated: false,
                },
                tenant_admin_shared::query::DatabaseQueryResult {
                    columns: Vec::new(),
                    rows: Vec::new(),
                    affected_rows: 4,
                    truncated: false,
                },
            ],
        }
    }

    fn status_response(code: u16) -> Value {
        json!({
            "apiVersion":"v1",
            "kind":"Status",
            "status":"Failure",
            "code":code,
            "reason":if code == 404 {"NotFound"} else {"ServiceUnavailable"},
            "message":"private-sentinel"
        })
    }

    #[tokio::test]
    async fn deterministic_cluster_resource_uses_exact_get() {
        let calls = Arc::new(Mutex::new(Vec::<(String, String)>::new()));
        let service_calls = calls.clone();
        let client = Client::new(
            service_fn(move |request: Request<Body>| {
                let calls = service_calls.clone();
                async move {
                    let path = request.uri().path().to_owned();
                    let query = request.uri().query().unwrap_or_default().to_owned();
                    calls
                        .lock()
                        .expect("call log lock")
                        .push((path.clone(), query));
                    let (status, body) = if path == "/api/v1/namespaces/tenant-a" {
                        (
                            200,
                            json!({
                                "metadata": {
                                    "name": "tenant-a",
                                    "uid": "namespace-uid"
                                }
                            }),
                        )
                    } else {
                        (
                            404,
                            json!({
                                "apiVersion": "v1",
                                "kind": "Status",
                                "status": "Failure",
                                "code": 404,
                                "reason": "NotFound",
                                "message": "not found"
                            }),
                        )
                    };
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "application/json")
                            .body(Body::from(
                                serde_json::to_vec(&body).expect("response body"),
                            ))
                            .expect("response"),
                    )
                }
            }),
            "default",
        );
        let source = KubeDataSource::new(client);

        let resources = source
            .list_management_resources(ProviderMode::Local, "tenant-a")
            .await
            .expect("resource scan");

        let namespace = resources
            .iter()
            .find(|resource| resource.metadata.uid.as_deref() == Some("namespace-uid"))
            .expect("exact namespace");
        let types = namespace.types.as_ref().expect("restored type metadata");
        assert_eq!(types.api_version, "v1");
        assert_eq!(types.kind, "Namespace");

        let calls = calls.lock().expect("call log lock");
        assert!(
            calls
                .iter()
                .any(|(path, _)| path == "/api/v1/namespaces/tenant-a")
        );
        assert!(
            calls.iter().all(|(path, _)| path != "/api/v1/namespaces"),
            "Namespace inventory must not list every cluster Namespace"
        );
    }

    #[tokio::test]
    async fn local_database_observation_parses_primary_standbys_and_exact_path() {
        let (management, management_calls) = fixture_client(Vec::new());
        let (tenant_client, tenant_calls) = fixture_client(vec![(200, cnpg_cluster())]);
        let (source, endpoint_calls) =
            source_with_recorded_tenant_client(management, tenant_client);

        let observation = source
            .database_observation(ProviderMode::Local, &local_tenant(), &local_resources())
            .await
            .expect("database observation");

        let DatabaseObservation::Available { cluster, .. } = observation else {
            panic!("expected available database observation");
        };
        assert_eq!(cluster.desired_instances, 3);
        assert_eq!(cluster.observed_instances, 3);
        assert_eq!(cluster.ready_instances, 3);
        assert_eq!(cluster.current_primary.as_deref(), Some("capi-postgres-1"));
        assert_eq!(cluster.timeline, Some(4));
        assert_eq!(cluster.nodes_used, Some(3));
        assert_eq!(cluster.storage.healthy, 3);
        assert_eq!(cluster.instances.len(), 3);
        assert_eq!(cluster.instances[0].role, DatabaseInstanceRole::Primary);
        assert!(
            cluster.instances[1..]
                .iter()
                .all(|instance| instance.role == DatabaseInstanceRole::Standby)
        );
        assert_eq!(cluster.instances[0].node.as_deref(), Some("worker-a"));
        let serialized = serde_json::to_string(&cluster).expect("cluster serializes");
        for forbidden in [
            "10.244.0.",
            "private-label",
            "private-system-id",
            "private-role",
            "managedRoles",
            "systemID",
        ] {
            assert!(!serialized.contains(forbidden), "{forbidden} leaked");
        }
        assert!(
            management_calls
                .lock()
                .expect("management calls")
                .is_empty()
        );
        assert_eq!(
            endpoint_calls.lock().expect("endpoint calls").as_slice(),
            ["172.18.255.2:6443"]
        );
        assert_eq!(
            *tenant_calls.lock().expect("tenant calls"),
            vec![(
                Method::GET,
                "/apis/postgresql.cnpg.io/v1/namespaces/database/clusters/capi-postgres".into()
            )]
        );
    }

    #[tokio::test]
    async fn database_query_uses_exact_cluster_pod_secret_gets_and_mock_executor() {
        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, tenant_calls) = fixture_client(vec![
            (200, cnpg_cluster()),
            (200, database_pod()),
            (200, database_secret()),
        ]);
        let (source, executor_calls) =
            source_with_query_executor(management, tenant_client, Ok(query_execution()));

        let response = source
            .database_query(
                ProviderMode::Local,
                &local_tenant(),
                &local_resources(),
                &query_request(),
            )
            .await
            .expect("database query");

        assert_eq!(response.tenant, "tenant-a");
        assert_eq!(response.cluster, "capi-postgres");
        assert_eq!(response.results.len(), 2);
        assert_eq!(response.results[0].rows[0][0].as_deref(), Some("1"));
        assert_eq!(response.results[1].affected_rows, 4);
        assert_eq!(
            *tenant_calls.lock().expect("tenant calls"),
            vec![
                (
                    Method::GET,
                    "/apis/postgresql.cnpg.io/v1/namespaces/database/clusters/capi-postgres".into(),
                ),
                (
                    Method::GET,
                    "/api/v1/namespaces/database/pods/capi-postgres-1".into(),
                ),
                (
                    Method::GET,
                    "/api/v1/namespaces/database/secrets/capi-postgres-superuser".into(),
                ),
            ]
        );
        let calls = executor_calls.lock().expect("executor calls");
        assert_eq!(
            calls.as_slice(),
            [QueryExecutorCall {
                pod: "capi-postgres-1".into(),
                pod_uid: "pod-uid".into(),
                cluster_uid: "database-uid".into(),
                database: "postgres".into(),
                username: "postgres".into(),
                password: "private-password".into(),
                sql: "select 1; update values set active = true".into(),
            }]
        );
    }

    #[tokio::test]
    async fn database_query_rejects_provider_and_noncurrent_instance_before_pod_access() {
        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, calls) = fixture_client(Vec::new());
        let (source, executor_calls) =
            source_with_query_executor(management, tenant_client, Ok(query_execution()));
        let error = source
            .database_query(ProviderMode::Azure, &azure_tenant(), &[], &query_request())
            .await
            .expect_err("Azure query must fail");
        assert!(matches!(
            error,
            SourceError::DatabaseUnavailable {
                retryable: false,
                ..
            }
        ));
        assert!(calls.lock().expect("calls").is_empty());
        assert!(executor_calls.lock().expect("executor calls").is_empty());

        let mut request = query_request();
        request.instance = "capi-postgres-9".into();
        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, calls) = fixture_client(vec![(200, cnpg_cluster())]);
        let (source, executor_calls) =
            source_with_query_executor(management, tenant_client, Ok(query_execution()));
        let error = source
            .database_query(
                ProviderMode::Local,
                &local_tenant(),
                &local_resources(),
                &request,
            )
            .await
            .expect_err("unknown instance must fail");
        assert!(matches!(
            error,
            SourceError::DatabaseUnavailable {
                retryable: false,
                ..
            }
        ));
        assert_eq!(calls.lock().expect("calls").len(), 1);
        assert!(executor_calls.lock().expect("executor calls").is_empty());

        let mut cluster = cnpg_cluster();
        cluster["status"]["targetPrimary"] = json!("pending");
        let mut request = query_request();
        request.instance = "pending".into();
        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, calls) = fixture_client(vec![(200, cluster)]);
        let (source, executor_calls) =
            source_with_query_executor(management, tenant_client, Ok(query_execution()));
        assert!(
            source
                .database_query(
                    ProviderMode::Local,
                    &local_tenant(),
                    &local_resources(),
                    &request,
                )
                .await
                .is_err()
        );
        assert_eq!(calls.lock().expect("calls").len(), 1);
        assert!(executor_calls.lock().expect("executor calls").is_empty());
    }

    #[test]
    fn database_query_request_validation_is_bounded() {
        for request in [
            DatabaseQueryRequest {
                instance: "INVALID".into(),
                ..query_request()
            },
            DatabaseQueryRequest {
                database: "bad\0database".into(),
                ..query_request()
            },
            DatabaseQueryRequest {
                sql: " ".into(),
                ..query_request()
            },
            DatabaseQueryRequest {
                sql: "x".repeat(MAX_SQL_BYTES + 1),
                ..query_request()
            },
        ] {
            assert!(validate_query_request(&request).is_err());
        }
        assert!(validate_query_request(&query_request()).is_ok());
    }

    #[tokio::test]
    async fn database_query_requires_live_owned_ready_pod() {
        for edit in 0..8 {
            let mut pod = database_pod();
            match edit {
                0 => pod["metadata"]["uid"] = json!(""),
                1 => pod["metadata"]["deletionTimestamp"] = json!("2026-09-29T22:00:00Z"),
                2 => pod["status"]["phase"] = json!("Pending"),
                3 => pod["status"]["conditions"][0]["status"] = json!("False"),
                4 => pod["metadata"]["ownerReferences"][0]["uid"] = json!("foreign-uid"),
                5 => {
                    pod["metadata"]["ownerReferences"][0]["apiVersion"] =
                        json!("postgresql.cnpg.io/v1beta1")
                }
                6 => pod["metadata"]["ownerReferences"][0]["controller"] = json!(false),
                _ => pod["metadata"]["ownerReferences"][0]["blockOwnerDeletion"] = json!(false),
            }
            let (management, _) = fixture_client(Vec::new());
            let (tenant_client, calls) = fixture_client(vec![(200, cnpg_cluster()), (200, pod)]);
            let (source, executor_calls) =
                source_with_query_executor(management, tenant_client, Ok(query_execution()));
            let error = source
                .database_query(
                    ProviderMode::Local,
                    &local_tenant(),
                    &local_resources(),
                    &query_request(),
                )
                .await
                .expect_err("untrusted Pod must fail");
            assert!(matches!(error, SourceError::DatabaseUnavailable { .. }));
            assert_eq!(calls.lock().expect("calls").len(), 2, "edit {edit}");
            assert!(executor_calls.lock().expect("executor calls").is_empty());
        }

        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, calls) =
            fixture_client(vec![(200, cnpg_cluster()), (404, status_response(404))]);
        let (source, executor_calls) =
            source_with_query_executor(management, tenant_client, Ok(query_execution()));
        assert!(
            source
                .database_query(
                    ProviderMode::Local,
                    &local_tenant(),
                    &local_resources(),
                    &query_request(),
                )
                .await
                .is_err()
        );
        assert_eq!(calls.lock().expect("calls").len(), 2);
        assert!(executor_calls.lock().expect("executor calls").is_empty());
    }

    #[tokio::test]
    async fn database_query_requires_owned_well_formed_basic_auth_secret() {
        for edit in 0..9 {
            let mut secret = database_secret();
            match edit {
                0 => secret["type"] = json!("Opaque"),
                1 => secret["metadata"]["ownerReferences"][0]["uid"] = json!("foreign-uid"),
                2 => secret["metadata"]["ownerReferences"][0]["kind"] = json!("Foreign"),
                3 => {
                    secret["metadata"]["ownerReferences"][0]["apiVersion"] =
                        json!("postgresql.cnpg.io/v1beta1")
                }
                4 => secret["metadata"]["ownerReferences"][0]["controller"] = json!(false),
                5 => {
                    secret["metadata"]["ownerReferences"][0]
                        .as_object_mut()
                        .expect("owner reference")
                        .remove("controller");
                }
                6 => secret["data"]["username"] = json!(""),
                7 => secret["data"]["username"] = json!("//4="),
                _ => {
                    secret["data"]
                        .as_object_mut()
                        .expect("data")
                        .remove("password");
                }
            };
            let (management, _) = fixture_client(Vec::new());
            let (tenant_client, calls) = fixture_client(vec![
                (200, cnpg_cluster()),
                (200, database_pod()),
                (200, secret),
            ]);
            let (source, executor_calls) =
                source_with_query_executor(management, tenant_client, Ok(query_execution()));
            let error = source
                .database_query(
                    ProviderMode::Local,
                    &local_tenant(),
                    &local_resources(),
                    &query_request(),
                )
                .await
                .expect_err("invalid Secret must fail");
            assert!(matches!(
                error,
                SourceError::DatabaseUnavailable {
                    retryable: false,
                    ..
                }
            ));
            assert_eq!(calls.lock().expect("calls").len(), 3, "edit {edit}");
            assert!(executor_calls.lock().expect("executor calls").is_empty());
        }

        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, calls) = fixture_client(vec![
            (200, cnpg_cluster()),
            (200, database_pod()),
            (404, status_response(404)),
        ]);
        let (source, executor_calls) =
            source_with_query_executor(management, tenant_client, Ok(query_execution()));
        assert!(
            source
                .database_query(
                    ProviderMode::Local,
                    &local_tenant(),
                    &local_resources(),
                    &query_request(),
                )
                .await
                .is_err()
        );
        assert_eq!(calls.lock().expect("calls").len(), 3);
        assert!(executor_calls.lock().expect("executor calls").is_empty());
    }

    #[tokio::test]
    async fn database_query_maps_executor_query_error_and_timeout_without_sql() {
        for (executor_error, expected) in [
            (
                QueryExecutionError::QueryFailed {
                    sqlstate: Some("42601".into()),
                    message: "syntax error".into(),
                },
                SourceError::QueryFailed {
                    sqlstate: Some("42601".into()),
                    message: "syntax error".into(),
                },
            ),
            (
                QueryExecutionError::ResponseLimitExceeded,
                SourceError::QueryResponseTooLarge,
            ),
            (QueryExecutionError::TimedOut, SourceError::QueryTimedOut),
            (
                QueryExecutionError::OutcomeUnknown,
                SourceError::QueryOutcomeUnknown,
            ),
        ] {
            let (management, _) = fixture_client(Vec::new());
            let (tenant_client, _) = fixture_client(vec![
                (200, cnpg_cluster()),
                (200, database_pod()),
                (200, database_secret()),
            ]);
            let (source, _) =
                source_with_query_executor(management, tenant_client, Err(executor_error));
            let error = source
                .database_query(
                    ProviderMode::Local,
                    &local_tenant(),
                    &local_resources(),
                    &query_request(),
                )
                .await
                .expect_err("executor error");
            assert_eq!(error, expected);
            assert!(!format!("{error:?}").contains("select 1"));
        }
    }

    #[tokio::test]
    async fn trusted_endpoint_rejects_mismatched_uid_host_and_port() {
        for edit in 0..4 {
            let mut cluster = management_cluster();
            match edit {
                0 => cluster.metadata.uid = Some("foreign-cluster-uid".into()),
                1 => {
                    cluster.data["spec"]["controlPlaneEndpoint"]["host"] = json!("172.18.255.99");
                }
                2 => cluster.data["spec"]["controlPlaneEndpoint"]["port"] = json!(0),
                _ => cluster.data["spec"]["controlPlaneEndpoint"]["port"] = json!(65_536),
            }
            let (management, management_calls) = fixture_client(Vec::new());
            let (tenant_client, tenant_calls) = fixture_client(Vec::new());
            let (source, endpoint_calls) =
                source_with_recorded_tenant_client(management, tenant_client);
            let observation = source
                .database_observation(
                    ProviderMode::Local,
                    &local_tenant(),
                    &[cluster, control_plane()],
                )
                .await
                .expect("database observation");

            let expected_reason = if edit == 0 {
                DatabaseUnavailableReason::TenantAccessInvalid
            } else {
                DatabaseUnavailableReason::Malformed
            };
            assert!(matches!(
                observation,
                DatabaseObservation::Unavailable {
                    reason,
                    retryable: false,
                    ..
                } if reason == expected_reason
            ));
            assert!(
                management_calls
                    .lock()
                    .expect("management calls")
                    .is_empty()
            );
            assert!(tenant_calls.lock().expect("tenant calls").is_empty());
            assert!(endpoint_calls.lock().expect("endpoint calls").is_empty());
        }
    }

    #[tokio::test]
    async fn missing_or_non_deterministic_management_cluster_is_unavailable() {
        for edit in 0..4 {
            let mut resources = local_resources();
            match edit {
                0 => {
                    resources.remove(0);
                }
                1 => {
                    resources[0].types.as_mut().expect("types").api_version =
                        "cluster.x-k8s.io/v1beta1".into()
                }
                2 => resources[0].metadata.name = Some("foreign".into()),
                _ => resources[0].metadata.namespace = Some("foreign".into()),
            }
            let (management, _) = fixture_client(Vec::new());
            let (tenant_client, tenant_calls) = fixture_client(Vec::new());
            let (source, endpoint_calls) =
                source_with_recorded_tenant_client(management, tenant_client);
            let observation = source
                .database_observation(ProviderMode::Local, &local_tenant(), &resources)
                .await
                .expect("database observation");

            assert!(matches!(
                observation,
                DatabaseObservation::Unavailable {
                    reason: DatabaseUnavailableReason::ManagementResourceMissing,
                    ..
                }
            ));
            assert!(tenant_calls.lock().expect("tenant calls").is_empty());
            assert!(endpoint_calls.lock().expect("endpoint calls").is_empty());
        }
    }

    #[tokio::test]
    async fn control_plane_requires_current_markers_and_trusted_cluster_owner() {
        for edit in 0..4 {
            let mut control_plane = control_plane();
            match edit {
                0 => control_plane.metadata.annotations = None,
                1 => {
                    control_plane
                        .metadata
                        .annotations
                        .as_mut()
                        .expect("annotations")
                        .insert(TENANT_UID_ANNOTATION.into(), "foreign-tenant-uid".into());
                }
                2 => {
                    control_plane
                        .metadata
                        .owner_references
                        .as_mut()
                        .expect("owner references")[0]
                        .uid = "foreign-cluster-uid".into();
                }
                _ => control_plane.metadata.owner_references = None,
            }
            let (management, _) = fixture_client(Vec::new());
            let (tenant_client, tenant_calls) = fixture_client(Vec::new());
            let (source, endpoint_calls) =
                source_with_recorded_tenant_client(management, tenant_client);
            let observation = source
                .database_observation(
                    ProviderMode::Local,
                    &local_tenant(),
                    &[management_cluster(), control_plane],
                )
                .await
                .expect("database observation");

            assert!(
                matches!(
                    observation,
                    DatabaseObservation::Unavailable {
                        reason: DatabaseUnavailableReason::TenantAccessInvalid,
                        retryable: false,
                        ..
                    }
                ),
                "edit {edit}: {observation:?}"
            );
            assert!(tenant_calls.lock().expect("tenant calls").is_empty());
            assert!(endpoint_calls.lock().expect("endpoint calls").is_empty());
        }
    }

    #[tokio::test]
    async fn pending_failover_marker_is_not_projected_as_an_instance() {
        let mut cluster = cnpg_cluster();
        cluster["status"]["targetPrimary"] = json!("pending");
        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, _) = fixture_client(vec![(200, cluster)]);
        let source = source_with_tenant_client(management, tenant_client);
        let observation = source
            .database_observation(ProviderMode::Local, &local_tenant(), &local_resources())
            .await
            .expect("database observation");
        let DatabaseObservation::Available { cluster, .. } = observation else {
            panic!("expected available database observation");
        };

        assert_eq!(cluster.target_primary.as_deref(), Some("pending"));
        assert_eq!(cluster.instances.len(), 3);
        assert!(
            cluster
                .instances
                .iter()
                .all(|instance| instance.name != "pending")
        );
    }

    #[tokio::test]
    async fn missing_cluster_and_tenant_api_failure_are_explicit_and_sanitized() {
        for (status, expected_reason) in [
            (404, DatabaseUnavailableReason::ClusterMissing),
            (503, DatabaseUnavailableReason::TenantApiUnavailable),
        ] {
            let (management, _) = fixture_client(Vec::new());
            let (tenant_client, calls) = fixture_client(vec![(status, status_response(status))]);
            let source = source_with_tenant_client(management, tenant_client);
            let observation = source
                .database_observation(ProviderMode::Local, &local_tenant(), &local_resources())
                .await
                .expect("database observation");
            let DatabaseObservation::Unavailable { reason, .. } = &observation else {
                panic!("expected unavailable database observation");
            };
            assert_eq!(*reason, expected_reason);
            let serialized = serde_json::to_string(&observation).expect("observation serializes");
            assert!(!serialized.contains("private-sentinel"));
            assert_eq!(
                calls.lock().expect("calls").as_slice(),
                [(
                    Method::GET,
                    "/apis/postgresql.cnpg.io/v1/namespaces/database/clusters/capi-postgres".into()
                )]
            );
        }
    }

    #[tokio::test]
    async fn malformed_fields_are_unavailable_and_large_fields_are_bounded() {
        let mut bounded_cluster = cnpg_cluster();
        bounded_cluster["spec"]["instances"] = json!(999_999);
        bounded_cluster["status"]["phase"] = json!("x".repeat(MAX_TEXT * 4));
        bounded_cluster["status"]["instanceNames"] = Value::Array(
            (0..(MAX_DATABASE_INSTANCES * 2))
                .map(|index| Value::String(format!("capi-postgres-{index}")))
                .collect(),
        );
        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, _) = fixture_client(vec![(200, bounded_cluster)]);
        let source = source_with_tenant_client(management, tenant_client);
        let observation = source
            .database_observation(ProviderMode::Local, &local_tenant(), &local_resources())
            .await
            .expect("bounded observation");
        let DatabaseObservation::Available { cluster, .. } = observation else {
            panic!("expected bounded available observation");
        };
        assert_eq!(cluster.desired_instances, MAX_DATABASE_COUNT);
        assert_eq!(cluster.instances.len(), MAX_DATABASE_INSTANCES);
        assert!(cluster.phase.expect("phase").chars().count() <= MAX_TEXT);

        let mut malformed_cluster = cnpg_cluster();
        malformed_cluster["status"]["instances"] = json!("not-a-number");
        let (management, _) = fixture_client(Vec::new());
        let (tenant_client, _) = fixture_client(vec![(200, malformed_cluster)]);
        let source = source_with_tenant_client(management, tenant_client);
        let observation = source
            .database_observation(ProviderMode::Local, &local_tenant(), &local_resources())
            .await
            .expect("malformed observation");
        assert!(matches!(
            observation,
            DatabaseObservation::Unavailable {
                reason: DatabaseUnavailableReason::Malformed,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn azure_is_not_applicable_without_management_or_tenant_api_access() {
        let (management, management_calls) = fixture_client(Vec::new());
        let (tenant_client, tenant_calls) = fixture_client(Vec::new());
        let source = source_with_tenant_client(management, tenant_client);

        let observation = source
            .database_observation(ProviderMode::Azure, &azure_tenant(), &[])
            .await
            .expect("Azure observation");

        assert!(matches!(
            observation,
            DatabaseObservation::NotApplicable {
                reason: DatabaseNotApplicableReason::ProviderUnsupported,
                ..
            }
        ));
        assert!(
            management_calls
                .lock()
                .expect("management calls")
                .is_empty()
        );
        assert!(tenant_calls.lock().expect("tenant calls").is_empty());
    }

    #[tokio::test]
    async fn validated_secret_failures_do_not_expose_secret_data_or_versions() {
        let secret = json!({
            "apiVersion":"v1",
            "kind":"Secret",
            "metadata":{
                "name":"tenant-a-kubeconfig",
                "namespace":"tenant-a",
                "uid":"secret-uid",
                "resourceVersion":"private-resource-version",
                "ownerReferences":[{
                    "apiVersion":"controlplane.cluster.x-k8s.io/v1alpha2",
                    "kind":"KamajiControlPlane",
                    "name":"tenant-a",
                    "uid":"control-plane-uid"
                }]
            },
            "type":"cluster.x-k8s.io/secret",
            "data":{"value":"cHJpdmF0ZS1zZW50aW5lbA=="}
        });
        let (management, calls) = fixture_client(vec![(200, secret)]);
        let source = KubeDataSource::new(management);

        let observation = source
            .database_observation(ProviderMode::Local, &local_tenant(), &local_resources())
            .await
            .expect("database observation");

        assert!(matches!(
            observation,
            DatabaseObservation::Unavailable {
                reason: DatabaseUnavailableReason::TenantAccessInvalid,
                ..
            }
        ));
        let serialized = format!(
            "{observation:?} {}",
            serde_json::to_string(&observation).expect("observation serializes")
        );
        for forbidden in [
            "private-sentinel",
            "cHJpdmF0ZS1zZW50aW5lbA==",
            "private-resource-version",
            "resourceVersion",
            "\"data\"",
        ] {
            assert!(!serialized.contains(forbidden), "{forbidden} leaked");
        }
        assert_eq!(
            calls.lock().expect("calls").as_slice(),
            [(
                Method::GET,
                "/api/v1/namespaces/tenant-a/secrets/tenant-a-kubeconfig".into()
            )]
        );
    }

    #[tokio::test]
    async fn creation_capability_requires_converged_matching_controller_deployment() {
        let deployment = json!({
            "apiVersion":"apps/v1","kind":"Deployment",
            "metadata":{"name":"tenant-controller","namespace":"tenant-system","generation":3},
            "spec":{"replicas":1,"template":{"spec":{"containers":[{
                "name":"manager","image":"controller",
                "args":["--provider=local","--supported-kubernetes-version=v1.36.4"]
            }]}}},
            "status":{"observedGeneration":3,"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}
        });
        let (client, calls) = fixture_client(vec![(200, deployment)]);
        let capability = KubeDataSource::new(client)
            .creation_capability(ProviderMode::Local)
            .await
            .unwrap();
        assert!(capability.available);
        assert_eq!(
            capability.supported_kubernetes_version.as_deref(),
            Some("1.36.4")
        );
        assert_eq!(
            calls.lock().unwrap().as_slice(),
            [(
                Method::GET,
                "/apis/apps/v1/namespaces/tenant-system/deployments/tenant-controller".into()
            )]
        );

        let rolling = json!({
            "apiVersion":"apps/v1","kind":"Deployment",
            "metadata":{"name":"tenant-controller","namespace":"tenant-system","generation":4},
            "spec":{"replicas":1,"template":{"spec":{"containers":[]}}},
            "status":{"observedGeneration":3,"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}
        });
        let (client, _) = fixture_client(vec![(200, rolling)]);
        assert!(
            !KubeDataSource::new(client)
                .creation_capability(ProviderMode::Local)
                .await
                .unwrap()
                .available
        );

        let mixed = json!({
            "apiVersion":"apps/v1","kind":"Deployment",
            "metadata":{"name":"tenant-controller","namespace":"tenant-system","generation":5},
            "spec":{"replicas":1,"template":{"spec":{"containers":[{
                "name":"manager","image":"controller",
                "args":[
                    "--provider=azure","--provider","local",
                    "--supported-kubernetes-version=1.35.0",
                    "--supported-kubernetes-version","v1.36.4"
                ]
            }]}}},
            "status":{"observedGeneration":5,"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}
        });
        let (client, _) = fixture_client(vec![(200, mixed.clone())]);
        let capability = KubeDataSource::new(client)
            .creation_capability(ProviderMode::Local)
            .await
            .unwrap();
        assert!(capability.available);
        assert_eq!(
            capability.supported_kubernetes_version.as_deref(),
            Some("1.36.4")
        );
        let (client, _) = fixture_client(vec![(200, mixed)]);
        assert!(
            !KubeDataSource::new(client)
                .creation_capability(ProviderMode::Azure)
                .await
                .unwrap()
                .available
        );

        let missing_generation = json!({
            "apiVersion":"apps/v1","kind":"Deployment",
            "metadata":{"name":"tenant-controller","namespace":"tenant-system"},
            "spec":{"replicas":1,"template":{"spec":{"containers":[{
                "name":"manager","args":["--provider=local","--supported-kubernetes-version=1.36.4"]
            }]}}},
            "status":{"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}
        });
        let (client, _) = fixture_client(vec![(200, missing_generation)]);
        assert!(
            !KubeDataSource::new(client)
                .creation_capability(ProviderMode::Local)
                .await
                .unwrap()
                .available
        );
        let (client, _) = fixture_client(vec![(
            404,
            json!({"apiVersion":"v1","kind":"Status","reason":"NotFound","code":404}),
        )]);
        assert!(
            !KubeDataSource::new(client)
                .creation_capability(ProviderMode::Local)
                .await
                .unwrap()
                .available
        );
    }

    #[tokio::test]
    async fn tenant_create_and_delete_classify_conflict_and_exact_identity() {
        let mut tenant = Tenant::new("tenant-a", TenantSpec::local("1.36.4", 1));
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.resource_version = Some("7".into());
        tenant.metadata.generation = Some(2);
        let status = json!({
            "apiVersion":"v1","kind":"Status","status":"Failure",
            "reason":"AlreadyExists","code":409
        });
        let (client, _) = fixture_client(vec![(409, status)]);
        assert_eq!(
            KubeDataSource::new(client)
                .create_tenant(tenant.clone())
                .await,
            Err(SourceError::Conflict)
        );
        for (code, expected) in [(403, SourceError::Forbidden), (422, SourceError::Rejected)] {
            let status = json!({
                "apiVersion":"v1","kind":"Status","status":"Failure","code":code
            });
            let (client, _) = fixture_client(vec![(code, status)]);
            assert_eq!(
                KubeDataSource::new(client)
                    .create_tenant(tenant.clone())
                    .await,
                Err(expected)
            );
        }

        let serialized = serde_json::to_value(&tenant).unwrap();
        let (client, calls) = fixture_client(vec![(200, serialized.clone())]);
        assert_eq!(
            KubeDataSource::new(client)
                .delete_tenant("tenant-a", "replacement-uid")
                .await,
            Err(SourceError::StaleIdentity)
        );
        assert_eq!(calls.lock().unwrap().len(), 1);

        let deleted = json!({
            "apiVersion":"v1","kind":"Status","status":"Success","code":200
        });
        let (client, calls) = fixture_client(vec![(200, serialized), (200, deleted)]);
        let response = KubeDataSource::new(client)
            .delete_tenant("tenant-a", "tenant-uid")
            .await
            .unwrap();
        assert_eq!(response.state, TenantDeleteState::Accepted);
        assert_eq!(
            calls.lock().unwrap().as_slice(),
            [
                (
                    Method::GET,
                    "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a".into()
                ),
                (
                    Method::DELETE,
                    "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a".into()
                )
            ]
        );

        let (client, calls) = fixture_client(vec![(
            404,
            json!({"apiVersion":"v1","kind":"Status","reason":"NotFound","code":404}),
        )]);
        let response = KubeDataSource::new(client)
            .delete_tenant("tenant-a", "tenant-uid")
            .await
            .unwrap();
        assert_eq!(response.state, TenantDeleteState::Completed);
        assert_eq!(calls.lock().unwrap().len(), 1);

        let mut refreshed = tenant.clone();
        refreshed.metadata.resource_version = Some("8".into());
        let conflict = json!({
            "apiVersion":"v1","kind":"Status","status":"Failure",
            "reason":"Conflict","code":409
        });
        let deleted = json!({
            "apiVersion":"v1","kind":"Status","status":"Success","code":200
        });
        let (client, calls) = fixture_client(vec![
            (200, serde_json::to_value(&tenant).unwrap()),
            (409, conflict),
            (200, serde_json::to_value(refreshed).unwrap()),
            (200, deleted),
        ]);
        assert_eq!(
            KubeDataSource::new(client)
                .delete_tenant("tenant-a", "tenant-uid")
                .await
                .unwrap()
                .state,
            TenantDeleteState::Accepted
        );
        assert_eq!(calls.lock().unwrap().len(), 4);

        let mut deleting = tenant;
        deleting.metadata.deletion_timestamp =
            Some(serde_json::from_value(json!("2026-09-30T00:00:00Z")).unwrap());
        let (client, calls) = fixture_client(vec![(200, serde_json::to_value(deleting).unwrap())]);
        let response = KubeDataSource::new(client)
            .delete_tenant("tenant-a", "tenant-uid")
            .await
            .unwrap();
        assert_eq!(response.state, TenantDeleteState::Accepted);
        assert_eq!(calls.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn tenant_delete_sends_uid_and_resource_version_preconditions() {
        let mut tenant = Tenant::new("tenant-a", TenantSpec::local("1.36.4", 1));
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.resource_version = Some("17".into());
        tenant.metadata.generation = Some(2);
        let tenant = serde_json::to_vec(&tenant).unwrap();
        let captured = Arc::new(Mutex::new(None));
        let captured_body = captured.clone();
        let client = Client::new(
            service_fn(move |request: Request<Body>| {
                let tenant = tenant.clone();
                let captured = captured_body.clone();
                async move {
                    let (status, body) = if request.method() == Method::DELETE {
                        let bytes = request.into_body().collect().await.unwrap().to_bytes();
                        *captured.lock().unwrap() =
                            Some(serde_json::from_slice::<Value>(&bytes).unwrap());
                        (
                            200,
                            serde_json::to_vec(&json!({
                                "apiVersion":"v1","kind":"Status","status":"Success","code":200
                            }))
                            .unwrap(),
                        )
                    } else {
                        (200, tenant)
                    };
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "application/json")
                            .body(Body::from(body))
                            .unwrap(),
                    )
                }
            }),
            "default",
        );
        KubeDataSource::new(client)
            .delete_tenant("tenant-a", "tenant-uid")
            .await
            .unwrap();
        let body = captured.lock().unwrap().clone().unwrap();
        assert_eq!(body["preconditions"]["uid"], "tenant-uid");
        assert_eq!(body["preconditions"]["resourceVersion"], "17");
    }
}
