use std::collections::{BTreeMap, BTreeSet};

use kube::{ResourceExt, core::DynamicObject};
use tenant_admin_shared::query::{
    AzureBindingView, AzureManagementView, AzureNodeView, AzureProviderView, AzureResourceView,
    AzureWorkerPoolView, ConditionStatus, DisplayAttribute, LocalAllocationView, LocalProviderView,
    ManagementResourceView, ProviderMode, ProviderSpecificationView, ProviderStatusView,
    ResourceIdentityView, TenantBlocker, TenantClassification, TenantCondition, TenantDetail,
    TenantProvider, TenantSpecificationView, TenantSummary, TopologyEdge, TopologyEdgeKind,
    TopologyGraph, TopologyHealth, TopologyNode, TopologyNodeKind, UnknownProviderView,
};
use tenant_controller::{
    api::{
        SUPPORTED_KUBERNETES_VERSION, Tenant, TenantPhase, TenantProviderSpec,
        TenantProviderStatus, canonical_spec, spec_hash,
    },
    management::{
        AZURE_MANAGEMENT_RESOURCES, MANAGEMENT_RESOURCES, ManagementResource, ResourceClass,
    },
    ownership::{
        FOUNDATION_ANNOTATION, RESOURCE_ANNOTATION, SPEC_HASH_ANNOTATION, TENANT_ANNOTATION,
        TENANT_UID_ANNOTATION, validate_owner_chain, validate_provider_owner,
    },
    sanitize,
};

const MAX_CONDITIONS: usize = 64;
const MAX_BLOCKERS: usize = 32;
const MAX_TEXT: usize = 512;
const MAX_IDENTITY: usize = 256;

const AZURE_TENANT_ANNOTATION: &str = "lifecycle.cnpg-vcluster.capi/tenant";
const AZURE_PROFILE_ANNOTATION: &str = "lifecycle.cnpg-vcluster.capi/profile";
const AZURE_SPEC_ANNOTATION: &str = "lifecycle.cnpg-vcluster.capi/specification-sha256";
const AZURE_FOUNDATION_ANNOTATION: &str = "lifecycle.cnpg-vcluster.capi/foundation-sha256";
const AZURE_OPERATION_ANNOTATION: &str = "lifecycle.cnpg-vcluster.capi/operation-id";

#[derive(Clone, Debug)]
pub struct TenantProjection {
    pub summary: TenantSummary,
    pub detail: TenantDetail,
    pub topology: TopologyGraph,
}

pub fn classify_tenant(mode: ProviderMode, tenant: &Tenant) -> TenantClassification {
    if tenant.metadata.deletion_timestamp.is_some() {
        return TenantClassification::Deleting;
    }
    let generation = tenant.metadata.generation;
    let status = tenant.status.as_ref();
    let phase = status.and_then(|status| status.phase);
    let current =
        generation.is_some() && status.and_then(|status| status.observed_generation) == generation;
    let ready = status
        .and_then(|status| {
            status
                .conditions
                .iter()
                .find(|condition| condition.type_ == "Ready")
        })
        .is_some_and(|condition| {
            condition.status == "True" && condition.observed_generation == generation
        });
    if current && ready && phase == Some(TenantPhase::Ready) {
        return if provider_status_matches(tenant) {
            TenantClassification::Ready
        } else {
            TenantClassification::OwnershipInvalid
        };
    }
    if mode == ProviderMode::Azure && !current {
        return TenantClassification::Progressing;
    }
    match phase {
        None | Some(TenantPhase::Pending | TenantPhase::Progressing) => {
            TenantClassification::Progressing
        }
        Some(TenantPhase::Ready | TenantPhase::Degraded) => TenantClassification::Degraded,
        Some(TenantPhase::Deleting) => TenantClassification::Deleting,
        Some(TenantPhase::Failed) => TenantClassification::Failed,
        Some(TenantPhase::OwnershipInvalid) => TenantClassification::OwnershipInvalid,
    }
}

pub fn project_summary(mode: ProviderMode, tenant: &Tenant) -> TenantSummary {
    let provider = provider(&tenant.spec.provider);
    let status = tenant.status.as_ref();
    let endpoint = status.and_then(|status| match status.provider.as_ref() {
        Some(TenantProviderStatus::Local(local)) => local
            .allocation
            .as_ref()
            .map(|allocation| bounded(&allocation.endpoint, MAX_TEXT)),
        Some(TenantProviderStatus::Azure(_)) => trusted_azure_status(tenant)
            .and_then(|azure| azure.endpoint.as_deref())
            .map(|value| bounded(value, MAX_TEXT)),
        None => None,
    });
    TenantSummary {
        name: bounded(&tenant.name_any(), MAX_IDENTITY),
        provider,
        classification: classify_tenant(mode, tenant),
        kubernetes_version: bounded(&tenant.spec.kubernetes_version, MAX_IDENTITY),
        requested_workers: nonnegative(tenant.spec.workers),
        requested_databases: match tenant.spec.provider {
            TenantProviderSpec::Local { databases } => Some(nonnegative(databases)),
            TenantProviderSpec::Azure { .. } => None,
        },
        endpoint,
        created_at: tenant
            .metadata
            .creation_timestamp
            .as_ref()
            .map(|timestamp| timestamp.0.to_string()),
        conditions: projected_conditions(tenant),
    }
}

fn trusted_azure_status(tenant: &Tenant) -> Option<&tenant_controller::api::AzureProviderStatus> {
    if !matches!(tenant.spec.provider, TenantProviderSpec::Azure { .. }) {
        return None;
    }
    let tenant_uid = tenant
        .metadata
        .uid
        .as_deref()
        .filter(|value| !value.is_empty())?;
    let status = tenant.status.as_ref()?.azure()?;
    status
        .binding
        .as_ref()
        .is_some_and(|binding| binding.tenant_uid == tenant_uid)
        .then_some(status)
}

impl TenantProjection {
    pub fn new(mode: ProviderMode, tenant: Tenant, resources: Vec<DynamicObject>) -> Self {
        let summary = project_summary(mode, &tenant);
        let accepted = accepted_resources(mode, &tenant, &resources);
        let management_resources = accepted
            .iter()
            .map(|resource| management_resource_view(resource.object, resource.definition))
            .collect();
        let provider_status = provider_status(&tenant);
        let detail = TenantDetail {
            summary: summary.clone(),
            uid: tenant
                .metadata
                .uid
                .as_deref()
                .map_or_else(String::new, |value| bounded(value, MAX_IDENTITY)),
            generation: tenant.metadata.generation.unwrap_or_default(),
            observed_generation: tenant
                .status
                .as_ref()
                .and_then(|status| status.observed_generation),
            specification: specification(&tenant),
            provider_status,
            blockers: blockers(mode, &tenant),
            management_resources,
        };
        let topology = topology(&tenant, &summary, &accepted);
        Self {
            summary,
            detail,
            topology,
        }
    }
}

#[derive(Clone, Copy)]
struct AcceptedResource<'a> {
    object: &'a DynamicObject,
    definition: ManagementResource,
}

fn accepted_resources<'a>(
    mode: ProviderMode,
    tenant: &Tenant,
    inventory: &'a [DynamicObject],
) -> Vec<AcceptedResource<'a>> {
    let mut accepted = match mode {
        ProviderMode::Local => accepted_local(tenant, inventory),
        ProviderMode::Azure => accepted_azure(tenant, inventory),
    };
    accepted.sort_by(compare_resources);
    accepted
}

