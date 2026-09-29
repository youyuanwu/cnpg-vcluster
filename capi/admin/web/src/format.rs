use std::time::{SystemTime, UNIX_EPOCH};

use tenant_admin_shared::query::{
    ConditionStatus, ProviderMode, TenantClassification, TenantProvider, TopologyEdgeKind,
    TopologyHealth, TopologyNodeKind,
};

pub const fn provider_mode_label(provider: ProviderMode) -> &'static str {
    match provider {
        ProviderMode::Local => "Local",
        ProviderMode::Azure => "Azure",
    }
}

pub const fn provider_label(provider: TenantProvider) -> &'static str {
    match provider {
        TenantProvider::Local => "Local",
        TenantProvider::Azure => "Azure",
        TenantProvider::Unknown => "Unknown",
    }
}

pub const fn classification_label(classification: TenantClassification) -> &'static str {
    match classification {
        TenantClassification::Ready => "Ready",
        TenantClassification::Progressing => "Progressing",
        TenantClassification::Degraded => "Degraded",
        TenantClassification::Failed => "Failed",
        TenantClassification::Deleting => "Deleting",
        TenantClassification::OwnershipInvalid => "Ownership invalid",
    }
}

pub const fn classification_class(classification: TenantClassification) -> &'static str {
    match classification {
        TenantClassification::Ready => "ready",
        TenantClassification::Progressing => "progressing",
        TenantClassification::Degraded => "degraded",
        TenantClassification::Failed => "failed",
        TenantClassification::Deleting => "deleting",
        TenantClassification::OwnershipInvalid => "ownership-invalid",
    }
}

pub const fn condition_status_label(status: ConditionStatus) -> &'static str {
    match status {
        ConditionStatus::True => "True",
        ConditionStatus::False => "False",
        ConditionStatus::Unknown => "Unknown",
    }
}

pub const fn health_label(health: TopologyHealth) -> &'static str {
    match health {
        TopologyHealth::Ready => "Ready",
        TopologyHealth::Progressing => "Progressing",
        TopologyHealth::Degraded => "Degraded",
        TopologyHealth::Failed => "Failed",
        TopologyHealth::Deleting => "Deleting",
        TopologyHealth::Unknown => "Unknown",
    }
}

pub const fn health_class(health: TopologyHealth) -> &'static str {
    match health {
        TopologyHealth::Ready => "ready",
        TopologyHealth::Progressing => "progressing",
        TopologyHealth::Degraded => "degraded",
        TopologyHealth::Failed => "failed",
        TopologyHealth::Deleting => "deleting",
        TopologyHealth::Unknown => "unknown",
    }
}

pub const fn node_kind_label(kind: TopologyNodeKind) -> &'static str {
    match kind {
        TopologyNodeKind::Tenant => "Tenant",
        TopologyNodeKind::ControlPlane => "Control plane",
        TopologyNodeKind::WorkerPool => "Worker pool",
        TopologyNodeKind::Machine => "Machine",
        TopologyNodeKind::Node => "Node",
        TopologyNodeKind::ProviderResource => "Provider resource",
        TopologyNodeKind::AddOn => "Add-on",
        TopologyNodeKind::Database => "Database",
    }
}

pub const fn edge_kind_label(kind: TopologyEdgeKind) -> &'static str {
    match kind {
        TopologyEdgeKind::Owns => "owns",
        TopologyEdgeKind::Contains => "contains",
        TopologyEdgeKind::Manages => "manages",
        TopologyEdgeKind::Provides => "provides",
        TopologyEdgeKind::Represents => "represents",
        TopologyEdgeKind::DependsOn => "depends on",
    }
}

pub fn optional_text(value: Option<&str>) -> &str {
    value.filter(|text| !text.trim().is_empty()).unwrap_or("—")
}

pub fn format_age(created_at: Option<&str>) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs() as i64);
    created_at.map_or_else(|| "Unknown".to_owned(), |value| format_age_at(value, now))
}

pub fn format_age_at(created_at: &str, now: i64) -> String {
    let Some(created) = parse_rfc3339_seconds(created_at) else {
        return "Unknown".to_owned();
    };
    let elapsed = now.saturating_sub(created).max(0);
    match elapsed {
        0..=59 => "just now".to_owned(),
        60..=3_599 => format!("{}m", elapsed / 60),
        3_600..=86_399 => format!("{}h", elapsed / 3_600),
        86_400..=2_592_000 => format!("{}d", elapsed / 86_400),
        2_592_001..=31_536_000 => format!("{}mo", elapsed / 2_592_000),
        _ => format!("{}y", elapsed / 31_536_000),
    }
}

fn parse_rfc3339_seconds(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() < 20
        || bytes.get(4) != Some(&b'-')
        || bytes.get(7) != Some(&b'-')
        || !matches!(bytes.get(10), Some(b'T' | b't' | b' '))
        || bytes.get(13) != Some(&b':')
        || bytes.get(16) != Some(&b':')
    {
        return None;
    }
    let year = parse_number(bytes.get(0..4)?)? as i64;
    let month = parse_number(bytes.get(5..7)?)? as i64;
    let day = parse_number(bytes.get(8..10)?)? as i64;
    let hour = parse_number(bytes.get(11..13)?)? as i64;
    let minute = parse_number(bytes.get(14..16)?)? as i64;
    let second = parse_number(bytes.get(17..19)?)? as i64;
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }
    let suffix = value.get(19..)?;
    if !suffix.starts_with('Z') && !suffix.starts_with('z') && !suffix.starts_with('.') {
        return None;
    }

    let year_before_month = year - i64::from(month <= 2);
    let era = year_before_month.div_euclid(400);
    let year_of_era = year_before_month - era * 400;
    let shifted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    Some(days * 86_400 + hour * 3_600 + minute * 60 + second)
}

fn parse_number(bytes: &[u8]) -> Option<u32> {
    bytes.iter().try_fold(0_u32, |value, byte| {
        byte.is_ascii_digit()
            .then(|| value * 10 + u32::from(byte - b'0'))
    })
}

#[cfg(test)]
mod tests {
    use tenant_admin_shared::query::{TenantClassification, TopologyHealth};

    use super::{classification_class, classification_label, format_age_at, health_class};

    #[test]
    fn formats_all_status_variants_for_display_and_css() {
        assert_eq!(
            classification_label(TenantClassification::OwnershipInvalid),
            "Ownership invalid"
        );
        assert_eq!(
            classification_class(TenantClassification::Progressing),
            "progressing"
        );
        assert_eq!(health_class(TopologyHealth::Unknown), "unknown");
    }

    #[test]
    fn formats_relative_age_without_panicking_on_invalid_input() {
        let now = 1_704_110_400;
        assert_eq!(format_age_at("2024-01-01T11:59:30Z", now), "just now");
        assert_eq!(format_age_at("2024-01-01T10:00:00Z", now), "2h");
        assert_eq!(format_age_at("not-a-date", now), "Unknown");
    }
}
