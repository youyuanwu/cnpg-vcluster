//! Pure ownership checks. Inventories must come from uncached, unfiltered reads;
//! an incomplete inventory is an error, never proof that a descendant is owned.

use std::collections::{BTreeMap, BTreeSet};

use k8s_openapi::{
    api::core::v1::Secret,
    apimachinery::pkg::apis::meta::v1::{ObjectMeta, OwnerReference},
};
use kube::core::DynamicObject;
use thiserror::Error;

use crate::{
    api::{GROUP, Tenant, VERSION},
    management::{MANAGEMENT_RESOURCES, ResourceClass},
};

pub const TENANT_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/tenant";
pub const TENANT_UID_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/tenant-uid";
pub const SPEC_HASH_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/spec-hash";
pub const FOUNDATION_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/foundation-hash";
pub const RESOURCE_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/resource";
pub const CLUSTER_API_VERSION: &str = "cluster.x-k8s.io/v1beta2";
pub const CONTROL_PLANE_API_VERSION: &str = "controlplane.cluster.x-k8s.io/v1alpha2";

#[derive(Clone, Copy, Debug)]
pub struct Identity<'a> {
    pub tenant_name: &'a str,
    pub tenant_uid: &'a str,
    pub spec_hash: &'a str,
    pub foundation_hash: &'a str,
    pub ownership_label: &'a str,
    pub lab_prefix: &'a str,
}

impl Identity<'_> {
    pub fn labels(&self) -> BTreeMap<String, String> {
        BTreeMap::from([(self.ownership_label.into(), self.lab_prefix.into())])
    }

    pub fn annotations(&self, resource: &str) -> BTreeMap<String, String> {
        [
            (TENANT_ANNOTATION, self.tenant_name),
            (TENANT_UID_ANNOTATION, self.tenant_uid),
            (SPEC_HASH_ANNOTATION, self.spec_hash),
            (FOUNDATION_ANNOTATION, self.foundation_hash),
            (RESOURCE_ANNOTATION, resource),
        ]
        .into_iter()
        .map(|(key, value)| (key.into(), value.into()))
        .collect()
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum OwnershipError {
    #[error("{0} has no UID")]
    MissingUid(String),
    #[error("{0} ownership markers do not match the Tenant")]
    Markers(String),
    #[error("{0} must not have a Tenant owner reference")]
    TenantOwner(String),
    #[error("provider owner is pending")]
    ProviderOwnerPending,
    #[error("{0} has an unexpected provider owner")]
    ProviderOwner(String),
    #[error("{0} has no exact provider owner chain")]
    OwnerChain(String),
    #[error("provider owner chain contains a cycle")]
    Cycle,
    #[error("provider owner API version is invalid")]
    ApiVersion,
    #[error("provider owner {0} is missing from the live inventory")]
    MissingOwner(String),
    #[error("provider owner {0} occurs more than once in the live inventory")]
    DuplicateOwner(String),
    #[error("provider owner UID mismatch")]
    OwnerUid,
    #[error("Cluster identity changed from {expected} to {actual}")]
    ClusterUid { expected: String, actual: String },
    #[error("Tenant kubeconfig Secret contract is invalid")]
    SecretContract,
    #[error("Tenant kubeconfig Secret ownership cannot be proven")]
    SecretOwner,
}

pub fn validate_root_ownership(
    object: &ObjectMeta,
    identity: Identity<'_>,
    resource: &str,
) -> Result<(), OwnershipError> {
    if object.uid.as_deref().is_none_or(str::is_empty) {
        return Err(OwnershipError::MissingUid(resource.into()));
    }
    if object
        .labels
        .as_ref()
        .and_then(|labels| labels.get(identity.ownership_label))
        .map(String::as_str)
        != Some(identity.lab_prefix)
    {
        return Err(OwnershipError::Markers(resource.into()));
    }
    validate_tenant_object_ownership(object, identity, resource)?;
    if object
        .owner_references
        .iter()
        .flatten()
        .any(|owner| owner.api_version == format!("{GROUP}/{VERSION}") && owner.kind == "Tenant")
    {
        return Err(OwnershipError::TenantOwner(resource.into()));
    }
    Ok(())
}

/// Tenant-cluster static objects intentionally check annotations only, not content.
pub fn validate_tenant_object_ownership(
    object: &ObjectMeta,
    identity: Identity<'_>,
    resource: &str,
) -> Result<(), OwnershipError> {
    if identity
        .annotations(resource)
        .iter()
        .any(|(key, value)| object.annotations.as_ref().and_then(|a| a.get(key)) != Some(value))
    {
        return Err(OwnershipError::Markers(resource.into()));
    }
    Ok(())
}

pub fn validate_cluster_uid(tenant: &Tenant, object: &ObjectMeta) -> Result<(), OwnershipError> {
    if let Some(expected) = tenant
        .status
        .as_ref()
        .and_then(|s| s.cluster_uid.as_deref())
        && !expected.is_empty()
        && object.uid.as_deref() != Some(expected)
    {
        return Err(OwnershipError::ClusterUid {
            expected: expected.into(),
            actual: object.uid.clone().unwrap_or_default(),
        });
    }
    Ok(())
}

fn kind(object: &DynamicObject) -> &str {
    object.types.as_ref().map_or("", |types| &types.kind)
}

fn description(object: &DynamicObject) -> String {
    format!(
        "{}/{}",
        kind(object),
        object.metadata.name.as_deref().unwrap_or("")
    )
}

fn owners(object: &DynamicObject) -> &[OwnerReference] {
    object
        .metadata
        .owner_references
        .as_deref()
        .unwrap_or_default()
}

fn lookup<'a>(
    inventory: &'a [DynamicObject],
    namespace: Option<&str>,
    api_version: &str,
    kind: &str,
    name: &str,
) -> Result<Option<&'a DynamicObject>, OwnershipError> {
    let mut matching = inventory.iter().filter(|object| {
        object
            .types
            .as_ref()
            .is_some_and(|types| types.api_version == api_version && types.kind == kind)
            && object.metadata.namespace.as_deref() == namespace
            && object.metadata.name.as_deref() == Some(name)
    });
    let found = matching.next();
    if matching.next().is_some() {
        return Err(OwnershipError::DuplicateOwner(format!("{kind}/{name}")));
    }
    Ok(found)
}