fn accepted_local<'a>(
    tenant: &Tenant,
    inventory: &'a [DynamicObject],
) -> Vec<AcceptedResource<'a>> {
    let name = tenant.name_any();
    let Some(tenant_uid) = tenant
        .metadata
        .uid
        .as_deref()
        .filter(|value| !value.is_empty())
    else {
        return Vec::new();
    };
    let Ok(canonical) = canonical_spec(&name, &tenant.spec, SUPPORTED_KUBERNETES_VERSION) else {
        return Vec::new();
    };
    let specification_hash = spec_hash(&canonical);
    let Some(foundation_hash) = tenant
        .status
        .as_ref()
        .and_then(|status| status.local())
        .and_then(|status| status.foundation_hash.as_deref())
        .filter(|value| !value.is_empty())
    else {
        return Vec::new();
    };
    let recorded_cluster_uid = tenant
        .status
        .as_ref()
        .and_then(|status| status.cluster_uid());
    let mut roots = Vec::new();
    for object in inventory {
        let Some(definition) = definition_for(MANAGEMENT_RESOURCES, object) else {
            continue;
        };
        if definition.kind == "Secret"
            || definition.class == ResourceClass::Descendant
            || !expected_name_matches(definition, &name, object)
            || !local_markers_match(
                object,
                definition,
                &name,
                tenant_uid,
                &specification_hash,
                foundation_hash,
            )
            || object.owner_references().iter().any(|owner| {
                owner.kind == "Tenant" && owner.api_version == "tenancy.cnpg-vcluster.io/v1alpha2"
            })
            || (definition.kind == "Cluster"
                && recorded_cluster_uid.is_some()
                && object.metadata.uid.as_deref() != recorded_cluster_uid)
            || validate_provider_owner(object, &name, false, inventory).is_err()
        {
            continue;
        }
        roots.push(AcceptedResource { object, definition });
    }
    let root_uids: Vec<_> = roots
        .iter()
        .filter_map(|resource| resource.object.metadata.uid.as_deref())
        .collect();
    let mut accepted = roots;
    for object in inventory {
        let Some(definition) = definition_for(MANAGEMENT_RESOURCES, object) else {
            continue;
        };
        if definition.class != ResourceClass::Descendant || definition.kind == "Secret" {
            continue;
        }
        if root_uids
            .iter()
            .any(|uid| validate_owner_chain(object, uid, inventory).is_ok())
        {
            accepted.push(AcceptedResource { object, definition });
        }
    }
    accepted
}

fn accepted_azure<'a>(
    tenant: &Tenant,
    inventory: &'a [DynamicObject],
) -> Vec<AcceptedResource<'a>> {
    let name = tenant.name_any();
    let Some(azure) = trusted_azure_status(tenant) else {
        return Vec::new();
    };
    let Some(binding) = azure.binding.as_ref() else {
        return Vec::new();
    };
    let recorded = azure_recorded_roots(&name, azure);
    let mut roots = Vec::new();
    for object in inventory {
        let Some(definition) = definition_for(AZURE_MANAGEMENT_RESOURCES, object) else {
            continue;
        };
        if definition.kind == "Secret" || definition.class == ResourceClass::Descendant {
            continue;
        }
        let key = (
            definition.kind.to_owned(),
            object
                .metadata
                .name
                .as_deref()
                .map_or("", |value| value)
                .to_owned(),
        );
        let Some(expected_uid) = recorded.get(&key) else {
            continue;
        };
        if object.metadata.uid.as_deref() != Some(expected_uid.as_str())
            || !azure_markers_match(object, &name, binding)
        {
            continue;
        }
        roots.push(AcceptedResource { object, definition });
    }
    let root_uids: BTreeSet<_> = roots
        .iter()
        .filter_map(|resource| resource.object.metadata.uid.clone())
        .collect();
    let recorded_resources: BTreeMap<_, _> = azure
        .provider_resources
        .iter()
        .map(|resource| {
            (
                (
                    resource.api_version.as_str(),
                    resource.kind.as_str(),
                    resource.namespace.as_deref(),
                    resource.name.as_str(),
                ),
                resource,
            )
        })
        .collect();
    let mut accepted = roots;
    for object in inventory {
        let Some(definition) = definition_for(AZURE_MANAGEMENT_RESOURCES, object) else {
            continue;
        };
        if definition.kind == "Secret" || definition.class != ResourceClass::Descendant {
            continue;
        }
        let Some(types) = object.types.as_ref() else {
            continue;
        };
        let key = (
            types.api_version.as_str(),
            types.kind.as_str(),
            object.metadata.namespace.as_deref(),
            object.metadata.name.as_deref().map_or("", |value| value),
        );
        let Some(recorded_resource) = recorded_resources.get(&key) else {
            continue;
        };
        if object.metadata.uid.as_deref() != Some(recorded_resource.uid.as_str()) {
            continue;
        }
        let live_owner_uids: BTreeSet<_> = object
            .owner_references()
            .iter()
            .map(|owner| owner.uid.as_str())
            .collect();
        if !recorded_resource
            .owner_uids
            .iter()
            .all(|uid| live_owner_uids.contains(uid.as_str()))
        {
            continue;
        }
        if root_uids
            .iter()
            .any(|uid| validate_owner_chain(object, uid, inventory).is_ok())
        {
            accepted.push(AcceptedResource { object, definition });
        }
    }
    accepted
}

fn local_markers_match(
    object: &DynamicObject,
    definition: ManagementResource,
    tenant_name: &str,
    tenant_uid: &str,
    specification_hash: &str,
    foundation_hash: &str,
) -> bool {
    let annotations = object.annotations();
    annotations.get(TENANT_ANNOTATION).map(String::as_str) == Some(tenant_name)
        && annotations.get(TENANT_UID_ANNOTATION).map(String::as_str) == Some(tenant_uid)
        && annotations.get(SPEC_HASH_ANNOTATION).map(String::as_str) == Some(specification_hash)
        && annotations.get(FOUNDATION_ANNOTATION).map(String::as_str) == Some(foundation_hash)
        && annotations.get(RESOURCE_ANNOTATION).map(String::as_str) == Some(definition.role)
}

fn azure_markers_match(
    object: &DynamicObject,
    tenant_name: &str,
    binding: &tenant_controller::api::AzureBindingStatus,
) -> bool {
    let annotations = object.annotations();
    annotations.get(AZURE_TENANT_ANNOTATION).map(String::as_str) == Some(tenant_name)
        && annotations
            .get(AZURE_PROFILE_ANNOTATION)
            .map(String::as_str)
            == Some("azure")
        && annotations.get(AZURE_SPEC_ANNOTATION).map(String::as_str)
            == Some(binding.specification_sha256.as_str())
        && annotations
            .get(AZURE_FOUNDATION_ANNOTATION)
            .map(String::as_str)
            == Some(binding.foundation_sha256.as_str())
        && annotations
            .get(AZURE_OPERATION_ANNOTATION)
            .map(String::as_str)
            == Some(binding.operation_id.as_str())
}

