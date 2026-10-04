use tenant_admin_shared::routes::{
    API_DATABASE_PATH, API_DATABASE_QUERY_PATH, API_DATABASES_PATH, API_TENANT_CREATE_PATH,
    API_TENANT_DELETE_PATH,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AppRoute {
    Overview,
    Tenant(String),
    NotFound,
}

pub fn parse_route(path: &str) -> AppRoute {
    let path = path.split(['?', '#']).next().unwrap_or(path);
    let trimmed = path.trim_end_matches('/');
    if trimmed.is_empty() {
        return AppRoute::Overview;
    }

    let Some(name) = trimmed.strip_prefix("/tenants/") else {
        return AppRoute::NotFound;
    };
    if name.is_empty() || name.contains('/') || !valid_tenant_name(name) {
        return AppRoute::NotFound;
    }
    AppRoute::Tenant(name.to_owned())
}

pub fn tenant_href(name: &str) -> Option<String> {
    valid_tenant_name(name).then(|| format!("/tenants/{name}"))
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
        AppRoute, database_path, database_query_path, databases_path, parse_route,
        tenant_create_path, tenant_delete_path, tenant_href,
    };

    #[test]
    fn parses_supported_routes() {
        assert_eq!(parse_route("/"), AppRoute::Overview);
        assert_eq!(parse_route(""), AppRoute::Overview);
        assert_eq!(
            parse_route("/tenants/team-a/"),
            AppRoute::Tenant("team-a".to_owned())
        );
        assert_eq!(
            parse_route("/tenants/team-a?ignored=true"),
            AppRoute::Tenant("team-a".to_owned())
        );
    }

    #[test]
    fn rejects_unknown_or_unsafe_routes() {
        for path in [
            "/other",
            "/tenants",
            "/tenants/",
            "/tenants/team/a",
            "/tenants/Team-A",
            "/tenants/-team",
            "/tenants/team.example",
            "/tenants/this-name-is-longer-than-thirty-characters",
            "/tenants/team%2Fa",
        ] {
            assert_eq!(parse_route(path), AppRoute::NotFound, "{path}");
        }
        assert_eq!(tenant_href("Team-A"), None);
        assert_eq!(tenant_href("team-a"), Some("/tenants/team-a".to_owned()));
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
        assert_eq!(database_path("Team-A", uid), None);
        assert_eq!(tenant_create_path(), "/api/v1/tenants");
        assert_eq!(
            tenant_delete_path("team-a"),
            Some("/api/v1/tenants/team-a".into())
        );
    }
}
