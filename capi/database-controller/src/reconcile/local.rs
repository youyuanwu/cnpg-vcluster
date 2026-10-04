use std::time::Duration;

use k8s_openapi::apimachinery::pkg::apis::meta::v1::{Condition, OwnerReference, Time};
use kube::{
    Api, Client, ResourceExt,
    api::{ApiResource, DeleteParams, ListParams, PostParams, Preconditions},
    core::{DynamicObject, GroupVersionKind},
    runtime::controller::Action,
};
use serde_json::{Value, json};

use super::{
    ObserveError, Progress, creation_order, next_action, stop_after_progress, verify_current,
};
use crate::{
    api::{
        CreateIntent, CreateState, DatabasePhase, EntryStatus, FinalizationStatus,
        InstanceObservation, ProviderIdentity, QueryIdentity, ResourceIdentity, StorageIdentity,
        TenantDatabaseCatalog,
    },
    finalize::local::all_creates_resolved,
    local_path, ownership,
    resources::cnpg,
    status,
    tenant_access::{self, LocalAccess},
};

const SIZE: i64 = 1024 * 1024 * 1024;
const RETRY: Duration = Duration::from_secs(10);
const RESYNC: Duration = Duration::from_secs(60);
const CNPG_CLUSTER_VERSION: &str = "postgresql.cnpg.io/v1";

pub(super) fn owned_by_cluster(
    owners: Option<&[OwnerReference]>,
    cluster: &ResourceIdentity,
) -> bool {
    owners.is_some_and(|owners| {
        owners.len() == 1
            && owners[0].api_version == CNPG_CLUSTER_VERSION
            && owners[0].kind == "Cluster"
            && owners[0].name == cluster.name
            && owners[0].uid == cluster.uid
    })
}

pub(super) fn api(
    client: Client,
    namespace: Option<&str>,
    group: &str,
    version: &str,
    kind: &str,
    plural: &str,
) -> Api<DynamicObject> {
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(group, version, kind));
    resource.plural = plural.into();
    match namespace {
        Some(namespace) => Api::namespaced_with(client, namespace, &resource),
        None => Api::all_with(client, &resource),
    }
}

pub(super) fn core(
    client: Client,
    namespace: Option<&str>,
    kind: &str,
    plural: &str,
) -> Api<DynamicObject> {
    api(client, namespace, "", "v1", kind, plural)
}

pub(super) fn object(
    kind: &str,
    name: &str,
    namespace: Option<&str>,
    labels: Value,
    spec: Option<Value>,
) -> Result<DynamicObject, ObserveError> {
    let mut value = json!({"apiVersion":"v1","kind":kind,"metadata":{"name":name,"labels":labels}});
    if let Some(namespace) = namespace {
        value["metadata"]["namespace"] = json!(namespace);
    }
    if let Some(spec) = spec {
        value["spec"] = spec;
    }
    serde_json::from_value(value).map_err(|_| ObserveError::Identity)
}

pub(super) fn identity(catalog: &TenantDatabaseCatalog, uid: &str) -> Result<Value, ObserveError> {
    Ok(json!(
        ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?
    ))
}

pub(super) fn check(
    object: &DynamicObject,
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    name: &str,
    expected: Option<&ResourceIdentity>,
) -> Result<ResourceIdentity, ObserveError> {
    if object.name_any() != name {
        return Err(ObserveError::Foreign);
    }
    let owners = object
        .metadata
        .owner_references
        .as_deref()
        .unwrap_or_default();
    if !owners.is_empty()
        && !(object
            .types
            .as_ref()
            .is_some_and(|types| types.kind == "PersistentVolumeClaim")
            && owners.len() == 1
            && catalog
                .status
                .as_ref()
                .and_then(|status| status.entries.get(uid))
                .and_then(|status| status.cnpg_cluster.as_ref())
                .is_some_and(|cluster| owned_by_cluster(Some(owners), cluster)))
    {
        return Err(ObserveError::Foreign);
    }
    ownership::check(&object.metadata, catalog, uid, expected).map_err(|_| ObserveError::Foreign)
}

fn contains(actual: &Value, desired: &Value) -> bool {
    match (actual, desired) {
        (Value::Object(actual), Value::Object(desired)) => desired.iter().all(|(key, value)| {
            actual
                .get(key)
                .is_some_and(|actual| contains(actual, value))
        }),
        _ => actual == desired,
    }
}

pub(super) fn matches_desired(live: &DynamicObject, desired: &DynamicObject) -> bool {
    let mut desired_data = desired.data.clone();
    if desired
        .types
        .as_ref()
        .is_some_and(|types| types.kind == "PersistentVolumeClaim")
    {
        let wanted = desired_data
            .pointer("/spec/volumeName")
            .and_then(Value::as_str);
        let actual = live
            .data
            .pointer("/spec/volumeName")
            .and_then(Value::as_str);
        if actual.is_some() && actual != wanted {
            return false;
        }
        desired_data
            .as_object_mut()
            .and_then(|data| data.get_mut("spec"))
            .and_then(Value::as_object_mut)
            .map(|spec| spec.remove("volumeName"));
    }
    live.name_any() == desired.name_any()
        && live.namespace() == desired.namespace()
        && desired.metadata.labels.as_ref().is_some_and(|labels| {
            labels.iter().all(|(key, value)| {
                live.metadata
                    .labels
                    .as_ref()
                    .and_then(|actual| actual.get(key))
                    == Some(value)
            })
        })
        && contains(&live.data, &desired_data)
}

fn bound<'a>(state: &'a EntryStatus, kind: &str, ordinal: i32) -> Option<&'a ResourceIdentity> {
    match kind {
        "Namespace" => state.namespace.as_ref(),
        "Cluster" => state.cnpg_cluster.as_ref(),
        "PersistentVolume" => state
            .storage
            .iter()
            .find(|s| s.ordinal == ordinal)
            .and_then(|s| s.pv.as_ref()),
        "PersistentVolumeClaim" => state
            .storage
            .iter()
            .find(|s| s.ordinal == ordinal)
            .and_then(|s| s.pvc.as_ref()),
        _ => None,
    }
}

fn entry(uid: &str, generation: i64) -> EntryStatus {
    EntryStatus {
        logical_uid: uid.into(),
        observed_generation: generation,
        phase: DatabasePhase::Pending,
        conditions: vec![],
        provider: Some(ProviderIdentity {
            kind: "local".into(),
            storage_namespace: None,
        }),
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

pub(super) fn set_health(
    state: &mut EntryStatus,
    ready: bool,
    generation: i64,
    blocked: Option<&str>,
) {
    let reason = if let Some(reason) = blocked {
        reason
    } else if ready {
        "AllInstancesReady"
    } else if state.credentials.is_none() {
        "CredentialsPending"
    } else if state.storage.iter().any(|s| !s.healthy) {
        "StoragePending"
    } else {
        "InstancesPending"
    };
    let status = if ready { "True" } else { "False" };
    let previous = state
        .conditions
        .iter()
        .find(|condition| condition.type_ == "Ready");
    let transition = previous
        .filter(|condition| condition.status == status && condition.reason == reason)
        .map(|condition| condition.last_transition_time.clone())
        .unwrap_or_else(|| {
            let now = k8s_openapi::jiff::Timestamp::now();
            Time(
                k8s_openapi::jiff::Timestamp::from_second(now.as_second())
                    .expect("current Unix second is representable"),
            )
        });
    state.conditions = vec![Condition {
        type_: "Ready".into(),
        status: status.into(),
        reason: reason.into(),
        message: reason.into(),
        last_transition_time: transition,
        observed_generation: Some(generation),
    }];
    state.phase = if ready {
        DatabasePhase::Ready
    } else if matches!(state.phase, DatabasePhase::Ready | DatabasePhase::Degraded) {
        DatabasePhase::Degraded
    } else {
        DatabasePhase::Progressing
    };
}

pub(crate) async fn blocked(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    reason: &str,
) -> Result<bool, ObserveError> {
    state.query = None;
    state.observed_generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    set_health(state, false, state.observed_generation, Some(reason));
    if catalog
        .status
        .as_ref()
        .and_then(|status| status.entries.get(uid))
        != Some(state)
    {
        save(management, catalog, uid, state).await?;
        return Ok(true);
    }
    Ok(false)
}

pub(crate) async fn blocked_progress(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    reason: &str,
) -> Result<Progress, ObserveError> {
    Ok(if blocked(management, catalog, uid, state, reason).await? {
        Progress::Changed
    } else {
        Progress::Waiting
    })
}

pub(crate) async fn save(
    client: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &EntryStatus,
) -> Result<(), ObserveError> {
    *catalog = status::update(client, catalog, uid, Some(state.clone())).await?;
    Ok(())
}

pub(crate) fn intent(
    state: &EntryStatus,
    kind: &str,
    name: &str,
    ordinal: i32,
) -> Result<Option<CreateState>, ObserveError> {
    let mut matching = state
        .create_intents
        .iter()
        .filter(|i| i.kind == kind && i.ordinal == ordinal);
    let current = matching.next();
    if matching.next().is_some() {
        return Err(ObserveError::Foreign);
    }
    if current.is_some_and(|i| i.name != name) {
        return Err(ObserveError::Foreign);
    }
    Ok(current.map(|i| i.state))
}

#[expect(
    clippy::too_many_arguments,
    reason = "each durable intent is scoped to an exact catalog, entry, kind, name and ordinal"
)]
pub(crate) async fn record(
    client: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    kind: &str,
    name: &str,
    ordinal: i32,
    next: CreateState,
) -> Result<(), ObserveError> {
    if let Some(existing) = state
        .create_intents
        .iter_mut()
        .find(|i| i.kind == kind && i.ordinal == ordinal)
    {
        if existing.name != name {
            return Err(ObserveError::Foreign);
        }
        existing.state = next;
    } else {
        state.create_intents.push(CreateIntent {
            kind: kind.into(),
            name: name.into(),
            ordinal,
            state: next,
        });
    }
    save(client, catalog, uid, state).await
}

pub(super) fn definite_rejection(error: &kube::Error) -> bool {
    matches!(error, kube::Error::Api(status) if (400..500).contains(&status.code) && !matches!(status.code, 408 | 409 | 429))
}

pub(super) fn may_issue(previous: Option<CreateState>) -> bool {
    !matches!(previous, Some(CreateState::Issued | CreateState::Observed))
}

#[expect(
    clippy::too_many_arguments,
    reason = "creation binds the exact catalog entry and named resource"
)]
pub(super) async fn create(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    api: Api<DynamicObject>,
    desired: DynamicObject,
    kind: &str,
    ordinal: i32,
) -> Result<Option<ResourceIdentity>, ObserveError> {
    create_with_replay(
        management, catalog, uid, state, api, desired, kind, ordinal, false,
    )
    .await
}

