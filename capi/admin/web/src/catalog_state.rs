use tenant_admin_shared::{
    catalog::{
        CatalogQueryRequest, CatalogQueryResponse, CatalogView, DatabaseAddRequest,
        DatabaseDeleteRequest, DatabaseView, InstanceView,
    },
    query::TenantClassification,
};

use crate::route::valid_tenant_name;

pub const MAX_DATABASES: usize = 3;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CatalogRecovery {
    ExistingIdentity,
    Absent,
    Deleting,
    Replaced,
    Unchanged,
}

pub fn visible_databases(catalog: &CatalogView) -> &[DatabaseView] {
    &catalog.databases[..catalog.databases.len().min(MAX_DATABASES)]
}

pub fn catalog_current(catalog: &CatalogView, tenant: &str, tenant_uid: &str) -> bool {
    catalog.tenant == tenant
        && !tenant_uid.is_empty()
        && catalog.tenant_uid == tenant_uid
        && !catalog.catalog_uid.is_empty()
}

pub fn add_disabled_reason(
    catalog: &CatalogView,
    tenant: &str,
    tenant_uid: &str,
    classification: TenantClassification,
) -> Option<&'static str> {
    if !catalog_current(catalog, tenant, tenant_uid) {
        Some("Catalog identity does not match this Tenant. Refresh before making changes.")
    } else if classification == TenantClassification::Deleting || catalog.closed {
        Some("Tenant or catalog is deleting; new database clusters cannot be added.")
    } else if !catalog.capability_available {
        Some("Database capability is disabled. Refresh after it recovers.")
    } else if classification != TenantClassification::Ready {
        Some("Tenant infrastructure is not Ready. Refresh after it recovers.")
    } else if catalog.databases.len() >= MAX_DATABASES {
        Some("All three database cluster slots are occupied, including deleting entries.")
    } else {
        None
    }
}

pub fn create_request(
    catalog: &CatalogView,
    name: &str,
    instances: &str,
) -> Result<DatabaseAddRequest, &'static str> {
    if !valid_tenant_name(name) {
        return Err("Use a 1 to 30 character lowercase DNS label for the cluster name.");
    }
    if catalog.databases.iter().any(|entry| entry.name == name) {
        return Err("This name is reserved by an existing or deleting database cluster.");
    }
    let instances = instances
        .parse::<u32>()
        .ok()
        .filter(|count| (1..=3).contains(count))
        .ok_or("Instances must be from 1 through 3.")?;
    Ok(DatabaseAddRequest {
        catalog_uid: catalog.catalog_uid.clone(),
        name: name.into(),
        instances,
    })
}

pub fn entry_actions_enabled(
    catalog: &CatalogView,
    tenant: &str,
    tenant_uid: &str,
    classification: TenantClassification,
    entry: &DatabaseView,
) -> bool {
    entry_deletable(catalog, tenant, tenant_uid, entry)
        && catalog.capability_available
        && classification == TenantClassification::Ready
}

pub fn entry_deletable(
    catalog: &CatalogView,
    tenant: &str,
    tenant_uid: &str,
    entry: &DatabaseView,
) -> bool {
    catalog_current(catalog, tenant, tenant_uid)
        && !catalog.closed
        && !entry.deleting
        && catalog
            .databases
            .iter()
            .any(|current| current.logical_uid == entry.logical_uid && current.name == entry.name)
}

pub fn delete_request(
    catalog: &CatalogView,
    entry: &DatabaseView,
    confirmation: &str,
) -> Option<DatabaseDeleteRequest> {
    if catalog.closed
        || entry.deleting
        || confirmation != entry.name
        || !catalog
            .databases
            .iter()
            .any(|item| item.logical_uid == entry.logical_uid && item.name == entry.name)
    {
        return None;
    }
    Some(DatabaseDeleteRequest {
        catalog_uid: catalog.catalog_uid.clone(),
        logical_uid: entry.logical_uid.clone(),
        confirmation: entry.name.clone(),
    })
}

