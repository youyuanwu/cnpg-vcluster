use kube::{
    Client,
    api::{DeleteParams, Preconditions},
};

use crate::{
    api::{CreateState, EntryStatus, TenantDatabaseCatalog},
    finalize::local::all_creates_resolved,
    reconcile::{
        ObserveError,
        azure::{
            Access, arm_disk, arm_id, disk, disk_api, disk_identity, disk_name, storage, tags,
        },
        local, verify_current,
    },
    status,
};

pub(crate) async fn cleanup(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    cluster_name: &str,
    state: &mut EntryStatus,
    instances: i32,
    access: &Access,
) -> Result<bool, ObserveError> {
    let disks = disk_api(management.clone(), &access.storage_namespace);
    for ordinal in 1..=instances {
        let name = disk_name(cluster_name, ordinal);
        let expected_arm = arm_id(&access.group_id, &name);
        let Some(item) = state.storage.iter().find(|s| s.ordinal == ordinal) else {
            if access.arm()?.request(&expected_arm, false).await?.is_some() {
                return Err(ObserveError::Foreign);
            }
            if record_absence(state, &expected_arm)? {
                local::save(management.clone(), catalog, uid, state).await?;
                return Ok(true);
            }
            continue;
        };
        if item.arm_id.as_deref() != Some(expected_arm.as_str()) {
            return Err(ObserveError::Foreign);
        }
        let previous = local::intent(state, "Disk", &name, ordinal)?;
        let desired = disk(catalog, uid, access, &name)?;
        if let Some(live) = disks.get_opt(&name).await? {
            if !matches!(previous, Some(CreateState::Issued | CreateState::Observed)) {
                return Err(ObserveError::Foreign);
            }
            let id = disk_identity(
                &live,
                &desired,
                catalog,
                uid,
                item.disk.as_ref(),
                &expected_arm,
            )?;
            if item.disk.is_none() || previous != Some(CreateState::Observed) {
                storage(state, ordinal).disk = Some(id);
                local::record(
                    management.clone(),
                    catalog,
                    uid,
                    state,
                    "Disk",
                    &name,
                    ordinal,
                    CreateState::Observed,
                )
                .await?;
                return Ok(true);
            }
            verify_current(management.clone(), catalog).await?;
            disks
                .delete(
                    &name,
                    &DeleteParams {
                        preconditions: Some(Preconditions {
                            uid: Some(id.uid),
                            resource_version: None,
                        }),
                        ..Default::default()
                    },
                )
                .await?;
            return Ok(false);
        }
        if matches!(previous, Some(CreateState::Planned | CreateState::Issued)) {
            return local::blocked(management, catalog, uid, state, "UnknownCreateOutcome").await;
        }
        if let Some(actual) = access.arm()?.request(&expected_arm, false).await? {
            arm_disk(
                &actual,
                &expected_arm,
                &tags(catalog, uid)?,
                &access.location,
            )?;
            if previous != Some(CreateState::Observed) || item.disk.is_none() {
                return Err(ObserveError::Foreign);
            }
            verify_current(management.clone(), catalog).await?;
            access.arm()?.request(&expected_arm, true).await?;
            return Ok(false);
        }
        if record_absence(state, &expected_arm)? {
            local::save(management.clone(), catalog, uid, state).await?;
            return Ok(true);
        }
    }
    if !all_creates_resolved(state) {
        return local::blocked(management, catalog, uid, state, "UnknownCreateOutcome").await;
    }
    let finalization = state.finalization.as_mut().ok_or(ObserveError::Foreign)?;
    if !finalization.pending.is_empty() || finalization.verified_absent.len() != instances as usize
    {
        return Err(ObserveError::Foreign);
    }
    finalization.terminal_verified = true;
    if catalog.status.as_ref().and_then(|s| s.entries.get(uid)) != Some(state) {
        local::save(management.clone(), catalog, uid, state).await?;
        return Ok(true);
    }
    *catalog = status::remove_spec(management, catalog, uid).await?;
    Ok(true)
}

fn record_absence(state: &mut EntryStatus, expected_arm: &str) -> Result<bool, ObserveError> {
    let finalization = state.finalization.as_mut().ok_or(ObserveError::Foreign)?;
    if finalization.pending.iter().any(|id| id == expected_arm) {
        finalization.pending.retain(|id| id != expected_arm);
        finalization.verified_absent.push(expected_arm.into());
        Ok(true)
    } else if finalization
        .verified_absent
        .iter()
        .any(|id| id == expected_arm)
    {
        Ok(false)
    } else {
        Err(ObserveError::Foreign)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{DatabasePhase, FinalizationStatus};
    use std::collections::BTreeSet;

    #[test]
    fn nine_expected_ids_require_nine_distinct_direct_absence_receipts() {
        let group = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/pg-rg";
        let clusters = ["pg-first", "pg-second", "pg-third"];
        let expected: Vec<_> = clusters
            .iter()
            .flat_map(|cluster| (1..=3).map(|ordinal| arm_id(group, &disk_name(cluster, ordinal))))
            .collect();
        assert_eq!(expected.iter().collect::<BTreeSet<_>>().len(), 9);
        let mut states: Vec<EntryStatus> = clusters
            .iter()
            .map(|_| EntryStatus {
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
                create_intents: vec![],
                finalization: None,
            })
            .collect();
        for (index, state) in states.iter_mut().enumerate() {
            state.finalization = Some(FinalizationStatus {
                terminal_verified: false,
                pending: expected[index * 3..index * 3 + 3].to_vec(),
                verified_absent: vec![],
            });
            assert!(record_absence(state, &expected[index * 3]).unwrap());
            assert!(!record_absence(state, &expected[index * 3]).unwrap());
            assert!(record_absence(state, &expected[(index + 1) * 3 % 9]).is_err());
            assert!(record_absence(state, &expected[index * 3 + 1]).unwrap());
            assert!(record_absence(state, &expected[index * 3 + 2]).unwrap());
            let proof = state.finalization.as_ref().unwrap();
            assert!(proof.pending.is_empty());
            assert_eq!(proof.verified_absent.len(), 3);
        }
        assert_eq!(
            states
                .iter()
                .map(|s| s.finalization.as_ref().unwrap().verified_absent.len())
                .sum::<usize>(),
            9
        );
    }
}