#[expect(
    clippy::too_many_arguments,
    reason = "creation binds the exact catalog entry and named resource"
)]
pub(super) async fn create_with_replay(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    api: Api<DynamicObject>,
    desired: DynamicObject,
    kind: &str,
    ordinal: i32,
    replay_issued: bool,
) -> Result<Option<ResourceIdentity>, ObserveError> {
    let name = desired.name_any();
    let previous = intent(state, kind, &name, ordinal)?;
    if let Some(existing) = api.get_opt(&name).await? {
        let id = check(&existing, catalog, uid, &name, bound(state, kind, ordinal))?;
        if !matches_desired(&existing, &desired) {
            return Err(ObserveError::Foreign);
        }
        if !matches!(previous, Some(CreateState::Issued | CreateState::Observed)) {
            return Err(ObserveError::Foreign);
        }
        if previous != Some(CreateState::Observed) {
            record(
                management,
                catalog,
                uid,
                state,
                kind,
                &name,
                ordinal,
                CreateState::Observed,
            )
            .await?;
        }
        return Ok(Some(id));
    }
    let replay = replay_issued && previous == Some(CreateState::Issued);
    if !may_issue(previous) && !replay {
        return Ok(None);
    }
    if replay {
        tracing::warn!(
            kind,
            name,
            ordinal,
            "replaying issued create after controller restart"
        );
    }
    if !replay && previous != Some(CreateState::Planned) {
        record(
            management.clone(),
            catalog,
            uid,
            state,
            kind,
            &name,
            ordinal,
            CreateState::Planned,
        )
        .await?;
    }
    if !replay {
        record(
            management.clone(),
            catalog,
            uid,
            state,
            kind,
            &name,
            ordinal,
            CreateState::Issued,
        )
        .await?;
    }
    verify_current(management.clone(), catalog).await?;
    match api.create(&PostParams::default(), &desired).await {
        Ok(created) => {
            let id = check(&created, catalog, uid, &name, bound(state, kind, ordinal))?;
            record(
                management,
                catalog,
                uid,
                state,
                kind,
                &name,
                ordinal,
                CreateState::Observed,
            )
            .await?;
            Ok(Some(id))
        }
        Err(error) if definite_rejection(&error) => {
            record(
                management,
                catalog,
                uid,
                state,
                kind,
                &name,
                ordinal,
                CreateState::Rejected,
            )
            .await?;
            Err(ObserveError::Api(error))
        }
        Err(error) => Err(ObserveError::Api(error)),
    }
}

async fn path(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    access: &LocalAccess,
    ordinal: i32,
) -> Result<Option<String>, ObserveError> {
    let catalog_uid = catalog
        .metadata
        .uid
        .as_deref()
        .ok_or(ObserveError::Identity)?
        .to_owned();
    let name = format!("{catalog_uid}/{uid}/{ordinal}");
    let previous = intent(state, "Path", &name, ordinal)?;
    if local_path::inspect(&access.root, &catalog_uid, uid, ordinal)? {
        if !matches!(previous, Some(CreateState::Issued | CreateState::Observed)) {
            return Err(ObserveError::Foreign);
        }
        if previous != Some(CreateState::Observed) {
            record(
                management,
                catalog,
                uid,
                state,
                "Path",
                &name,
                ordinal,
                CreateState::Observed,
            )
            .await?;
        }
        return Ok(Some(
            local_path::prepare(&access.root, &catalog_uid, uid, ordinal)?
                .to_string_lossy()
                .into_owned(),
        ));
    }
    if !may_issue(previous) {
        return Ok(None);
    }
    if previous != Some(CreateState::Planned) {
        record(
            management.clone(),
            catalog,
            uid,
            state,
            "Path",
            &name,
            ordinal,
            CreateState::Planned,
        )
        .await?;
    }
    record(
        management.clone(),
        catalog,
        uid,
        state,
        "Path",
        &name,
        ordinal,
        CreateState::Issued,
    )
    .await?;
    verify_current(management.clone(), catalog).await?;
    let prepared = local_path::prepare(&access.root, &catalog_uid, uid, ordinal)?;
    record(
        management,
        catalog,
        uid,
        state,
        "Path",
        &name,
        ordinal,
        CreateState::Observed,
    )
    .await?;
    Ok(Some(prepared.to_string_lossy().into_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{CatalogEntry, TenantDatabaseCatalogSpec};

    const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const FIRST: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const REPLACEMENT: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

    fn fixture() -> TenantDatabaseCatalog {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant".into(),
                closed: false,
                entries: [(
                    FIRST.into(),
                    CatalogEntry {
                        name: "orders".into(),
                        instances: 3,
                        deleting: false,
                    },
                )]
                .into(),
            },
        );
        catalog.metadata.uid = Some(CATALOG.into());
        catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
        catalog.metadata.generation = Some(2);
        catalog
    }

    fn api_failure(code: u16) -> kube::Error {
        kube::Error::Api(
            serde_json::from_value(json!({
                "status":"Failure","message":"request failed","reason":"Failure","code":code
            }))
            .unwrap(),
        )
    }

    #[test]
    fn restart_and_ambiguous_create_do_not_reissue_without_observation() {
        let (_, name) = ownership::names(CATALOG, FIRST).unwrap();
        let mut state = entry(FIRST, 2);
        assert!(may_issue(intent(&state, "Cluster", &name, 0).unwrap()));
        state.create_intents.push(CreateIntent {
            kind: "Cluster".into(),
            name: name.clone(),
            ordinal: 0,
            state: CreateState::Planned,
        });
        assert!(may_issue(intent(&state, "Cluster", &name, 0).unwrap()));
        state.create_intents[0].state = CreateState::Issued;
        assert!(!may_issue(intent(&state, "Cluster", &name, 0).unwrap()));
        assert!(!definite_rejection(&api_failure(408)));
        assert!(!definite_rejection(&api_failure(409)));
        assert!(!definite_rejection(&api_failure(503)));
        assert!(definite_rejection(&api_failure(403)));
        state.create_intents[0].state = CreateState::Rejected;
        assert!(may_issue(intent(&state, "Cluster", &name, 0).unwrap()));
        state.create_intents[0].state = CreateState::Observed;
        assert!(!may_issue(intent(&state, "Cluster", &name, 0).unwrap()));
        assert!(intent(&state, "Cluster", "forged", 0).is_err());
    }

    #[test]
    fn health_and_degradation_are_independent_per_entry() {
        let mut entries = [entry(FIRST, 3), entry(REPLACEMENT, 3), entry(CATALOG, 3)];
        entries[0].credentials = Some(ResourceIdentity {
            name: "first-superuser".into(),
            uid: "a".into(),
        });
        set_health(&mut entries[0], true, 3, None);
        set_health(&mut entries[1], false, 3, None);
        set_health(&mut entries[2], true, 3, None);
        assert_eq!(
            entries.iter().map(|entry| entry.phase).collect::<Vec<_>>(),
            vec![
                DatabasePhase::Ready,
                DatabasePhase::Progressing,
                DatabasePhase::Ready
            ]
        );
        let transition = entries[0].conditions[0].last_transition_time.clone();
        set_health(&mut entries[0], true, 3, None);
        assert_eq!(entries[0].conditions[0].last_transition_time, transition);
        set_health(&mut entries[0], false, 4, None);
        assert_eq!(entries[0].phase, DatabasePhase::Degraded);
        assert_eq!(entries[1].phase, DatabasePhase::Progressing);
        assert_eq!(entries[2].phase, DatabasePhase::Ready);
        assert_eq!(entries[0].conditions[0].observed_generation, Some(4));
    }

    #[test]
    fn replacement_and_foreign_resources_never_match_original_entry() {
        let mut catalog = fixture();
        let (namespace, cluster) = ownership::names(CATALOG, FIRST).unwrap();
        let replacement = ownership::names(CATALOG, REPLACEMENT).unwrap();
        assert_ne!((namespace.clone(), cluster.clone()), replacement);
        let mut live = object(
            "Namespace",
            &namespace,
            None,
            identity(&catalog, FIRST).unwrap(),
            None,
        )
        .unwrap();
        live.metadata.uid = Some("resource-1".into());
        assert!(check(&live, &catalog, FIRST, &namespace, None).is_ok());
        assert!(check(&live, &catalog, REPLACEMENT, &namespace, None).is_err());
        let expected = ResourceIdentity {
            name: namespace.clone(),
            uid: "old-uid".into(),
        };
        assert!(check(&live, &catalog, FIRST, &namespace, Some(&expected)).is_err());
        live.metadata
            .labels
            .as_mut()
            .unwrap()
            .insert(ownership::ENTRY_LABEL.into(), REPLACEMENT.into());
        assert!(check(&live, &catalog, FIRST, &namespace, None).is_err());
        catalog.spec.entries.remove(FIRST);
        catalog.spec.entries.insert(
            REPLACEMENT.into(),
            CatalogEntry {
                name: "orders".into(),
                instances: 3,
                deleting: false,
            },
        );
        assert_eq!(
            catalog.spec.entries.get(REPLACEMENT).unwrap().name,
            "orders"
        );
    }

    #[test]
    fn validation_refuses_foreign_persistent_volume_and_uid_path() {
        let catalog = fixture();
        let (namespace, cluster) = ownership::names(CATALOG, FIRST).unwrap();
        let mut state = entry(FIRST, 2);
        state.namespace = Some(ResourceIdentity {
            name: namespace.clone(),
            uid: "ns".into(),
        });
        state.storage.push(StorageIdentity {
            ordinal: 1,
            requested_bytes: SIZE,
            path: None,
            pv: Some(ResourceIdentity {
                name: format!("pv-{cluster}-1"),
                uid: "pv".into(),
            }),
            pvc: None,
            disk: None,
            arm_id: None,
            healthy: false,
        });
        let root = std::path::PathBuf::from("/var/lib/docker/volumes/tenant/_data");
        assert!(validate_state(&state, FIRST, &namespace, &cluster, 3, CATALOG, &root).is_ok());
        state.storage[0].pv.as_mut().unwrap().name = "pv-foreign".into();
        assert!(validate_state(&state, FIRST, &namespace, &cluster, 3, CATALOG, &root).is_err());
        let mut state = entry(FIRST, 2);
        state.storage.push(StorageIdentity {
            ordinal: 1,
            requested_bytes: SIZE,
            path: Some("/foreign".into()),
            pv: None,
            pvc: None,
            disk: None,
            arm_id: None,
            healthy: false,
        });
        assert!(validate_state(&state, FIRST, &namespace, &cluster, 3, CATALOG, &root).is_err());
        let expected = root
            .join("volumes/cnpg")
            .join(CATALOG)
            .join(FIRST)
            .join("1");
        assert_ne!(state.storage[0].path.as_deref(), expected.to_str());
        assert!(!contains(
            &json!({"spec":{"hostPath":{"path":"/foreign"}}}),
            &json!({"spec":{"hostPath":{"path":expected}}})
        ));
        assert_eq!(catalog.spec.entries.len(), 1);
    }

    #[test]
    fn generated_claim_requires_exact_cluster_owner_and_entry_labels() {
        let mut catalog = fixture();
        let (namespace, cluster) = ownership::names(CATALOG, FIRST).unwrap();
        let mut state = entry(FIRST, 2);
        state.cnpg_cluster = Some(ResourceIdentity {
            name: cluster.clone(),
            uid: "cluster-uid".into(),
        });
        catalog.status = Some(crate::api::CatalogStatus {
            entries: [(FIRST.into(), state)].into(),
            observer: None,
        });
        let mut claim = cnpg::claim(&catalog, FIRST, &namespace, &cluster, 1).unwrap();
        claim.metadata.uid = Some("claim-uid".into());
        claim.metadata.owner_references = Some(vec![
            k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference {
                api_version: "postgresql.cnpg.io/v1".into(),
                kind: "Cluster".into(),
                name: cluster.clone(),
                uid: "cluster-uid".into(),
                ..Default::default()
            },
        ]);
        assert!(check(&claim, &catalog, FIRST, &claim.name_any(), None).is_ok());
        claim.metadata.owner_references.as_mut().unwrap()[0].api_version =
            "postgresql.cnpg.io/v1beta1".into();
        assert!(check(&claim, &catalog, FIRST, &claim.name_any(), None).is_err());
        claim.metadata.owner_references.as_mut().unwrap()[0].api_version =
            CNPG_CLUSTER_VERSION.into();
        claim.metadata.owner_references.as_mut().unwrap()[0].uid = "foreign-cluster".into();
        assert!(check(&claim, &catalog, FIRST, &claim.name_any(), None).is_err());
        claim.metadata.owner_references.as_mut().unwrap()[0].uid = "cluster-uid".into();
        claim
            .metadata
            .labels
            .as_mut()
            .unwrap()
            .insert(ownership::ENTRY_LABEL.into(), REPLACEMENT.into());
        assert!(check(&claim, &catalog, FIRST, &claim.name_any(), None).is_err());
    }

    #[test]
    fn generated_secret_and_pod_owner_requires_exact_api_version() {
        let cluster = ResourceIdentity {
            name: "pg-a".into(),
            uid: "uid-a".into(),
        };
        let owner = OwnerReference {
            api_version: CNPG_CLUSTER_VERSION.into(),
            kind: "Cluster".into(),
            name: cluster.name.clone(),
            uid: cluster.uid.clone(),
            ..Default::default()
        };
        assert!(owned_by_cluster(
            Some(std::slice::from_ref(&owner)),
            &cluster
        ));
        for wrong in [
            OwnerReference {
                api_version: "postgresql.cnpg.io/v1beta1".into(),
                ..owner.clone()
            },
            OwnerReference {
                uid: "replacement".into(),
                ..owner.clone()
            },
            OwnerReference {
                kind: "Pod".into(),
                ..owner.clone()
            },
        ] {
            assert!(!owned_by_cluster(Some(&[wrong]), &cluster));
        }
        assert!(!owned_by_cluster(Some(&[owner.clone(), owner]), &cluster));
    }
}

