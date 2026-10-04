use tenant_admin_shared::routes::{
    API_DATABASE_PATH, API_DATABASE_QUERY_PATH, API_DATABASES_PATH, API_TENANT_CREATE_PATH,
    API_TENANT_DELETE_PATH,
};

const MAX_SELECTION_LENGTH: usize = 320;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TenantSection {
    Overview,
    Resources,
    Databases,
    Status,
    Settings,
}

impl TenantSection {
    pub const ALL: [Self; 5] = [
        Self::Overview,
        Self::Resources,
        Self::Databases,
        Self::Status,
        Self::Settings,
    ];

    pub const fn segment(self) -> &'static str {
        match self {
            Self::Overview => "overview",
            Self::Resources => "resources",
            Self::Databases => "databases",
            Self::Status => "status",
            Self::Settings => "settings",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AppRoute {
    Overview,
    Tenant {
        name: String,
        section: TenantSection,
        selected_resource: Option<String>,
        invalid_selection: bool,
    },
    TenantSectionNotFound {
        name: String,
    },
    NotFound,
}

pub fn parse_route(path: &str) -> AppRoute {
    let (path, search) = path.split_once('?').unwrap_or((path, ""));
    parse_location(path, search)
}

pub fn parse_location(path: &str, search: &str) -> AppRoute {
    let path = path.split('#').next().unwrap_or(path);
    let trimmed = path.trim_end_matches('/');
    if trimmed.is_empty() {
        return AppRoute::Overview;
    }
    let Some(rest) = trimmed.strip_prefix("/tenants/") else {
        return AppRoute::NotFound;
    };
    let mut segments = rest.split('/');
    let Some(name) = segments.next() else {
        return AppRoute::NotFound;
    };
    if !valid_tenant_name(name) {
        return AppRoute::NotFound;
    }
    let section = match segments.next() {
        None | Some("") | Some("overview") => TenantSection::Overview,
        Some("resources") => TenantSection::Resources,
        Some("databases") => TenantSection::Databases,
        Some("status") => TenantSection::Status,
        Some("settings") => TenantSection::Settings,
        Some(_) => {
            return AppRoute::TenantSectionNotFound {
                name: name.to_owned(),
            };
        }
    };
    if segments.next().is_some() {
        return AppRoute::TenantSectionNotFound {
            name: name.to_owned(),
        };
    }
    let (selected_resource, invalid_selection) = if section == TenantSection::Resources {
        selection_from_search(search)
    } else {
        (None, false)
    };
    AppRoute::Tenant {
        name: name.to_owned(),
        section,
        selected_resource,
        invalid_selection,
    }
}

pub fn tenant_href(name: &str) -> Option<String> {
    valid_tenant_name(name).then(|| format!("/tenants/{name}"))
}

pub fn tenant_section_href(name: &str, section: TenantSection) -> Option<String> {
    valid_tenant_name(name).then(|| format!("/tenants/{name}/{}", section.segment()))
}

pub fn tenant_resource_href(name: &str, node_id: &str) -> Option<String> {
    if !valid_tenant_name(name)
        || node_id.is_empty()
        || node_id.chars().count() > MAX_SELECTION_LENGTH
        || node_id.chars().any(char::is_control)
    {
        return None;
    }
    Some(format!(
        "/tenants/{name}/resources?select={}",
        percent_encode(node_id)
    ))
}

fn selection_from_search(search: &str) -> (Option<String>, bool) {
    let query = search.trim_start_matches('?');
    if query.is_empty() {
        return (None, false);
    }
    let mut selected = None;
    for item in query.split('&') {
        let Some((key, value)) = item.split_once('=') else {
            if item == "select" {
                return (None, true);
            }
            continue;
        };
        if key != "select" {
            continue;
        }
        if selected.is_some() {
            return (None, true);
        }
        selected = Some(value);
    }
    let Some(value) = selected else {
        return (None, false);
    };
    match percent_decode(value) {
        Some(value)
            if !value.is_empty()
                && value.chars().count() <= MAX_SELECTION_LENGTH
                && !value.chars().any(char::is_control) =>
        {
            (Some(value), false)
        }
        _ => (None, true),
    }
}

fn percent_encode(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            encoded.push(hex(byte >> 4));
            encoded.push(hex(byte & 0x0f));
        }
    }
    encoded
}

fn percent_decode(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' => {
                let high = *bytes.get(index + 1)?;
                let low = *bytes.get(index + 2)?;
                decoded.push((unhex(high)? << 4) | unhex(low)?);
                index += 3;
            }
            byte if byte.is_ascii() => {
                decoded.push(byte);
                index += 1;
            }
            _ => return None,
        }
    }
    String::from_utf8(decoded).ok()
}

const fn hex(value: u8) -> char {
    match value {
        0..=9 => (b'0' + value) as char,
        _ => (b'A' + value - 10) as char,
    }
}

