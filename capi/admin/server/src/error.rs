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
    DatabaseUnavailable {
        message: String,
        retryable: bool,
    },
    QueryFailed {
        sqlstate: Option<String>,
        message: String,
    },
    QueryResponseTooLarge,
    QueryTimedOut,
    QueryOutcomeUnknown,
}

impl fmt::Display for SourceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::KubernetesUnavailable => "Kubernetes API request failed",
            Self::ResponseTooLarge => "Kubernetes API response exceeded the service limit",
            Self::DatabaseUnavailable { message, .. } => message,
            Self::QueryFailed { .. } => "PostgreSQL query failed",
            Self::QueryResponseTooLarge => "PostgreSQL response exceeded the service limit",
            Self::QueryTimedOut => "PostgreSQL query timed out",
            Self::QueryOutcomeUnknown => "PostgreSQL query outcome is unknown",
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

    pub fn database_unavailable(message: impl Into<String>, retryable: bool) -> Self {
        Self {
            status: StatusCode::SERVICE_UNAVAILABLE,
            error: ApiError::new(ApiErrorCode::DatabaseUnavailable, message, retryable),
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
            SourceError::DatabaseUnavailable { message, retryable } => {
                Self::database_unavailable(message, retryable)
            }
            SourceError::QueryFailed { sqlstate, message } => {
                let sqlstate = sqlstate
                    .as_deref()
                    .map(|code| format!(" (SQLSTATE {code})"))
                    .unwrap_or_default();
                Self {
                    status: StatusCode::UNPROCESSABLE_ENTITY,
                    error: ApiError::new(
                        ApiErrorCode::QueryFailed,
                        format!("PostgreSQL query failed{sqlstate}: {message}"),
                        false,
                    ),
                }
            }
            SourceError::QueryResponseTooLarge => Self {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                error: ApiError::new(
                    ApiErrorCode::QueryResponseTooLarge,
                    error.to_string(),
                    false,
                ),
            },
            SourceError::QueryTimedOut => Self {
                status: StatusCode::GATEWAY_TIMEOUT,
                error: ApiError::new(ApiErrorCode::QueryTimedOut, error.to_string(), true),
            },
            SourceError::QueryOutcomeUnknown => Self {
                status: StatusCode::GATEWAY_TIMEOUT,
                error: ApiError::new(ApiErrorCode::QueryOutcomeUnknown, error.to_string(), false),
            },
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        (self.status, Json(ApiErrorEnvelope::new(self.error))).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_source_errors_map_to_typed_http_statuses_without_query_text() {
        for (source, expected_status, expected_code) in [
            (
                SourceError::DatabaseUnavailable {
                    message: "Database connection is unavailable".into(),
                    retryable: true,
                },
                StatusCode::SERVICE_UNAVAILABLE,
                ApiErrorCode::DatabaseUnavailable,
            ),
            (
                SourceError::QueryFailed {
                    sqlstate: Some("42601".into()),
                    message: "syntax error".into(),
                },
                StatusCode::UNPROCESSABLE_ENTITY,
                ApiErrorCode::QueryFailed,
            ),
            (
                SourceError::QueryResponseTooLarge,
                StatusCode::UNPROCESSABLE_ENTITY,
                ApiErrorCode::QueryResponseTooLarge,
            ),
            (
                SourceError::QueryTimedOut,
                StatusCode::GATEWAY_TIMEOUT,
                ApiErrorCode::QueryTimedOut,
            ),
            (
                SourceError::QueryOutcomeUnknown,
                StatusCode::GATEWAY_TIMEOUT,
                ApiErrorCode::QueryOutcomeUnknown,
            ),
        ] {
            let error = AppError::from(source);
            assert_eq!(error.status, expected_status);
            assert_eq!(error.error.code, expected_code);
            assert!(!error.error.message.contains("private-sql"));
        }
    }

    #[test]
    fn query_failures_include_only_sqlstate_and_sanitized_database_message() {
        let error = AppError::from(SourceError::QueryFailed {
            sqlstate: Some("23505".into()),
            message: "duplicate key value".into(),
        });
        assert_eq!(
            error.error.message,
            "PostgreSQL query failed (SQLSTATE 23505): duplicate key value"
        );
        assert!(!error.error.retryable);

        let unknown = AppError::from(SourceError::QueryOutcomeUnknown);
        assert!(!unknown.error.retryable);
    }
}