fn azure_recorded_roots(
    tenant_name: &str,
    azure: &tenant_controller::api::AzureProviderStatus,
) -> BTreeMap<(String, String), String> {
    let mut roots = BTreeMap::new();
    let Some(management) = azure.management.as_ref() else {
        return roots;
    };
    let values = [
        (
            "Namespace",
            tenant_name.to_owned(),
            &management.namespace_uid,
        ),
        (
            "AzureClusterIdentity",
            format!("{tenant_name}-identity"),
            &management.azure_cluster_identity_uid,
        ),
        ("Cluster", tenant_name.to_owned(), &management.cluster_uid),
        (
            "AzureCluster",
            tenant_name.to_owned(),
            &management.azure_cluster_uid,
        ),
        (
            "KamajiControlPlane",
            tenant_name.to_owned(),
            &management.kamaji_control_plane_uid,
        ),
        (
            "KubeadmConfig",
            format!("{tenant_name}-worker"),
            &management.kubeadm_config_uid,
        ),
        (
            "AzureMachinePool",
            format!("{tenant_name}-worker"),
            &management.azure_machine_pool_uid,
        ),
        (
            "MachinePool",
            format!("{tenant_name}-worker"),
            &management.machine_pool_uid,
        ),
        (
            "ConfigMap",
            format!("{tenant_name}-azure-cloud-provider-values"),
            &management.cloud_values_config_map_uid,
        ),
        (
            "ConfigMap",
            format!("{tenant_name}-calico-values"),
            &management.network_values_config_map_uid,
        ),
        (
            "Deployment",
            format!("{tenant_name}-status-probe"),
            &management.status_probe_deployment_uid,
        ),
        (
            "Job",
            format!("{tenant_name}-install-addons"),
            &management.addon_job_uid,
        ),
    ];
    for (kind, name, uid) in values {
        if let Some(uid) = uid.as_ref().filter(|value| !value.is_empty()) {
            roots.insert((kind.to_owned(), name), uid.clone());
        }
    }
    roots
}

fn definition_for(
    catalog: &'static [ManagementResource],
    object: &DynamicObject,
) -> Option<ManagementResource> {
    let types = object.types.as_ref()?;
    catalog.iter().copied().find(|definition| {
        definition.api_version == types.api_version && definition.kind == types.kind
    })
}

fn expected_name_matches(
    definition: ManagementResource,
    tenant_name: &str,
    object: &DynamicObject,
) -> bool {
    definition
        .expected_name(tenant_name)
        .is_none_or(|name| object.metadata.name.as_deref() == Some(name.as_str()))
}

fn compare_resources(
    left: &AcceptedResource<'_>,
    right: &AcceptedResource<'_>,
) -> std::cmp::Ordering {
    let left_types = left.object.types.as_ref();
    let right_types = right.object.types.as_ref();
    (
        left_types.map_or("", |value| value.api_version.as_str()),
        left_types.map_or("", |value| value.kind.as_str()),
        left.object
            .metadata
            .namespace
            .as_deref()
            .map_or("", |value| value),
        left.object
            .metadata
            .name
            .as_deref()
            .map_or("", |value| value),
    )
        .cmp(&(
            right_types.map_or("", |value| value.api_version.as_str()),
            right_types.map_or("", |value| value.kind.as_str()),
            right
                .object
                .metadata
                .namespace
                .as_deref()
                .map_or("", |value| value),
            right
                .object
                .metadata
                .name
                .as_deref()
                .map_or("", |value| value),
        ))
}

fn provider_status_matches(tenant: &Tenant) -> bool {
    match (
        &tenant.spec.provider,
        tenant
            .status
            .as_ref()
            .and_then(|status| status.provider.as_ref()),
    ) {
        (TenantProviderSpec::Local { .. }, Some(TenantProviderStatus::Local(_))) => true,
        (TenantProviderSpec::Azure { .. }, Some(TenantProviderStatus::Azure(_))) => {
            trusted_azure_status(tenant).is_some()
        }
        _ => false,
    }
}

fn provider(specification: &TenantProviderSpec) -> TenantProvider {
    match specification {
        TenantProviderSpec::Local { .. } => TenantProvider::Local,
        TenantProviderSpec::Azure { .. } => TenantProvider::Azure,
    }
}

fn specification(tenant: &Tenant) -> TenantSpecificationView {
    let provider = match &tenant.spec.provider {
        TenantProviderSpec::Local { databases } => ProviderSpecificationView::Local {
            databases: nonnegative(*databases),
        },
        TenantProviderSpec::Azure {
            pod_cidr,
            service_cidr,
        } => ProviderSpecificationView::Azure {
            pod_cidr: bounded(pod_cidr, MAX_IDENTITY),
            service_cidr: bounded(service_cidr, MAX_IDENTITY),
        },
    };
    TenantSpecificationView {
        kubernetes_version: bounded(&tenant.spec.kubernetes_version, MAX_IDENTITY),
        workers: nonnegative(tenant.spec.workers),
        provider,
    }
}

fn provider_status(tenant: &Tenant) -> ProviderStatusView {
    if let Some(status) = trusted_azure_status(tenant) {
        return ProviderStatusView::Azure(azure_provider_view(tenant, status));
    }
    match (
        &tenant.spec.provider,
        tenant
            .status
            .as_ref()
            .and_then(|status| status.provider.as_ref()),
    ) {
        (TenantProviderSpec::Local { .. }, Some(TenantProviderStatus::Local(status))) => {
            ProviderStatusView::Local(LocalProviderView {
                allocation: status.allocation.as_ref().and_then(|allocation| {
                    allocation
                        .slot_id
                        .parse::<u32>()
                        .ok()
                        .map(|slot_id| LocalAllocationView {
                            slot_id,
                            endpoint: bounded(&allocation.endpoint, MAX_TEXT),
                            pod_cidr: bounded(&allocation.pod_cidr, MAX_IDENTITY),
                            service_cidr: bounded(&allocation.service_cidr, MAX_IDENTITY),
                        })
                }),
                foundation_hash: status
                    .foundation_hash
                    .as_deref()
                    .map(|value| bounded(value, MAX_IDENTITY)),
                cluster_uid: status
                    .cluster_uid
                    .as_deref()
                    .map(|value| bounded(value, MAX_IDENTITY)),
            })
        }
        (specification, observed) => ProviderStatusView::Unknown(UnknownProviderView {
            provider_type: match specification {
                TenantProviderSpec::Local { .. } => "local",
                TenantProviderSpec::Azure { .. } => "azure",
            }
            .into(),
            summary: Some(
                match (specification, observed) {
                    (TenantProviderSpec::Azure { .. }, Some(TenantProviderStatus::Azure(_))) => {
                        "provider status binding does not match current Tenant identity"
                    }
                    (_, Some(TenantProviderStatus::Local(_))) => {
                        "provider status is local but the specification is not"
                    }
                    (_, Some(TenantProviderStatus::Azure(_))) => {
                        "provider status is azure but the specification is not"
                    }
                    (_, None) => "provider status is absent",
                }
                .into(),
            ),
        }),
    }
}

