//! Desired objects only: no Kubernetes, Docker, filesystem, or process effects.
//! Management provider objects and CNPG Cluster are dynamic. Namespaces and
//! other tenant objects are create-or-validate; bootstrap RBAC is content-validated.

mod access;
mod bootstrap;
mod cnpg;
mod controlplane;
mod manifest;
mod network;
mod workers;

pub use access::*;
pub use bootstrap::*;
pub use cnpg::*;
pub use controlplane::*;
pub use manifest::*;
pub use network::*;
pub use workers::*;

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use ipnet::IpNet;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use kube::core::{DynamicObject, TypeMeta};
use serde_json::{Value, json};
use thiserror::Error;

use crate::{
    api::{CanonicalSpec, Tenant},
    ownership::Identity,
};

pub use crate::foundation::FoundationInputs as Inputs;

/// Caller supplies the validated Tenant/spec, allocated network, and live mount.
#[derive(Clone, Debug)]
pub struct Context<'a> {
    pub tenant: &'a Tenant,
    pub spec: &'a CanonicalSpec,
    pub spec_hash: &'a str,
    pub foundation_hash: &'a str,
    /// Allocated host:port, with brackets for IPv6.
    pub endpoint: &'a str,
    pub pod_cidr: &'a str,
    pub service_cidr: &'a str,
    /// The inspected Docker volume mountpoint, never a constructed host path.
    pub volume_path: &'a str,
    pub worker_bootstrap_commands: &'a [String],
    pub inputs: &'a Inputs,
}

impl Context<'_> {
    pub fn name(&self) -> &str {
        self.tenant.metadata.name.as_deref().unwrap_or("")
    }

    pub fn identity(&self) -> Identity<'_> {
        Identity {
            tenant_name: self.name(),
            tenant_uid: self.tenant.metadata.uid.as_deref().unwrap_or(""),
            spec_hash: self.spec_hash,
            foundation_hash: self.foundation_hash,
            ownership_label: &self.inputs.ownership_label,
            lab_prefix: &self.inputs.lab_prefix,
        }
    }

    pub fn metadata(&self, name: &str, namespace: &str, resource: &str) -> ObjectMeta {
        ObjectMeta {
            name: Some(name.into()),
            namespace: (!namespace.is_empty()).then(|| namespace.into()),
            labels: Some(self.identity().labels()),
            annotations: Some(self.identity().annotations(resource)),
            ..Default::default()
        }
    }

    fn object(
        &self,
        api_version: &str,
        kind: &str,
        name: &str,
        namespace: &str,
        resource: &str,
        spec: Value,
    ) -> DynamicObject {
        DynamicObject {
            types: Some(TypeMeta {
                api_version: api_version.into(),
                kind: kind.into(),
            }),
            metadata: self.metadata(name, namespace, resource),
            data: if spec.is_null() {
                json!({})
            } else {
                json!({"spec":spec})
            },
        }
    }
}

#[derive(Debug, Error)]
pub enum BuildError {
    #[error("parse Tenant endpoint: {0}")]
    Endpoint(String),
    #[error("parse service CIDR: {0}")]
    ServiceCidr(String),
    #[error("service CIDR does not contain DNS service address")]
    DnsAddress,
    #[error("image reference must include a tag and digest")]
    ImageReference,
    #[error("decode manifest: {0}")]
    Yaml(#[from] serde_yaml::Error),
    #[error("invalid Kubernetes object: {0}")]
    Json(#[from] serde_json::Error),
    #[error("{0}")]
    Manifest(String),
    #[error("worker image {0} is missing, duplicated, or not marked for preparation")]
    WorkerImage(String),
    #[error("offline Tenant foundation registry is incomplete")]
    Registry,
    #[error("invalid worker bootstrap input: {0}")]
    Bootstrap(String),
}

pub fn dns_service_ip(cidr: &str) -> Result<String, BuildError> {
    let network: IpNet = cidr
        .parse()
        .map_err(|error: ipnet::AddrParseError| BuildError::ServiceCidr(error.to_string()))?;
    let address = match network.network() {
        IpAddr::V4(ip) => u32::from(ip)
            .checked_add(10)
            .map(Ipv4Addr::from)
            .map(IpAddr::V4),
        IpAddr::V6(ip) => u128::from(ip)
            .checked_add(10)
            .map(Ipv6Addr::from)
            .map(IpAddr::V6),
    }
    .ok_or(BuildError::DnsAddress)?;
    if !network.contains(&address) {
        return Err(BuildError::DnsAddress);
    }
    Ok(address.to_string())
}

fn endpoint(value: &str) -> Result<(&str, i32), BuildError> {
    let (host, port) = if let Some(rest) = value.strip_prefix('[') {
        rest.split_once("]:")
    } else {
        value
            .split_once(':')
            .filter(|(_, port)| !port.contains(':'))
    }
    .ok_or_else(|| BuildError::Endpoint("expected host:port".into()))?;
    let port: i32 = port
        .parse()
        .map_err(|error: std::num::ParseIntError| BuildError::Endpoint(error.to_string()))?;
    if host.is_empty() || !(1..=65535).contains(&port) || host.chars().any(char::is_whitespace) {
        return Err(BuildError::Endpoint("invalid host or port".into()));
    }
    Ok((host, port))
}

fn split_image(reference: &str) -> Result<(&str, String), BuildError> {
    let (tagged, digest) = reference
        .rsplit_once('@')
        .ok_or(BuildError::ImageReference)?;
    let (repository, tag) = tagged.rsplit_once(':').ok_or(BuildError::ImageReference)?;
    if repository.is_empty() || tag.is_empty() || tag.contains('/') || digest.is_empty() {
        return Err(BuildError::ImageReference);
    }
    Ok((repository, format!("{tag}@{digest}")))
}
