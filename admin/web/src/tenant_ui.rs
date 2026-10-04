use tenant_admin_shared::query::{
    LifecycleStage, LifecycleStageState, SectionAvailability, TenantSnapshot,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TenantContext {
    pub name: String,
    pub uid: String,
    pub classification: String,
    pub provider: String,
    pub kubernetes_version: String,
    pub age: String,
    pub observed_at: String,
}

pub fn section_available(section: &SectionAvailability) -> bool {
    matches!(section, SectionAvailability::Available)
}

pub const fn lifecycle_stage_label(stage: LifecycleStage) -> &'static str {
    match stage {
        LifecycleStage::RequestAccepted => "Request accepted",
        LifecycleStage::Infrastructure => "Infrastructure",
        LifecycleStage::ControlPlane => "Control plane",
        LifecycleStage::Workers => "Workers",
        LifecycleStage::AddOns => "Add-ons",
        LifecycleStage::Databases => "Databases",
        LifecycleStage::Ready => "Ready",
    }
}

pub const fn lifecycle_state_label(state: LifecycleStageState) -> &'static str {
    match state {
        LifecycleStageState::Completed => "Completed",
        LifecycleStageState::Current => "Current",
        LifecycleStageState::Blocked => "Blocked",
        LifecycleStageState::Pending => "Pending",
        LifecycleStageState::NotApplicable => "Not applicable",
        LifecycleStageState::Unknown => "Unknown",
    }
}

pub fn resource_target_exists(snapshot: &TenantSnapshot, target: Option<&str>) -> bool {
    target.is_some_and(|target| snapshot.topology.nodes.iter().any(|node| node.id == target))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lifecycle_labels_are_explicit() {
        assert_eq!(
            lifecycle_stage_label(LifecycleStage::RequestAccepted),
            "Request accepted"
        );
        assert_eq!(
            lifecycle_state_label(LifecycleStageState::NotApplicable),
            "Not applicable"
        );
    }

    #[test]
    fn unavailable_sections_are_not_empty_success() {
        assert!(section_available(&SectionAvailability::Available));
        assert!(!section_available(&SectionAvailability::Unavailable {
            code: "unavailable".into(),
            message: "failed".into(),
            retryable: true,
        }));
    }
}