fn azure_provider_view(
    tenant: &Tenant,
    status: &tenant_controller::api::AzureProviderStatus,
) -> AzureProviderView {
    let binding = status.binding.as_ref().map(|binding| AzureBindingView {
        cluster_name: bounded(&tenant.name_any(), MAX_IDENTITY),
        resource_group: resource_name(&binding.resource_group_id),
        binding_hash: bounded(&binding.specification_sha256, MAX_IDENTITY),
    });
    let management = status
        .management
        .as_ref()
        .map(|management| AzureManagementView {
            cluster_uid: management
                .cluster_uid
                .as_deref()
                .map(|value| bounded(value, MAX_IDENTITY)),
            infrastructure_uid: management
                .azure_cluster_uid
                .as_deref()
                .map(|value| bounded(value, MAX_IDENTITY)),
            control_plane_uid: management
                .kamaji_control_plane_uid
                .as_deref()
                .map(|value| bounded(value, MAX_IDENTITY)),
        });
    let worker_pool = status
        .management
        .as_ref()
        .map(|management| AzureWorkerPoolView {
            name: format!("{}-worker", bounded(&tenant.name_any(), MAX_IDENTITY)),
            uid: management
                .machine_pool_uid
                .as_deref()
                .map(|value| bounded(value, MAX_IDENTITY)),
            scale_set_name: status
                .vmss
                .as_ref()
                .and_then(|vmss| vmss.id.as_deref())
                .map(resource_name),
            desired_replicas: nonnegative(tenant.spec.workers),
            ready_replicas: usize_u32(status.nodes.len()),
        });
    let mut nodes: Vec<_> = status
        .nodes
        .iter()
        .take(MAX_CONDITIONS)
        .map(|node| AzureNodeView {
            name: bounded(&node.name, MAX_IDENTITY),
            uid: bounded(&node.uid, MAX_IDENTITY),
            provider_id: Some(bounded(&node.provider_id, MAX_TEXT)),
            internal_ip: Some(bounded(&node.internal_ip, MAX_IDENTITY)),
            ready: true,
        })
        .collect();
    nodes.sort_by(|left, right| (&left.name, &left.uid).cmp(&(&right.name, &right.uid)));
    let component_definitions = BTreeMap::from([
        (
            "cloudController",
            (
                "apps/v1",
                "Deployment",
                "kube-system",
                "cloud-controller-manager",
            ),
        ),
        (
            "cloudNode",
            ("apps/v1", "DaemonSet", "kube-system", "cloud-node-manager"),
        ),
        (
            "calicoNode",
            ("apps/v1", "DaemonSet", "calico-system", "calico-node"),
        ),
        (
            "calicoControllers",
            (
                "apps/v1",
                "Deployment",
                "calico-system",
                "calico-kube-controllers",
            ),
        ),
    ]);
    let add_ons = status
        .addon_components
        .iter()
        .filter_map(|(component, uid)| {
            component_definitions.get(component.as_str()).map(
                |(api_version, kind, namespace, name)| ResourceIdentityView {
                    api_version: (*api_version).into(),
                    kind: (*kind).into(),
                    namespace: Some((*namespace).into()),
                    name: (*name).into(),
                    uid: Some(bounded(uid, MAX_IDENTITY)),
                },
            )
        })
        .collect();
    let mut resources: Vec<_> = validated_azure_resources(status)
        .into_iter()
        .take(MAX_CONDITIONS)
        .map(|resource| AzureResourceView {
            identity: ResourceIdentityView {
                api_version: bounded(&resource.api_version, MAX_IDENTITY),
                kind: bounded(&resource.kind, MAX_IDENTITY),
                namespace: resource
                    .namespace
                    .as_deref()
                    .map(|value| bounded(value, MAX_IDENTITY)),
                name: bounded(&resource.name, MAX_IDENTITY),
                uid: Some(bounded(&resource.uid, MAX_IDENTITY)),
            },
            resource_id: resource
                .resource_id
                .as_deref()
                .map(|value| bounded(value, MAX_TEXT)),
            owner_uids: resource
                .owner_uids
                .iter()
                .take(MAX_CONDITIONS)
                .map(|value| bounded(value, MAX_IDENTITY))
                .collect(),
        })
        .collect();
    resources.sort_by(|left, right| {
        (
            &left.identity.api_version,
            &left.identity.kind,
            &left.identity.namespace,
            &left.identity.name,
        )
            .cmp(&(
                &right.identity.api_version,
                &right.identity.kind,
                &right.identity.namespace,
                &right.identity.name,
            ))
    });
    AzureProviderView {
        binding,
        endpoint: status
            .endpoint
            .as_deref()
            .map(|value| bounded(value, MAX_TEXT)),
        management,
        worker_pool,
        nodes,
        add_ons,
        resources,
    }
}

fn validated_azure_resources(
    status: &tenant_controller::api::AzureProviderStatus,
) -> Vec<&tenant_controller::api::AzureProviderResourceIdentity> {
    let Some(management) = status.management.as_ref() else {
        return Vec::new();
    };
    let mut owned: BTreeSet<String> = management
        .recorded_uids()
        .into_iter()
        .map(str::to_owned)
        .collect();
    let mut uid_counts = BTreeMap::new();
    for resource in &status.provider_resources {
        let count = uid_counts.entry(resource.uid.as_str()).or_insert(0_u8);
        *count = count.saturating_add(1);
    }
    let mut selected = BTreeSet::new();
    let mut result = Vec::new();
    loop {
        let before = selected.len();
        for (index, resource) in status.provider_resources.iter().enumerate() {
            if selected.contains(&index)
                || resource.api_version.is_empty()
                || resource.kind.is_empty()
                || resource.name.is_empty()
                || resource.uid.is_empty()
                || uid_counts.get(resource.uid.as_str()) != Some(&1)
                || resource.owner_uids.is_empty()
                || !resource
                    .owner_uids
                    .iter()
                    .any(|owner| owned.contains(owner))
            {
                continue;
            }
            owned.insert(resource.uid.clone());
            selected.insert(index);
            result.push(resource);
        }
        if selected.len() == before {
            break;
        }
    }
    result
}

fn projected_conditions(tenant: &Tenant) -> Vec<TenantCondition> {
    let mut conditions: Vec<_> = tenant
        .status
        .as_ref()
        .map_or(&[][..], |status| status.conditions.as_slice())
        .iter()
        .take(MAX_CONDITIONS)
        .map(|condition| TenantCondition {
            condition_type: bounded(&condition.type_, MAX_IDENTITY),
            status: match condition.status.as_str() {
                "True" => ConditionStatus::True,
                "False" => ConditionStatus::False,
                _ => ConditionStatus::Unknown,
            },
            reason: Some(bounded(&sanitize::text(&condition.reason), MAX_TEXT)),
            message: Some(bounded(&sanitize::text(&condition.message), MAX_TEXT)),
            observed_generation: condition.observed_generation,
            last_transition_time: Some(condition.last_transition_time.0.to_string()),
        })
        .collect();
    conditions.sort_by(|left, right| {
        (&left.condition_type, &left.last_transition_time)
            .cmp(&(&right.condition_type, &right.last_transition_time))
    });
    conditions
}

