use std::fmt;

use serde::Serialize;
use serde_json::Value;

pub const REDACTED: &str = "REDACTED";
const MAX_DEPTH: usize = 8;

#[must_use]
pub fn value(value: &Value) -> Value {
    sanitize_value(value, 0)
}

#[must_use]
pub fn serializable<T: Serialize>(value: &T) -> Value {
    serde_json::to_value(value)
        .map(|value| sanitize_value(&value, 0))
        .unwrap_or_else(|_| Value::String(REDACTED.into()))
}

#[must_use]
pub fn text(value: &str) -> String {
    sanitize_text(value, 0)
}

pub struct TracingSafe(Value);

impl TracingSafe {
    #[must_use]
    pub fn new<T: Serialize>(value: &T) -> Self {
        Self(serializable(value))
    }

    #[must_use]
    pub fn into_inner(self) -> Value {
        self.0
    }
}

impl fmt::Debug for TracingSafe {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(self, formatter)
    }
}

impl fmt::Display for TracingSafe {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.0 {
            Value::String(value) => formatter.write_str(value),
            value => write!(formatter, "{value}"),
        }
    }
}

fn sanitize_value(value: &Value, depth: usize) -> Value {
    if depth > MAX_DEPTH {
        return Value::String(REDACTED.into());
    }
    match value {
        Value::Object(values) => Value::Object(
            values
                .iter()
                .map(|(key, value)| {
                    let sanitized = if sensitive_key(key) {
                        Value::String(REDACTED.into())
                    } else {
                        sanitize_value(value, depth + 1)
                    };
                    (key.clone(), sanitized)
                })
                .collect(),
        ),
        Value::Array(values) => Value::Array(
            values
                .iter()
                .map(|value| sanitize_value(value, depth + 1))
                .collect(),
        ),
        Value::String(value) => Value::String(sanitize_text(value, depth + 1)),
        value => value.clone(),
    }
}

fn sanitize_text(value: &str, depth: usize) -> String {
    if depth > MAX_DEPTH {
        return REDACTED.into();
    }
    if let Ok(decoded) = serde_json::from_str::<Value>(value) {
        return serde_json::to_string(&sanitize_value(&decoded, depth + 1))
            .unwrap_or_else(|_| REDACTED.into());
    }

    let mut sanitized = sanitize_embedded_json(value, depth);
    sanitized = redact_private_keys(&sanitized);
    sanitized = sanitized
        .lines()
        .map(redact_sensitive_line)
        .collect::<Vec<_>>()
        .join("\n");
    for key in [
        "authorization",
        "password",
        "token",
        "client_secret",
        "client-secret",
        "subscription_id",
        "subscription-id",
    ] {
        sanitized = redact_assignments(&sanitized, key);
    }
    sanitized
}

fn sanitize_embedded_json(value: &str, depth: usize) -> String {
    let boundaries: Vec<usize> = value
        .char_indices()
        .map(|(index, _)| index)
        .chain(std::iter::once(value.len()))
        .collect();
    for (start, character) in value.char_indices() {
        if !matches!(character, '{' | '[') {
            continue;
        }
        for &end in boundaries.iter().rev().take_while(|&&end| end > start) {
            let Ok(decoded) = serde_json::from_str::<Value>(&value[start..end]) else {
                continue;
            };
            let encoded = serde_json::to_string(&sanitize_value(&decoded, depth + 1))
                .unwrap_or_else(|_| REDACTED.into());
            return format!(
                "{}{}{}",
                &value[..start],
                encoded,
                sanitize_text(&value[end..], depth + 1)
            );
        }
    }
    value.to_owned()
}