fn reference_matches(owner: &OwnerReference, object: &DynamicObject) -> bool {
    !owner.uid.is_empty()
        && object.metadata.uid.as_deref() == Some(&owner.uid)
        && object.metadata.name.as_deref() == Some(&owner.name)
        && object
            .types
            .as_ref()
            .is_some_and(|types| types.api_version == owner.api_version && types.kind == owner.kind)
}

pub fn validate_owner_chain(
    object: &DynamicObject,
    expected_root_uid: &str,
    inventory: &[DynamicObject],
) -> Result<(), OwnershipError> {
    if expected_root_uid.is_empty() {
        return Err(OwnershipError::MissingUid("provider root".into()));
    }
    let mut current = object;
    let mut visited = BTreeSet::new();
    loop {
        let uid = current
            .metadata
            .uid
            .as_deref()
            .filter(|uid| !uid.is_empty())
            .ok_or_else(|| OwnershipError::MissingUid(description(current)))?;
        if uid == expected_root_uid {
            return Ok(());
        }
        if !visited.insert(uid) {
            return Err(OwnershipError::Cycle);
        }
        let [owner] = owners(current) else {
            return Err(OwnershipError::OwnerChain(description(current)));
        };
        if owner.uid.is_empty() {
            return Err(OwnershipError::OwnerChain(description(current)));
        }
        let parts: Vec<_> = owner.api_version.split('/').collect();
        if parts.len() > 2 || parts.iter().any(|part| part.is_empty()) {
            return Err(OwnershipError::ApiVersion);
        }
        let next = lookup(
            inventory,
            current.metadata.namespace.as_deref(),
            &owner.api_version,
            &owner.kind,
            &owner.name,
        )?
        .ok_or_else(|| OwnershipError::MissingOwner(format!("{}/{}", owner.kind, owner.name)))?;
        if next.metadata.uid.as_deref() != Some(&owner.uid) {
            return Err(OwnershipError::OwnerUid);
        }
        current = next;
    }
}

pub fn validate_provider_owner(
    object: &DynamicObject,
    tenant_name: &str,
    required: bool,
    inventory: &[DynamicObject],
) -> Result<(), OwnershipError> {
    let references = owners(object);
    let definition = MANAGEMENT_RESOURCES
        .iter()
        .find(|resource| resource.kind == kind(object));
    let Some(definition) = definition else {
        return Ok(());
    };
    if definition.parent_kind.is_none() {
        return if references.is_empty() {
            Ok(())
        } else {
            Err(OwnershipError::ProviderOwner(description(object)))
        };
    }
    if definition.class != ResourceClass::Root {
        return Ok(());
    }
    if references.is_empty() {
        return if required {
            Err(OwnershipError::ProviderOwnerPending)
        } else {
            Ok(())
        };
    }
    let [owner] = references else {
        return Err(OwnershipError::ProviderOwner(description(object)));
    };
    for parent_kind in [definition.parent_kind, definition.alternate_parent_kind]
        .into_iter()
        .flatten()
    {
        let parent = MANAGEMENT_RESOURCES
            .iter()
            .find(|resource| resource.kind == parent_kind)
            .ok_or(OwnershipError::ApiVersion)?;
        let observed = lookup(
            inventory,
            Some(tenant_name),
            parent.api_version,
            parent.kind,
            &parent
                .expected_name(tenant_name)
                .ok_or(OwnershipError::ApiVersion)?,
        )?;
        if observed.is_some_and(|observed| reference_matches(owner, observed)) {
            return Ok(());
        }
    }
    Err(OwnershipError::ProviderOwner(description(object)))
}

