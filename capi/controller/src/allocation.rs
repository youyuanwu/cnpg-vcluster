//! Durable, per-slot Lease claims. The caller must validate the foundation and
//! Tenant spec before constructing a claim, and must establish terminal residue
//! absence independently before requesting a completed release.

use std::collections::{BTreeMap, HashSet};

use k8s_openapi::{api::coordination::v1::Lease, apimachinery::pkg::apis::meta::v1::ObjectMeta};
use kube::{
    Api, Client,
    api::{DeleteParams, ListParams, PostParams, Preconditions},
};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::{
    api::AllocationStatus,
    foundation::AllocationSlot,
    ownership::{
        FOUNDATION_ANNOTATION, RESOURCE_ANNOTATION, SPEC_HASH_ANNOTATION, TENANT_ANNOTATION,
        TENANT_UID_ANNOTATION,
    },
};

const PREFIX: &str = "tenant-slot-";
const SLOT_LABEL: &str = "tenancy.cnpg-vcluster.io/slot-id";
const ENDPOINT_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/endpoint";
const POD_CIDR_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/pod-cidr";
const SERVICE_CIDR_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/service-cidr";

#[derive(Clone, Copy)]
pub struct ClaimContext<'a> {
    pub namespace: &'a str,
    pub ownership_label: &'a str,
    pub lab_prefix: &'a str,
    pub tenant_name: &'a str,
    pub tenant_uid: &'a str,
    pub spec_hash: &'a str,
    pub foundation_hash: &'a str,
    pub slots: &'a [AllocationSlot],
}