fn storage(state: &mut EntryStatus, ordinal: i32) -> &mut StorageIdentity {
    if !state.storage.iter().any(|s| s.ordinal == ordinal) {
        state.storage.push(StorageIdentity {
            ordinal,
            requested_bytes: SIZE,
            path: None,
            pv: None,
            pvc: None,
            disk: None,
            arm_id: None,
            healthy: false,
        });
    }
    state
        .storage
        .iter_mut()
        .find(|s| s.ordinal == ordinal)
        .expect("inserted")
}

fn validate_state(
    state: &EntryStatus,
    uid: &str,
    namespace: &str,
    cluster: &str,
    instances: i32,
    catalog_uid: &str,
    root: &std::path::Path,
) -> Result<(), ObserveError> {
    if state.logical_uid != uid
        || state
            .namespace
            .as_ref()
            .is_some_and(|value| value.name != namespace || value.uid.is_empty())
        || state
            .cnpg_cluster
            .as_ref()
            .is_some_and(|value| value.name != cluster || value.uid.is_empty())
        || state.credentials.as_ref().is_some_and(|value| {
            value.name != format!("{cluster}-superuser") || value.uid.is_empty()
        })
        || state
            .provider
            .as_ref()
            .is_some_and(|provider| provider.kind != "local")
        || state.storage.iter().any(|s| {
            !(1..=instances).contains(&s.ordinal)
                || s.requested_bytes != SIZE
                || s.disk.is_some()
                || s.arm_id.is_some()
                || s.path.as_ref().is_some_and(|path| {
                    std::path::Path::new(path)
                        != root
                            .join("volumes/cnpg")
                            .join(catalog_uid)
                            .join(uid)
                            .join(s.ordinal.to_string())
                })
                || s.pv
                    .as_ref()
                    .is_some_and(|value| value.name != format!("pv-{cluster}-{}", s.ordinal))
                || s.pvc
                    .as_ref()
                    .is_some_and(|value| value.name != format!("{cluster}-{}", s.ordinal))
        })
        || state.storage.iter().enumerate().any(|(index, item)| {
            state.storage[index + 1..]
                .iter()
                .any(|other| other.ordinal == item.ordinal)
        })
        || state.create_intents.len() > 12
        || state
            .create_intents
            .iter()
            .enumerate()
            .any(|(index, item)| {
                state.create_intents[index + 1..]
                    .iter()
                    .any(|other| other.kind == item.kind && other.ordinal == item.ordinal)
            })
        || state
            .create_intents
            .iter()
            .any(|intent| match intent.kind.as_str() {
                "Path" | "PersistentVolume" | "PersistentVolumeClaim" => {
                    !(1..=instances).contains(&intent.ordinal)
                }
                "Namespace" | "Cluster" => intent.ordinal != 0,
                _ => true,
            })
    {
        return Err(ObserveError::Foreign);
    }
    Ok(())
}

pub async fn reconcile(
    management: Client,
    observed: &TenantDatabaseCatalog,
) -> Result<(Action, TenantDatabaseCatalog), ObserveError> {
    let mut catalog = verify_current(management.clone(), observed).await?;
    let mut waiting = false;
    if let Some(uid) = catalog
        .status
        .as_ref()
        .and_then(|s| {
            s.entries
                .keys()
                .find(|uid| !catalog.spec.entries.contains_key(*uid))
        })
        .cloned()
    {
        catalog = status::update(management, &catalog, &uid, None).await?;
        return Ok((Action::requeue(RETRY), catalog));
    }
    let access = tenant_access::load(management.clone(), &catalog).await?;
    let generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    let mut entries: Vec<_> = catalog.spec.entries.clone().into_iter().collect();
    entries.sort_by_key(|(_, spec)| creation_order(spec.deleting, catalog.spec.closed));
    for (uid, spec) in entries {
        let (namespace, cluster) = ownership::names(
            catalog
                .metadata
                .uid
                .as_deref()
                .ok_or(ObserveError::Identity)?,
            &uid,
        )
        .map_err(|_| ObserveError::Foreign)?;
        let mut state = catalog
            .status
            .as_ref()
            .and_then(|status| status.entries.get(&uid))
            .cloned()
            .unwrap_or_else(|| entry(&uid, generation));
        validate_state(
            &state,
            &uid,
            &namespace,
            &cluster,
            spec.instances,
            catalog
                .metadata
                .uid
                .as_deref()
                .ok_or(ObserveError::Identity)?,
            &access.root,
        )?;
        if !catalog
            .status
            .as_ref()
            .is_some_and(|s| s.entries.contains_key(&uid))
        {
            save(management.clone(), &mut catalog, &uid, &state).await?;
            waiting = true;
            continue;
        }
        let creating = !spec.deleting && !catalog.spec.closed;
        let result = if !creating {
            finalize(
                management.clone(),
                &mut catalog,
                &uid,
                &namespace,
                &cluster,
                &mut state,
                spec.instances,
                &access,
            )
            .await
        } else {
            ensure(
                management.clone(),
                &mut catalog,
                &uid,
                &namespace,
                &cluster,
                &mut state,
                spec.instances,
                &access,
            )
            .await
        };
        match result {
            Ok(progress) => {
                if stop_after_progress(progress, creating, &mut waiting) {
                    return Ok((Action::requeue(RETRY), catalog));
                }
            }
            Err(
                ObserveError::Foreign
                | ObserveError::Path(
                    local_path::PathError::UnsafeEntry | local_path::PathError::Changed,
                ),
            ) => {
                state.phase = DatabasePhase::OwnershipInvalid;
                state.query = None;
                state.observed_generation =
                    catalog.metadata.generation.ok_or(ObserveError::Identity)?;
                set_health(
                    &mut state,
                    false,
                    catalog.metadata.generation.ok_or(ObserveError::Identity)?,
                    Some("OwnershipInvalid"),
                );
                state.phase = DatabasePhase::OwnershipInvalid;
                if catalog
                    .status
                    .as_ref()
                    .and_then(|status| status.entries.get(&uid))
                    != Some(&state)
                {
                    save(management.clone(), &mut catalog, &uid, &state).await?;
                    waiting = true;
                }
            }
            Err(error) => return Err(error),
        }
    }
    Ok((next_action(waiting, RETRY, RESYNC), catalog))
}

