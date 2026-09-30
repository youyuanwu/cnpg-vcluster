use ipnet::Ipv4Net;
use k8s_openapi::api::{coordination::v1::Lease, core::v1::ConfigMap};
use kube::{Api, Client, api::ListParams};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::{
    allocation::{self, AZURE_CATALOG_UID_LABEL, AllocationError, ClaimContext, ReleaseDecision},
    api::{AllocationStatus, AzureAllocationStatus},
    foundation::AllocationSlot,
    ownership::{FOUNDATION_ANNOTATION, TENANT_UID_ANNOTATION},
    reconcile::FOUNDATION_NAMESPACE,
};

pub const CONFIG_NAME: &str = "tenant-azure-allocation";
pub const APPROVED_SHA256_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/approved-allocation-sha256";
const PLACEHOLDER_ENDPOINT: &str = "0.0.0.0";

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AzureAllocationDocument { pub schema: u8, pub reserved_cidrs: Vec<String>, pub slots: Vec<AzureNetworkSlot> }

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AzureNetworkSlot {
    pub slot_id: String, #[serde(rename = "podCIDR")] pub pod_cidr: String,
    #[serde(rename = "serviceCIDR")] pub service_cidr: String,
}

#[rustfmt::skip]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AzureAllocationCatalog { pub values: AzureAllocationDocument, pub config_map_uid: String, pub sha256: String }

#[rustfmt::skip]
#[derive(Clone, Copy)]
pub struct AzureClaimIdentity<'a> { pub tenant_name: &'a str, pub tenant_uid: &'a str, pub spec_hash: &'a str }

#[rustfmt::skip]
#[derive(Debug, Error, PartialEq, Eq)]
pub enum AzureAllocationError {
    #[error("Azure allocation ConfigMap slots.json is missing")] Missing, #[error("Azure allocation ConfigMap slots.json is invalid: {0}")] Json(String),
    #[error("Azure allocation ConfigMap UID is missing")] Uid, #[error("Azure allocation catalog schema must be 1")] Schema,
    #[error("Azure allocation catalog approval is missing or stale")] Approval, #[error("Azure allocation slot {0} is invalid")] Slot(String),
    #[error("Azure allocation networks overlap")] Overlap, #[error("Azure allocation network overlaps management network {0}")] Management(String),
}

impl AzureAllocationCatalog {
    #[rustfmt::skip]
    pub fn from_config_map(config: &ConfigMap) -> Result<Self, AzureAllocationError> {
        let raw = config.data.as_ref().and_then(|data| data.get("slots.json")).ok_or(AzureAllocationError::Missing)?;
        let values: AzureAllocationDocument = serde_json::from_str(raw).map_err(|error| AzureAllocationError::Json(error.to_string()))?;
        values.validate()?;
        let canonical = serde_json::to_vec(&serde_json::to_value(&values)
            .map_err(|error| AzureAllocationError::Json(error.to_string()))?)
            .map_err(|error| AzureAllocationError::Json(error.to_string()))?;
        let sha256 = hex::encode(Sha256::digest(canonical));
        if config.metadata.annotations.as_ref().and_then(|annotations| annotations.get(APPROVED_SHA256_ANNOTATION))
            .map(String::as_str) != Some(sha256.as_str()) { return Err(AzureAllocationError::Approval); }
        let config_map_uid = config.metadata.uid.clone().filter(|value| !value.is_empty()).ok_or(AzureAllocationError::Uid)?;
        Ok(Self { values, config_map_uid, sha256 })
    }
}