pub fn query_instances(entry: &DatabaseView) -> Vec<InstanceView> {
    if entry.deleting || entry.phase != "ready" || entry.query_identity.is_none() {
        return Vec::new();
    }
    let mut instances = entry
        .instance_topology
        .iter()
        .filter(|instance| instance.ready && !instance.uid.is_empty() && !instance.name.is_empty())
        .take(3)
        .cloned()
        .collect::<Vec<_>>();
    instances.sort_by(|left, right| {
        (left.role != "primary", &left.name).cmp(&(right.role != "primary", &right.name))
    });
    instances
}

pub fn query_request(
    catalog: &CatalogView,
    entry: &DatabaseView,
    instance_uid: &str,
    database: &str,
    sql: &str,
) -> Option<CatalogQueryRequest> {
    let current = catalog
        .databases
        .iter()
        .find(|item| item.logical_uid == entry.logical_uid && item.name == entry.name)?;
    let instance = query_instances(current)
        .into_iter()
        .find(|instance| instance.uid == instance_uid)?;
    Some(CatalogQueryRequest {
        catalog_uid: catalog.catalog_uid.clone(),
        logical_uid: entry.logical_uid.clone(),
        instance: instance.name,
        instance_uid: instance.uid,
        database: database.into(),
        sql: sql.into(),
    })
}

pub fn query_response_matches(
    request: &CatalogQueryRequest,
    response: &CatalogQueryResponse,
) -> bool {
    response.catalog_uid == request.catalog_uid
        && response.logical_uid == request.logical_uid
        && response.instance_uid == request.instance_uid
        && response.instance == request.instance
}

pub fn add_recovery(prior: &CatalogView, current: &CatalogView, name: &str) -> CatalogRecovery {
    if current.catalog_uid != prior.catalog_uid || current.tenant_uid != prior.tenant_uid {
        CatalogRecovery::Replaced
    } else if current.databases.iter().any(|entry| entry.name == name) {
        CatalogRecovery::ExistingIdentity
    } else {
        CatalogRecovery::Absent
    }
}

pub fn delete_recovery(
    prior: &CatalogView,
    current: &CatalogView,
    entry: &DatabaseView,
) -> CatalogRecovery {
    if current.catalog_uid != prior.catalog_uid || current.tenant_uid != prior.tenant_uid {
        CatalogRecovery::Replaced
    } else {
        match current
            .databases
            .iter()
            .find(|item| item.name == entry.name)
        {
            Some(item) if item.logical_uid != entry.logical_uid => CatalogRecovery::Replaced,
            Some(item) if item.deleting => CatalogRecovery::Deleting,
            Some(_) => CatalogRecovery::Unchanged,
            None => CatalogRecovery::Absent,
        }
    }
}

#[cfg(test)]
mod tests {
    use tenant_admin_shared::{
        catalog::{CatalogView, DatabaseView, InstanceView, QueryIdentityView},
        query::{TenantClassification, TenantProvider, TopologyGraph},
    };

    use super::*;

    const UID: &str = "11111111-1111-1111-1111-111111111111";

    fn entry(name: &str, uid: &str) -> DatabaseView {
        DatabaseView {
            logical_uid: uid.into(),
            name: name.into(),
            instances: 3,
            deleting: false,
            phase: "ready".into(),
            observed_generation: Some(1),
            provider: Some("local".into()),
            namespace: None,
            namespace_uid: None,
            cluster: None,
            cluster_uid: None,
            credential_uid: None,
            query_identity: Some(QueryIdentityView {
                cluster_uid: "cluster".into(),
                credential_uid: "credential".into(),
            }),
            ready_instances: 1,
            storage_requested_bytes: 0,
            storage_healthy: 0,
            storage: vec![],
            conditions: vec![],
            finalization: None,
            instance_topology: vec![InstanceView {
                name: format!("{name}-1"),
                uid: format!("{uid}-instance"),
                role: "primary".into(),
                ready: true,
            }],
            blockers: vec![],
            topology: TopologyGraph {
                tenant_name: "tenant-a".into(),
                provider: TenantProvider::Local,
                nodes: vec![],
                edges: vec![],
            },
        }
    }

