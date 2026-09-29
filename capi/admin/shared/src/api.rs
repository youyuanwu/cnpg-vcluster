use serde::{Deserialize, Serialize};

use crate::API_SCHEMA_VERSION;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiEnvelope<T> {
    pub schema_version: u16,
    pub data: T,
}

impl<T> ApiEnvelope<T> {
    pub const fn new(data: T) -> Self {
        Self {
            schema_version: API_SCHEMA_VERSION,
            data,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiErrorEnvelope {
    pub schema_version: u16,
    pub error: ApiError,
}

impl ApiErrorEnvelope {
    pub const fn new(error: ApiError) -> Self {
        Self {
            schema_version: API_SCHEMA_VERSION,
            error,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ApiError {
    pub code: ApiErrorCode,
    pub message: String,
    pub retryable: bool,
}

impl ApiError {
    pub fn new(code: ApiErrorCode, message: impl Into<String>, retryable: bool) -> Self {
        Self {
            code,
            message: message.into(),
            retryable,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ApiErrorCode {
    NotFound,
    InvalidRequest,
    SchemaMismatch,
    KubernetesUnavailable,
    Internal,
}
