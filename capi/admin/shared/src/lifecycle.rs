use serde::{Deserialize, Serialize};

use crate::query::TenantProvider;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreationCapability {
    pub available: bool,
    pub supported_kubernetes_version: Option<String>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TenantCreateRequest {
    pub name: String,
    pub workers: u32,
    pub databases: Option<u32>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TenantDeleteRequest {
    pub uid: String,
    pub confirmation: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantMutationIdentity {
    pub name: String,
    pub uid: String,
    pub generation: Option<i64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantCreateResponse {
    pub identity: TenantMutationIdentity,
    pub provider: TenantProvider,
    pub kubernetes_version: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TenantDeleteState {
    Accepted,
    Completed,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantDeleteResponse {
    pub identity: TenantMutationIdentity,
    pub state: TenantDeleteState,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TenantField {
    Name,
    Workers,
    Databases,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TenantFieldError {
    pub field: TenantField,
    pub code: String,
    pub message: String,
}

impl CreationCapability {
    pub fn unavailable(reason: impl Into<String>) -> Self {
        Self {
            available: false,
            supported_kubernetes_version: None,
            reason: Some(reason.into()),
        }
    }

    pub fn available(version: impl Into<String>) -> Self {
        Self {
            available: true,
            supported_kubernetes_version: Some(version.into()),
            reason: None,
        }
    }
}
