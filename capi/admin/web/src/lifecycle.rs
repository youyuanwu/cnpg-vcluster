use tenant_admin_shared::{
    lifecycle::{TenantCreateRequest, TenantField, TenantFieldError},
    query::{ProviderMode, TenantClassification},
};

use crate::error::UiErrorKind;

pub fn create_request(
    provider: ProviderMode,
    name: &str,
    workers: &str,
    databases: &str,
) -> Result<TenantCreateRequest, Vec<TenantFieldError>> {
    let mut errors = Vec::new();
    if !valid_tenant_name(name) {
        errors.push(field_error(
            TenantField::Name,
            "invalid-name",
            "Use a 1 to 30 character lowercase DNS label.",
        ));
    }
    let workers = parse_count(workers).unwrap_or_else(|| {
        errors.push(field_error(
            TenantField::Workers,
            "invalid-count",
            "Workers must be from 1 through 3.",
        ));
        0
    });
    let databases = match provider {
        ProviderMode::Local => parse_count(databases).or_else(|| {
            errors.push(field_error(
                TenantField::Databases,
                "invalid-count",
                "Databases must be from 1 through 3.",
            ));
            None
        }),
        ProviderMode::Azure => None,
    };
    errors.sort_by_key(|error| error.field);
    if errors.is_empty() {
        Ok(TenantCreateRequest {
            name: name.into(),
            workers,
            databases,
        })
    } else {
        Err(errors)
    }
}

pub fn delete_enabled(name: &str, confirmation: &str, running: bool) -> bool {
    !running && confirmation == name
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeleteRecovery {
    Reload,
    Overview,
    StaleIdentity,
    InspectCurrent,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CreateRecovery {
    InspectExisting,
    PreserveError,
}

pub fn create_recovery(observed_exists: bool) -> CreateRecovery {
    if observed_exists {
        CreateRecovery::InspectExisting
    } else {
        CreateRecovery::PreserveError
    }
}

pub fn requires_authoritative_read(kind: UiErrorKind) -> bool {
    matches!(
        kind,
        UiErrorKind::Network | UiErrorKind::KubernetesUnavailable
    )
}

pub fn delete_recovery(
    requested_uid: &str,
    observed: Option<(&str, TenantClassification)>,
) -> DeleteRecovery {
    match observed {
        None => DeleteRecovery::Overview,
        Some((uid, _)) if uid != requested_uid => DeleteRecovery::StaleIdentity,
        Some((_, TenantClassification::Deleting)) => DeleteRecovery::Reload,
        Some(_) => DeleteRecovery::InspectCurrent,
    }
}

fn parse_count(value: &str) -> Option<u32> {
    value.parse().ok().filter(|value| (1..=3).contains(value))
}

fn valid_tenant_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=30).contains(&bytes.len())
        && bytes
            .first()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        && bytes
            .last()
            .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
        && bytes
            .iter()
            .all(|value| value.is_ascii_lowercase() || value.is_ascii_digit() || *value == b'-')
}

fn field_error(field: TenantField, code: &str, message: &str) -> TenantFieldError {
    TenantFieldError {
        field,
        code: code.into(),
        message: message.into(),
    }
}

#[cfg(test)]
mod tests {
    use tenant_admin_shared::{
        lifecycle::TenantField,
        query::{ProviderMode, TenantClassification},
    };

    use crate::error::UiErrorKind;

    use super::{
        CreateRecovery, DeleteRecovery, create_recovery, create_request, delete_enabled,
        delete_recovery, requires_authoritative_read,
    };

    #[test]
    fn provider_specific_create_requests_validate_all_fields() {
        let local = create_request(ProviderMode::Local, "tenant-a", "2", "3").unwrap();
        assert_eq!(local.databases, Some(3));
        let azure = create_request(ProviderMode::Azure, "tenant-a", "2", "ignored").unwrap();
        assert_eq!(azure.databases, None);
        let errors = create_request(ProviderMode::Local, "Invalid", "0", "9").unwrap_err();
        assert_eq!(
            errors
                .into_iter()
                .map(|error| error.field)
                .collect::<Vec<_>>(),
            vec![
                TenantField::Name,
                TenantField::Workers,
                TenantField::Databases
            ]
        );
    }

    #[test]
    fn deletion_requires_exact_name_and_idle_state() {
        assert!(delete_enabled("tenant-a", "tenant-a", false));
        assert!(!delete_enabled("tenant-a", "tenant-b", false));
        assert!(!delete_enabled("tenant-a", "tenant-a", true));
    }

    #[test]
    fn ambiguous_delete_recovery_uses_authoritative_identity_and_state() {
        assert_eq!(delete_recovery("uid", None), DeleteRecovery::Overview);
        assert_eq!(
            delete_recovery("uid", Some(("replacement", TenantClassification::Ready))),
            DeleteRecovery::StaleIdentity
        );
        assert_eq!(
            delete_recovery("uid", Some(("uid", TenantClassification::Deleting))),
            DeleteRecovery::Reload
        );
        assert_eq!(
            delete_recovery("uid", Some(("uid", TenantClassification::Ready))),
            DeleteRecovery::InspectCurrent
        );
    }

    #[test]
    fn ambiguous_create_and_error_kinds_require_explicit_recovery() {
        assert_eq!(create_recovery(true), CreateRecovery::InspectExisting);
        assert_eq!(create_recovery(false), CreateRecovery::PreserveError);
        assert!(requires_authoritative_read(UiErrorKind::Network));
        assert!(requires_authoritative_read(
            UiErrorKind::KubernetesUnavailable
        ));
        assert!(!requires_authoritative_read(UiErrorKind::InvalidRequest));
    }
}
