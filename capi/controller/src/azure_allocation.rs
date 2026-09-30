use ipnet::Ipv4Net;
use k8s_openapi::api::core::v1::ConfigMap;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

pub const CONFIG_NAME: &str = "tenant-azure-allocation";
pub const CONFIG_KEY: &str = "slots.json";
pub const APPROVED_SHA256_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/approved-allocation-sha256";

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AzureAllocationDocument { pub schema: u8, pub slots: Vec<AzureNetworkSlot> }

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
#[derive(Debug, Error, PartialEq, Eq)]
pub enum AzureAllocationError {
    #[error("Azure allocation ConfigMap slots.json is missing")] Missing,
    #[error("Azure allocation ConfigMap slots.json is invalid: {0}")] Json(String),
    #[error("Azure allocation ConfigMap UID is missing")] Uid,
    #[error("Azure allocation catalog schema must be 1")] Schema,
    #[error("Azure allocation catalog approval is missing or stale")] Approval,
    #[error("Azure allocation slot {0} is invalid")] Slot(String),
    #[error("Azure allocation networks overlap")] Overlap,
    #[error("Azure allocation network overlaps management network {0}")] Management(String),
}

impl AzureAllocationCatalog {
    #[rustfmt::skip]
    pub fn from_config_map(config: &ConfigMap) -> Result<Self, AzureAllocationError> {
        let raw = config.data.as_ref().and_then(|data| data.get(CONFIG_KEY))
            .ok_or(AzureAllocationError::Missing)?;
        let values: AzureAllocationDocument = serde_json::from_str(raw)
            .map_err(|error| AzureAllocationError::Json(error.to_string()))?;
        values.validate(&[])?;
        let value = serde_json::to_value(&values).map_err(|error| AzureAllocationError::Json(error.to_string()))?;
        let canonical = serde_json::to_vec(&value).map_err(|error| AzureAllocationError::Json(error.to_string()))?;
        let sha256 = hex::encode(Sha256::digest(canonical));
        if config.metadata.annotations.as_ref()
            .and_then(|annotations| annotations.get(APPROVED_SHA256_ANNOTATION))
            .map(String::as_str) != Some(sha256.as_str()) {
            return Err(AzureAllocationError::Approval);
        }
        let config_map_uid = config.metadata.uid.clone().filter(|value| !value.is_empty())
            .ok_or(AzureAllocationError::Uid)?;
        Ok(Self {
            values, config_map_uid, sha256,
        })
    }
}

impl AzureAllocationDocument {
    pub fn validate(&self, management: &[Ipv4Net]) -> Result<(), AzureAllocationError> {
        if self.schema != 1 {
            return Err(AzureAllocationError::Schema);
        }
        let mut ids = std::collections::BTreeSet::new();
        let mut networks = Vec::new();
        for slot in &self.slots {
            if !valid_name(&slot.slot_id) || !ids.insert(slot.slot_id.as_str()) {
                return Err(AzureAllocationError::Slot(slot.slot_id.clone()));
            }
            let pod = network(&slot.pod_cidr, &slot.slot_id)?;
            let service = network(&slot.service_cidr, &slot.slot_id)?;
            if service.prefix_len() > 28 {
                return Err(AzureAllocationError::Slot(slot.slot_id.clone()));
            }
            networks.extend([pod, service]);
        }
        for (index, network) in networks.iter().enumerate() {
            if networks[index + 1..]
                .iter()
                .any(|candidate| overlaps(network, candidate))
            {
                return Err(AzureAllocationError::Overlap);
            }
            if let Some(conflict) = management
                .iter()
                .find(|candidate| overlaps(network, candidate))
            {
                return Err(AzureAllocationError::Management(conflict.to_string()));
            }
        }
        Ok(())
    }
}

#[rustfmt::skip]
fn network(value: &str, slot: &str) -> Result<Ipv4Net, AzureAllocationError> {
    value.parse::<Ipv4Net>().ok().filter(|network| network.addr() == network.network())
        .ok_or_else(|| AzureAllocationError::Slot(slot.into()))
}
#[rustfmt::skip]
fn overlaps(left: &Ipv4Net, right: &Ipv4Net) -> bool { left.contains(&right.network()) || right.contains(&left.network()) }
#[rustfmt::skip]
fn valid_name(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=63).contains(&bytes.len())
        && bytes.first().is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && bytes.last().is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && bytes.iter().all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
    use std::collections::BTreeMap;

    fn document() -> AzureAllocationDocument {
        AzureAllocationDocument {
            schema: 1,
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
        values
            .validate(&["10.220.0.0/16".parse().unwrap()])
            .unwrap();
        let mut overlap = values.clone();
        overlap.slots[1].pod_cidr = "10.142.128.0/17".into();
        assert_eq!(overlap.validate(&[]), Err(AzureAllocationError::Overlap));
        assert!(matches!(
            values.validate(&["10.72.1.0/24".parse().unwrap()]),
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
            data: Some(BTreeMap::from([(CONFIG_KEY.into(), raw)])),
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