/// Recorded Cluster identity remains usable after the Cluster has disappeared.
pub fn validate_provider_owner_for_deletion(
    object: &DynamicObject,
    tenant: &Tenant,
    inventory: &[DynamicObject],
) -> Result<(), OwnershipError> {
    let references = owners(object);
    if references.is_empty() {
        return Ok(());
    }
    let [owner] = references else {
        return Err(OwnershipError::ProviderOwner(description(object)));
    };
    let definition = MANAGEMENT_RESOURCES
        .iter()
        .find(|resource| resource.kind == kind(object));
    let Some(definition) = definition else {
        return Ok(());
    };
    if definition.parent_kind.is_none() {
        return Err(OwnershipError::ProviderOwner(description(object)));
    }
    if definition.class != ResourceClass::Root {
        return Ok(());
    }
    let tenant_name = tenant.metadata.name.as_deref().unwrap_or("");
    let cluster_uid = tenant
        .status
        .as_ref()
        .and_then(|status| status.cluster_uid.as_deref());
    if definition.parent_kind == Some("Cluster")
        && owner.api_version == CLUSTER_API_VERSION
        && owner.kind == "Cluster"
        && owner.name == tenant_name
        && !owner.uid.is_empty()
        && cluster_uid == Some(&owner.uid)
    {
        return Ok(());
    }
    if let Some(parent_kind) = definition.alternate_parent_kind {
        let parent = MANAGEMENT_RESOURCES
            .iter()
            .find(|resource| resource.kind == parent_kind)
            .ok_or(OwnershipError::ApiVersion)?;
        if owner.api_version != parent.api_version
            || owner.kind != parent.kind
            || Some(owner.name.clone()) != parent.expected_name(tenant_name)
        {
            return Err(OwnershipError::ProviderOwner(description(object)));
        }
        let observed = lookup(
            inventory,
            Some(tenant_name),
            parent.api_version,
            parent.kind,
            &owner.name,
        )?
        .ok_or_else(|| OwnershipError::MissingOwner(format!("{parent_kind}/{}", owner.name)))?;
        if reference_matches(owner, observed) {
            return Ok(());
        }
    }
    Err(OwnershipError::ProviderOwner(description(object)))
}

pub fn has_owner_uid(owners: &[OwnerReference], uid: &str) -> bool {
    !uid.is_empty() && owners.iter().any(|owner| owner.uid == uid)
}

pub fn validate_kubeconfig_secret(
    secret: &Secret,
    control_plane: Option<&DynamicObject>,
) -> Result<(), OwnershipError> {
    if secret.type_.as_deref() != Some("cluster.x-k8s.io/secret")
        || secret
            .data
            .as_ref()
            .and_then(|data| data.get("value"))
            .is_none_or(|v| v.0.is_empty())
    {
        return Err(OwnershipError::SecretContract);
    }
    let uid = control_plane
        .and_then(|cp| cp.metadata.uid.as_deref())
        .unwrap_or("");
    if !has_owner_uid(
        secret
            .metadata
            .owner_references
            .as_deref()
            .unwrap_or_default(),
        uid,
    ) {
        return Err(OwnershipError::SecretOwner);
    }
    Ok(())
}

pub fn validate_kubeconfig_secret_for_deletion(
    secret: &Secret,
    control_plane: Option<&DynamicObject>,
) -> Result<(), OwnershipError> {
    let Some(control_plane) = control_plane else {
        return Err(OwnershipError::SecretOwner);
    };
    let Some([owner]) = secret.metadata.owner_references.as_deref() else {
        return Err(OwnershipError::SecretOwner);
    };
    if owner.api_version != CONTROL_PLANE_API_VERSION
        || owner.kind != "KamajiControlPlane"
        || !reference_matches(owner, control_plane)
    {
        return Err(OwnershipError::SecretOwner);
    }
    Ok(())
}
