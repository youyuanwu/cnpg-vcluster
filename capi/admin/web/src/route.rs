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

fn valid_tenant_name(name: &str) -> bool {
    name.len() <= 253
        && name.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'.')
        })
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
    use super::{AppRoute, parse_route, tenant_href};

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
            "/tenants/team%2Fa",
        ] {
            assert_eq!(parse_route(path), AppRoute::NotFound, "{path}");
        }
        assert_eq!(tenant_href("Team-A"), None);
        assert_eq!(tenant_href("team-a"), Some("/tenants/team-a".to_owned()));
    }
}
