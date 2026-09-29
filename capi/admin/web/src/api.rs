use gloo_net::http::Request;
use serde_json::Value;
use tenant_admin_shared::{API_SCHEMA_VERSION, ApiEnvelope};

use crate::error::{UiError, map_error_response};

pub async fn get_envelope<T>(path: &str) -> Result<T, UiError>
where
    T: serde::de::DeserializeOwned,
{
    let response = Request::get(path)
        .send()
        .await
        .map_err(|error| UiError::network(error.to_string()))?;
    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|error| UiError::network(error.to_string()))?;
    if !(200..300).contains(&status) {
        return Err(map_error_response(status, &body));
    }

    let envelope = serde_json::from_str::<ApiEnvelope<T>>(&body).map_err(|_| {
        let actual = serde_json::from_str::<Value>(&body)
            .ok()
            .and_then(|value| value.get("schemaVersion")?.as_u64())
            .and_then(|version| u16::try_from(version).ok())
            .unwrap_or(0);
        UiError::schema_mismatch(actual)
    })?;
    if envelope.schema_version != API_SCHEMA_VERSION {
        return Err(UiError::schema_mismatch(envelope.schema_version));
    }
    Ok(envelope.data)
}
