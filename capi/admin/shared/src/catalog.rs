use serde::{Deserialize, Serialize};

use crate::query::{DatabaseQueryResult, TopologyGraph};

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DatabaseAddRequest {
    pub catalog_uid: String,
    pub name: String,
    pub instances: u32,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DatabaseDeleteRequest {
    pub catalog_uid: String,
    pub logical_uid: String,
    pub confirmation: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CatalogQueryRequest {
    pub catalog_uid: String,
    pub logical_uid: String,
    pub instance: String,
    pub instance_uid: String,
    pub database: String,
    pub sql: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CatalogQueryResponse {
    pub catalog_uid: String,
    pub logical_uid: String,
    pub instance: String,
    pub instance_uid: String,
    pub executed_at: String,
    pub duration_ms: u64,
    pub truncated: bool,
    pub results: Vec<DatabaseQueryResult>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CatalogView {
    pub tenant: String,
    pub tenant_uid: String,
    pub catalog_uid: String,
    pub resource_version: String,
    pub closed: bool,
    pub capability_available: bool,
    pub databases: Vec<DatabaseView>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DatabaseView {
    pub logical_uid: String,
    pub name: String,
    pub instances: u32,
    pub deleting: bool,
    pub phase: String,
    pub observed_generation: Option<i64>,
    pub provider: Option<String>,
    pub namespace: Option<String>,
    pub namespace_uid: Option<String>,
    pub cluster: Option<String>,
    pub cluster_uid: Option<String>,
    pub credential_uid: Option<String>,
    pub query_identity: Option<QueryIdentityView>,
    pub ready_instances: u32,
    pub storage_requested_bytes: u64,
    pub storage_healthy: u32,
    pub storage: Vec<StorageView>,
    pub conditions: Vec<DatabaseConditionView>,
    pub finalization: Option<FinalizationView>,
    pub instance_topology: Vec<InstanceView>,
    pub blockers: Vec<DatabaseBlocker>,
    pub topology: TopologyGraph,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct QueryIdentityView {
    pub cluster_uid: String,
    pub credential_uid: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StorageView {
    pub ordinal: u32,
    pub requested_bytes: u64,
    pub healthy: bool,
    pub pv_uid: Option<String>,
    pub pvc_uid: Option<String>,
    pub disk_uid: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DatabaseConditionView {
    pub condition_type: String,
    pub status: String,
    pub reason: String,
    pub message: String,
    pub observed_generation: Option<i64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FinalizationView {
    pub terminal_verified: bool,
    pub verified_absent_count: u32,
    pub pending_count: u32,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstanceView {
    pub name: String,
    pub uid: String,
    pub role: String,
    pub ready: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DatabaseBlocker {
    pub code: String,
    pub message: String,
}