const fn unhex(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

pub fn databases_path(name: &str) -> Option<String> {
    valid_tenant_name(name).then(|| API_DATABASES_PATH.replace("{name}", name))
}

pub fn database_path(name: &str, uid: &str) -> Option<String> {
    (valid_tenant_name(name) && valid_database_uid(uid)).then(|| {
        API_DATABASE_PATH
            .replace("{name}", name)
            .replace("{uid}", uid)
    })
}

pub fn database_query_path(name: &str, uid: &str) -> Option<String> {
    (valid_tenant_name(name) && valid_database_uid(uid)).then(|| {
        API_DATABASE_QUERY_PATH
            .replace("{name}", name)
            .replace("{uid}", uid)
    })
}

fn valid_database_uid(uid: &str) -> bool {
    uid.len() == 36
        && uid.bytes().enumerate().all(|(index, byte)| {
            if [8, 13, 18, 23].contains(&index) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
}

pub fn tenant_create_path() -> &'static str {
    API_TENANT_CREATE_PATH
}

pub fn tenant_delete_path(name: &str) -> Option<String> {
    valid_tenant_name(name).then(|| API_TENANT_DELETE_PATH.replace("{name}", name))
}

pub fn valid_tenant_name(name: &str) -> bool {
    (1..=30).contains(&name.len())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        && name
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        && name
            .as_bytes()
            .last()
            .is_some_and(u8::is_ascii_alphanumeric)
}

#[cfg(test)]
mod tests {
    use super::{
        AppRoute, TenantSection, database_path, database_query_path, databases_path,
        parse_location, parse_route, tenant_create_path, tenant_delete_path, tenant_href,
        tenant_resource_href, tenant_section_href,
    };

    #[test]
    fn parses_supported_routes_and_selection() {
        assert_eq!(parse_route("/"), AppRoute::Overview);
        assert_eq!(
            parse_route("/tenants/team-a/"),
            AppRoute::Tenant {
                name: "team-a".into(),
                section: TenantSection::Overview,
                selected_resource: None,
                invalid_selection: false,
            }
        );
        assert_eq!(
            parse_location("/tenants/team-a/resources", "?select=resource%3A1234"),
            AppRoute::Tenant {
                name: "team-a".into(),
                section: TenantSection::Resources,
                selected_resource: Some("resource:1234".into()),
                invalid_selection: false,
            }
        );
        assert_eq!(
            parse_location("/tenants/team-a/resources", "?select=%GG"),
            AppRoute::Tenant {
                name: "team-a".into(),
                section: TenantSection::Resources,
                selected_resource: None,
                invalid_selection: true,
            }
        );
        assert_eq!(
            parse_location("/tenants/team-a/resources", "?select"),
            AppRoute::Tenant {
                name: "team-a".into(),
                section: TenantSection::Resources,
                selected_resource: None,
                invalid_selection: true,
            }
        );
    }

    #[test]
    fn rejects_unknown_or_unsafe_routes() {
        for path in [
            "/other",
            "/tenants",
            "/tenants/",
            "/tenants/Team-A",
            "/tenants/-team",
            "/tenants/team.example",
            "/tenants/this-name-is-longer-than-thirty-characters",
            "/tenants/team%2Fa",
        ] {
            assert!(matches!(parse_route(path), AppRoute::NotFound), "{path}");
        }
        assert_eq!(
            parse_route("/tenants/team/a"),
            AppRoute::TenantSectionNotFound {
                name: "team".into()
            }
        );
        assert_eq!(
            parse_route("/tenants/team-a/unknown"),
            AppRoute::TenantSectionNotFound {
                name: "team-a".into()
            }
        );
        assert_eq!(tenant_href("Team-A"), None);
        assert_eq!(tenant_href("team-a"), Some("/tenants/team-a".to_owned()));
        assert_eq!(
            tenant_section_href("team-a", TenantSection::Status),
            Some("/tenants/team-a/status".into())
        );
        assert_eq!(
            tenant_resource_href("team-a", "resource:uid"),
            Some("/tenants/team-a/resources?select=resource%3Auid".into())
        );
        let uid = "12345678-1234-1234-1234-123456789abc";
        assert_eq!(databases_path("Team-A"), None);
        assert_eq!(
            databases_path("team-a"),
            Some("/api/v1/tenants/team-a/databases".to_owned())
        );
        assert_eq!(
            database_path("team-a", uid),
            Some(format!("/api/v1/tenants/team-a/databases/{uid}"))
        );
        assert_eq!(
            database_query_path("team-a", uid),
            Some(format!("/api/v1/tenants/team-a/databases/{uid}/query"))
        );
        assert_eq!(database_query_path("team-a", "../other"), None);
        assert_eq!(tenant_create_path(), "/api/v1/tenants");
        assert_eq!(
            tenant_delete_path("team-a"),
            Some("/api/v1/tenants/team-a".into())
        );
    }
}
