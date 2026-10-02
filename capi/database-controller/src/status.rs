use crate::api::{CatalogStatus, EntryStatus, TenantDatabaseCatalog};
use crate::reconcile::{ObserveError, verify_current};
use kube::{Api, Client, ResourceExt, api::PostParams};

fn guard(
    current: &TenantDatabaseCatalog,
    observed: &TenantDatabaseCatalog,
) -> Result<(), ObserveError> {
    if current.metadata.uid != observed.metadata.uid
        || current.metadata.resource_version != observed.metadata.resource_version
        || current
            .metadata
            .resource_version
            .as_deref()
            .is_none_or(str::is_empty)
    {
        return Err(ObserveError::Identity);
    }
    Ok(())
}

fn new_status(
    current: &TenantDatabaseCatalog,
    entry_uid: &str,
    next: Option<EntryStatus>,
) -> Result<CatalogStatus, ObserveError> {
    let mut status = current.status.clone().unwrap_or_default();
    if let Some(next) = next {
        if next.logical_uid != entry_uid || !current.spec.entries.contains_key(entry_uid) {
            return Err(ObserveError::Identity);
        }
        status.entries.insert(entry_uid.into(), next);
    } else {
        if current.spec.entries.contains_key(entry_uid) {
            return Err(ObserveError::Identity);
        }
        status.entries.remove(entry_uid);
    }
    Ok(status)
}

pub async fn update(
    client: Client,
    observed: &TenantDatabaseCatalog,
    entry_uid: &str,
    next: Option<EntryStatus>,
) -> Result<TenantDatabaseCatalog, ObserveError> {
    let current = verify_current(client.clone(), observed).await?;
    guard(&current, observed)?;
    let mut updated = current.clone();
    updated.status = Some(new_status(&current, entry_uid, next)?);
    let api = Api::<TenantDatabaseCatalog>::namespaced(
        client.clone(),
        &current.namespace().ok_or(ObserveError::Identity)?,
    );
    let result = api
        .replace_status(&current.name_any(), &PostParams::default(), &updated)
        .await?;
    if result.metadata.uid != current.metadata.uid
        || result.metadata.generation != current.metadata.generation
        || result.metadata.resource_version == current.metadata.resource_version
        || result.status != updated.status
    {
        return Err(ObserveError::Identity);
    }
    let latest = verify_current(client, &result).await?;
    if latest.metadata.resource_version != result.metadata.resource_version
        || latest.status != updated.status
    {
        return Err(ObserveError::Identity);
    }
    Ok(latest)
}

pub async fn remove_spec(
    client: Client,
    observed: &TenantDatabaseCatalog,
    entry_uid: &str,
) -> Result<TenantDatabaseCatalog, ObserveError> {
    let current = verify_current(client.clone(), observed).await?;
    guard(&current, observed)?;
    if !current
        .spec
        .entries
        .get(entry_uid)
        .is_some_and(|entry| entry.deleting)
        || !current
            .status
            .as_ref()
            .and_then(|status| status.entries.get(entry_uid))
            .is_some_and(|status| {
                status.logical_uid == entry_uid
                    && status
                        .finalization
                        .as_ref()
                        .is_some_and(|proof| proof.terminal_verified && proof.pending.is_empty())
            })
    {
        return Err(ObserveError::Identity);
    }
    let mut next = current.clone();
    next.spec.entries.remove(entry_uid);
    let api = Api::<TenantDatabaseCatalog>::namespaced(
        client,
        &current.namespace().ok_or(ObserveError::Identity)?,
    );
    let result = api
        .replace(&current.name_any(), &PostParams::default(), &next)
        .await?;
    if result.metadata.uid != current.metadata.uid || result.spec.entries.contains_key(entry_uid) {
        return Err(ObserveError::Identity);
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{CatalogEntry, CatalogObservation, DatabasePhase, TenantDatabaseCatalogSpec};
    const OLD: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const NEXT: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const SIBLING_A: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    const SIBLING_B: &str = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    fn fixture() -> TenantDatabaseCatalog {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant".into(),
                closed: false,
                entries: [(
                    OLD.into(),
                    CatalogEntry {
                        name: "orders".into(),
                        instances: 1,
                        deleting: true,
                    },
                )]
                .into(),
            },
        );
        catalog.metadata.uid = Some(OLD.into());
        catalog.metadata.resource_version = Some("1".into());
        catalog
    }
    fn entry(uid: &str, phase: DatabasePhase) -> EntryStatus {
        EntryStatus {
            logical_uid: uid.into(),
            observed_generation: 1,
            phase,
            conditions: vec![],
            provider: None,
            namespace: None,
            cnpg_cluster: None,
            credentials: None,
            storage: vec![],
            instances: vec![],
            query: None,
            finalization: None,
            create_intents: vec![],
        }
    }
    #[test]
    fn exact_uid_pruning_preserves_siblings_and_observer_after_immediate_readd() {
        let mut catalog = fixture();
        for (uid, name) in [(SIBLING_A, "metrics"), (SIBLING_B, "reports")] {
            catalog.spec.entries.insert(
                uid.into(),
                CatalogEntry {
                    name: name.into(),
                    instances: 1,
                    deleting: false,
                },
            );
        }
        catalog.status = Some(CatalogStatus {
            entries: [
                (OLD.into(), entry(OLD, DatabasePhase::Deleting)),
                (SIBLING_A.into(), entry(SIBLING_A, DatabasePhase::Ready)),
                (SIBLING_B.into(), entry(SIBLING_B, DatabasePhase::Ready)),
            ]
            .into(),
            observer: Some(CatalogObservation {
                catalog_uid: OLD.into(),
                observed_generation: 1,
                observed_resource_version: "1".into(),
                pod_uid: "pod".into(),
                instance_id: "boot".into(),
            }),
        });
        catalog.spec.entries.remove(OLD);
        catalog.spec.entries.insert(
            NEXT.into(),
            CatalogEntry {
                name: "orders".into(),
                instances: 1,
                deleting: false,
            },
        );
        let updated = new_status(&catalog, OLD, None).unwrap();
        assert!(!updated.entries.contains_key(OLD));
        assert_eq!(updated.entries.len(), 2);
        assert_eq!(updated.entries[SIBLING_A].phase, DatabasePhase::Ready);
        assert_eq!(updated.observer, catalog.status.clone().unwrap().observer);
        catalog.status = Some(updated);
        let inserted =
            new_status(&catalog, NEXT, Some(entry(NEXT, DatabasePhase::Pending))).unwrap();
        assert_eq!(inserted.entries.len(), 3);
        assert_eq!(inserted.entries[NEXT].phase, DatabasePhase::Pending);
        assert_eq!(inserted.entries[SIBLING_B].phase, DatabasePhase::Ready);
    }
    #[test]
    fn stale_resource_version_and_foreign_uid_cannot_replace_status() {
        let current = fixture();
        let mut stale = current.clone();
        stale.metadata.resource_version = Some("0".into());
        assert!(guard(&current, &stale).is_err());
        stale = current.clone();
        stale.metadata.uid = Some(NEXT.into());
        assert!(guard(&current, &stale).is_err());
        assert!(new_status(&current, NEXT, Some(entry(NEXT, DatabasePhase::Ready))).is_err());
        assert!(new_status(&current, OLD, None).is_err());
    }
}