fn blockers(mode: ProviderMode, tenant: &Tenant) -> Vec<TenantBlocker> {
    let mut blockers = Vec::new();
    let generation = tenant.metadata.generation;
    let status = tenant.status.as_ref();
    if tenant.metadata.deletion_timestamp.is_some() {
        push_blocker(&mut blockers, "deleting", "Tenant is deleting", None);
    }
    if generation.is_none() {
        push_blocker(
            &mut blockers,
            "generation-invalid",
            "Tenant generation is missing",
            None,
        );
    } else if status.and_then(|status| status.observed_generation) != generation {
        push_blocker(
            &mut blockers,
            "generation-stale",
            "status does not observe the current generation",
            None,
        );
    }
    let ready = status.and_then(|status| {
        status
            .conditions
            .iter()
            .find(|condition| condition.type_ == "Ready")
    });
    if !ready.is_some_and(|condition| condition.status == "True") {
        push_blocker(
            &mut blockers,
            "ready-false",
            "Ready condition is not true",
            Some("Ready"),
        );
    }
    if !ready.is_some_and(|condition| condition.observed_generation == generation) {
        push_blocker(
            &mut blockers,
            "ready-stale",
            "Ready condition does not observe the current generation",
            Some("Ready"),
        );
    }
    if !provider_status_matches(tenant) {
        push_blocker(
            &mut blockers,
            "provider-status-invalid",
            "provider status does not match the Tenant specification",
            None,
        );
    }
    if let Some(status) = status {
        for condition in status.conditions.iter().filter(|condition| {
            condition.status == "False" && condition.observed_generation == generation
        }) {
            push_blocker(
                &mut blockers,
                &condition.reason,
                &sanitize::text(&condition.message),
                Some(&condition.type_),
            );
        }
    }
    if mode == ProviderMode::Local {
        if let TenantProviderSpec::Local { databases } = tenant.spec.provider
            && databases < 0
        {
            push_blocker(
                &mut blockers,
                "spec-invalid",
                "requested database count is invalid",
                None,
            );
        }
        if tenant.spec.workers < 0 {
            push_blocker(
                &mut blockers,
                "spec-invalid",
                "requested worker count is invalid",
                None,
            );
        }
    }
    blockers.truncate(MAX_BLOCKERS);
    blockers
}

fn push_blocker(
    blockers: &mut Vec<TenantBlocker>,
    code: &str,
    message: &str,
    condition_type: Option<&str>,
) {
    if blockers.len() >= MAX_BLOCKERS {
        return;
    }
    blockers.push(TenantBlocker {
        code: bounded(code, MAX_IDENTITY),
        message: bounded(&sanitize::text(message), MAX_TEXT),
        condition_type: condition_type.map(|value| bounded(value, MAX_IDENTITY)),
    });
}

fn management_resource_view(
    object: &DynamicObject,
    definition: ManagementResource,
) -> ManagementResourceView {
    ManagementResourceView {
        identity: identity(object),
        role: definition.role.into(),
        health: object_health(object),
        message: object_message(object),
    }
}

fn identity(object: &DynamicObject) -> ResourceIdentityView {
    let types = object.types.as_ref();
    ResourceIdentityView {
        api_version: types
            .map_or("", |value| value.api_version.as_str())
            .chars()
            .take(MAX_IDENTITY)
            .collect(),
        kind: types
            .map_or("", |value| value.kind.as_str())
            .chars()
            .take(MAX_IDENTITY)
            .collect(),
        namespace: object
            .metadata
            .namespace
            .as_deref()
            .map(|value| bounded(value, MAX_IDENTITY)),
        name: object
            .metadata
            .name
            .as_deref()
            .map_or_else(String::new, |value| bounded(value, MAX_IDENTITY)),
        uid: object
            .metadata
            .uid
            .as_deref()
            .map(|value| bounded(value, MAX_IDENTITY)),
    }
}

fn topology(
    tenant: &Tenant,
    summary: &TenantSummary,
    accepted: &[AcceptedResource<'_>],
) -> TopologyGraph {
    let tenant_id = "tenant".to_owned();
    let mut nodes = vec![TopologyNode {
        id: tenant_id.clone(),
        kind: TopologyNodeKind::Tenant,
        label: summary.name.clone(),
        health: classification_health(summary.classification),
        resource: Some(ResourceIdentityView {
            api_version: "tenancy.cnpg-vcluster.io/v1alpha2".into(),
            kind: "Tenant".into(),
            namespace: None,
            name: summary.name.clone(),
            uid: Some(
                tenant
                    .metadata
                    .uid
                    .as_deref()
                    .map_or_else(String::new, |value| bounded(value, MAX_IDENTITY)),
            ),
        }),
        attributes: vec![
            DisplayAttribute {
                label: "Kubernetes".into(),
                value: summary.kubernetes_version.clone(),
            },
            DisplayAttribute {
                label: "Workers".into(),
                value: summary.requested_workers.to_string(),
            },
        ],
    }];
    let mut edges = Vec::new();
    let mut ids_by_uid = BTreeMap::new();
    for resource in accepted {
        let Some(uid) = resource.object.metadata.uid.as_deref() else {
            continue;
        };
        let id = format!("resource:{}", bounded(uid, MAX_IDENTITY));
        ids_by_uid.insert(uid, id.clone());
        nodes.push(TopologyNode {
            id,
            kind: topology_kind(resource.definition),
            label: resource
                .object
                .metadata
                .name
                .as_deref()
                .map_or_else(String::new, |value| bounded(value, MAX_IDENTITY)),
            health: object_health(resource.object),
            resource: Some(identity(resource.object)),
            attributes: vec![DisplayAttribute {
                label: "Role".into(),
                value: resource.definition.role.into(),
            }],
        });
    }
    for resource in accepted {
        let Some(uid) = resource.object.metadata.uid.as_deref() else {
            continue;
        };
        let Some(target) = ids_by_uid.get(uid) else {
            continue;
        };
        let source = match resource
            .object
            .owner_references()
            .iter()
            .find_map(|owner| ids_by_uid.get(owner.uid.as_str()))
        {
            Some(source) => source,
            None => &tenant_id,
        };
        edges.push(TopologyEdge {
            id: format!("edge:{source}:{target}"),
            source: source.clone(),
            target: target.clone(),
            kind: TopologyEdgeKind::Owns,
            label: None,
        });
    }
    match &tenant.spec.provider {
        TenantProviderSpec::Local { databases } => {
            add_summary_node(
                &mut nodes,
                &mut edges,
                &tenant_id,
                SummaryNode {
                    id: "workers",
                    kind: TopologyNodeKind::WorkerPool,
                    label: "Workers",
                    count: nonnegative(tenant.spec.workers),
                    health: condition_health(tenant, "WorkersReady"),
                },
            );
            add_summary_node(
                &mut nodes,
                &mut edges,
                &tenant_id,
                SummaryNode {
                    id: "databases",
                    kind: TopologyNodeKind::Database,
                    label: "Databases",
                    count: nonnegative(*databases),
                    health: condition_health(tenant, "DatabaseReady"),
                },
            );
        }
        TenantProviderSpec::Azure { .. } => {
            if let Some(status) = trusted_azure_status(tenant) {
                add_azure_status_nodes(
                    tenant,
                    status,
                    &mut nodes,
                    &mut edges,
                    &tenant_id,
                    &ids_by_uid,
                );
            }
        }
    }
    nodes.sort_by(|left, right| left.id.cmp(&right.id));
    edges.sort_by(|left, right| left.id.cmp(&right.id));
    TopologyGraph {
        tenant_name: summary.name.clone(),
        provider: summary.provider,
        nodes,
        edges,
    }
}

struct SummaryNode<'a> {
    id: &'a str,
    kind: TopologyNodeKind,
    label: &'a str,
    count: u32,
    health: TopologyHealth,
}

fn add_summary_node(
    nodes: &mut Vec<TopologyNode>,
    edges: &mut Vec<TopologyEdge>,
    tenant_id: &str,
    summary: SummaryNode<'_>,
) {
    let node_id = format!("summary:{}", summary.id);
    nodes.push(TopologyNode {
        id: node_id.clone(),
        kind: summary.kind,
        label: summary.label.into(),
        health: summary.health,
        resource: None,
        attributes: vec![DisplayAttribute {
            label: "Requested".into(),
            value: summary.count.to_string(),
        }],
    });
    edges.push(TopologyEdge {
        id: format!("edge:{tenant_id}:{node_id}"),
        source: tenant_id.into(),
        target: node_id,
        kind: TopologyEdgeKind::Contains,
        label: None,
    });
}