fn sensitive_key(key: &str) -> bool {
    let normalized: String = key
        .chars()
        .filter(|character| character.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect();
    [
        "authorization",
        "password",
        "token",
        "secret",
        "privatekey",
        "clientkey",
        "clientcertificate",
        "certificateauthority",
        "kubeconfig",
        "bootstrapdata",
        "subscriptionid",
        "stringdata",
    ]
    .iter()
    .any(|sensitive| normalized.contains(sensitive))
        || normalized == "data"
}

fn redact_sensitive_line(line: &str) -> String {
    let Some((key, _)) = line.split_once(':') else {
        return line.to_owned();
    };
    let key = key.trim_start_matches([' ', '\t', '-']).trim();
    if sensitive_key(key) {
        let prefix_len = line.find(':').expect("split line contains colon") + 1;
        format!("{} {REDACTED}", &line[..prefix_len])
    } else {
        line.to_owned()
    }
}

fn redact_private_keys(value: &str) -> String {
    let upper = value.to_ascii_uppercase();
    let mut output = String::with_capacity(value.len());
    let mut offset = 0;
    while let Some(relative_start) = upper[offset..].find("-----BEGIN ") {
        let start = offset + relative_start;
        let Some(header_end_relative) = upper[start..].find("PRIVATE KEY-----") else {
            break;
        };
        let header_end = start + header_end_relative + "PRIVATE KEY-----".len();
        let Some(kind) =
            upper[start + "-----BEGIN ".len()..header_end].strip_suffix("PRIVATE KEY-----")
        else {
            break;
        };
        let footer = format!("-----END {kind}PRIVATE KEY-----");
        let Some(end_relative) = upper[header_end..].find(&footer) else {
            break;
        };
        let end = header_end + end_relative + footer.len();
        output.push_str(&value[offset..start]);
        output.push_str(REDACTED);
        offset = end;
    }
    output.push_str(&value[offset..]);
    output
}

fn redact_assignments(value: &str, key: &str) -> String {
    let lower = value.to_ascii_lowercase();
    let mut output = String::with_capacity(value.len());
    let mut offset = 0;
    while let Some(relative_start) = lower[offset..].find(key) {
        let start = offset + relative_start;
        let before_is_word = start
            .checked_sub(1)
            .and_then(|index| lower.as_bytes().get(index))
            .is_some_and(u8::is_ascii_alphanumeric);
        let mut separator_index = start + key.len();
        while lower
            .as_bytes()
            .get(separator_index)
            .is_some_and(u8::is_ascii_whitespace)
        {
            separator_index += 1;
        }
        let Some(separator) = lower.as_bytes().get(separator_index) else {
            break;
        };
        if before_is_word || !matches!(separator, b'=' | b':') {
            let next = start + key.len();
            output.push_str(&value[offset..next]);
            offset = next;
            continue;
        }
        let mut value_start = separator_index + 1;
        while value
            .as_bytes()
            .get(value_start)
            .is_some_and(u8::is_ascii_whitespace)
        {
            value_start += 1;
        }
        let value_end = value[value_start..]
            .find(|character: char| character.is_whitespace() || matches!(character, ',' | ';'))
            .map_or(value.len(), |relative| value_start + relative);
        output.push_str(&value[offset..value_start]);
        output.push_str(REDACTED);
        offset = value_end;
    }
    output.push_str(&value[offset..]);
    output
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn recursively_redacts_secret_keys_and_nested_text() {
        let sanitized = value(&json!({
            "name": "tenant-a",
            "token": "abc",
            "nested": {
                "message": "{\"password\":\"value\",\"safe\":true}",
                "items": [{"client-key-data": "base64"}]
            }
        }));
        assert_eq!(sanitized["name"], "tenant-a");
        assert_eq!(sanitized["token"], REDACTED);
        assert_eq!(
            sanitized["nested"]["message"],
            r#"{"password":"REDACTED","safe":true}"#
        );
        assert_eq!(sanitized["nested"]["items"][0]["client-key-data"], REDACTED);
        let encoded = sanitized.to_string();
        assert!(!encoded.contains("abc"));
        assert!(!encoded.contains("value"));
        assert!(!encoded.contains("base64"));
    }

    #[test]
    fn redacts_pem_yaml_headers_and_assignments() {
        let message = concat!(
            "authorization: Bearer abc\n",
            "client-key-data: xyz\n",
            "password = hunter2 token:token-value\n",
            "request failed: {\"token\":\"embedded\",\"safe\":true}\n",
            "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
        );
        let sanitized = text(message);
        for secret in [
            "Bearer abc",
            "xyz",
            "hunter2",
            "token-value",
            "embedded",
            "secret",
        ] {
            assert!(!sanitized.contains(secret), "{secret} leaked: {sanitized}");
        }
        assert!(sanitized.matches(REDACTED).count() >= 6);
    }

    #[test]
    fn tracing_safe_debug_and_display_are_sanitized() {
        let safe = TracingSafe::new(&json!({"safe":"shown","secret":"hidden"}));
        let display = safe.to_string();
        assert!(display.contains("shown"));
        assert!(display.contains(REDACTED));
        assert!(!display.contains("hidden"));
        assert_eq!(format!("{safe:?}"), display);
    }

    #[test]
    fn depth_limit_fails_closed() {
        let mut nested = json!("secret");
        for _ in 0..12 {
            nested = json!([nested]);
        }
        assert!(value(&nested).to_string().contains(REDACTED));
        assert!(!value(&nested).to_string().contains("secret"));
    }
}
