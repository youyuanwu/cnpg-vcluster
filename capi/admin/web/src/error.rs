use tenant_admin_shared::{API_SCHEMA_VERSION, ApiError, ApiErrorCode, ApiErrorEnvelope};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UiErrorKind {
    NotFound,
    InvalidRequest,
    SchemaMismatch,
    KubernetesUnavailable,
    Network,
    Internal,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UiError {
    pub kind: UiErrorKind,
    pub message: String,
    pub retryable: bool,
}

impl UiError {
    pub fn network(message: impl Into<String>) -> Self {
        Self {
            kind: UiErrorKind::Network,
            message: message.into(),
            retryable: true,
        }
    }

    pub fn schema_mismatch(actual: u16) -> Self {
        Self {
            kind: UiErrorKind::SchemaMismatch,
            message: format!(
                "The server returned API schema version {actual}; this UI requires version {API_SCHEMA_VERSION}."
            ),
            retryable: false,
        }
    }

    pub fn title(&self) -> &'static str {
        match self.kind {
            UiErrorKind::NotFound => "Tenant not found",
            UiErrorKind::InvalidRequest => "Invalid request",
            UiErrorKind::SchemaMismatch => "UI and server versions do not match",
            UiErrorKind::KubernetesUnavailable => "Kubernetes is unavailable",
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
            ApiErrorCode::Internal => UiErrorKind::Internal,
        };
        Self {
            kind,
            message: error.message,
            retryable: error.retryable,
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
    }
}

#[cfg(test)]
mod tests {
    use tenant_admin_shared::{ApiError, ApiErrorCode, ApiErrorEnvelope};

    use super::{UiErrorKind, map_error_response};

    #[test]
    fn maps_typed_kubernetes_and_not_found_errors() {
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
