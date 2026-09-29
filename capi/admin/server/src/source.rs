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
use kube::{
    Api, Client,
    api::{ListParams, ObjectList},
    core::{ApiResource, DynamicObject, GroupVersionKind},
};
use serde::Deserialize;
use tenant_admin_shared::query::{
    ConditionStatus, DatabaseClusterIdentity, DatabaseClusterObservation, DatabaseCondition,
    DatabaseInstanceObservation, DatabaseInstanceRole, DatabaseNotApplicableReason,
    DatabaseObservation, DatabaseObservationFreshness, DatabasePvcHealth, DatabaseServices,
    DatabaseUnavailableReason, ProviderMode,
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

use crate::{SourceError, projection::is_accepted_local_management_resource};

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
    fn check_ready(&self) -> SourceFuture<'_, ()>;
}

#[derive(Clone)]
pub struct KubeDataSource {
    client: Client,
    tenant_clients: Arc<dyn TenantClientLoader>,
}

impl KubeDataSource {
    pub fn new(client: Client) -> Self {
        Self {
            client,
            tenant_clients: Arc::new(ValidatedTenantClientLoader),
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
            || matches!(tenant.spec.provider, TenantProviderSpec::Azure { .. })
        {
            return not_applicable(
                observed_at,
                DatabaseNotApplicableReason::ProviderUnsupported,
            );
        }
        let tenant_name = tenant.metadata.name.as_deref().unwrap_or_default();
        if tenant_name.is_empty() {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::Malformed,
                "Tenant identity is malformed",
                false,
            );
        }
        let Some(local_status) = tenant.status.as_ref().and_then(|status| status.local()) else {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::Pending,
                "Tenant API endpoint is pending",
                true,
            );
        };
        let Some(allocation_host) = local_status
            .allocation
            .as_ref()
            .map(|allocation| allocation.endpoint.as_str())
            .filter(|endpoint| !endpoint.is_empty())
        else {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::Pending,
                "Tenant API endpoint is pending",
                true,
            );
        };
        let Some(cluster_uid) = local_status
            .cluster_uid
            .as_deref()
            .filter(|uid| !uid.is_empty())
        else {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::Pending,
                "Tenant management Cluster identity is pending",
                true,
            );
        };
        let Some(cluster) = expected_management_cluster(management_resources, tenant_name) else {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::ManagementResourceMissing,
                "Expected management Cluster is unavailable",
                true,
            );
        };
        if !is_accepted_local_management_resource(tenant, management_resources, cluster) {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::TenantAccessInvalid,
                "Tenant management-cluster ownership is invalid",
                false,
            );
        }
        let endpoint = match trusted_endpoint_authority(cluster, cluster_uid, allocation_host) {
            Ok(endpoint) => endpoint,
            Err(()) => {
                tracing::warn!(
                    tenant = %tenant_controller::sanitize::text(tenant_name),
                    "trusted Tenant API endpoint metadata is invalid"
                );
                return unavailable(
                    observed_at,
                    DatabaseUnavailableReason::Malformed,
                    "Trusted Tenant API endpoint metadata is invalid",
                    false,
                );
            }
        };
        let Some(control_plane) = expected_control_plane(management_resources, tenant_name) else {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::ManagementResourceMissing,
                "Expected Tenant control plane is unavailable",
                true,
            );
        };
        if !is_accepted_local_management_resource(tenant, management_resources, control_plane) {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::TenantAccessInvalid,
                "Tenant control-plane ownership is invalid",
                false,
            );
        }
        if validate_provider_owner(control_plane, tenant_name, true, management_resources).is_err()
        {
            return unavailable(
                observed_at,
                DatabaseUnavailableReason::TenantAccessInvalid,
                "Tenant control-plane ownership is invalid",
                false,
            );
        }
        let tenant_client = match self
            .tenant_clients
            .load(
                self.client.clone(),
                control_plane,
                tenant_name,
                tenant_name,
                &endpoint,
            )
            .await
        {
            Ok(client) => client,
            Err(error) => {
                let (reason, message, retryable) = match error.class() {
                    TenantApiErrorClass::Pending => (
                        DatabaseUnavailableReason::Pending,
                        "Tenant administrative access is pending",
                        true,
                    ),
                    TenantApiErrorClass::Conflict | TenantApiErrorClass::Retryable => (
                        DatabaseUnavailableReason::TenantApiUnavailable,
                        "Tenant API is unavailable",
                        true,
                    ),
                    TenantApiErrorClass::Terminal => (
                        DatabaseUnavailableReason::TenantAccessInvalid,
                        "Tenant administrative access is invalid",
                        false,
                    ),
                };
                tracing::warn!(
                    tenant = %tenant_controller::sanitize::text(tenant_name),
                    reason = ?reason,
                    "live database access failed"
                );
                return unavailable(observed_at, reason, message, retryable);
            }
        };
        let cluster = match read_database_cluster(tenant_client).await {
            Ok(Some(cluster)) => cluster,
            Ok(None) => {
                return unavailable(
                    observed_at,
                    DatabaseUnavailableReason::ClusterMissing,
                    "Managed database Cluster was not found",
                    true,
                );
            }
            Err(error) => {
                tracing::warn!(
                    tenant = %tenant_controller::sanitize::text(tenant_name),
                    status = database_error_status(&error),
                    "live database read failed"
                );
                return unavailable(
                    observed_at,
                    DatabaseUnavailableReason::TenantApiUnavailable,
                    "Tenant API database read failed",
                    true,
                );
            }
        };
        match project_database_cluster(cluster) {
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

fn database_error_status(error: &kube::Error) -> u16 {
    match error {
        kube::Error::Api(response) => response.code,
        _ => 0,
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
                .filter(|definition| definition.kind != "Secret")
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
        sync::{Arc, Mutex},
    };

    use axum::http::{Method, Request, Response};
    use kube::client::Body;
    use serde_json::{Value, json};
    use tenant_controller::{
        api::{SUPPORTED_KUBERNETES_VERSION, canonical_spec, spec_hash},
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
        };
        (source, endpoints)
    }

    fn local_tenant() -> Tenant {
        serde_json::from_value(json!({
            "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha2",
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
            "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha2",
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
}