fn add_azure_status_nodes(
    tenant: &Tenant,
    status: &tenant_controller::api::AzureProviderStatus,
    nodes: &mut Vec<TopologyNode>,
    edges: &mut Vec<TopologyEdge>,
    tenant_id: &str,
    ids_by_uid: &BTreeMap<&str, String>,
) {
    let worker_id = match status
        .management
        .as_ref()
        .and_then(|management| management.machine_pool_uid.as_deref())
        .and_then(|uid| ids_by_uid.get(uid))
        .cloned()
    {
        Some(worker_id) => worker_id,
        None => "summary:workers".into(),
    };
    if worker_id == "summary:workers" {
        add_summary_node(
            nodes,
            edges,
            tenant_id,
            SummaryNode {
                id: "workers",
                kind: TopologyNodeKind::WorkerPool,
                label: "Workers",
                count: nonnegative(tenant.spec.workers),
                health: condition_health(tenant, "AzureWorkersReady"),
            },
        );
    }
    for node in status.nodes.iter().take(MAX_CONDITIONS) {
        let id = format!("node:{}", bounded(&node.uid, MAX_IDENTITY));
        nodes.push(TopologyNode {
            id: id.clone(),
            kind: TopologyNodeKind::Node,
            label: bounded(&node.name, MAX_IDENTITY),
            health: TopologyHealth::Ready,
            resource: Some(ResourceIdentityView {
                api_version: "v1".into(),
                kind: "Node".into(),
                namespace: None,
                name: bounded(&node.name, MAX_IDENTITY),
                uid: Some(bounded(&node.uid, MAX_IDENTITY)),
            }),
            attributes: vec![
                DisplayAttribute {
                    label: "Provider ID".into(),
                    value: bounded(&node.provider_id, MAX_TEXT),
                },
                DisplayAttribute {
                    label: "Internal IP".into(),
                    value: bounded(&node.internal_ip, MAX_IDENTITY),
                },
            ],
        });
        edges.push(TopologyEdge {
            id: format!("edge:{worker_id}:{id}"),
            source: worker_id.clone(),
            target: id,
            kind: TopologyEdgeKind::Represents,
            label: None,
        });
    }
    if let Some(vmss) = status.vmss.as_ref().and_then(|vmss| vmss.id.as_deref()) {
        let id = "provider:vmss".to_owned();
        nodes.push(TopologyNode {
            id: id.clone(),
            kind: TopologyNodeKind::ProviderResource,
            label: resource_name(vmss),
            health: condition_health(tenant, "AzureWorkersReady"),
            resource: None,
            attributes: vec![DisplayAttribute {
                label: "Resource ID".into(),
                value: bounded(vmss, MAX_TEXT),
            }],
        });
        edges.push(TopologyEdge {
            id: format!("edge:{worker_id}:{id}"),
            source: worker_id,
            target: id,
            kind: TopologyEdgeKind::Provides,
            label: None,
        });
    }
    for (component, uid) in status.addon_components.iter().take(MAX_CONDITIONS) {
        let id = format!("addon:{}", bounded(uid, MAX_IDENTITY));
        nodes.push(TopologyNode {
            id: id.clone(),
            kind: TopologyNodeKind::AddOn,
            label: bounded(component, MAX_IDENTITY),
            health: TopologyHealth::Ready,
            resource: None,
            attributes: Vec::new(),
        });
        edges.push(TopologyEdge {
            id: format!("edge:{tenant_id}:{id}"),
            source: tenant_id.into(),
            target: id,
            kind: TopologyEdgeKind::Contains,
            label: None,
        });
    }
    let mut status_ids: BTreeMap<String, String> = ids_by_uid
        .iter()
        .map(|(uid, id)| ((*uid).to_owned(), id.clone()))
        .collect();
    for resource in validated_azure_resources(status)
        .into_iter()
        .take(MAX_CONDITIONS)
    {
        if status_ids.contains_key(&resource.uid) {
            continue;
        }
        let Some(source) = resource
            .owner_uids
            .iter()
            .find_map(|owner| status_ids.get(owner))
            .cloned()
        else {
            continue;
        };
        let id = format!("provider:{}", bounded(&resource.uid, MAX_IDENTITY));
        nodes.push(TopologyNode {
            id: id.clone(),
            kind: match resource.kind.as_str() {
                "Machine" | "AzureMachinePoolMachine" | "MachineSet" => TopologyNodeKind::Machine,
                _ => TopologyNodeKind::ProviderResource,
            },
            label: bounded(&resource.name, MAX_IDENTITY),
            health: TopologyHealth::Ready,
            resource: Some(ResourceIdentityView {
                api_version: bounded(&resource.api_version, MAX_IDENTITY),
                kind: bounded(&resource.kind, MAX_IDENTITY),
                namespace: resource
                    .namespace
                    .as_deref()
                    .map(|value| bounded(value, MAX_IDENTITY)),
                name: bounded(&resource.name, MAX_IDENTITY),
                uid: Some(bounded(&resource.uid, MAX_IDENTITY)),
            }),
            attributes: resource
                .resource_id
                .as_deref()
                .map_or_else(Vec::new, |resource_id| {
                    vec![DisplayAttribute {
                        label: "Resource ID".into(),
                        value: bounded(resource_id, MAX_TEXT),
                    }]
                }),
        });
        edges.push(TopologyEdge {
            id: format!("edge:{source}:{id}"),
            source,
            target: id.clone(),
            kind: TopologyEdgeKind::Owns,
            label: None,
        });
        status_ids.insert(resource.uid.clone(), id);
    }
}

fn topology_kind(definition: ManagementResource) -> TopologyNodeKind {
    match definition.role {
        "kamaji-control-plane" | "provider" => TopologyNodeKind::ControlPlane,
        "machine-deployment" | "machine-pool" | "azure-machine-pool" => {
            TopologyNodeKind::WorkerPool
        }
        "machine" | "machine-set" | "azure-machine-pool-machine" => TopologyNodeKind::Machine,
        "addon-values" | "status-probe" | "addon-job" => TopologyNodeKind::AddOn,
        _ => TopologyNodeKind::ProviderResource,
    }
}

fn object_health(object: &DynamicObject) -> TopologyHealth {
    if object.metadata.deletion_timestamp.is_some() {
        return TopologyHealth::Deleting;
    }
    if let Some(conditions) = object
        .data
        .pointer("/status/conditions")
        .and_then(serde_json::Value::as_array)
    {
        for condition_type in ["Ready", "Available"] {
            if let Some(condition) = conditions.iter().find(|condition| {
                condition.get("type").and_then(serde_json::Value::as_str) == Some(condition_type)
            }) {
                return match condition.get("status").and_then(serde_json::Value::as_str) {
                    Some("True") => TopologyHealth::Ready,
                    Some("False") => TopologyHealth::Degraded,
                    _ => TopologyHealth::Progressing,
                };
            }
        }
    }
    match object
        .data
        .pointer("/status/phase")
        .and_then(serde_json::Value::as_str)
    {
        Some("Ready" | "Running" | "Available" | "Succeeded") => TopologyHealth::Ready,
        Some("Failed" | "Error") => TopologyHealth::Failed,
        Some(_) => TopologyHealth::Progressing,
        None => TopologyHealth::Unknown,
    }
}

