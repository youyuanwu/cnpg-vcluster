use tenant_admin_shared::{
    API_SCHEMA_VERSION, ApiError, ApiErrorCode, ApiErrorEnvelope, lifecycle::TenantFieldError,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UiErrorKind {
    NotFound,
    InvalidRequest,
    SchemaMismatch,
    KubernetesUnavailable,
    CreationUnavailable,
    Conflict,
    StaleIdentity,
    DatabaseUnavailable,
    QueryFailed,
    QueryResponseTooLarge,
    QueryTimedOut,
    QueryOutcomeUnknown,
    Network,
    Internal,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UiError {
    pub kind: UiErrorKind,
    pub message: String,
    pub retryable: bool,
    pub field_errors: Vec<TenantFieldError>,
}

impl UiError {
    pub fn network(message: impl Into<String>) -> Self {
        Self {
            kind: UiErrorKind::Network,
            message: message.into(),
            retryable: true,
            field_errors: Vec::new(),
        }
    }

    pub fn schema_mismatch(actual: u16) -> Self {
        Self {
            kind: UiErrorKind::SchemaMismatch,
            message: format!(
                "The server returned API schema version {actual}; this UI requires version {API_SCHEMA_VERSION}."
            ),
            retryable: false,
            field_errors: Vec::new(),
        }
    }

    pub fn title(&self) -> &'static str {
        match self.kind {
            UiErrorKind::NotFound => "Tenant not found",
            UiErrorKind::InvalidRequest => "Invalid request",
            UiErrorKind::SchemaMismatch => "UI and server versions do not match",
            UiErrorKind::KubernetesUnavailable => "Kubernetes is unavailable",
            UiErrorKind::CreationUnavailable => "Tenant creation is unavailable",
            UiErrorKind::Conflict => "Tenant request conflicts with current state",
            UiErrorKind::StaleIdentity => "Tenant identity changed",
            UiErrorKind::DatabaseUnavailable => "The database is unavailable",
            UiErrorKind::QueryFailed => "The SQL query failed",
            UiErrorKind::QueryResponseTooLarge => "The SQL response was too large",
            UiErrorKind::QueryTimedOut => "The SQL query timed out",
            UiErrorKind::QueryOutcomeUnknown => "The SQL query outcome is unknown",
            UiErrorKind::Network => "The server could not be reached",
            UiErrorKind::Internal => "The request could not be completed",
        }
    }
}

impl From<ApiError> for UiError {
    fn from(error: ApiError) -> Self {
        let kind = match error.code {
            ApiErrorCode::NotFound => UiErrorKind::NotFound,
            ApiErrorCode::InvalidRequest => UiErrorKind::InvalidRequest,
            ApiErrorCode::SchemaMismatch => UiErrorKind::SchemaMismatch,
            ApiErrorCode::KubernetesUnavailable => UiErrorKind::KubernetesUnavailable,
            ApiErrorCode::CreationUnavailable => UiErrorKind::CreationUnavailable,
            ApiErrorCode::Conflict => UiErrorKind::Conflict,
            ApiErrorCode::StaleIdentity => UiErrorKind::StaleIdentity,
            ApiErrorCode::DatabaseUnavailable => UiErrorKind::DatabaseUnavailable,
            ApiErrorCode::QueryFailed => UiErrorKind::QueryFailed,
            ApiErrorCode::QueryResponseTooLarge => UiErrorKind::QueryResponseTooLarge,
            ApiErrorCode::QueryTimedOut => UiErrorKind::QueryTimedOut,
            ApiErrorCode::QueryOutcomeUnknown => UiErrorKind::QueryOutcomeUnknown,
            ApiErrorCode::Internal => UiErrorKind::Internal,
        };
        Self {
            kind,
            message: error.message,
            retryable: error.retryable,
            field_errors: error.field_errors,
        }
    }
}

pub fn map_error_response(status: u16, body: &str) -> UiError {
    if let Ok(envelope) = serde_json::from_str::<ApiErrorEnvelope>(body) {
        if envelope.schema_version != API_SCHEMA_VERSION {
            return UiError::schema_mismatch(envelope.schema_version);
        }
        return envelope.error.into();
    }

    let (kind, message, retryable) = match status {
        404 => (
            UiErrorKind::NotFound,
            "The requested Tenant does not exist.",
            false,
        ),
        400..=499 => (
            UiErrorKind::InvalidRequest,
            "The server rejected the request.",
            false,
        ),
        502..=504 => (
            UiErrorKind::KubernetesUnavailable,
            "The management Kubernetes API is temporarily unavailable.",
            true,
        ),
        _ => (
            UiErrorKind::Internal,
            "The server returned an unexpected response.",
            status >= 500,
        ),
    };
    UiError {
        kind,
        message: message.to_owned(),
        retryable,
        field_errors: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use tenant_admin_shared::{ApiError, ApiErrorCode, ApiErrorEnvelope};

    use super::{UiErrorKind, map_error_response};

    #[test]
    fn maps_typed_errors_and_preserves_sanitized_messages() {
        let unavailable = serde_json::to_string(&ApiErrorEnvelope::new(ApiError::new(
            ApiErrorCode::KubernetesUnavailable,
            "API unavailable",
            true,
        )))
        .expect("test envelope serializes");
        let error = map_error_response(503, &unavailable);
        assert_eq!(error.kind, UiErrorKind::KubernetesUnavailable);
        assert!(error.retryable);

        let error = map_error_response(404, "not json");
        assert_eq!(error.kind, UiErrorKind::NotFound);
        assert!(!error.retryable);

        let query_failed = serde_json::to_string(&ApiErrorEnvelope::new(ApiError::new(
            ApiErrorCode::QueryFailed,
            "relation does not exist",
            false,
        )))
        .expect("test envelope serializes");
        let error = map_error_response(422, &query_failed);
        assert_eq!(error.kind, UiErrorKind::QueryFailed);
        assert_eq!(error.title(), "The SQL query failed");
        assert_eq!(error.message, "relation does not exist");

        for (code, expected_kind, expected_title) in [
            (
                ApiErrorCode::DatabaseUnavailable,
                UiErrorKind::DatabaseUnavailable,
                "The database is unavailable",
            ),
            (
                ApiErrorCode::QueryResponseTooLarge,
                UiErrorKind::QueryResponseTooLarge,
                "The SQL response was too large",
            ),
            (
                ApiErrorCode::QueryTimedOut,
                UiErrorKind::QueryTimedOut,
                "The SQL query timed out",
            ),
            (
                ApiErrorCode::QueryOutcomeUnknown,
                UiErrorKind::QueryOutcomeUnknown,
                "The SQL query outcome is unknown",
            ),
        ] {
            let body = serde_json::to_string(&ApiErrorEnvelope::new(ApiError::new(
                code,
                "sanitized database detail",
                true,
            )))
            .expect("test envelope serializes");
            let error = map_error_response(503, &body);
            assert_eq!(error.kind, expected_kind);
            assert_eq!(error.title(), expected_title);
            assert_eq!(error.message, "sanitized database detail");
        }
    }

    #[test]
    fn rejects_mismatched_error_schema() {
        let body =
            r#"{"schemaVersion":99,"error":{"code":"internal","message":"x","retryable":false}}"#;
        assert_eq!(
            map_error_response(500, body).kind,
            UiErrorKind::SchemaMismatch
        );
    }
}