#[expect(
    clippy::too_many_arguments,
    reason = "local resources require exact catalog, entry and tenant access"
)]
async fn ensure(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster: &str,
    state: &mut EntryStatus,
    instances: i32,
    access: &LocalAccess,
) -> Result<Progress, ObserveError> {
    let ns_api = core(access.client.clone(), None, "Namespace", "namespaces");
    let ns = object("Namespace", namespace, None, identity(catalog, uid)?, None)?;
    let Some(id) = create(
        management.clone(),
        catalog,
        uid,
        state,
        ns_api,
        ns,
        "Namespace",
        0,
    )
    .await?
    else {
        return blocked_progress(management, catalog, uid, state, "UnknownCreateOutcome").await;
    };
    if state.namespace.as_ref() != Some(&id) {
        if state.namespace.is_some() {
            return Err(ObserveError::Foreign);
        }
        state.namespace = Some(id);
        save(management, catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let mut paths_changed = false;
    for ordinal in 1..=instances {
        let Some(path) = path(management.clone(), catalog, uid, state, access, ordinal).await?
        else {
            return blocked_progress(management, catalog, uid, state, "UnknownCreateOutcome").await;
        };
        if storage(state, ordinal).path.as_deref() != Some(&path) {
            if storage(state, ordinal).path.is_some() {
                return Err(ObserveError::Foreign);
            }
            storage(state, ordinal).path = Some(path);
            paths_changed = true;
        }
    }
    if paths_changed {
        save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let mut volumes_changed = false;
    for ordinal in 1..=instances {
        let path = storage(state, ordinal)
            .path
            .as_deref()
            .ok_or(ObserveError::Foreign)?;
        let pv_api = core(
            access.client.clone(),
            None,
            "PersistentVolume",
            "persistentvolumes",
        );
        let pv = cnpg::volume(catalog, uid, namespace, cluster, ordinal, access, path)?;
        let Some(id) = create(
            management.clone(),
            catalog,
            uid,
            state,
            pv_api,
            pv,
            "PersistentVolume",
            ordinal,
        )
        .await?
        else {
            return blocked_progress(management, catalog, uid, state, "UnknownCreateOutcome").await;
        };
        if storage(state, ordinal).pv.as_ref() != Some(&id) {
            if storage(state, ordinal).pv.is_some() {
                return Err(ObserveError::Foreign);
            }
            storage(state, ordinal).pv = Some(id);
            volumes_changed = true;
        }
    }
    if volumes_changed {
        save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let cluster_api = api(
        access.client.clone(),
        Some(namespace),
        "postgresql.cnpg.io",
        "v1",
        "Cluster",
        "clusters",
    );
    let desired = cnpg::cluster(catalog, uid, namespace, cluster, instances, &access.image)?;
    let Some(id) = create(
        management.clone(),
        catalog,
        uid,
        state,
        cluster_api.clone(),
        desired,
        "Cluster",
        0,
    )
    .await?
    else {
        return blocked_progress(management, catalog, uid, state, "UnknownCreateOutcome").await;
    };
    if state.cnpg_cluster.as_ref() != Some(&id) {
        if state.cnpg_cluster.is_some() {
            return Err(ObserveError::Foreign);
        }
        state.cnpg_cluster = Some(id);
        save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let mut claims_waiting = false;
    let mut claims_changed = false;
    for ordinal in 1..=instances {
        let desired = cnpg::claim(catalog, uid, namespace, cluster, ordinal)?;
        let Some(actual) = core(
            access.client.clone(),
            Some(namespace),
            "PersistentVolumeClaim",
            "persistentvolumeclaims",
        )
        .get_opt(&desired.name_any())
        .await?
        else {
            claims_waiting = true;
            continue;
        };
        let bound = state
            .storage
            .iter()
            .find(|s| s.ordinal == ordinal)
            .and_then(|s| s.pvc.as_ref());
        let claimed = check(&actual, catalog, uid, &desired.name_any(), bound)?;
        if !owned_by_cluster(actual.metadata.owner_references.as_deref(), &id)
            || !matches_desired(&actual, &desired)
        {
            return Err(ObserveError::Foreign);
        }
        if storage(state, ordinal).pvc.as_ref() != Some(&claimed) {
            storage(state, ordinal).pvc = Some(claimed);
            claims_changed = true;
        }
    }
    if claims_waiting {
        return blocked_progress(management, catalog, uid, state, "ClaimPending").await;
    }
    if claims_changed {
        save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let live = cluster_api.get(cluster).await?;
    check(&live, catalog, uid, cluster, state.cnpg_cluster.as_ref())?;
    let secret_name = format!("{cluster}-superuser");
    let secrets = core(access.client.clone(), Some(namespace), "Secret", "secrets");
    if let Some(secret) = secrets.get_opt(&secret_name).await? {
        if !owned_by_cluster(secret.metadata.owner_references.as_deref(), &id)
            || secret.data.pointer("/type").and_then(Value::as_str)
                != Some("kubernetes.io/basic-auth")
            || secret.metadata.uid.as_deref().is_none_or(str::is_empty)
        {
            return Err(ObserveError::Foreign);
        }
        let secret_id = ResourceIdentity {
            name: secret_name,
            uid: secret.metadata.uid.clone().ok_or(ObserveError::Foreign)?,
        };
        if state
            .credentials
            .as_ref()
            .is_some_and(|bound| bound != &secret_id)
        {
            return Err(ObserveError::Foreign);
        }
        state.credentials = Some(secret_id);
    } else if state.credentials.is_some() {
        return Err(ObserveError::Foreign);
    }
    let pod_list = core(access.client.clone(), Some(namespace), "Pod", "pods")
        .list(
            &ListParams::default()
                .labels(&format!("cnpg.io/cluster={cluster}"))
                .limit(4),
        )
        .await?;
    if pod_list.items.len() > instances as usize || pod_list.metadata.continue_.is_some() {
        return Err(ObserveError::Foreign);
    }
    state.instances = pod_list
        .items
        .iter()
        .map(|pod| {
            if !owned_by_cluster(pod.metadata.owner_references.as_deref(), &id) {
                return Err(ObserveError::Foreign);
            }
            Ok(InstanceObservation {
                name: pod.name_any(),
                uid: pod.metadata.uid.clone().ok_or(ObserveError::Foreign)?,
                role: pod
                    .metadata
                    .labels
                    .as_ref()
                    .and_then(|labels| labels.get("cnpg.io/instanceRole"))
                    .cloned()
                    .unwrap_or_else(|| "unknown".into()),
                ready: pod
                    .data
                    .pointer("/status/conditions")
                    .and_then(Value::as_array)
                    .is_some_and(|conditions| {
                        conditions.iter().any(|condition| {
                            condition.pointer("/type").and_then(Value::as_str) == Some("Ready")
                                && condition.pointer("/status").and_then(Value::as_str)
                                    == Some("True")
                        })
                    }),
            })
        })
        .collect::<Result<_, _>>()?;
    for storage in &mut state.storage {
        let pv = core(
            access.client.clone(),
            None,
            "PersistentVolume",
            "persistentvolumes",
        )
        .get(&format!("pv-{cluster}-{}", storage.ordinal))
        .await?;
        let pvc = core(
            access.client.clone(),
            Some(namespace),
            "PersistentVolumeClaim",
            "persistentvolumeclaims",
        )
        .get(&format!("{cluster}-{}", storage.ordinal))
        .await?;
        check(&pv, catalog, uid, &pv.name_any(), storage.pv.as_ref())?;
        check(&pvc, catalog, uid, &pvc.name_any(), storage.pvc.as_ref())?;
        storage.healthy = pv.data.pointer("/status/phase").and_then(Value::as_str) == Some("Bound")
            && pvc.data.pointer("/status/phase").and_then(Value::as_str) == Some("Bound")
            && storage.path.is_some();
    }
    let healthy = state.storage.iter().all(|s| s.healthy);
    let ready = live
        .data
        .pointer("/status/readyInstances")
        .and_then(Value::as_i64)
        == Some(i64::from(instances))
        && state.credentials.is_some()
        && state.instances.len() == instances as usize
        && state.instances.iter().all(|instance| instance.ready)
        && healthy;
    let generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    set_health(state, ready, generation, None);
    state.query = state
        .credentials
        .as_ref()
        .filter(|_| ready)
        .map(|secret| QueryIdentity {
            cluster_uid: id.uid.clone(),
            credential_uid: secret.uid.clone(),
        });
    state.observed_generation = generation;
    if catalog.status.as_ref().and_then(|s| s.entries.get(uid)) != Some(state) {
        save(management, catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    Ok(if ready {
        Progress::Stable
    } else {
        Progress::Waiting
    })
}

pub(super) async fn delete_exact(
    management: Client,
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    api: Api<DynamicObject>,
    desired: &DynamicObject,
    bound: Option<&ResourceIdentity>,
) -> Result<Progress, ObserveError> {
    let name = desired.name_any();
    let Some(live) = api.get_opt(&name).await? else {
        return Ok(Progress::Stable);
    };
    let Some(bound) = bound else {
        return Err(ObserveError::Foreign);
    };
    check(&live, catalog, uid, &name, Some(bound))?;
    if !matches_desired(&live, desired) {
        return Err(ObserveError::Foreign);
    }
    verify_current(management, catalog).await?;
    let params = DeleteParams {
        preconditions: Some(Preconditions {
            uid: Some(bound.uid.clone()),
            resource_version: None,
        }),
        ..Default::default()
    };
    api.delete(&name, &params).await?;
    Ok(Progress::Waiting)
}

#[expect(
    clippy::too_many_arguments,
    reason = "adoption binds the observed resource to a persisted exact intent"
)]
pub(super) async fn adopt(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    api: Api<DynamicObject>,
    desired: &DynamicObject,
    kind: &str,
    ordinal: i32,
) -> Result<bool, ObserveError> {
    let name = desired.name_any();
    if let Some(live) = api.get_opt(&name).await?
        && bound(state, kind, ordinal).is_none()
    {
        if kind != "PersistentVolumeClaim"
            && !matches!(
                intent(state, kind, &name, ordinal)?,
                Some(CreateState::Issued | CreateState::Observed)
            )
        {
            return Err(ObserveError::Foreign);
        }
        let id = check(&live, catalog, uid, &name, None)?;
        if !matches_desired(&live, desired) {
            return Err(ObserveError::Foreign);
        }
        if kind == "PersistentVolumeClaim"
            && !state.cnpg_cluster.as_ref().is_some_and(|cluster| {
                owned_by_cluster(live.metadata.owner_references.as_deref(), cluster)
            })
        {
            return Err(ObserveError::Foreign);
        }
        match kind {
            "Namespace" => state.namespace = Some(id),
            "Cluster" => state.cnpg_cluster = Some(id),
            "PersistentVolume" => storage(state, ordinal).pv = Some(id),
            "PersistentVolumeClaim" => storage(state, ordinal).pvc = Some(id),
            _ => return Err(ObserveError::Foreign),
        }
        if kind == "PersistentVolumeClaim" {
            save(management, catalog, uid, state).await?;
        } else {
            record(
                management,
                catalog,
                uid,
                state,
                kind,
                &name,
                ordinal,
                CreateState::Observed,
            )
            .await?;
        }
        return Ok(true);
    }
    Ok(false)
}

#[expect(
    clippy::too_many_arguments,
    reason = "exact finalization binds Tenant access, catalog entry and workload names"
)]
async fn finalize(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster: &str,
    state: &mut EntryStatus,
    instances: i32,
    access: &LocalAccess,
) -> Result<Progress, ObserveError> {
    set_health(
        state,
        false,
        catalog.metadata.generation.ok_or(ObserveError::Identity)?,
        Some("Deleting"),
    );
    state.phase = DatabasePhase::Deleting;
    state.query = None;
    state.observed_generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    if catalog.status.as_ref().and_then(|s| s.entries.get(uid)) != Some(state) {
        save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let catalog_uid = catalog
        .metadata
        .uid
        .as_deref()
        .ok_or(ObserveError::Identity)?
        .to_owned();
    let path_ordinals: Vec<_> = state
        .create_intents
        .iter()
        .filter(|intent| intent.kind == "Path")
        .map(|intent| intent.ordinal)
        .collect();
    for ordinal in path_ordinals {
        let expected = access
            .root
            .join("volumes/cnpg")
            .join(&catalog_uid)
            .join(uid)
            .join(ordinal.to_string());
        if local_path::inspect(&access.root, &catalog_uid, uid, ordinal)?
            && storage(state, ordinal).path.is_none()
        {
            storage(state, ordinal).path = Some(expected.to_string_lossy().into_owned());
            record(
                management.clone(),
                catalog,
                uid,
                state,
                "Path",
                &format!("{catalog_uid}/{uid}/{ordinal}"),
                ordinal,
                CreateState::Observed,
            )
            .await?;
            return Ok(Progress::Changed);
        }
    }
    let cluster_api = api(
        access.client.clone(),
        Some(namespace),
        "postgresql.cnpg.io",
        "v1",
        "Cluster",
        "clusters",
    );
    let desired_cluster =
        cnpg::cluster(catalog, uid, namespace, cluster, instances, &access.image)?;
    if adopt(
        management.clone(),
        catalog,
        uid,
        state,
        cluster_api.clone(),
        &desired_cluster,
        "Cluster",
        0,
    )
    .await?
    {
        return Ok(Progress::Changed);
    }
    if delete_exact(
        management.clone(),
        catalog,
        uid,
        cluster_api,
        &desired_cluster,
        state.cnpg_cluster.as_ref(),
    )
    .await?
        == Progress::Waiting
    {
        return Ok(Progress::Waiting);
    }
    let mut dependent_waiting = false;
    let secret_name = format!("{cluster}-superuser");
    if let Some(secret) = core(access.client.clone(), Some(namespace), "Secret", "secrets")
        .get_opt(&secret_name)
        .await?
    {
        if !state
            .cnpg_cluster
            .as_ref()
            .is_some_and(|id| owned_by_cluster(secret.metadata.owner_references.as_deref(), id))
            || secret.metadata.uid.as_deref().is_none_or(str::is_empty)
            || state
                .credentials
                .as_ref()
                .is_some_and(|id| id.uid != secret.metadata.uid.clone().unwrap_or_default())
        {
            return Err(ObserveError::Foreign);
        }
        if state.credentials.is_none() {
            state.credentials = Some(ResourceIdentity {
                name: secret_name.clone(),
                uid: secret.metadata.uid.clone().ok_or(ObserveError::Foreign)?,
            });
            save(management.clone(), catalog, uid, state).await?;
            return Ok(Progress::Changed);
        }
        let params = DeleteParams {
            preconditions: Some(Preconditions {
                uid: secret.metadata.uid,
                resource_version: None,
            }),
            ..Default::default()
        };
        verify_current(management.clone(), catalog).await?;
        core(access.client.clone(), Some(namespace), "Secret", "secrets")
            .delete(&secret_name, &params)
            .await?;
        dependent_waiting = true;
    }
    for ordinal in 1..=instances {
        let desired_claim = cnpg::claim(catalog, uid, namespace, cluster, ordinal)?;
        let claims = core(
            access.client.clone(),
            Some(namespace),
            "PersistentVolumeClaim",
            "persistentvolumeclaims",
        );
        if adopt(
            management.clone(),
            catalog,
            uid,
            state,
            claims.clone(),
            &desired_claim,
            "PersistentVolumeClaim",
            ordinal,
        )
        .await?
        {
            return Ok(Progress::Changed);
        }
        if delete_exact(
            management.clone(),
            catalog,
            uid,
            claims,
            &desired_claim,
            state
                .storage
                .iter()
                .find(|s| s.ordinal == ordinal)
                .and_then(|s| s.pvc.as_ref()),
        )
        .await?
            == Progress::Waiting
        {
            dependent_waiting = true;
        }
    }
    if dependent_waiting {
        return Ok(Progress::Waiting);
    }
    let mut volume_waiting = false;
    for ordinal in 1..=instances {
        let path = access
            .root
            .join("volumes/cnpg")
            .join(&catalog_uid)
            .join(uid)
            .join(ordinal.to_string());
        let desired_volume = cnpg::volume(
            catalog,
            uid,
            namespace,
            cluster,
            ordinal,
            access,
            path.to_str().ok_or(ObserveError::Foreign)?,
        )?;
        let volumes = core(
            access.client.clone(),
            None,
            "PersistentVolume",
            "persistentvolumes",
        );
        if adopt(
            management.clone(),
            catalog,
            uid,
            state,
            volumes.clone(),
            &desired_volume,
            "PersistentVolume",
            ordinal,
        )
        .await?
        {
            return Ok(Progress::Changed);
        }
        if delete_exact(
            management.clone(),
            catalog,
            uid,
            volumes,
            &desired_volume,
            state
                .storage
                .iter()
                .find(|s| s.ordinal == ordinal)
                .and_then(|s| s.pv.as_ref()),
        )
        .await?
            == Progress::Waiting
        {
            volume_waiting = true;
        }
    }
    if volume_waiting {
        return Ok(Progress::Waiting);
    }
    let namespaces = core(access.client.clone(), None, "Namespace", "namespaces");
    let desired_namespace = object("Namespace", namespace, None, identity(catalog, uid)?, None)?;
    if adopt(
        management.clone(),
        catalog,
        uid,
        state,
        namespaces.clone(),
        &desired_namespace,
        "Namespace",
        0,
    )
    .await?
    {
        return Ok(Progress::Changed);
    }
    if delete_exact(
        management.clone(),
        catalog,
        uid,
        namespaces,
        &desired_namespace,
        state.namespace.as_ref(),
    )
    .await?
        == Progress::Waiting
    {
        return Ok(Progress::Waiting);
    }
    let catalog_uid = catalog
        .metadata
        .uid
        .as_deref()
        .ok_or(ObserveError::Identity)?;
    for ordinal in 1..=instances {
        let expected = access
            .root
            .join("volumes/cnpg")
            .join(catalog_uid)
            .join(uid)
            .join(ordinal.to_string());
        if local_path::inspect(&access.root, catalog_uid, uid, ordinal)? {
            if state
                .storage
                .iter()
                .find(|s| s.ordinal == ordinal)
                .and_then(|s| s.path.as_deref())
                != expected.to_str()
            {
                return Err(ObserveError::Foreign);
            }
            verify_current(management.clone(), catalog).await?;
            local_path::remove(&access.root, catalog_uid, uid, ordinal)?;
        }
    }
    if !local_path::entry_absent(&access.root, catalog_uid, uid)? {
        return Err(ObserveError::Foreign);
    }
    if !all_creates_resolved(state) {
        return Ok(Progress::Waiting);
    }
    state.finalization = Some(FinalizationStatus {
        terminal_verified: true,
        verified_absent: vec![
            "cluster".into(),
            "secret".into(),
            "pvc".into(),
            "pv".into(),
            "namespace".into(),
            "path".into(),
        ],
        pending: vec![],
    });
    if catalog.status.as_ref().and_then(|s| s.entries.get(uid)) != Some(state) {
        save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    *catalog = status::remove_spec(management, catalog, uid).await?;
    Ok(Progress::Changed)
}

#[cfg(test)]
mod api_scenarios {
    use super::*;
    use crate::api::{CatalogEntry, CatalogStatus, FINALIZER, TenantDatabaseCatalogSpec};
    use axum::{
        body::Body as AxumBody,
        http::{Method, Request, Response, StatusCode},
    };
    use http_body_util::BodyExt;
    use kube::client::Body;
    use std::{
        convert::Infallible,
        sync::{Arc, Mutex},
    };
    use tower::service_fn;

    const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const ENTRY: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

    struct Mock {
        catalog: TenantDatabaseCatalog,
        pending: Option<DynamicObject>,
        persist: bool,
        creates: usize,
        deletes: usize,
    }

    #[cfg(test)]
    mod catalog_api_scenarios {
        use super::*;
        use crate::api::{
            CatalogEntry, CatalogStatus, FINALIZER, TenantDatabaseCatalogSpec,
            validate_conditional_update,
        };
        use axum::{
            body::Body as AxumBody,
            http::{Method, Request, Response, StatusCode},
        };
        use http_body_util::BodyExt;
        use kube::client::Body;
        use std::{
            collections::BTreeMap,
            convert::Infallible,
            sync::{Arc, Mutex},
        };
        use tower::service_fn;

        const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        const ENTRIES: [(&str, &str); 3] = [
            ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "orders"),
            ("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "metrics"),
            ("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "reports"),
        ];
        const REPLACEMENT: &str = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
        const CATALOG_PATH: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabasecatalogs/tenant-a";

        struct Server {
            catalog: TenantDatabaseCatalog,
            objects: BTreeMap<String, DynamicObject>,
            creates: BTreeMap<String, usize>,
            deletes: usize,
            hidden: Option<String>,
            status_conflict: bool,
            spec_conflict: bool,
            status_writes: usize,
            spec_writes: usize,
        }

        impl Server {
            fn new() -> Self {
                let mut catalog = TenantDatabaseCatalog::new(
                    "tenant-a",
                    TenantDatabaseCatalogSpec {
                        tenant_name: "tenant-a".into(),
                        tenant_uid: "tenant-uid".into(),
                        closed: false,
                        entries: ENTRIES
                            .iter()
                            .map(|(uid, name)| {
                                (
                                    (*uid).into(),
                                    CatalogEntry {
                                        name: (*name).into(),
                                        instances: 3,
                                        deleting: false,
                                    },
                                )
                            })
                            .collect(),
                    },
                );
                catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
                catalog.metadata.uid = Some(CATALOG.into());
                catalog.metadata.resource_version = Some("1".into());
                catalog.metadata.generation = Some(1);
                catalog.metadata.finalizers = Some(vec![FINALIZER.into()]);
                catalog.metadata.owner_references = Some(vec![OwnerReference {
                    api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
                    kind: "Tenant".into(),
                    name: "tenant-a".into(),
                    uid: "tenant-uid".into(),
                    ..Default::default()
                }]);
                catalog.status = Some(CatalogStatus {
                    entries: ENTRIES
                        .iter()
                        .map(|(uid, _)| ((*uid).into(), entry(uid, 1)))
                        .collect(),
                    observer: None,
                });
                Self {
                    catalog,
                    objects: BTreeMap::new(),
                    creates: BTreeMap::new(),
                    deletes: 0,
                    hidden: None,
                    status_conflict: false,
                    spec_conflict: false,
                    status_writes: 0,
                    spec_writes: 0,
                }
            }

            fn bump(&mut self) {
                let next = self
                    .catalog
                    .metadata
                    .resource_version
                    .as_deref()
                    .unwrap()
                    .parse::<u64>()
                    .unwrap()
                    + 1;
                self.catalog.metadata.resource_version = Some(next.to_string());
            }
        }

        fn client(server: Arc<Mutex<Server>>) -> Client {
            Client::new(
                service_fn(move |request: Request<Body>| {
                    let server = server.clone();
                    async move {
                        let method = request.method().clone();
                        let path = request.uri().path().to_owned();
                        let body = request.into_body().collect().await.unwrap().to_bytes();
                        let mut server = server.lock().unwrap();
                        let (code, value) = match (method.clone(), path.as_str()) {
                            (Method::GET, CATALOG_PATH) => (StatusCode::OK, json!(server.catalog)),
                            (
                                Method::GET,
                                "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a",
                            ) => (
                                StatusCode::OK,
                                json!({
                                    "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha4","kind":"Tenant",
                                    "metadata":{"name":"tenant-a","uid":"tenant-uid","finalizers":["tenancy.cnpg-vcluster.io/finalizer"]},
                                    "spec":{"provider":{"type":"local"}},
                                    "status":{"catalogCreateIntent":{"namespace":"tenant-db-tenant-a","name":"tenant-a","tenantUID":"tenant-uid"},
                                        "databaseCapability":{"namespace":"tenant-db-tenant-a","namespaceUID":"ns-uid","catalogUID":CATALOG}}
                                }),
                            ),
                            (Method::GET, "/api/v1/namespaces/tenant-db-tenant-a") => (
                                StatusCode::OK,
                                json!({"apiVersion":"v1","kind":"Namespace",
                                    "metadata":{"name":"tenant-db-tenant-a","uid":"ns-uid",
                                        "labels":{"tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid"}}}),
                            ),
                            (Method::PUT, path) if path == format!("{CATALOG_PATH}/status") => {
                                let mut candidate: TenantDatabaseCatalog =
                                    serde_json::from_slice(&body).unwrap();
                                if server.status_conflict {
                                    server.status_conflict = false;
                                    server.bump();
                                    (StatusCode::CONFLICT, failure(409))
                                } else {
                                    assert_eq!(candidate.spec, server.catalog.spec);
                                    assert_eq!(candidate.metadata.uid, server.catalog.metadata.uid);
                                    assert_eq!(
                                        candidate.metadata.resource_version,
                                        server.catalog.metadata.resource_version
                                    );
                                    server.bump();
                                    candidate.metadata.resource_version =
                                        server.catalog.metadata.resource_version.clone();
                                    server.catalog = candidate.clone();
                                    server.status_writes += 1;
                                    (StatusCode::OK, json!(candidate))
                                }
                            }
                            (Method::PUT, CATALOG_PATH) => {
                                let mut candidate: TenantDatabaseCatalog =
                                    serde_json::from_slice(&body).unwrap();
                                if server.spec_conflict {
                                    server.spec_conflict = false;
                                    server.bump();
                                    (StatusCode::CONFLICT, failure(409))
                                } else {
                                    validate_conditional_update(
                                        &server.catalog,
                                        &candidate,
                                        server.catalog.metadata.uid.as_deref().unwrap(),
                                        server
                                            .catalog
                                            .metadata
                                            .resource_version
                                            .as_deref()
                                            .unwrap(),
                                    )
                                    .unwrap();
                                    assert_eq!(candidate.status, server.catalog.status);
                                    server.bump();
                                    candidate.metadata.resource_version =
                                        server.catalog.metadata.resource_version.clone();
                                    candidate.metadata.generation =
                                        Some(server.catalog.metadata.generation.unwrap() + 1);
                                    server.catalog = candidate.clone();
                                    server.spec_writes += 1;
                                    (StatusCode::OK, json!(candidate))
                                }
                            }
                            (Method::GET, path)
                                if path.starts_with("/api/v1/namespaces/db-")
                                    && !path.contains("/persistentvolumeclaims/")
                                    || path.starts_with("/api/v1/persistentvolumes/pv-")
                                    || path.starts_with("/api/v1/namespaces/db-")
                                        && path.contains("/persistentvolumeclaims/")
                                    || path.starts_with(
                                        "/apis/postgresql.cnpg.io/v1/namespaces/db-",
                                    ) && path.contains("/clusters/") =>
                            {
                                match server
                                    .objects
                                    .get(path)
                                    .filter(|_| server.hidden.as_deref() != Some(path))
                                {
                                    Some(object) => (StatusCode::OK, json!(object)),
                                    None => (StatusCode::NOT_FOUND, failure(404)),
                                }
                            }
                            (Method::POST, path)
                                if path == "/api/v1/namespaces"
                                    || path == "/api/v1/persistentvolumes"
                                    || path.starts_with(
                                        "/apis/postgresql.cnpg.io/v1/namespaces/db-",
                                    ) && path.ends_with("/clusters") =>
                            {
                                let mut created: DynamicObject =
                                    serde_json::from_slice(&body).unwrap();
                                let name = created.name_any();
                                let key = format!("{path}/{name}");
                                assert!(
                                    !server.objects.contains_key(&key),
                                    "duplicate CREATE {key}"
                                );
                                *server.creates.entry(key.clone()).or_default() += 1;
                                created.metadata.uid =
                                    Some(format!("resource-{}", server.creates.len()));
                                created.metadata.resource_version = Some("1".into());
                                server.objects.insert(key.clone(), created.clone());
                                if server.hidden.as_deref() == Some(&key) {
                                    (StatusCode::GATEWAY_TIMEOUT, failure(504))
                                } else {
                                    (StatusCode::CREATED, json!(created))
                                }
                            }
                            (Method::DELETE, path) if server.objects.contains_key(path) => {
                                let request: Value = serde_json::from_slice(&body).unwrap();
                                assert_eq!(
                                    request["preconditions"]["uid"],
                                    server.objects[path].metadata.uid.as_deref().unwrap()
                                );
                                server.objects.remove(path);
                                server.deletes += 1;
                                (
                                    StatusCode::OK,
                                    json!({"apiVersion":"v1","kind":"Status","status":"Success","code":200}),
                                )
                            }
                            _ => panic!("unexpected API request: {method} {path}"),
                        };
                        Ok::<_, Infallible>(
                            Response::builder()
                                .status(code)
                                .header("content-type", "application/json")
                                .body(AxumBody::from(serde_json::to_vec(&value).unwrap()))
                                .unwrap(),
                        )
                    }
                }),
                "default",
            )
        }

        fn failure(code: u16) -> Value {
            let reason = if code == 404 { "NotFound" } else { "Failure" };
            json!({"apiVersion":"v1","kind":"Status","status":"Failure",
                "message":"injected failure","reason":reason,"code":code})
        }

        fn access(client: Client) -> LocalAccess {
            LocalAccess {
                client,
                root: "/var/lib/docker/volumes/tenant/_data".into(),
                worker_root: "/mnt/storage".into(),
                image: "postgres:16".into(),
            }
        }

        fn volume(
            catalog: &TenantDatabaseCatalog,
            uid: &str,
            ordinal: i32,
            access: &LocalAccess,
        ) -> DynamicObject {
            let (namespace, cluster) = ownership::names(CATALOG, uid).unwrap();
            let path = access
                .root
                .join("volumes/cnpg")
                .join(CATALOG)
                .join(uid)
                .join(ordinal.to_string());
            cnpg::volume(
                catalog,
                uid,
                &namespace,
                &cluster,
                ordinal,
                access,
                path.to_str().unwrap(),
            )
            .unwrap()
        }

        #[tokio::test]
        async fn creation_batches_each_three_instance_preparation_barrier() {
            let server = Arc::new(Mutex::new(Server::new()));
            let client = client(server.clone());
            let root = tempfile::tempdir().unwrap();
            let mut access = access(client.clone());
            access.root = root.path().to_path_buf();
            access.worker_root = root.path().join("worker");
            let uid = ENTRIES[0].0;
            let (namespace, cluster) = ownership::names(CATALOG, uid).unwrap();
            let mut catalog = server.lock().unwrap().catalog.clone();
            let mut state = catalog.status.as_ref().unwrap().entries[uid].clone();

            for expected in [
                Progress::Changed,
                Progress::Changed,
                Progress::Changed,
                Progress::Changed,
            ] {
                assert_eq!(
                    ensure(
                        client.clone(),
                        &mut catalog,
                        uid,
                        &namespace,
                        &cluster,
                        &mut state,
                        3,
                        &access,
                    )
                    .await
                    .unwrap(),
                    expected,
                );
            }
            assert_eq!(
                ensure(
                    client.clone(),
                    &mut catalog,
                    uid,
                    &namespace,
                    &cluster,
                    &mut state,
                    3,
                    &access,
                )
                .await
                .unwrap(),
                Progress::Changed,
            );
            assert_eq!(
                ensure(
                    client.clone(),
                    &mut catalog,
                    uid,
                    &namespace,
                    &cluster,
                    &mut state,
                    3,
                    &access,
                )
                .await
                .unwrap(),
                Progress::Waiting,
            );

            assert!(state.namespace.is_some());
            assert_eq!(state.storage.len(), 3);
            assert!(state.storage.iter().all(|item| item.path.is_some()));
            assert!(state.storage.iter().all(|item| item.pv.is_some()));
            assert!(state.cnpg_cluster.is_some());
            let server = server.lock().unwrap();
            assert_eq!(server.creates.len(), 5);
            assert_eq!(
                server
                    .creates
                    .keys()
                    .filter(|path| path.starts_with("/api/v1/persistentvolumes/"))
                    .count(),
                3,
            );
        }

        #[tokio::test]
        async fn three_by_three_api_create_plan_preserves_independent_statuses() {
            let server = Arc::new(Mutex::new(Server::new()));
            let client = client(server.clone());
            let access = access(client.clone());
            let mut catalog = server.lock().unwrap().catalog.clone();
            for (index, (uid, _)) in ENTRIES.iter().enumerate() {
                let mut state = catalog.status.as_ref().unwrap().entries[*uid].clone();
                let (namespace, cluster) = ownership::names(CATALOG, uid).unwrap();
                for ordinal in 1..=3 {
                    let desired = volume(&catalog, uid, ordinal, &access);
                    let id = create(
                        client.clone(),
                        &mut catalog,
                        uid,
                        &mut state,
                        core(
                            client.clone(),
                            None,
                            "PersistentVolume",
                            "persistentvolumes",
                        ),
                        desired,
                        "PersistentVolume",
                        ordinal,
                    )
                    .await
                    .unwrap()
                    .unwrap();
                    storage(&mut state, ordinal).pv = Some(id);
                    save(client.clone(), &mut catalog, uid, &state)
                        .await
                        .unwrap();
                }
                let desired_cluster =
                    cnpg::cluster(&catalog, uid, &namespace, &cluster, 3, &access.image).unwrap();
                let id = create(
                    client.clone(),
                    &mut catalog,
                    uid,
                    &mut state,
                    api(
                        client.clone(),
                        Some(&namespace),
                        "postgresql.cnpg.io",
                        "v1",
                        "Cluster",
                        "clusters",
                    ),
                    desired_cluster,
                    "Cluster",
                    0,
                )
                .await
                .unwrap()
                .unwrap();
                state.cnpg_cluster = Some(id);
                if index == 1 {
                    set_health(&mut state, true, 1, None);
                    set_health(&mut state, false, 1, Some("StoragePending"));
                } else {
                    set_health(&mut state, index == 0, 1, None);
                }
                save(client.clone(), &mut catalog, uid, &state)
                    .await
                    .unwrap();
                let latest = server.lock().unwrap().catalog.clone();
                assert_eq!(latest.status.as_ref().unwrap().entries.len(), 3);
                assert_eq!(latest.status.as_ref().unwrap().entries[*uid], state);
            }
            let server = server.lock().unwrap();
            assert_eq!(server.creates.len(), 12);
            assert!(server.creates.values().all(|count| *count == 1));
            assert_eq!(server.spec_writes, 0);
            let states = &server.catalog.status.as_ref().unwrap().entries;
            assert_eq!(states[ENTRIES[0].0].phase, DatabasePhase::Ready);
            assert_eq!(states[ENTRIES[1].0].phase, DatabasePhase::Degraded);
            assert_eq!(states[ENTRIES[2].0].phase, DatabasePhase::Progressing);
            for (uid, _) in ENTRIES {
                let (_, cluster) = ownership::names(CATALOG, uid).unwrap();
                for ordinal in 1..=3 {
                    let key = format!("/api/v1/persistentvolumes/pv-{cluster}-{ordinal}");
                    assert_eq!(server.creates[&key], 1);
                    assert_eq!(
                        server.objects[&key].data.pointer("/spec/hostPath/path"),
                        Some(&json!(format!(
                            "{}/volumes/cnpg/{CATALOG}/{uid}/{ordinal}",
                            access.worker_root.display()
                        )))
                    );
                }
                assert_eq!(states[uid].create_intents.len(), 4);
            }
        }

        #[tokio::test]
        async fn delayed_cluster_and_pv_creation_survive_restart_without_replay() {
            for kind in ["Cluster", "PersistentVolume"] {
                let server = Arc::new(Mutex::new(Server::new()));
                let client = client(server.clone());
                let access = access(client.clone());
                let uid = ENTRIES[0].0;
                let (namespace, cluster) = ownership::names(CATALOG, uid).unwrap();
                let (resource_api, desired, ordinal, key) = if kind == "Cluster" {
                    (
                        api(
                            client.clone(),
                            Some(&namespace),
                            "postgresql.cnpg.io",
                            "v1",
                            "Cluster",
                            "clusters",
                        ),
                        cnpg::cluster(
                            &server.lock().unwrap().catalog,
                            uid,
                            &namespace,
                            &cluster,
                            3,
                            &access.image,
                        )
                        .unwrap(),
                        0,
                        format!(
                            "/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters/{cluster}"
                        ),
                    )
                } else {
                    (
                        core(
                            client.clone(),
                            None,
                            "PersistentVolume",
                            "persistentvolumes",
                        ),
                        volume(&server.lock().unwrap().catalog, uid, 1, &access),
                        1,
                        format!("/api/v1/persistentvolumes/pv-{cluster}-1"),
                    )
                };
                server.lock().unwrap().hidden = Some(key.clone());
                let mut catalog = server.lock().unwrap().catalog.clone();
                let mut state = catalog.status.as_ref().unwrap().entries[uid].clone();
                assert!(matches!(
                    create(
                        client.clone(),
                        &mut catalog,
                        uid,
                        &mut state,
                        resource_api.clone(),
                        desired.clone(),
                        kind,
                        ordinal
                    )
                    .await,
                    Err(ObserveError::Api(_))
                ));
                assert_eq!(
                    server
                        .lock()
                        .unwrap()
                        .catalog
                        .status
                        .as_ref()
                        .unwrap()
                        .entries[uid]
                        .create_intents[0]
                        .state,
                    CreateState::Issued
                );
                let mut restarted = server.lock().unwrap().catalog.clone();
                let mut restored = restarted.status.as_ref().unwrap().entries[uid].clone();
                assert!(
                    create(
                        client.clone(),
                        &mut restarted,
                        uid,
                        &mut restored,
                        resource_api.clone(),
                        desired.clone(),
                        kind,
                        ordinal
                    )
                    .await
                    .unwrap()
                    .is_none()
                );
                assert_eq!(server.lock().unwrap().creates[&key], 1);
                server.lock().unwrap().hidden = None;
                let found = create(
                    client.clone(),
                    &mut restarted,
                    uid,
                    &mut restored,
                    resource_api,
                    desired,
                    kind,
                    ordinal,
                )
                .await
                .unwrap()
                .unwrap();
                assert_eq!(found.uid, "resource-1");
                assert_eq!(restored.create_intents[0].state, CreateState::Observed);
                assert_eq!(server.lock().unwrap().creates[&key], 1);
            }
        }

        #[tokio::test]
        async fn foreign_pv_path_pvc_and_same_name_replacement_block_api_deletion() {
            let server = Arc::new(Mutex::new(Server::new()));
            let client = client(server.clone());
            let access = access(client.clone());
            let uid = ENTRIES[0].0;
            let (namespace, cluster) = ownership::names(CATALOG, uid).unwrap();
            let desired = volume(&server.lock().unwrap().catalog, uid, 1, &access);
            let key = format!("/api/v1/persistentvolumes/{}", desired.name_any());
            let mut live = desired.clone();
            live.metadata.uid = Some("old-pv-uid".into());
            live.data["spec"]["hostPath"]["path"] = json!("/foreign/path");
            server
                .lock()
                .unwrap()
                .objects
                .insert(key.clone(), live.clone());
            let mut catalog = server.lock().unwrap().catalog.clone();
            let mut state = catalog.status.as_ref().unwrap().entries[uid].clone();
            state.create_intents.push(CreateIntent {
                kind: "PersistentVolume".into(),
                name: desired.name_any(),
                ordinal: 1,
                state: CreateState::Issued,
            });
            assert!(matches!(
                create(
                    client.clone(),
                    &mut catalog,
                    uid,
                    &mut state,
                    core(
                        client.clone(),
                        None,
                        "PersistentVolume",
                        "persistentvolumes"
                    ),
                    desired.clone(),
                    "PersistentVolume",
                    1
                )
                .await,
                Err(ObserveError::Foreign)
            ));
            live.data["spec"]["hostPath"]["path"] =
                desired.data["spec"]["hostPath"]["path"].clone();
            live.metadata
                .labels
                .as_mut()
                .unwrap()
                .insert(ownership::ENTRY_LABEL.into(), ENTRIES[1].0.into());
            server.lock().unwrap().objects.insert(key.clone(), live);
            assert!(matches!(
                delete_exact(
                    client.clone(),
                    &catalog,
                    uid,
                    core(
                        client.clone(),
                        None,
                        "PersistentVolume",
                        "persistentvolumes"
                    ),
                    &desired,
                    Some(&ResourceIdentity {
                        name: desired.name_any(),
                        uid: "old-pv-uid".into()
                    })
                )
                .await,
                Err(ObserveError::Foreign)
            ));
            let mut replacement = desired.clone();
            replacement.metadata.uid = Some("replacement-pv-uid".into());
            server.lock().unwrap().objects.insert(key, replacement);
            assert!(matches!(
                delete_exact(
                    client.clone(),
                    &catalog,
                    uid,
                    core(
                        client.clone(),
                        None,
                        "PersistentVolume",
                        "persistentvolumes"
                    ),
                    &desired,
                    Some(&ResourceIdentity {
                        name: desired.name_any(),
                        uid: "old-pv-uid".into()
                    })
                )
                .await,
                Err(ObserveError::Foreign)
            ));
            state.cnpg_cluster = Some(ResourceIdentity {
                name: cluster.clone(),
                uid: "cluster-uid".into(),
            });
            catalog
                .status
                .as_mut()
                .unwrap()
                .entries
                .insert(uid.into(), state.clone());
            let claim = cnpg::claim(&catalog, uid, &namespace, &cluster, 1).unwrap();
            let claim_key = format!(
                "/api/v1/namespaces/{namespace}/persistentvolumeclaims/{}",
                claim.name_any()
            );
            let mut foreign = claim.clone();
            foreign.metadata.uid = Some("pvc-uid".into());
            foreign.metadata.owner_references = Some(vec![OwnerReference {
                api_version: CNPG_CLUSTER_VERSION.into(),
                kind: "Cluster".into(),
                name: cluster,
                uid: "foreign-cluster-uid".into(),
                ..Default::default()
            }]);
            server.lock().unwrap().objects.insert(claim_key, foreign);
            assert!(matches!(
                adopt(
                    client.clone(),
                    &mut catalog,
                    uid,
                    &mut state,
                    core(
                        client.clone(),
                        Some(&namespace),
                        "PersistentVolumeClaim",
                        "persistentvolumeclaims"
                    ),
                    &claim,
                    "PersistentVolumeClaim",
                    1
                )
                .await,
                Err(ObserveError::Foreign)
            ));
            assert_eq!(server.lock().unwrap().deletes, 0);
            assert!(server.lock().unwrap().creates.is_empty());
        }

        #[tokio::test]
        async fn capacity_delete_readd_uid_prune_and_stale_cas_are_api_backed() {
            let server = Arc::new(Mutex::new(Server::new()));
            let client = client(server.clone());
            let old = ENTRIES[0].0;
            let mut catalog = server.lock().unwrap().catalog.clone();
            let mut deleted = catalog.clone();
            deleted.spec.entries.get_mut(old).unwrap().deleting = true;
            let api =
                Api::<TenantDatabaseCatalog>::namespaced(client.clone(), "tenant-db-tenant-a");
            catalog = api
                .replace("tenant-a", &PostParams::default(), &deleted)
                .await
                .unwrap();
            let mut old_state = catalog.status.as_ref().unwrap().entries[old].clone();
            old_state.phase = DatabasePhase::Deleting;
            old_state.finalization = Some(FinalizationStatus {
                terminal_verified: true,
                verified_absent: vec!["cluster".into(), "pvc".into(), "pv".into(), "path".into()],
                pending: vec![],
            });
            catalog = status::update(client.clone(), &catalog, old, Some(old_state))
                .await
                .unwrap();
            let stale = catalog.clone();
            server.lock().unwrap().spec_conflict = true;
            assert!(matches!(
                status::remove_spec(client.clone(), &catalog, old).await,
                Err(ObserveError::Api(_))
            ));
            assert_eq!(server.lock().unwrap().catalog.spec.entries.len(), 3);
            assert_eq!(server.lock().unwrap().spec_writes, 1);
            catalog = server.lock().unwrap().catalog.clone();
            catalog = status::remove_spec(client.clone(), &catalog, old)
                .await
                .unwrap();
            assert_eq!(catalog.spec.entries.len(), 2);
            assert!(catalog.status.as_ref().unwrap().entries.contains_key(old));
            let mut readded = catalog.clone();
            readded.spec.entries.insert(
                REPLACEMENT.into(),
                CatalogEntry {
                    name: "orders".into(),
                    instances: 3,
                    deleting: false,
                },
            );
            catalog = api
                .replace("tenant-a", &PostParams::default(), &readded)
                .await
                .unwrap();
            assert_eq!(catalog.spec.entries.len(), 3);
            assert!(matches!(
                status::update(client.clone(), &stale, old, None).await,
                Err(ObserveError::Identity)
            ));
            let before = server.lock().unwrap().status_writes;
            server.lock().unwrap().status_conflict = true;
            assert!(matches!(
                status::update(client.clone(), &catalog, old, None).await,
                Err(ObserveError::Api(_))
            ));
            assert_eq!(server.lock().unwrap().status_writes, before);
            catalog = server.lock().unwrap().catalog.clone();
            catalog = status::update(client.clone(), &catalog, old, None)
                .await
                .unwrap();
            assert!(!catalog.status.as_ref().unwrap().entries.contains_key(old));
            assert_eq!(catalog.status.as_ref().unwrap().entries.len(), 2);
            catalog = status::update(
                client,
                &catalog,
                REPLACEMENT,
                Some(entry(REPLACEMENT, catalog.metadata.generation.unwrap())),
            )
            .await
            .unwrap();
            let states = &catalog.status.as_ref().unwrap().entries;
            assert_eq!(states.len(), 3);
            assert_eq!(states[REPLACEMENT].phase, DatabasePhase::Pending);
            for (uid, _) in &ENTRIES[1..] {
                assert_eq!(states[*uid].logical_uid, *uid);
            }
            assert_eq!(server.lock().unwrap().spec_writes, 3);
        }
    }

    fn client(mock: Arc<Mutex<Mock>>) -> Client {
        Client::new(
            service_fn(move |request: Request<Body>| {
                let mock = mock.clone();
                async move {
                    let method = request.method().clone();
                    let path = request.uri().path().to_owned();
                    let body = request.into_body().collect().await.unwrap().to_bytes();
                    let mut state = mock.lock().unwrap();
                    let (code, response) = if path
                        == "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabasecatalogs/tenant-a"
                    {
                        (StatusCode::OK, json!(state.catalog))
                    } else if path
                        == "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabasecatalogs/tenant-a/status"
                    {
                        assert_eq!(method, Method::PUT);
                        let mut candidate: TenantDatabaseCatalog =
                            serde_json::from_slice(&body).unwrap();
                        assert_eq!(
                            candidate.metadata.resource_version,
                            state.catalog.metadata.resource_version
                        );
                        candidate.metadata.resource_version = Some(
                            (state
                                .catalog
                                .metadata
                                .resource_version
                                .as_deref()
                                .unwrap()
                                .parse::<u64>()
                                .unwrap()
                                + 1)
                            .to_string(),
                        );
                        state.catalog = candidate.clone();
                        (StatusCode::OK, json!(candidate))
                    } else if path == "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a" {
                        (
                            StatusCode::OK,
                            json!({
                                "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha4","kind":"Tenant",
                                "metadata":{"name":"tenant-a","uid":"tenant-uid","finalizers":["tenancy.cnpg-vcluster.io/finalizer"]},
                                "spec":{"provider":{"type":"local"}},
                                "status":{"catalogCreateIntent":{"namespace":"tenant-db-tenant-a","name":"tenant-a","tenantUID":"tenant-uid"},
                                    "databaseCapability":{"namespace":"tenant-db-tenant-a","namespaceUID":"ns-uid","catalogUID":CATALOG}}
                            }),
                        )
                    } else if path == "/api/v1/namespaces/tenant-db-tenant-a" {
                        (
                            StatusCode::OK,
                            json!({"apiVersion":"v1","kind":"Namespace",
                        "metadata":{"name":"tenant-db-tenant-a","uid":"ns-uid","labels":{"tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid"}}}),
                        )
                    } else if path.starts_with("/api/v1/namespaces/db-") && method == Method::GET {
                        if state.persist {
                            let created = state.pending.as_ref().unwrap();
                            (StatusCode::OK, json!(created))
                        } else {
                            (
                                StatusCode::NOT_FOUND,
                                json!({"kind":"Status","apiVersion":"v1",
                            "status":"Failure","message":"not found","reason":"NotFound","code":404}),
                            )
                        }
                    } else if path.starts_with("/api/v1/namespaces/db-") && method == Method::DELETE
                    {
                        let request: Value = serde_json::from_slice(&body).unwrap();
                        assert_eq!(request["preconditions"]["uid"], "created-uid");
                        state.deletes += 1;
                        state.persist = false;
                        (
                            StatusCode::OK,
                            json!({"kind":"Status","apiVersion":"v1","status":"Success","code":200}),
                        )
                    } else if path.starts_with("/api/v1/persistentvolumes/pv-")
                        && method == Method::GET
                    {
                        (StatusCode::OK, json!(state.pending))
                    } else if path == "/api/v1/namespaces" && method == Method::POST {
                        state.creates += 1;
                        let mut object: DynamicObject = serde_json::from_slice(&body).unwrap();
                        object.metadata.uid = Some("created-uid".into());
                        state.pending = Some(object);
                        (
                            StatusCode::GATEWAY_TIMEOUT,
                            json!({"kind":"Status","apiVersion":"v1",
                        "status":"Failure","message":"timed out","reason":"Timeout","code":504}),
                        )
                    } else {
                        panic!("unexpected API request: {method} {path}");
                    };
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(code)
                            .header("content-type", "application/json")
                            .body(AxumBody::from(serde_json::to_vec(&response).unwrap()))
                            .unwrap(),
                    )
                }
            }),
            "default",
        )
    }

    #[tokio::test]
    async fn post_timeout_create_requires_explicit_replay_or_observation() {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant-uid".into(),
                closed: false,
                entries: [(
                    ENTRY.into(),
                    CatalogEntry {
                        name: "orders".into(),
                        instances: 1,
                        deleting: false,
                    },
                )]
                .into(),
            },
        );
        catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
        catalog.metadata.uid = Some(CATALOG.into());
        catalog.metadata.resource_version = Some("1".into());
        catalog.metadata.generation = Some(1);
        catalog.metadata.finalizers = Some(vec![FINALIZER.into()]);
        catalog.metadata.owner_references = Some(vec![
            k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference {
                api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
                kind: "Tenant".into(),
                name: "tenant-a".into(),
                uid: "tenant-uid".into(),
                ..Default::default()
            },
        ]);
        catalog.status = Some(CatalogStatus {
            entries: [(ENTRY.into(), entry(ENTRY, 1))].into(),
            observer: None,
        });
        let mock = Arc::new(Mutex::new(Mock {
            catalog: catalog.clone(),
            pending: None,
            persist: false,
            creates: 0,
            deletes: 0,
        }));
        let client = client(mock.clone());
        let (name, _) = ownership::names(CATALOG, ENTRY).unwrap();
        let desired = object(
            "Namespace",
            &name,
            None,
            identity(&catalog, ENTRY).unwrap(),
            None,
        )
        .unwrap();
        let mut state = entry(ENTRY, 1);
        assert!(
            create(
                client.clone(),
                &mut catalog,
                ENTRY,
                &mut state,
                core(client.clone(), None, "Namespace", "namespaces"),
                desired.clone(),
                "Namespace",
                0
            )
            .await
            .is_err()
        );
        assert_eq!(state.create_intents[0].state, CreateState::Issued);
        assert_eq!(mock.lock().unwrap().creates, 1);
        let mut restarted = mock.lock().unwrap().catalog.clone();
        let mut state = restarted.status.as_ref().unwrap().entries[ENTRY].clone();
        let missing = create(
            client.clone(),
            &mut restarted,
            ENTRY,
            &mut state,
            core(client.clone(), None, "Namespace", "namespaces"),
            desired.clone(),
            "Namespace",
            0,
        )
        .await
        .unwrap();
        assert!(missing.is_none());
        assert_eq!(mock.lock().unwrap().creates, 1);
        assert!(
            create_with_replay(
                client.clone(),
                &mut restarted,
                ENTRY,
                &mut state,
                core(client.clone(), None, "Namespace", "namespaces"),
                desired.clone(),
                "Namespace",
                0,
                true,
            )
            .await
            .is_err()
        );
        assert_eq!(state.create_intents[0].state, CreateState::Issued);
        assert_eq!(mock.lock().unwrap().creates, 2);
        mock.lock().unwrap().persist = true;
        let present = create(
            client.clone(),
            &mut restarted,
            ENTRY,
            &mut state,
            core(client.clone(), None, "Namespace", "namespaces"),
            desired,
            "Namespace",
            0,
        )
        .await
        .unwrap();
        assert_eq!(present.unwrap().uid, "created-uid");
        assert_eq!(state.create_intents[0].state, CreateState::Observed);
        assert_eq!(mock.lock().unwrap().creates, 2);
    }

    #[tokio::test]
    async fn exact_uid_precondition_blocks_replaced_namespace_and_waits_for_absence() {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant-uid".into(),
                closed: true,
                entries: [(
                    ENTRY.into(),
                    CatalogEntry {
                        name: "orders".into(),
                        instances: 1,
                        deleting: true,
                    },
                )]
                .into(),
            },
        );
        catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
        catalog.metadata.uid = Some(CATALOG.into());
        catalog.metadata.resource_version = Some("1".into());
        catalog.metadata.generation = Some(1);
        catalog.metadata.finalizers = Some(vec![FINALIZER.into()]);
        catalog.metadata.owner_references = Some(vec![
            k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference {
                api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
                kind: "Tenant".into(),
                name: "tenant-a".into(),
                uid: "tenant-uid".into(),
                ..Default::default()
            },
        ]);
        catalog.status = Some(CatalogStatus {
            entries: [(ENTRY.into(), entry(ENTRY, 1))].into(),
            observer: None,
        });
        let (namespace, _) = ownership::names(CATALOG, ENTRY).unwrap();
        let mut live = object(
            "Namespace",
            &namespace,
            None,
            identity(&catalog, ENTRY).unwrap(),
            None,
        )
        .unwrap();
        live.metadata.uid = Some("created-uid".into());
        let mock = Arc::new(Mutex::new(Mock {
            catalog: catalog.clone(),
            pending: Some(live),
            persist: true,
            creates: 0,
            deletes: 0,
        }));
        let client = client(mock.clone());
        let desired = object(
            "Namespace",
            &namespace,
            None,
            identity(&catalog, ENTRY).unwrap(),
            None,
        )
        .unwrap();
        assert!(
            delete_exact(
                client.clone(),
                &catalog,
                ENTRY,
                core(client.clone(), None, "Namespace", "namespaces"),
                &desired,
                Some(&ResourceIdentity {
                    name: namespace.clone(),
                    uid: "replaced-uid".into()
                })
            )
            .await
            .is_err()
        );
        assert_eq!(mock.lock().unwrap().deletes, 0);
        let bound = ResourceIdentity {
            name: namespace.clone(),
            uid: "created-uid".into(),
        };
        assert_eq!(
            delete_exact(
                client.clone(),
                &catalog,
                ENTRY,
                core(client.clone(), None, "Namespace", "namespaces"),
                &desired,
                Some(&bound)
            )
            .await
            .unwrap(),
            Progress::Waiting,
        );
        assert_eq!(mock.lock().unwrap().deletes, 1);
        assert_eq!(
            delete_exact(
                client.clone(),
                &catalog,
                ENTRY,
                core(client, None, "Namespace", "namespaces"),
                &desired,
                Some(&bound)
            )
            .await
            .unwrap(),
            Progress::Stable,
        );
    }

    #[tokio::test]
    async fn foreign_pv_path_blocks_adoption_and_deletion_even_with_matching_uid_labels() {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant-uid".into(),
                closed: true,
                entries: [(
                    ENTRY.into(),
                    CatalogEntry {
                        name: "orders".into(),
                        instances: 1,
                        deleting: true,
                    },
                )]
                .into(),
            },
        );
        catalog.metadata.uid = Some(CATALOG.into());
        catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
        let mut state = entry(ENTRY, 1);
        let (namespace, cluster) = ownership::names(CATALOG, ENTRY).unwrap();
        let root = std::path::PathBuf::from("/var/lib/docker/volumes/tenant/_data");
        let path = root
            .join("volumes/cnpg")
            .join(CATALOG)
            .join(ENTRY)
            .join("1");
        state.create_intents.push(CreateIntent {
            kind: "PersistentVolume".into(),
            name: format!("pv-{cluster}-1"),
            ordinal: 1,
            state: CreateState::Issued,
        });
        catalog.status = Some(CatalogStatus {
            entries: [(ENTRY.into(), state.clone())].into(),
            observer: None,
        });
        let mock = Arc::new(Mutex::new(Mock {
            catalog: catalog.clone(),
            pending: None,
            persist: true,
            creates: 0,
            deletes: 0,
        }));
        let client = client(mock.clone());
        let access = LocalAccess {
            client: client.clone(),
            root,
            worker_root: "/mnt/storage".into(),
            image: "postgres".into(),
        };
        let desired = cnpg::volume(
            &catalog,
            ENTRY,
            &namespace,
            &cluster,
            1,
            &access,
            path.to_str().unwrap(),
        )
        .unwrap();
        let mut foreign = desired.clone();
        foreign.metadata.uid = Some("pv-uid".into());
        foreign.data["spec"]["hostPath"]["path"] = "/foreign".into();
        mock.lock().unwrap().pending = Some(foreign);
        assert!(
            adopt(
                client.clone(),
                &mut catalog,
                ENTRY,
                &mut state,
                core(
                    client.clone(),
                    None,
                    "PersistentVolume",
                    "persistentvolumes"
                ),
                &desired,
                "PersistentVolume",
                1
            )
            .await
            .is_err()
        );
        assert!(
            delete_exact(
                client.clone(),
                &catalog,
                ENTRY,
                core(client, None, "PersistentVolume", "persistentvolumes"),
                &desired,
                Some(&ResourceIdentity {
                    name: desired.name_any(),
                    uid: "pv-uid".into()
                }),
            )
            .await
            .is_err()
        );
        assert_eq!(mock.lock().unwrap().deletes, 0);
    }
}
