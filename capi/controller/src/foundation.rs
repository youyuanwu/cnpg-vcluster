use std::collections::HashSet;
use std::net::Ipv4Addr;

use ipnet::Ipv4Net;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

const WORKER_IMAGES: [&str; 7] = [
    "CALICO_CNI_IMAGE",
    "CALICO_KUBE_CONTROLLERS_IMAGE",
    "CALICO_NODE_IMAGE",
    "KUBE_PROXY_IMAGE",
    "KONNECTIVITY_AGENT_IMAGE",
    "CNPG_CONTROLLER_IMAGE",
    "POSTGRES_IMAGE",
];

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum FoundationError {
    #[error("invalid foundation JSON: {0}")]
    Json(String),
    #[error("foundation checksum mismatch")]
    Checksum,
    #[error("Tenant foundation identity changed")]
    Identity,
    #[error("invalid foundation: {0}")]
    Invalid(String),
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AllocationSlot {
    pub slot_id: String,
    pub vip_ordinal: u32,
    pub endpoint: String,
    #[serde(rename = "podCIDR")]
    pub pod_cidr: String,
    #[serde(rename = "serviceCIDR")]
    pub service_cidr: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ImageArchive {
    pub key: String,
    pub path: String,
    pub sha256: String,
    pub reference: String,
    pub tagged: String,
    pub worker: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ImageCache {
    pub generation: String,
    pub image_archives: Vec<ImageArchive>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct OfflineRegistry {
    pub address: String,
    pub port: u16,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FoundationInputs {
    pub ownership_label: String,
    pub lab_prefix: String,
    pub api_port: u16,
    pub cluster_domain: String,
    pub node_image: String,
    pub cache_host_path: String,
    pub cache_container_path: String,
    pub storage_container_path: String,
    pub konnectivity_server_image: String,
    pub konnectivity_agent_image: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Foundation {
    pub schema: u32,
    pub network_id: String,
    pub subnet: String,
    #[serde(rename = "reservedCIDRs")]
    pub reserved_cidrs: Vec<String>,
    pub allowed_subnets: Vec<String>,
    pub kubernetes_version: String,
    pub controller_image: String,
    pub mutation_enabled: bool,
    pub offline_enforced: bool,
    pub slots: Vec<AllocationSlot>,
    pub cache: ImageCache,
    pub registry: Option<OfflineRegistry>,
    pub inputs: FoundationInputs,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DeletionFoundation {
    pub schema: u32,
    pub network_id: String,
    pub inputs: DeletionInputs,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DeletionInputs {
    pub ownership_label: String,
    pub lab_prefix: String,
    pub storage_container_path: String,
}

#[derive(Clone, Debug)]
pub struct VerifiedFoundation<T> {
    pub value: T,
    pub hash: String,
}

pub fn canonical_hash(raw_json: &str) -> Result<String, FoundationError> {
    let mut raw: Value =
        serde_json::from_str(raw_json).map_err(|err| FoundationError::Json(err.to_string()))?;
    let fields = raw
        .as_object_mut()
        .ok_or_else(|| invalid("foundation must be an object"))?;
    fields.remove("mutationEnabled");
    fields.remove("controllerImage");
    let canonical =
        serde_json::to_vec(&raw).map_err(|err| FoundationError::Json(err.to_string()))?;
    Ok(hex::encode(Sha256::digest(canonical)))
}

fn verified_raw(
    raw_json: &str,
    published_hash: &str,
    lifecycle_hash: Option<&str>,
) -> Result<(Value, String), FoundationError> {
    let actual = canonical_hash(raw_json)?;
    if published_hash != actual {
        return Err(FoundationError::Checksum);
    }
    if lifecycle_hash.is_some_and(|expected| !expected.is_empty() && expected != actual) {
        return Err(FoundationError::Identity);
    }
    let raw =
        serde_json::from_str(raw_json).map_err(|err| FoundationError::Json(err.to_string()))?;
    Ok((raw, actual))
}

pub fn parse_for_creation(
    raw_json: &str,
    published_hash: &str,
    lifecycle_hash: Option<&str>,
    supported_version: &str,
    expected_image: &str,
) -> Result<VerifiedFoundation<Foundation>, FoundationError> {
    let (raw, hash) = verified_raw(raw_json, published_hash, lifecycle_hash)?;
    let value: Foundation =
        serde_json::from_value(raw).map_err(|err| FoundationError::Json(err.to_string()))?;
    validate_creation(&value, supported_version, expected_image)?;
    Ok(VerifiedFoundation { value, hash })
}

pub fn parse_for_deletion(
    raw_json: &str,
    published_hash: &str,
    lifecycle_hash: Option<&str>,
) -> Result<VerifiedFoundation<DeletionFoundation>, FoundationError> {
    let (raw, hash) = verified_raw(raw_json, published_hash, lifecycle_hash)?;
    let value: DeletionFoundation =
        serde_json::from_value(raw).map_err(|err| FoundationError::Json(err.to_string()))?;
    if value.schema != 3
        || value.network_id.is_empty()
        || value.inputs.ownership_label.is_empty()
        || value.inputs.lab_prefix.is_empty()
        || !value.inputs.storage_container_path.starts_with('/')
    {
        return Err(invalid("deletion identity is incomplete"));
    }
    Ok(VerifiedFoundation { value, hash })
}

fn invalid(reason: impl Into<String>) -> FoundationError {
    FoundationError::Invalid(reason.into())
}

fn prefix(value: &str) -> Result<Ipv4Net, FoundationError> {
    let net: Ipv4Net = value
        .parse()
        .map_err(|_| invalid(format!("not an IPv4 CIDR: {value}")))?;
    if net.addr() != net.network() {
        return Err(invalid(format!("not a canonical IPv4 CIDR: {value}")));
    }
    Ok(net)
}

fn disjoint(left: &Ipv4Net, right: &Ipv4Net) -> bool {
    !left.contains(&right.network()) && !right.contains(&left.network())
}

fn cidrs(values: &[String]) -> Result<Vec<Ipv4Net>, FoundationError> {
    let mut result = Vec::with_capacity(values.len());
    for value in values {
        let net = prefix(value)?;
        if result.contains(&net) {
            return Err(invalid(format!("duplicate CIDR: {value}")));
        }
        result.push(net);
    }
    Ok(result)
}

pub fn validate_creation(
    f: &Foundation,
    supported_version: &str,
    expected_image: &str,
) -> Result<(), FoundationError> {
    if f.schema != 3 || f.network_id.is_empty() {
        return Err(invalid("schema or network identity"));
    }
    if f.kubernetes_version.trim_start_matches('v') != supported_version.trim_start_matches('v')
        || expected_image.is_empty()
        || f.controller_image != expected_image
    {
        return Err(invalid("Kubernetes version or controller image mismatch"));
    }
    let subnet = prefix(&f.subnet)?;
    let reserved = cidrs(&f.reserved_cidrs)?;
    for (index, cidr) in reserved.iter().enumerate() {
        if !disjoint(cidr, &subnet)
            || reserved[..index]
                .iter()
                .any(|previous| !disjoint(cidr, previous))
        {
            return Err(invalid(
                "reserved CIDRs overlap each other or management subnet",
            ));
        }
    }
    let allowed = cidrs(&f.allowed_subnets)?;
    if !allowed.contains(&subnet) {
        return Err(invalid("allowed subnets omit management subnet"));
    }
    let inputs = &f.inputs;
    if [
        &inputs.ownership_label,
        &inputs.lab_prefix,
        &inputs.cluster_domain,
        &inputs.node_image,
        &inputs.konnectivity_server_image,
        &inputs.konnectivity_agent_image,
    ]
    .iter()
    .any(|value| value.is_empty())
        || inputs.api_port == 0
        || [
            &inputs.cache_host_path,
            &inputs.cache_container_path,
            &inputs.storage_container_path,
        ]
        .iter()
        .any(|value| !value.starts_with('/'))
    {
        return Err(invalid("resource inputs are incomplete"));
    }
    if f.slots.is_empty() {
        return Err(invalid("allocation slots are missing"));
    }
    let mut ids = HashSet::new();
    let mut ordinals = HashSet::new();
    let mut endpoints = HashSet::new();
    let mut networks = reserved.clone();
    networks.push(subnet);
    for slot in &f.slots {
        let id = slot.slot_id.as_bytes();
        if id.is_empty()
            || id.len() > 63
            || !id[0].is_ascii_lowercase()
            || !(id[id.len() - 1].is_ascii_lowercase() || id[id.len() - 1].is_ascii_digit())
            || !id
                .iter()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
            || !ids.insert(slot.slot_id.as_str())
            || !ordinals.insert(slot.vip_ordinal)
        {
            return Err(invalid("invalid or duplicate allocation slot ID/ordinal"));
        }
        let endpoint: Ipv4Addr = slot
            .endpoint
            .parse()
            .map_err(|_| invalid("slot endpoint is not IPv4"))?;
        if !subnet.contains(&endpoint)
            || reserved.iter().any(|net| net.contains(&endpoint))
            || !endpoints.insert(endpoint)
        {
            return Err(invalid(
                "slot endpoint is outside management subnet, reserved or duplicate",
            ));
        }
        for value in [&slot.pod_cidr, &slot.service_cidr] {
            let net = prefix(value)?;
            if networks.iter().any(|existing| !disjoint(existing, &net)) {
                return Err(invalid(format!(
                    "slot CIDR overlaps another network: {value}"
                )));
            }
            networks.push(net);
        }
    }
    if f.cache.generation.is_empty()
        || !f
            .cache
            .generation
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.')
        || f.cache.generation == "."
        || f.cache.generation == ".."
    {
        return Err(invalid("cache generation is invalid"));
    }
    let mut keys = HashSet::new();
    for archive in &f.cache.image_archives {
        if archive.key.is_empty()
            || !keys.insert(archive.key.as_str())
            || archive.path.is_empty()
            || archive
                .path
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
            || archive.path.contains('\\')
            || archive.sha256.len() != 64
            || !archive.sha256.bytes().all(|b| b.is_ascii_hexdigit())
            || archive.reference.is_empty()
            || archive.tagged.is_empty()
        {
            return Err(invalid("cache archive is invalid or duplicated"));
        }
    }
    if WORKER_IMAGES.iter().any(|key| {
        !f.cache
            .image_archives
            .iter()
            .any(|archive| archive.key == *key && archive.worker)
    }) {
        return Err(invalid(
            "required worker archive is missing or not marked for preparation",
        ));
    }
    if f.offline_enforced
        && !f.registry.as_ref().is_some_and(|registry| {
            registry.port != 0
                && registry
                    .address
                    .parse::<Ipv4Addr>()
                    .is_ok_and(|address| subnet.contains(&address))
        })
    {
        return Err(invalid("offline registry is incomplete"));
    }
    Ok(())
}

impl AllocationSlot {
    pub fn api_endpoint(&self, port: u16) -> String {
        format!("{}:{port}", self.endpoint)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (String, String) {
        let fixture: Value =
            serde_json::from_str(include_str!("../tests/fixtures/foundation-schema3.json"))
                .unwrap();
        (
            fixture["foundation"].to_string(),
            fixture["sha256"].as_str().unwrap().into(),
        )
    }

    fn creation() -> Foundation {
        let (raw, hash) = fixture();
        parse_for_creation(&raw, &hash, None, "1.36.4", "controller:one")
            .unwrap()
            .value
    }

    #[test]
    fn golden_parity_and_raw_hashing() {
        let (raw, hash) = fixture();
        assert_eq!(canonical_hash(&raw).unwrap(), hash);
        assert_eq!(
            canonical_hash(&format!(" \n{} \n", raw.replace(',', ",\n"))).unwrap(),
            hash
        );
        let mut data: Value = serde_json::from_str(&raw).unwrap();
        data["controllerImage"] = "controller:two".into();
        data["mutationEnabled"] = false.into();
        assert_eq!(canonical_hash(&data.to_string()).unwrap(), hash);
        data["extraMetadata"] = serde_json::json!({"revision": 1});
        assert_ne!(canonical_hash(&data.to_string()).unwrap(), hash);
        assert!(matches!(
            parse_for_creation(&data.to_string(), &hash, None, "1.36.4", "controller:one"),
            Err(FoundationError::Checksum)
        ));
        let new_hash = canonical_hash(&data.to_string()).unwrap();
        assert!(matches!(
            parse_for_creation(
                &data.to_string(),
                &new_hash,
                None,
                "1.36.4",
                "controller:one"
            ),
            Err(FoundationError::Json(_))
        ));
    }

    #[test]
    fn creation_and_minimal_deletion() {
        let (raw, hash) = fixture();
        let created = parse_for_creation(&raw, &hash, None, "v1.36.4", "controller:one").unwrap();
        assert_eq!(
            created.value.slots[0].api_endpoint(created.value.inputs.api_port),
            "172.18.255.223:6443"
        );
        assert_eq!(created.hash, hash);
        assert!(matches!(
            parse_for_creation(&raw, &hash, Some("wrong"), "1.36.4", "controller:one"),
            Err(FoundationError::Identity)
        ));
        let minimal = serde_json::json!({"schema":3,"networkId":"network-one",
            "inputs":{"ownershipLabel":"example.io/owned","labPrefix":"example",
                "storageContainerPath":"/var/lib/storage"}});
        let raw = minimal.to_string();
        let hash = canonical_hash(&raw).unwrap();
        assert_eq!(
            parse_for_deletion(&raw, &hash, Some(&hash))
                .unwrap()
                .value
                .network_id,
            "network-one"
        );
        assert!(parse_for_creation(&raw, &hash, None, "1.36.4", "controller:one").is_err());
        assert!(parse_for_deletion(&raw, "bad", None).is_err());
        assert!(parse_for_deletion(&raw, &hash, Some("wrong")).is_err());
    }

    #[test]
    fn rejects_slot_conflicts_and_networks() {
        let original = creation();
        let mut bad = original.clone();
        bad.slots[0].slot_id = "Invalid".into();
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.slots.clear();
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        for mut slot in [
            AllocationSlot {
                vip_ordinal: original.slots[0].vip_ordinal,
                ..original.slots[0].clone()
            },
            AllocationSlot {
                endpoint: original.slots[0].endpoint.clone(),
                slot_id: "tenant-01".into(),
                vip_ordinal: 1,
                ..original.slots[0].clone()
            },
            AllocationSlot {
                pod_cidr: original.slots[0].pod_cidr.clone(),
                slot_id: "tenant-01".into(),
                vip_ordinal: 1,
                endpoint: "172.18.255.224".into(),
                ..original.slots[0].clone()
            },
        ] {
            slot.slot_id = "tenant-01".into();
            bad = original.clone();
            bad.slots.push(slot);
            assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        }
        for (pod, service, endpoint) in [
            ("10.73.0.1/16", "10.143.0.0/16", "172.18.255.223"),
            ("10.72.0.0/16", "10.143.0.0/16", "172.18.255.223"),
            ("10.73.0.0/16", "10.73.0.0/16", "172.18.255.223"),
            ("10.73.0.0/16", "10.143.0.0/16", "10.73.0.5"),
            ("10.73.0.0/16", "10.143.0.0/16", "2001:db8::1"),
        ] {
            bad = original.clone();
            bad.slots[0].pod_cidr = pod.into();
            bad.slots[0].service_cidr = service.into();
            bad.slots[0].endpoint = endpoint.into();
            assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        }
        bad = original.clone();
        bad.reserved_cidrs.push(bad.reserved_cidrs[0].clone());
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.subnet = "172.18.0.1/16".into();
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        let subnet = bad.subnet.clone();
        bad.allowed_subnets.retain(|net| net != &subnet);
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.reserved_cidrs.push("172.18.0.0/24".into());
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
    }

    #[test]
    fn rejects_invalid_archives_inputs_offline_and_identity() {
        let original = creation();
        let mut bad = original.clone();
        bad.cache.generation = "../escape".into();
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.cache.image_archives[0].path = "images/../escape".into();
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.cache.image_archives[0].worker = false;
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.cache.image_archives.remove(0);
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.cache
            .image_archives
            .push(bad.cache.image_archives[0].clone());
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.cache.image_archives[0].sha256 = "invalid".into();
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.inputs.api_port = 0;
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad = original.clone();
        bad.offline_enforced = true;
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_err());
        bad.registry = Some(OfflineRegistry {
            address: "172.18.0.10".into(),
            port: 5000,
        });
        assert!(validate_creation(&bad, "1.36.4", "controller:one").is_ok());
        assert!(validate_creation(&original, "1.35.0", "controller:one").is_err());
        assert!(validate_creation(&original, "1.36.4", "controller:other").is_err());
        let (raw, _) = fixture();
        let mut data: Value = serde_json::from_str(&raw).unwrap();
        data["inputs"]["storageContainerPath"] = "".into();
        let raw = data.to_string();
        assert!(parse_for_deletion(&raw, &canonical_hash(&raw).unwrap(), None).is_err());
    }
}