fn object_message(object: &DynamicObject) -> Option<String> {
    object
        .data
        .pointer("/status/conditions")
        .and_then(serde_json::Value::as_array)
        .and_then(|conditions| {
            conditions.iter().find(|condition| {
                condition.get("status").and_then(serde_json::Value::as_str) == Some("False")
            })
        })
        .and_then(|condition| condition.get("message").and_then(serde_json::Value::as_str))
        .map(|message| bounded(&sanitize::text(message), MAX_TEXT))
}

fn condition_health(tenant: &Tenant, condition_type: &str) -> TopologyHealth {
    let generation = tenant.metadata.generation;
    tenant
        .status
        .as_ref()
        .and_then(|status| {
            status
                .conditions
                .iter()
                .find(|condition| condition.type_ == condition_type)
        })
        .map_or(TopologyHealth::Unknown, |condition| {
            if condition.observed_generation != generation {
                TopologyHealth::Progressing
            } else {
                match condition.status.as_str() {
                    "True" => TopologyHealth::Ready,
                    "False" => TopologyHealth::Degraded,
                    _ => TopologyHealth::Progressing,
                }
            }
        })
}

fn classification_health(classification: TenantClassification) -> TopologyHealth {
    match classification {
        TenantClassification::Ready => TopologyHealth::Ready,
        TenantClassification::Progressing => TopologyHealth::Progressing,
        TenantClassification::Degraded | TenantClassification::OwnershipInvalid => {
            TopologyHealth::Degraded
        }
        TenantClassification::Failed => TopologyHealth::Failed,
        TenantClassification::Deleting => TopologyHealth::Deleting,
    }
}

fn resource_name(resource_id: &str) -> String {
    bounded(
        resource_id
            .trim_end_matches('/')
            .rsplit('/')
            .next()
            .map_or(resource_id, |value| value),
        MAX_IDENTITY,
    )
}

fn nonnegative(value: i32) -> u32 {
    u32::try_from(value).unwrap_or_default()
}

fn usize_u32(value: usize) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

