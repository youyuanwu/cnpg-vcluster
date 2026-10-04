#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RefreshIntent {
    SnapshotOnly,
    CatalogOnly,
    All,
    MutationCommitted,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RefreshEffects {
    pub snapshot: bool,
    pub catalog: bool,
    pub preserve_catalog_lock: bool,
}

pub const fn refresh_effects(intent: RefreshIntent) -> RefreshEffects {
    match intent {
        RefreshIntent::SnapshotOnly => RefreshEffects {
            snapshot: true,
            catalog: false,
            preserve_catalog_lock: true,
        },
        RefreshIntent::CatalogOnly => RefreshEffects {
            snapshot: false,
            catalog: true,
            preserve_catalog_lock: false,
        },
        RefreshIntent::All => RefreshEffects {
            snapshot: true,
            catalog: true,
            preserve_catalog_lock: false,
        },
        RefreshIntent::MutationCommitted => RefreshEffects {
            snapshot: true,
            catalog: false,
            preserve_catalog_lock: true,
        },
    }
}

pub fn exact_confirmation_enabled(
    expected: &str,
    observed: &str,
    running: bool,
    locked: bool,
) -> bool {
    !running && !locked && !expected.is_empty() && expected == observed
}

pub fn unsafe_operation_enabled(
    identity_current: bool,
    resource_ready: bool,
    busy: bool,
    locked: bool,
) -> bool {
    identity_current && resource_ready && !busy && !locked
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SelectionRefresh {
    Retained,
    ClearedMissing,
    None,
}

pub fn selection_after_refresh(
    selected: Option<&str>,
    current_ids: impl IntoIterator<Item = impl AsRef<str>>,
) -> SelectionRefresh {
    let Some(selected) = selected else {
        return SelectionRefresh::None;
    };
    if current_ids
        .into_iter()
        .any(|identity| identity.as_ref() == selected)
    {
        SelectionRefresh::Retained
    } else {
        SelectionRefresh::ClearedMissing
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refresh_ownership_preserves_uncertain_mutation_lock() {
        assert_eq!(
            refresh_effects(RefreshIntent::MutationCommitted),
            RefreshEffects {
                snapshot: true,
                catalog: false,
                preserve_catalog_lock: true,
            }
        );
        assert_eq!(
            refresh_effects(RefreshIntent::All),
            RefreshEffects {
                snapshot: true,
                catalog: true,
                preserve_catalog_lock: false,
            }
        );
    }

    #[test]
    fn destructive_and_unsafe_controls_require_exact_current_state() {
        assert!(exact_confirmation_enabled(
            "tenant-a", "tenant-a", false, false
        ));
        assert!(!exact_confirmation_enabled(
            "tenant-a", "tenant-b", false, false
        ));
        assert!(!exact_confirmation_enabled(
            "tenant-a", "tenant-a", true, false
        ));
        assert!(unsafe_operation_enabled(true, true, false, false));
        assert!(!unsafe_operation_enabled(true, true, false, true));
    }

    #[test]
    fn selection_refresh_never_retargets_a_missing_identity() {
        assert_eq!(
            selection_after_refresh(Some("resource:a"), ["resource:a", "resource:b"]),
            SelectionRefresh::Retained
        );
        assert_eq!(
            selection_after_refresh(Some("resource:a"), ["resource:b"]),
            SelectionRefresh::ClearedMissing
        );
    }
}