#[derive(Debug, Error)]
pub enum AllocationError {
    #[error("allocation context or slot pool is invalid")]
    InvalidPool,
    #[error("Tenant UID has duplicate Lease claims")]
    Duplicate,
    #[error("Lease claim identity is foreign, malformed, or replaced: {0}")]
    Claim(String),
    #[error("Tenant name is held by a different Lease claim")]
    NameHeld,
    #[error("status-bound allocation does not match its Lease claim")]
    StatusMismatch,
    #[error("status-bound Lease is absent before terminal residue cleanup")]
    Missing,
    #[error("allocation slots are exhausted")]
    Exhausted,
    #[error(transparent)]
    Api(#[from] kube::Error),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Claim {
    pub slot: AllocationSlot,
    pub lease_uid: String,
    pub resource_version: String,
}

impl Claim {
    #[must_use]
    pub fn status(&self) -> AllocationStatus {
        AllocationStatus::from(&self.slot)
    }
}

impl From<&AllocationSlot> for AllocationStatus {
    fn from(slot: &AllocationSlot) -> Self {
        Self {
            slot_id: slot.slot_id.clone(),
            endpoint: slot.endpoint.clone(),
            pod_cidr: slot.pod_cidr.clone(),
            service_cidr: slot.service_cidr.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeleteIntent {
    pub name: String,
    pub uid: String,
    pub resource_version: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReleaseDecision {
    Delete(DeleteIntent),
    Pending,
    Complete,
}

#[must_use]
pub fn lease_name(slot_id: &str) -> String {
    let digest = hex::encode(Sha256::digest(slot_id.as_bytes()));
    format!("{PREFIX}{}", &digest[..63 - PREFIX.len()])
}

fn status_matches(slot: &AllocationSlot, status: &AllocationStatus) -> bool {
    slot.slot_id == status.slot_id
        && slot.endpoint == status.endpoint
        && slot.pod_cidr == status.pod_cidr
        && slot.service_cidr == status.service_cidr
}

fn dns_label(value: &str, max_len: usize) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= max_len
        && bytes
            .first()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && bytes
            .last()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && bytes
            .iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

fn valid_identity(context: &ClaimContext<'_>) -> Result<(), AllocationError> {
    if [
        context.namespace,
        context.ownership_label,
        context.lab_prefix,
        context.tenant_name,
        context.tenant_uid,
        context.spec_hash,
        context.foundation_hash,
    ]
    .iter()
    .any(|value| value.is_empty())
        || !dns_label(context.namespace, 63)
        || !dns_label(context.tenant_name, 30)
        || context.ownership_label == SLOT_LABEL
        || context.ownership_label == TENANT_ANNOTATION
    {
        return Err(AllocationError::InvalidPool);
    }
    Ok(())
}

fn valid_context(context: &ClaimContext<'_>) -> Result<(), AllocationError> {
    valid_identity(context)?;
    if context.slots.is_empty() {
        return Err(AllocationError::InvalidPool);
    }
    let mut names = HashSet::new();
    for slot in context.slots {
        if !dns_label(&slot.slot_id, 63) || !names.insert(lease_name(&slot.slot_id)) {
            return Err(AllocationError::InvalidPool);
        }
    }
    Ok(())
}

fn labels(context: &ClaimContext<'_>, slot: &AllocationStatus) -> BTreeMap<String, String> {
    [
        (context.ownership_label, context.lab_prefix),
        (SLOT_LABEL, slot.slot_id.as_str()),
        (TENANT_ANNOTATION, context.tenant_name),
    ]
    .into_iter()
    .map(|(key, value)| (key.into(), value.into()))
    .collect()
}

fn annotations(context: &ClaimContext<'_>, slot: &AllocationStatus) -> BTreeMap<String, String> {
    [
        (TENANT_ANNOTATION, context.tenant_name),
        (TENANT_UID_ANNOTATION, context.tenant_uid),
        (SPEC_HASH_ANNOTATION, context.spec_hash),
        (FOUNDATION_ANNOTATION, context.foundation_hash),
        (RESOURCE_ANNOTATION, "allocation-lease"),
        (SLOT_LABEL, slot.slot_id.as_str()),
        (ENDPOINT_ANNOTATION, slot.endpoint.as_str()),
        (POD_CIDR_ANNOTATION, slot.pod_cidr.as_str()),
        (SERVICE_CIDR_ANNOTATION, slot.service_cidr.as_str()),
    ]
    .into_iter()
    .map(|(key, value)| (key.into(), value.into()))
    .collect()
}

#[must_use]
pub fn new_lease(context: &ClaimContext<'_>, slot: &AllocationSlot) -> Lease {
    Lease {
        metadata: ObjectMeta {
            name: Some(lease_name(&slot.slot_id)),
            namespace: Some(context.namespace.into()),
            labels: Some(labels(context, &AllocationStatus::from(slot))),
            annotations: Some(annotations(context, &AllocationStatus::from(slot))),
            ..Default::default()
        },
        ..Default::default()
    }
}

fn claim(
    context: &ClaimContext<'_>,
    slot: &AllocationSlot,
    lease: &Lease,
) -> Result<Claim, AllocationError> {
    let verified = validate_claim(context, &AllocationStatus::from(slot), lease)?;
    Ok(Claim {
        slot: slot.clone(),
        lease_uid: verified.uid,
        resource_version: verified.resource_version,
    })
}

fn validate_claim(
    context: &ClaimContext<'_>,
    slot: &AllocationStatus,
    lease: &Lease,
) -> Result<DeleteIntent, AllocationError> {
    let name = lease_name(&slot.slot_id);
    if lease.metadata.name.as_deref() != Some(&name)
        || lease.metadata.namespace.as_deref() != Some(context.namespace)
        || lease.metadata.labels.as_ref() != Some(&labels(context, slot))
        || lease.metadata.annotations.as_ref() != Some(&annotations(context, slot))
        || lease
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || lease.metadata.deletion_timestamp.is_some()
        || lease
            .spec
            .as_ref()
            .is_some_and(|spec| spec != &Default::default())
    {
        return Err(AllocationError::Claim(name));
    }
    let uid = lease
        .metadata
        .uid
        .as_deref()
        .filter(|value| !value.is_empty());
    let rv = lease
        .metadata
        .resource_version
        .as_deref()
        .filter(|value| !value.is_empty());
    match (uid, rv) {
        (Some(uid), Some(rv)) => Ok(DeleteIntent {
            name,
            uid: uid.into(),
            resource_version: rv.into(),
        }),
        _ => Err(AllocationError::Claim(name)),
    }
}

fn annotation<'a>(lease: &'a Lease, key: &str) -> Result<&'a str, AllocationError> {
    lease
        .metadata
        .annotations
        .as_ref()
        .and_then(|annotations| annotations.get(key))
        .map(String::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AllocationError::Claim(lease.metadata.name.clone().unwrap_or_default()))
}

fn annotated_allocation(lease: &Lease) -> Result<AllocationStatus, AllocationError> {
    let allocation = AllocationStatus {
        slot_id: annotation(lease, SLOT_LABEL)?.into(),
        endpoint: annotation(lease, ENDPOINT_ANNOTATION)?.into(),
        pod_cidr: annotation(lease, POD_CIDR_ANNOTATION)?.into(),
        service_cidr: annotation(lease, SERVICE_CIDR_ANNOTATION)?.into(),
    };
    validate_allocation(&allocation)
        .map_err(|_| AllocationError::Claim(lease.metadata.name.clone().unwrap_or_default()))?;
    Ok(allocation)
}

fn validate_allocation(allocation: &AllocationStatus) -> Result<(), AllocationError> {
    if !dns_label(&allocation.slot_id, 63)
        || allocation.endpoint.parse::<std::net::Ipv4Addr>().is_err()
        || [&allocation.pod_cidr, &allocation.service_cidr]
            .iter()
            .any(|cidr| {
                !cidr
                    .parse::<ipnet::Ipv4Net>()
                    .is_ok_and(|net| net.addr() == net.network())
            })
    {
        return Err(AllocationError::StatusMismatch);
    }
    Ok(())
}

/// Recovers only a fully validated old claim, without consulting creation slots.
pub fn recover_allocation(
    context: &ClaimContext<'_>,
    leases: &[Lease],
) -> Result<Option<AllocationStatus>, AllocationError> {
    valid_identity(context)?;
    let mut matching = leases.iter().filter(|lease| {
        annotation(lease, TENANT_UID_ANNOTATION).is_ok_and(|uid| uid == context.tenant_uid)
    });
    let existing = matching.next();
    if matching.next().is_some() {
        return Err(AllocationError::Duplicate);
    }
    existing
        .map(|lease| {
            let allocation = annotated_allocation(lease)?;
            validate_claim(context, &allocation, lease)?;
            Ok(allocation)
        })
        .transpose()
}

fn validate_successor(
    context: &ClaimContext<'_>,
    lease: &Lease,
    leases: &[Lease],
    allocation: &AllocationStatus,
) -> Result<(), AllocationError> {
    let successor = ClaimContext {
        tenant_name: annotation(lease, TENANT_ANNOTATION)?,
        tenant_uid: annotation(lease, TENANT_UID_ANNOTATION)?,
        spec_hash: annotation(lease, SPEC_HASH_ANNOTATION)?,
        ..*context
    };
    valid_identity(&successor)?;
    validate_claim(&successor, allocation, lease)?;
    if leases
        .iter()
        .filter(|lease| {
            annotation(lease, TENANT_UID_ANNOTATION).is_ok_and(|uid| uid == successor.tenant_uid)
        })
        .count()
        != 1
    {
        return Err(AllocationError::Duplicate);
    }
    Ok(())
}

fn matching_claim<'a, 'b>(
    context: &'b ClaimContext<'_>,
    leases: &'a [Lease],
) -> Result<Option<(&'a Lease, &'b AllocationSlot)>, AllocationError> {
    if leases
        .iter()
        .filter(|lease| {
            lease
                .metadata
                .annotations
                .as_ref()
                .and_then(|a| a.get(TENANT_UID_ANNOTATION))
                .is_some_and(|uid| uid == context.tenant_uid)
        })
        .take(2)
        .count()
        > 1
    {
        return Err(AllocationError::Duplicate);
    }
    let mut found = None;
    for lease in leases {
        let meta = &lease.metadata;
        let same_uid = meta
            .annotations
            .as_ref()
            .and_then(|a| a.get(TENANT_UID_ANNOTATION))
            .is_some_and(|value| value == context.tenant_uid);
        let same_name = meta
            .annotations
            .as_ref()
            .and_then(|a| a.get(TENANT_ANNOTATION))
            .is_some_and(|value| value == context.tenant_name);
        let slot = context
            .slots
            .iter()
            .find(|slot| meta.name.as_deref() == Some(lease_name(&slot.slot_id).as_str()));
        if same_uid {
            let slot =
                slot.ok_or_else(|| AllocationError::Claim(meta.name.clone().unwrap_or_default()))?;
            claim(context, slot, lease)?;
            found = Some((lease, slot));
        } else if same_name {
            return Err(AllocationError::NameHeld);
        }
    }
    Ok(found)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ClaimDecision {
    Existing(Claim),
    Create(AllocationSlot),
}

/// The inventory must be an unfiltered, authoritative namespaced Lease list.
pub fn decide_claim(
    context: &ClaimContext<'_>,
    leases: &[Lease],
    bound: Option<&AllocationStatus>,
) -> Result<ClaimDecision, AllocationError> {
    valid_context(context)?;
    let existing = matching_claim(context, leases)?;
    if let Some(bound) = bound {
        let (lease, slot) = existing.ok_or(AllocationError::Missing)?;
        if !status_matches(slot, bound) {
            return Err(AllocationError::StatusMismatch);
        }
        return Ok(ClaimDecision::Existing(claim(context, slot, lease)?));
    }
    if let Some((lease, slot)) = existing {
        return Ok(ClaimDecision::Existing(claim(context, slot, lease)?));
    }
    for slot in context.slots {
        if !leases
            .iter()
            .any(|lease| lease.metadata.name.as_deref() == Some(lease_name(&slot.slot_id).as_str()))
        {
            return Ok(ClaimDecision::Create(slot.clone()));
        }
    }
    Err(AllocationError::Exhausted)
}

/// `all_old_residue_absent` is an authoritative proof supplied by the
/// finalizer, never inferred from this Lease inventory. Before that proof a
/// missing/reused status-bound claim cannot be treated as safely released.
/// Creation slots are not required: status and exact Lease annotations bind
/// the allocation throughout deletion.
pub fn decide_release(
    context: &ClaimContext<'_>,
    leases: &[Lease],
    bound: Option<&AllocationStatus>,
    all_old_residue_absent: bool,
) -> Result<ReleaseDecision, AllocationError> {
    valid_identity(context)?;
    let existing = recover_allocation(context, leases)?;
    for lease in leases {
        if annotation(lease, TENANT_ANNOTATION).is_ok_and(|name| name == context.tenant_name)
            && !annotation(lease, TENANT_UID_ANNOTATION).is_ok_and(|uid| uid == context.tenant_uid)
        {
            if !all_old_residue_absent {
                return Err(AllocationError::NameHeld);
            }
            validate_successor(context, lease, leases, &annotated_allocation(lease)?)?;
        }
    }
    if let Some(bound) = bound {
        validate_allocation(bound)?;
        if existing.as_ref().is_some_and(|existing| existing != bound) {
            return Err(AllocationError::StatusMismatch);
        }
        let expected_name = lease_name(&bound.slot_id);
        if let Some(lease) = leases
            .iter()
            .find(|lease| lease.metadata.name.as_deref() == Some(&expected_name))
        {
            let uid = lease
                .metadata
                .annotations
                .as_ref()
                .and_then(|annotations| annotations.get(TENANT_UID_ANNOTATION));
            if uid.is_none_or(String::is_empty) {
                return Err(AllocationError::Claim(expected_name));
            }
            if uid.map(String::as_str) != Some(context.tenant_uid)
                && all_old_residue_absent
                && existing.is_none()
            {
                validate_successor(context, lease, leases, bound)?;
                return Ok(ReleaseDecision::Complete);
            }
            let verified = validate_claim(context, bound, lease)?;
            if existing.is_none() {
                return Err(AllocationError::StatusMismatch);
            }
            if !all_old_residue_absent {
                return Ok(ReleaseDecision::Pending);
            }
            return Ok(ReleaseDecision::Delete(verified));
        }
        if existing.is_some() {
            return Err(AllocationError::StatusMismatch);
        }
        return if all_old_residue_absent {
            Ok(ReleaseDecision::Complete)
        } else {
            Err(AllocationError::Missing)
        };
    }
    if let Some(allocation) = existing {
        let lease = leases
            .iter()
            .find(|lease| {
                annotation(lease, TENANT_UID_ANNOTATION).is_ok_and(|uid| uid == context.tenant_uid)
            })
            .ok_or(AllocationError::Missing)?;
        let verified = validate_claim(context, &allocation, lease)?;
        if !all_old_residue_absent {
            return Ok(ReleaseDecision::Pending);
        }
        return Ok(ReleaseDecision::Delete(verified));
    }
    if all_old_residue_absent {
        Ok(ReleaseDecision::Complete)
    } else {
        Ok(ReleaseDecision::Pending)
    }
}

fn already_exists(error: &kube::Error) -> bool {
    matches!(error, kube::Error::Api(response) if response.code == 409 && response.reason == "AlreadyExists")
}

/// Scans for this Tenant UID before trying free slots. Create is the only
/// allocator mutation: the API server serializes races on each Lease name.
pub async fn allocate(
    client: Client,
    context: &ClaimContext<'_>,
    bound: Option<&AllocationStatus>,
) -> Result<Claim, AllocationError> {
    valid_context(context)?;
    let api: Api<Lease> = Api::namespaced(client, context.namespace);
    for _ in 0..=context.slots.len() {
        let inventory = api.list(&ListParams::default()).await?.items;
        match decide_claim(context, &inventory, bound)? {
            ClaimDecision::Existing(found) => {
                let live = api
                    .get_opt(&lease_name(&found.slot.slot_id))
                    .await?
                    .ok_or(AllocationError::Missing)?;
                let verified = claim(context, &found.slot, &live)?;
                return Ok(verified);
            }
            ClaimDecision::Create(slot) => {
                match api
                    .create(&PostParams::default(), &new_lease(context, &slot))
                    .await
                {
                    Ok(created) => {
                        let verified = claim(context, &slot, &created)?;
                        let fresh = api.list(&ListParams::default()).await?.items;
                        match decide_claim(context, &fresh, None)? {
                            ClaimDecision::Existing(observed)
                                if observed.lease_uid == verified.lease_uid =>
                            {
                                return Ok(observed);
                            }
                            ClaimDecision::Existing(_) | ClaimDecision::Create(_) => {
                                return Err(AllocationError::Claim(lease_name(&slot.slot_id)));
                            }
                        }
                    }
                    Err(error) if already_exists(&error) => {
                        let live = api.get_opt(&lease_name(&slot.slot_id)).await?;
                        if let Some(ref lease) = live
                            && lease
                                .metadata
                                .annotations
                                .as_ref()
                                .and_then(|a| a.get(TENANT_UID_ANNOTATION))
                                .is_some_and(|uid| uid == context.tenant_uid)
                        {
                            claim(context, &slot, lease)?;
                        }
                    }
                    Err(error) => return Err(error.into()),
                }
            }
        }
    }
    Err(AllocationError::Exhausted)
}

/// Only deletes an exact observed UID/resourceVersion. A failed precondition
/// is surfaced to the caller; it never triggers deletion of a successor.
pub async fn release(
    client: Client,
    context: &ClaimContext<'_>,
    bound: Option<&AllocationStatus>,
    all_old_residue_absent: bool,
) -> Result<ReleaseDecision, AllocationError> {
    valid_identity(context)?;
    let api: Api<Lease> = Api::namespaced(client, context.namespace);
    let inventory = api.list(&ListParams::default()).await?.items;
    match decide_release(context, &inventory, bound, all_old_residue_absent)? {
        ReleaseDecision::Delete(intent) => {
            let live = api.get_opt(&intent.name).await?;
            let Some(live) = live else {
                return if all_old_residue_absent {
                    Ok(ReleaseDecision::Complete)
                } else {
                    Err(AllocationError::Missing)
                };
            };
            let fresh = decide_release(context, &[live], bound, all_old_residue_absent)?;
            let ReleaseDecision::Delete(fresh) = fresh else {
                return Ok(fresh);
            };
            if fresh.uid != intent.uid {
                return Err(AllocationError::Claim(intent.name));
            }
            let params = DeleteParams {
                preconditions: Some(Preconditions {
                    uid: Some(fresh.uid),
                    resource_version: Some(fresh.resource_version),
                }),
                ..DeleteParams::default()
            };
            api.delete(&fresh.name, &params).await?;
            Ok(ReleaseDecision::Pending)
        }
        other => Ok(other),
    }
}