    fn catalog(provider: TenantProvider) -> CatalogView {
        let mut catalog = CatalogView {
            tenant: "tenant-a".into(),
            tenant_uid: "tenant-uid".into(),
            catalog_uid: UID.into(),
            resource_version: "4".into(),
            closed: false,
            capability_available: true,
            databases: vec![
                entry("alpha", "aaaa"),
                entry("beta", "bbbb"),
                entry("gamma", "cccc"),
            ],
        };
        for item in &mut catalog.databases {
            item.topology.provider = provider;
            item.provider = Some(
                match provider {
                    TenantProvider::Azure => "azure",
                    _ => "local",
                }
                .into(),
            );
        }
        catalog
    }

    #[test]
    fn bounded_three_cluster_projection_and_capacity_include_deleting() {
        for provider in [TenantProvider::Local, TenantProvider::Azure] {
            let mut state = catalog(provider);
            state.databases.push(entry("fourth", "dddd"));
            assert_eq!(visible_databases(&state).len(), 3);
            state.databases[0].deleting = true;
            assert!(
                add_disabled_reason(
                    &state,
                    "tenant-a",
                    "tenant-uid",
                    TenantClassification::Ready
                )
                .is_some()
            );
            state.databases.truncate(2);
            assert!(
                add_disabled_reason(
                    &state,
                    "tenant-a",
                    "tenant-uid",
                    TenantClassification::Ready
                )
                .is_none()
            );
            assert_eq!(create_request(&state, "new-db", "3").unwrap().instances, 3);
            for (name, count) in [
                ("INVALID", "1"),
                ("-invalid", "1"),
                ("db", "0"),
                ("db", "4"),
                ("db", "1.5"),
                ("beta", "1"),
            ] {
                assert!(create_request(&state, name, count).is_err());
            }
        }
    }

    #[test]
    fn stale_deleting_and_capability_states_disable_mutations() {
        let mut state = catalog(TenantProvider::Local);
        state.databases.truncate(1);
        let first = state.databases[0].clone();
        assert!(entry_actions_enabled(
            &state,
            "tenant-a",
            "tenant-uid",
            TenantClassification::Ready,
            &first
        ));
        for classification in [
            TenantClassification::Deleting,
            TenantClassification::Progressing,
            TenantClassification::OwnershipInvalid,
        ] {
            assert!(
                add_disabled_reason(&state, "tenant-a", "tenant-uid", classification).is_some()
            );
            assert!(!entry_actions_enabled(
                &state,
                "tenant-a",
                "tenant-uid",
                classification,
                &first
            ));
        }
        state.closed = true;
        assert!(!entry_actions_enabled(
            &state,
            "tenant-a",
            "tenant-uid",
            TenantClassification::Ready,
            &first
        ));
        state.closed = false;
        state.capability_available = false;
        assert!(
            add_disabled_reason(
                &state,
                "tenant-a",
                "tenant-uid",
                TenantClassification::Ready
            )
            .is_some()
        );
        assert!(!entry_actions_enabled(
            &state,
            "tenant-a",
            "tenant-uid",
            TenantClassification::Ready,
            &first
        ));
        state.capability_available = true;
        state.tenant_uid = "replacement".into();
        assert!(!entry_actions_enabled(
            &state,
            "tenant-a",
            "tenant-uid",
            TenantClassification::Ready,
            &first
        ));
    }

    #[test]
    fn degraded_capability_and_infrastructure_still_allow_exact_deletion() {
        for provider in [TenantProvider::Local, TenantProvider::Azure] {
            let mut state = catalog(provider);
            state.databases.truncate(1);
            let first = state.databases[0].clone();
            state.capability_available = false;
            for classification in [
                TenantClassification::Ready,
                TenantClassification::Degraded,
                TenantClassification::Progressing,
            ] {
                assert!(
                    add_disabled_reason(&state, "tenant-a", "tenant-uid", classification).is_some()
                );
                assert!(!entry_actions_enabled(
                    &state,
                    "tenant-a",
                    "tenant-uid",
                    classification,
                    &first
                ));
                assert!(entry_deletable(&state, "tenant-a", "tenant-uid", &first));
                assert_eq!(
                    delete_request(&state, &first, "alpha").unwrap().logical_uid,
                    first.logical_uid
                );
            }
            assert!(!entry_deletable(&state, "tenant-a", "replacement", &first));
            state.closed = true;
            assert!(!entry_deletable(&state, "tenant-a", "tenant-uid", &first));
            assert!(delete_request(&state, &first, "alpha").is_none());
            state.closed = false;
            state.databases[0].deleting = true;
            assert!(!entry_deletable(
                &state,
                "tenant-a",
                "tenant-uid",
                &state.databases[0]
            ));
            assert!(delete_request(&state, &state.databases[0], "alpha").is_none());
        }
    }