impl AzureAllocationDocument {
    #[rustfmt::skip]
    pub fn validate(&self) -> Result<(), AzureAllocationError> {
        if self.schema != 1 { return Err(AzureAllocationError::Schema); }
        let mut ids = std::collections::BTreeSet::new();
        let mut networks = Vec::new();
        let reserved: Vec<Ipv4Net> = self.reserved_cidrs.iter().map(|value| value.parse())
            .collect::<Result<_, _>>().map_err(|_| AzureAllocationError::Management("invalid".into()))?;
        for slot in &self.slots {
            if !slot.slot_id.starts_with("azure-") || !valid_name(&slot.slot_id) || !ids.insert(slot.slot_id.as_str())
                { return Err(AzureAllocationError::Slot(slot.slot_id.clone())); }
            let pod = network(&slot.pod_cidr, &slot.slot_id)?;
            let service = network(&slot.service_cidr, &slot.slot_id)?;
            if service.prefix_len() > 28 { return Err(AzureAllocationError::Slot(slot.slot_id.clone())); }
            networks.extend([pod, service]);
        }
        for (index, network) in networks.iter().enumerate() {
            if networks[index + 1..].iter().any(|candidate| overlaps(network, candidate)) { return Err(AzureAllocationError::Overlap); }
            if let Some(conflict) = reserved.iter().find(|candidate| overlaps(network, candidate))
                { return Err(AzureAllocationError::Management(conflict.to_string())); }
        }
        Ok(())
    }
}

#[rustfmt::skip]
pub async fn claim(client: Client, catalog: &AzureAllocationCatalog, identity: AzureClaimIdentity<'_>) -> Result<AzureAllocationStatus, AllocationError> {
    validate_active_claims(client.clone(), catalog).await?;
    let slots = slots(&catalog.values); let context = context(identity, &catalog.config_map_uid, &catalog.sha256, &slots);
    let claim = allocation::allocate(client, &context, None).await?; Ok(status(catalog, &claim.slot, &claim.lease_uid))
}

#[rustfmt::skip]
async fn validate_active_claims(client: Client, catalog: &AzureAllocationCatalog) -> Result<(), AllocationError> {
    let api: Api<Lease> = Api::namespaced(client, FOUNDATION_NAMESPACE); for lease in api.list(&ListParams::default()).await?.items.into_iter()
        .filter(|lease| lease.metadata.name.as_deref().is_some_and(|name| name.starts_with("tenant-azure-slot-"))) {
        let name = lease.metadata.name.clone().unwrap_or_default();
        let pod: Ipv4Net = metadata(&lease, false, allocation::POD_CIDR_ANNOTATION).and_then(|value| value.parse().ok())
            .ok_or_else(|| AllocationError::Claim(name.clone()))?;
        let service: Ipv4Net = metadata(&lease, false, allocation::SERVICE_CIDR_ANNOTATION).and_then(|value| value.parse().ok())
            .ok_or_else(|| AllocationError::Claim(name.clone()))?;
        for slot in &catalog.values.slots {
            let exact = name == allocation::lease_name(&slot.slot_id) && slot.pod_cidr == pod.to_string() && slot.service_cidr == service.to_string();
            let slot_pod: Ipv4Net = slot.pod_cidr.parse().map_err(|_| AllocationError::InvalidPool)?;
            let slot_service: Ipv4Net = slot.service_cidr.parse().map_err(|_| AllocationError::InvalidPool)?;
            if !exact && [pod, service].iter().any(|active| overlaps(active, &slot_pod) || overlaps(active, &slot_service))
                { return Err(AllocationError::Claim(name)); }
        }
    }
    Ok(())
}

#[rustfmt::skip]
pub async fn validate_recorded(client: Client, identity: AzureClaimIdentity<'_>, recorded: &AzureAllocationStatus) -> Result<(), AllocationError> {
    let slots = [slot(recorded)];
    let context = context(identity, &recorded.catalog_uid, &recorded.catalog_sha256, &slots);
    let claim = allocation::allocate(client, &context, Some(&local(recorded))).await?;
    if claim.lease_uid != recorded.lease_uid || allocation::lease_name(&recorded.slot_id) != recorded.lease_name {
        return Err(AllocationError::StatusMismatch);
    }
    Ok(())
}

