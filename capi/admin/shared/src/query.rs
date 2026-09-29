use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ProviderMode {
    Local,
    Azure,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TenantProvider {
    Local,
    Azure,
    Unknown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TenantClassification {
    Ready,
    Progressing,
    Degraded,
    Failed,
    Deleting,
    OwnershipInvalid,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum ConditionStatus {
    True,
    False,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantCondition {
    #[serde(rename = "type")]
    pub condition_type: String,
    pub status: ConditionStatus,
    pub reason: Option<String>,
    pub message: Option<String>,
    pub observed_generation: Option<i64>,
    pub last_transition_time: Option<String>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantCounts {
    pub total: u32,
    pub ready: u32,
    pub progressing: u32,
    pub degraded: u32,
    pub failed: u32,
    pub deleting: u32,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ManagementOverview {
    pub provider_mode: ProviderMode,
    pub tenants: TenantCounts,
    pub components: Vec<ManagementComponentView>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OverviewSnapshot {
    pub overview: ManagementOverview,
    pub tenants: Vec<TenantSummary>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ManagementComponentView {
    pub name: String,
    pub ready: bool,
    pub identity: Option<ResourceIdentityView>,
    pub message: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantSummary {
    pub name: String,
    pub provider: TenantProvider,
    pub classification: TenantClassification,
    pub kubernetes_version: String,
    pub requested_workers: u32,
    pub requested_databases: Option<u32>,
    pub endpoint: Option<String>,
    pub created_at: Option<String>,
    pub conditions: Vec<TenantCondition>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantDetail {
    pub summary: TenantSummary,
    pub uid: String,
    pub generation: i64,
    pub observed_generation: Option<i64>,
    pub specification: TenantSpecificationView,
    pub provider_status: ProviderStatusView,
    pub blockers: Vec<TenantBlocker>,
    pub management_resources: Vec<ManagementResourceView>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantSnapshotIdentity {
    pub uid: String,
    pub generation: i64,
    pub observed_generation: Option<i64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantSnapshot {
    pub identity: TenantSnapshotIdentity,
    pub detail: TenantDetail,
    pub topology: TopologyGraph,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantSpecificationView {
    pub kubernetes_version: String,
    pub workers: u32,
    pub provider: ProviderSpecificationView,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "provider", rename_all = "lowercase")]
pub enum ProviderSpecificationView {
    Local {
        databases: u32,
    },
    Azure {
        pod_cidr: String,
        service_cidr: String,
    },
    Unknown {
        provider_type: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "provider", content = "status", rename_all = "lowercase")]
pub enum ProviderStatusView {
    Local(LocalProviderView),
    Azure(AzureProviderView),
    Unknown(UnknownProviderView),
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalProviderView {
    pub allocation: Option<LocalAllocationView>,
    pub foundation_hash: Option<String>,
    pub cluster_uid: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalAllocationView {
    pub slot_id: u32,
    pub endpoint: String,
    pub pod_cidr: String,
    pub service_cidr: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AzureProviderView {
    pub binding: Option<AzureBindingView>,
    pub endpoint: Option<String>,
    pub management: Option<AzureManagementView>,
    pub worker_pool: Option<AzureWorkerPoolView>,
    pub nodes: Vec<AzureNodeView>,
    pub add_ons: Vec<ResourceIdentityView>,
    pub resources: Vec<AzureResourceView>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AzureBindingView {
    pub cluster_name: String,
    pub resource_group: String,
    pub binding_hash: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AzureManagementView {
    pub cluster_uid: Option<String>,
    pub infrastructure_uid: Option<String>,
    pub control_plane_uid: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AzureWorkerPoolView {
    pub name: String,
    pub uid: Option<String>,
    pub scale_set_name: Option<String>,
    pub desired_replicas: u32,
    pub ready_replicas: u32,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AzureNodeView {
    pub name: String,
    pub uid: String,
    pub provider_id: Option<String>,
    pub internal_ip: Option<String>,
    pub ready: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AzureResourceView {
    pub identity: ResourceIdentityView,
    pub resource_id: Option<String>,
    pub owner_uids: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UnknownProviderView {
    pub provider_type: String,
    pub summary: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantBlocker {
    pub code: String,
    pub message: String,
    pub condition_type: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ManagementResourceView {
    pub identity: ResourceIdentityView,
    pub role: String,
    pub health: TopologyHealth,
    pub message: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResourceIdentityView {
    pub api_version: String,
    pub kind: String,
    pub namespace: Option<String>,
    pub name: String,
    pub uid: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TopologyGraph {
    pub tenant_name: String,
    pub provider: TenantProvider,
    pub nodes: Vec<TopologyNode>,
    pub edges: Vec<TopologyEdge>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TopologyNode {
    pub id: String,
    pub kind: TopologyNodeKind,
    pub label: String,
    pub health: TopologyHealth,
    pub resource: Option<ResourceIdentityView>,
    pub attributes: Vec<DisplayAttribute>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TopologyNodeKind {
    Tenant,
    ControlPlane,
    WorkerPool,
    Machine,
    Node,
    ProviderResource,
    AddOn,
    Database,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TopologyHealth {
    Ready,
    Progressing,
    Degraded,
    Failed,
    Deleting,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DisplayAttribute {
    pub label: String,
    pub value: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TopologyEdge {
    pub id: String,
    pub source: String,
    pub target: String,
    pub kind: TopologyEdgeKind,
    pub label: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TopologyEdgeKind {
    Owns,
    Contains,
    Manages,
    Provides,
    Represents,
    DependsOn,
}