    #[test]
    fn exact_recovery_never_conflates_same_name_replacements() {
        let previous = catalog(TenantProvider::Azure);
        let selected = &previous.databases[0];
        let request = delete_request(&previous, selected, "alpha").unwrap();
        assert_eq!(request.catalog_uid, previous.catalog_uid);
        assert_eq!(request.logical_uid, selected.logical_uid);
        assert!(delete_request(&previous, selected, "beta").is_none());
        let mut current = previous.clone();
        assert_eq!(
            delete_recovery(&previous, &current, &previous.databases[0]),
            CatalogRecovery::Unchanged
        );
        current.databases[0].deleting = true;
        assert!(delete_request(&current, &current.databases[0], "alpha").is_none());
        assert_eq!(
            delete_recovery(&previous, &current, &previous.databases[0]),
            CatalogRecovery::Deleting
        );
        current.databases[0] = entry("alpha", "new-uid");
        assert!(delete_request(&current, selected, "alpha").is_none());
        assert_eq!(
            delete_recovery(&previous, &current, &previous.databases[0]),
            CatalogRecovery::Replaced
        );
        assert_eq!(
            add_recovery(&previous, &current, "alpha"),
            CatalogRecovery::ExistingIdentity
        );
        current.databases.remove(0);
        assert_eq!(
            delete_recovery(&previous, &current, &previous.databases[0]),
            CatalogRecovery::Absent
        );
        assert_eq!(
            add_recovery(&previous, &current, "alpha"),
            CatalogRecovery::Absent
        );
        current.catalog_uid = "replacement".into();
        assert_eq!(
            add_recovery(&previous, &current, "alpha"),
            CatalogRecovery::Replaced
        );
    }

    #[test]
    fn query_instance_is_scoped_by_ready_entry_and_exact_uid_on_both_providers() {
        for provider in [TenantProvider::Azure, TenantProvider::Local] {
            let state = catalog(provider);
            let first = &state.databases[0];
            let other = &state.databases[1];
            assert!(
                query_request(
                    &state,
                    first,
                    &other.instance_topology[0].uid,
                    "postgres",
                    "SELECT 1"
                )
                .is_none()
            );
            let mut tampered = first.clone();
            tampered.instance_topology = other.instance_topology.clone();
            assert!(
                query_request(
                    &state,
                    &tampered,
                    &other.instance_topology[0].uid,
                    "postgres",
                    "SELECT 1"
                )
                .is_none()
            );
            let request = query_request(
                &state,
                first,
                &first.instance_topology[0].uid,
                "postgres",
                "SELECT 1",
            )
            .unwrap();
            let response = CatalogQueryResponse {
                catalog_uid: request.catalog_uid.clone(),
                logical_uid: request.logical_uid.clone(),
                instance: request.instance.clone(),
                instance_uid: request.instance_uid.clone(),
                executed_at: "now".into(),
                duration_ms: 1,
                truncated: false,
                results: vec![],
            };
            assert!(query_response_matches(&request, &response));
            let mut wrong = response.clone();
            wrong.logical_uid = other.logical_uid.clone();
            assert!(!query_response_matches(&request, &wrong));
            let mut unavailable = first.clone();
            unavailable.deleting = true;
            assert!(query_instances(&unavailable).is_empty());
            unavailable.deleting = false;
            unavailable.phase = "degraded".into();
            assert!(query_instances(&unavailable).is_empty());
        }
    }
}