#[rustfmt::skip]
pub async fn recover(client: Client, identity: AzureClaimIdentity<'_>) -> Result<Option<AzureAllocationStatus>, AllocationError> {
    let api: Api<Lease> = Api::namespaced(client, FOUNDATION_NAMESPACE);
    let leases = api.list(&ListParams::default()).await?.items;
    let mut matching = leases.iter().filter(|lease| metadata(lease, false, TENANT_UID_ANNOTATION) == Some(identity.tenant_uid));
    let Some(lease) = matching.next() else { return Ok(None); };
    if matching.next().is_some() { return Err(AllocationError::Duplicate); }
    let name = lease.metadata.name.clone().unwrap_or_default();
    let catalog_uid = metadata(lease, true, AZURE_CATALOG_UID_LABEL).ok_or_else(|| AllocationError::Claim(name.clone()))?;
    let catalog_sha = metadata(lease, false, FOUNDATION_ANNOTATION).ok_or_else(|| AllocationError::Claim(name.clone()))?;
    let context = context(identity, catalog_uid, catalog_sha, &[]);
    let allocation = allocation::recover_allocation(&context, &leases)?.ok_or(AllocationError::Missing)?;
    let uid = lease.metadata.uid.clone().filter(|value| !value.is_empty()).ok_or_else(|| AllocationError::Claim(name))?;
    Ok(Some(AzureAllocationStatus {
        slot_id: allocation.slot_id.clone(), pod_cidr: allocation.pod_cidr, service_cidr: allocation.service_cidr,
        catalog_uid: catalog_uid.into(), catalog_sha256: catalog_sha.into(),
        lease_name: allocation::lease_name(&allocation.slot_id), lease_uid: uid,
    }))
}

#[rustfmt::skip]
pub async fn release(client: Client, identity: AzureClaimIdentity<'_>, recorded: Option<&AzureAllocationStatus>, residue_absent: bool) -> Result<ReleaseDecision, AllocationError> {
    let recovered;
    let recorded = match recorded {
        Some(recorded) => recorded,
        None => {
            recovered = recover(client.clone(), identity).await?;
            let Some(recorded) = recovered.as_ref() else {
                return Ok(if residue_absent { ReleaseDecision::Complete } else { ReleaseDecision::Pending });
            };
            recorded
        }
    };
    let api: Api<Lease> = Api::namespaced(client.clone(), FOUNDATION_NAMESPACE);
    if let Some(live) = api.get_opt(&recorded.lease_name).await? && metadata(&live, false, TENANT_UID_ANNOTATION) == Some(identity.tenant_uid)
        && live.metadata.uid.as_deref() != Some(recorded.lease_uid.as_str()) {
        return Err(AllocationError::StatusMismatch);
    }
    let context = context(identity, &recorded.catalog_uid, &recorded.catalog_sha256, &[]);
    allocation::release(client, &context, Some(&local(recorded)), residue_absent).await
}