fn bounded(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

#[cfg(test)]
mod tests {
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::{Condition, Time};
    use kube::core::TypeMeta;
    use serde_json::json;
    use tenant_controller::api::{
        AllocationStatus, AzureBindingStatus, AzureManagementStatus, AzureNodeIdentity,
        AzureProviderResourceIdentity, AzureProviderStatus, AzureVmssStatus, LocalProviderStatus,
        TenantSpec, TenantStatus,
    };

    use super::*;

    fn condition(condition_type: &str, status: &str, generation: i64) -> Condition {
        Condition {
            type_: condition_type.into(),
            status: status.into(),
            reason: format!("{condition_type}Reason"),
            message: format!("{condition_type} message token=secret"),
            observed_generation: Some(generation),
            last_transition_time: serde_json::from_str::<Time>(r#""2026-01-01T00:00:00Z""#)
                .expect("time"),
        }
    }

    fn local_tenant(phase: TenantPhase, observed: i64, ready: bool) -> Tenant {
        let mut tenant = Tenant::new("tenant-a", TenantSpec::local("1.36.4", 2, 1));
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.generation = Some(2);
        tenant.status = Some(TenantStatus {
            observed_generation: Some(observed),
            phase: Some(phase),
            conditions: vec![
                condition("Ready", if ready { "True" } else { "False" }, observed),
                condition("WorkersReady", "True", observed),
                condition("DatabaseReady", "True", observed),
            ],
            provider: Some(TenantProviderStatus::Local(LocalProviderStatus {
                allocation: Some(AllocationStatus {
                    slot_id: "1".into(),
                    endpoint: "https://tenant.example".into(),
                    pod_cidr: "10.0.0.0/16".into(),
                    service_cidr: "10.1.0.0/16".into(),
                }),
                foundation_hash: Some("foundation".into()),
                cluster_uid: Some("cluster-uid".into()),
            })),
        });
        tenant
    }

    fn local_root(tenant: &Tenant, uid: &str, foreign: bool) -> DynamicObject {
        let canonical =
            canonical_spec("tenant-a", &tenant.spec, SUPPORTED_KUBERNETES_VERSION).expect("spec");
        let mut annotations = BTreeMap::from([
            (TENANT_ANNOTATION.into(), "tenant-a".into()),
            (TENANT_UID_ANNOTATION.into(), "tenant-uid".into()),
            (SPEC_HASH_ANNOTATION.into(), spec_hash(&canonical)),
            (FOUNDATION_ANNOTATION.into(), "foundation".into()),
            (RESOURCE_ANNOTATION.into(), "cluster".into()),
        ]);
        if foreign {
            annotations.insert(TENANT_UID_ANNOTATION.into(), "foreign".into());
        }
        DynamicObject {
            types: Some(TypeMeta {
                api_version: "cluster.x-k8s.io/v1beta2".into(),
                kind: "Cluster".into(),
            }),
            metadata: kube::core::ObjectMeta {
                name: Some("tenant-a".into()),
                namespace: Some("tenant-a".into()),
                uid: Some(uid.into()),
                annotations: Some(annotations),
                ..Default::default()
            },
            data: json!({"status":{"phase":"Ready"}}),
        }
    }

    fn azure_tenant() -> Tenant {
        let mut tenant = Tenant::new(
            "tenant-a",
            TenantSpec {
                kubernetes_version: "1.36.4".into(),
                workers: 1,
                provider: TenantProviderSpec::Azure {
                    pod_cidr: "10.0.0.0/16".into(),
                    service_cidr: "10.1.0.0/16".into(),
                },
            },
        );
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.generation = Some(1);
        tenant.status = Some(TenantStatus {
            observed_generation: Some(1),
            phase: Some(TenantPhase::Ready),
            conditions: vec![
                condition("Ready", "True", 1),
                condition("AzureWorkersReady", "True", 1),
            ],
            provider: Some(TenantProviderStatus::Azure(Box::new(
                AzureProviderStatus {
                    binding: Some(AzureBindingStatus {
                        tenant_uid: "tenant-uid".into(),
                        specification_sha256: "specification".into(),
                        provider_config_uid: "provider-config".into(),
                        provider_config_sha256: "provider-config-sha".into(),
                        foundation_sha256: "foundation".into(),
                        foundation_defaults_sha256: "defaults".into(),
                        controller_image: "registry/controller@sha256:digest".into(),
                        resource_group_id: "/subscriptions/sub/resourceGroups/group".into(),
                        virtual_network_id: "/subscriptions/sub/resourceGroups/group/vnet".into(),
                        tenant_subnet_id: "/subscriptions/sub/resourceGroups/group/subnet".into(),
                        identity_id: "/subscriptions/sub/resourceGroups/group/identity".into(),
                        operation_id: "operation".into(),
                    }),
                    endpoint: Some("https://tenant.example".into()),
                    management: Some(AzureManagementStatus {
                        cluster_uid: Some("cluster-uid".into()),
                        machine_pool_uid: Some("pool-uid".into()),
                        ..AzureManagementStatus::default()
                    }),
                    kubeconfig: None,
                    vmss: Some(AzureVmssStatus {
                        id: Some(
                            "/subscriptions/sub/resourceGroups/group/providers/Microsoft.Compute/virtualMachineScaleSets/pool"
                                .into(),
                        ),
                        instance_ids: vec!["0".into()],
                    }),
                    nodes: vec![AzureNodeIdentity {
                        name: "node-a".into(),
                        uid: "node-uid".into(),
                        provider_id: "azure:///vmss/0".into(),
                        internal_ip: "10.0.0.4".into(),
                    }],
                    addon_components: BTreeMap::from([(
                        "cloudController".into(),
                        "addon-uid".into(),
                    )]),
                    provider_resources: vec![
                        AzureProviderResourceIdentity {
                            api_version: "cluster.x-k8s.io/v1beta1".into(),
                            kind: "Machine".into(),
                            namespace: Some("tenant-a".into()),
                            name: "tenant-a-machine".into(),
                            uid: "machine-uid".into(),
                            resource_id: None,
                            owner_uids: vec!["cluster-uid".into()],
                        },
                        AzureProviderResourceIdentity {
                            api_version: "cluster.x-k8s.io/v1beta1".into(),
                            kind: "Machine".into(),
                            namespace: Some("tenant-a".into()),
                            name: "foreign-machine".into(),
                            uid: "foreign-machine-uid".into(),
                            resource_id: None,
                            owner_uids: vec!["foreign-root".into()],
                        },
                    ],
                    deletion: None,
                },
            ))),
        });
        tenant
    }

    fn azure_cluster(uid: &str) -> DynamicObject {
        DynamicObject {
            types: Some(TypeMeta {
                api_version: "cluster.x-k8s.io/v1beta1".into(),
                kind: "Cluster".into(),
            }),
            metadata: kube::core::ObjectMeta {
                name: Some("tenant-a".into()),
                namespace: Some("tenant-a".into()),
                uid: Some(uid.into()),
                annotations: Some(BTreeMap::from([
                    (AZURE_TENANT_ANNOTATION.into(), "tenant-a".into()),
                    (AZURE_PROFILE_ANNOTATION.into(), "azure".into()),
                    (AZURE_SPEC_ANNOTATION.into(), "specification".into()),
                    (AZURE_FOUNDATION_ANNOTATION.into(), "foundation".into()),
                    (AZURE_OPERATION_ANNOTATION.into(), "operation".into()),
                ])),
                ..Default::default()
            },
            data: json!({"status":{"phase":"Ready"}}),
        }
    }

    #[test]
    fn classification_covers_ready_progressing_deleting_and_malformed() {
        assert_eq!(
            classify_tenant(
                ProviderMode::Local,
                &local_tenant(TenantPhase::Ready, 2, true)
            ),
            TenantClassification::Ready
        );
        assert_eq!(
            classify_tenant(
                ProviderMode::Local,
                &local_tenant(TenantPhase::Progressing, 2, false)
            ),
            TenantClassification::Progressing
        );
        assert_eq!(
            classify_tenant(
                ProviderMode::Local,
                &local_tenant(TenantPhase::Ready, 1, true)
            ),
            TenantClassification::Degraded
        );
        let mut deleting = local_tenant(TenantPhase::Ready, 2, true);
        deleting.metadata.deletion_timestamp =
            Some(serde_json::from_str::<Time>(r#""2026-01-01T00:00:00Z""#).expect("time"));
        assert_eq!(
            classify_tenant(ProviderMode::Local, &deleting),
            TenantClassification::Deleting
        );
        let mut malformed = local_tenant(TenantPhase::Ready, 2, true);
        malformed.status.as_mut().expect("status").provider =
            Some(TenantProviderStatus::Azure(Box::default()));
        assert_eq!(
            classify_tenant(ProviderMode::Local, &malformed),
            TenantClassification::OwnershipInvalid
        );
    }

    #[test]
    fn projection_sanitizes_conditions_and_excludes_foreign_resources() {
        let tenant = local_tenant(TenantPhase::Ready, 2, true);
        let owned = local_root(&tenant, "cluster-uid", false);
        let foreign = local_root(&tenant, "foreign-uid", true);
        let projection = TenantProjection::new(ProviderMode::Local, tenant, vec![owned, foreign]);
        assert_eq!(projection.detail.management_resources.len(), 1);
        assert_eq!(
            projection.detail.management_resources[0]
                .identity
                .uid
                .as_deref(),
            Some("cluster-uid")
        );
        let encoded = serde_json::to_string(&projection.detail).expect("serialize");
        assert!(!encoded.contains("secret"));
        assert!(encoded.contains("REDACTED"));
        assert!(
            projection
                .topology
                .nodes
                .iter()
                .any(|node| node.id == "summary:databases")
        );
        assert!(
            !projection
                .topology
                .nodes
                .iter()
                .any(|node| node.kind == TopologyNodeKind::Node)
        );
    }

    #[test]
    fn azure_projection_uses_recorded_management_and_tenant_status_identities() {
        let projection = TenantProjection::new(
            ProviderMode::Azure,
            azure_tenant(),
            vec![azure_cluster("cluster-uid"), azure_cluster("foreign-uid")],
        );
        assert_eq!(projection.detail.management_resources.len(), 1);
        let ProviderStatusView::Azure(status) = projection.detail.provider_status else {
            panic!("azure status");
        };
        assert_eq!(status.nodes.len(), 1);
        assert_eq!(status.resources.len(), 1);
        assert_eq!(
            status.resources[0].identity.uid.as_deref(),
            Some("machine-uid")
        );
        assert_eq!(
            status
                .worker_pool
                .as_ref()
                .and_then(|worker| worker.scale_set_name.as_deref()),
            Some("pool")
        );
        assert!(
            projection
                .topology
                .nodes
                .iter()
                .any(|node| node.kind == TopologyNodeKind::Node && node.label == "node-a")
        );
        assert!(
            projection
                .topology
                .nodes
                .iter()
                .any(|node| node.kind == TopologyNodeKind::AddOn)
        );
    }

    #[test]
    fn azure_projection_excludes_status_when_binding_is_missing_or_stale() {
        let mut tenant = azure_tenant();
        tenant.metadata.uid = Some("replacement-tenant-uid".into());
        let projection = TenantProjection::new(
            ProviderMode::Azure,
            tenant,
            vec![azure_cluster("cluster-uid")],
        );
        assert!(projection.summary.endpoint.is_none());
        assert!(projection.detail.management_resources.is_empty());
        assert!(matches!(
            projection.detail.provider_status,
            ProviderStatusView::Unknown(_)
        ));
        assert!(
            projection.topology.nodes.iter().all(|node| {
                !matches!(
                    node.kind,
                    TopologyNodeKind::WorkerPool
                        | TopologyNodeKind::Machine
                        | TopologyNodeKind::Node
                        | TopologyNodeKind::ProviderResource
                        | TopologyNodeKind::AddOn
                )
            }),
            "untrusted Azure status must not create topology nodes"
        );

        let mut tenant = azure_tenant();
        let Some(TenantProviderStatus::Azure(status)) = tenant
            .status
            .as_mut()
            .and_then(|status| status.provider.as_mut())
        else {
            panic!("azure status");
        };
        status.binding = None;
        let projection = TenantProjection::new(
            ProviderMode::Azure,
            tenant,
            vec![azure_cluster("cluster-uid")],
        );
        assert!(projection.detail.management_resources.is_empty());
        assert!(matches!(
            projection.detail.provider_status,
            ProviderStatusView::Unknown(_)
        ));
        assert_eq!(projection.topology.nodes.len(), 1);
        assert_eq!(projection.topology.nodes[0].kind, TopologyNodeKind::Tenant);
    }
}
