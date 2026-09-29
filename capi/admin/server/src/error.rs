use std::fmt;

use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use tenant_admin_shared::{ApiError, ApiErrorCode, ApiErrorEnvelope};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SourceError {
    KubernetesUnavailable,
    ResponseTooLarge,
}

impl fmt::Display for SourceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::KubernetesUnavailable => "Kubernetes API request failed",
            Self::ResponseTooLarge => "Kubernetes API response exceeded the service limit",
        })
    }
}

impl std::error::Error for SourceError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AppError {
    status: StatusCode,
    error: ApiError,
}

impl AppError {
    pub fn invalid_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            error: ApiError::new(ApiErrorCode::InvalidRequest, message, false),
        }
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            error: ApiError::new(ApiErrorCode::NotFound, message, false),
        }
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            error: ApiError::new(ApiErrorCode::Internal, message, false),
        }
    }
}

impl From<SourceError> for AppError {
    fn from(error: SourceError) -> Self {
        match error {
            SourceError::KubernetesUnavailable => Self {
                status: StatusCode::SERVICE_UNAVAILABLE,
                error: ApiError::new(ApiErrorCode::KubernetesUnavailable, error.to_string(), true),
            },
            SourceError::ResponseTooLarge => Self::internal(error.to_string()),
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        (self.status, Json(ApiErrorEnvelope::new(self.error))).into_response()
    }
}