#[rustfmt::skip]
fn slots(document: &AzureAllocationDocument) -> Vec<AllocationSlot> {
    document.slots.iter().map(|slot| AllocationSlot {
        slot_id: slot.slot_id.clone(), vip_ordinal: 0, endpoint: PLACEHOLDER_ENDPOINT.into(), pod_cidr: slot.pod_cidr.clone(), service_cidr: slot.service_cidr.clone(),
    }).collect()
}
#[rustfmt::skip]
fn slot(status: &AzureAllocationStatus) -> AllocationSlot {
    AllocationSlot { slot_id: status.slot_id.clone(), vip_ordinal: 0, endpoint: PLACEHOLDER_ENDPOINT.into(), pod_cidr: status.pod_cidr.clone(), service_cidr: status.service_cidr.clone() }
}
#[rustfmt::skip]
fn local(status: &AzureAllocationStatus) -> AllocationStatus {
    AllocationStatus { slot_id: status.slot_id.clone(), endpoint: PLACEHOLDER_ENDPOINT.into(), pod_cidr: status.pod_cidr.clone(), service_cidr: status.service_cidr.clone() }
}
#[rustfmt::skip]
fn context<'a>(identity: AzureClaimIdentity<'a>, catalog_uid: &'a str, catalog_sha: &'a str, slots: &'a [AllocationSlot]) -> ClaimContext<'a> {
    ClaimContext { namespace: FOUNDATION_NAMESPACE, ownership_label: AZURE_CATALOG_UID_LABEL, lab_prefix: catalog_uid,
        tenant_name: identity.tenant_name, tenant_uid: identity.tenant_uid, spec_hash: identity.spec_hash, foundation_hash: catalog_sha, slots }
}
#[rustfmt::skip]
fn status(catalog: &AzureAllocationCatalog, slot: &AllocationSlot, lease_uid: &str) -> AzureAllocationStatus {
    AzureAllocationStatus { slot_id: slot.slot_id.clone(), pod_cidr: slot.pod_cidr.clone(), service_cidr: slot.service_cidr.clone(),
        catalog_uid: catalog.config_map_uid.clone(), catalog_sha256: catalog.sha256.clone(), lease_name: allocation::lease_name(&slot.slot_id), lease_uid: lease_uid.into() }
}
#[rustfmt::skip]
fn metadata<'a>(lease: &'a Lease, label: bool, key: &str) -> Option<&'a str> {
    (if label { lease.metadata.labels.as_ref() } else { lease.metadata.annotations.as_ref() }).and_then(|values| values.get(key)).map(String::as_str).filter(|value| !value.is_empty())
}
#[rustfmt::skip]
fn network(value: &str, slot: &str) -> Result<Ipv4Net, AzureAllocationError> {
    value.parse::<Ipv4Net>().ok().filter(|network| network.addr() == network.network()).ok_or_else(|| AzureAllocationError::Slot(slot.into()))
}
#[rustfmt::skip]
fn overlaps(left: &Ipv4Net, right: &Ipv4Net) -> bool { left.contains(&right.network()) || right.contains(&left.network()) }
#[rustfmt::skip]
fn valid_name(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=63).contains(&bytes.len()) && bytes.first().is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && bytes.last().is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit()) && bytes.iter().all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
    use std::collections::BTreeMap;

    fn document() -> AzureAllocationDocument {
        AzureAllocationDocument {
            schema: 1,
            reserved_cidrs: vec![
                "10.220.0.0/16".into(),
                "10.221.0.0/16".into(),
                "10.222.0.0/16".into(),
            ],
            slots: vec![
                AzureNetworkSlot {
                    slot_id: "azure-01".into(),
                    pod_cidr: "10.72.0.0/16".into(),
                    service_cidr: "10.142.0.0/16".into(),
                },
                AzureNetworkSlot {
                    slot_id: "azure-02".into(),
                    pod_cidr: "10.73.0.0/16".into(),
                    service_cidr: "10.143.0.0/16".into(),
                },
            ],
        }
    }

    #[test]
    fn validates_all_pairs_and_management_ranges() {
        let values = document();
        values.validate().unwrap();
        let mut overlap = values.clone();
        overlap.slots[1].pod_cidr = "10.142.128.0/17".into();
        assert_eq!(overlap.validate(), Err(AzureAllocationError::Overlap));
        let mut management = values;
        management.reserved_cidrs.push("10.72.1.0/24".into());
        assert!(matches!(
            management.validate(),
            Err(AzureAllocationError::Management(_))
        ));
    }

    #[test]
    fn config_map_requires_exact_hash_approval() {
        let values = document();
        let raw = serde_json::to_string(&values).unwrap();
        let hash = hex::encode(Sha256::digest(
            serde_json::to_vec(&serde_json::to_value(&values).unwrap()).unwrap(),
        ));
        let config = ConfigMap {
            metadata: ObjectMeta {
                uid: Some("config-uid".into()),
                annotations: Some(BTreeMap::from([(
                    APPROVED_SHA256_ANNOTATION.into(),
                    hash.clone(),
                )])),
                ..Default::default()
            },
            data: Some(BTreeMap::from([("slots.json".into(), raw)])),
            ..Default::default()
        };
        assert_eq!(
            AzureAllocationCatalog::from_config_map(&config)
                .unwrap()
                .sha256,
            hash
        );
    }
}
