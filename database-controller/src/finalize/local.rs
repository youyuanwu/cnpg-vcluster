use crate::api::{CreateState, EntryStatus};

pub fn all_creates_resolved(state: &EntryStatus) -> bool {
    state
        .create_intents
        .iter()
        .all(|intent| match intent.state {
            CreateState::Planned | CreateState::Issued => false,
            CreateState::Rejected => true,
            CreateState::Observed => match intent.kind.as_str() {
                "Namespace" => state
                    .namespace
                    .as_ref()
                    .is_some_and(|id| id.name == intent.name),
                "Cluster" => state
                    .cnpg_cluster
                    .as_ref()
                    .is_some_and(|id| id.name == intent.name),
                "Path" => state.storage.iter().any(|s| {
                    s.ordinal == intent.ordinal
                        && s.path
                            .as_ref()
                            .is_some_and(|p| p.ends_with(&format!("/{}", intent.name)))
                }),
                "PersistentVolume" => state.storage.iter().any(|s| {
                    s.ordinal == intent.ordinal
                        && s.pv.as_ref().is_some_and(|id| id.name == intent.name)
                }),
                "PersistentVolumeClaim" => state.storage.iter().any(|s| {
                    s.ordinal == intent.ordinal
                        && s.pvc.as_ref().is_some_and(|id| id.name == intent.name)
                }),
                "Disk" => state.storage.iter().any(|s| {
                    s.ordinal == intent.ordinal
                        && s.disk.as_ref().is_some_and(|id| id.name == intent.name)
                        && s.arm_id.is_some()
                }),
                _ => false,
            },
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{CreateIntent, DatabasePhase};

    fn state() -> EntryStatus {
        EntryStatus {
            logical_uid: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into(),
            observed_generation: 1,
            phase: DatabasePhase::Deleting,
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
    fn unknown_create_outcomes_never_release_capacity() {
        let mut state = state();
        state.create_intents.push(CreateIntent {
            kind: "Cluster".into(),
            name: "pg-a".into(),
            ordinal: 0,
            state: CreateState::Planned,
        });
        assert!(!all_creates_resolved(&state));
        state.create_intents[0].state = CreateState::Issued;
        assert!(!all_creates_resolved(&state));
        state.create_intents[0].state = CreateState::Observed;
        assert!(!all_creates_resolved(&state));
        state.cnpg_cluster = Some(crate::api::ResourceIdentity {
            name: "pg-a".into(),
            uid: "cluster-uid".into(),
        });
        assert!(all_creates_resolved(&state));
        state.create_intents.push(CreateIntent {
            kind: "Path".into(),
            name: "catalog/entry/1".into(),
            ordinal: 1,
            state: CreateState::Issued,
        });
        assert!(!all_creates_resolved(&state));
    }
}
