use std::collections::BTreeSet;

use super::BuildError;
use crate::foundation::Foundation;

pub use crate::foundation::{ImageArchive as WorkerArchive, OfflineRegistry};

pub const REQUIRED_WORKER_IMAGE_KEYS: [&str; 7] = [
    "CALICO_CNI_IMAGE",
    "CALICO_KUBE_CONTROLLERS_IMAGE",
    "CALICO_NODE_IMAGE",
    "KUBE_PROXY_IMAGE",
    "KONNECTIVITY_AGENT_IMAGE",
    "CNPG_CONTROLLER_IMAGE",
    "POSTGRES_IMAGE",
];

#[derive(Clone, Copy, Debug)]
pub struct BootstrapInputs<'a> {
    pub archives: &'a [WorkerArchive],
    pub generation: &'a str,
    pub cache_container_path: &'a str,
    pub storage_container_path: &'a str,
    pub offline_enforced: bool,
    pub registry: Option<&'a OfflineRegistry>,
    pub allowed_subnets: &'a [String],
}

impl<'a> From<&'a Foundation> for BootstrapInputs<'a> {
    fn from(foundation: &'a Foundation) -> Self {
        Self {
            archives: &foundation.cache.image_archives,
            generation: &foundation.cache.generation,
            cache_container_path: &foundation.inputs.cache_container_path,
            storage_container_path: &foundation.inputs.storage_container_path,
            offline_enforced: foundation.offline_enforced,
            registry: foundation.registry.as_ref(),
            allowed_subnets: &foundation.allowed_subnets,
        }
    }
}

pub fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

pub fn canonical_exact_reference(archive: &WorkerArchive) -> Result<String, BuildError> {
    let (_, digest) = archive
        .reference
        .rsplit_once('@')
        .filter(|(_, digest)| !digest.is_empty())
        .ok_or(BuildError::ImageReference)?;
    Ok(format!("{}@{digest}", archive.tagged))
}

pub fn runtime_digest_reference(archive: &WorkerArchive) -> Result<String, BuildError> {
    let (_, digest) = archive
        .reference
        .rsplit_once('@')
        .filter(|(_, digest)| !digest.is_empty())
        .ok_or(BuildError::ImageReference)?;
    let tagged = match archive.tagged.rsplit_once(':') {
        Some((repository, tag)) if !tag.contains('/') => repository,
        _ => &archive.tagged,
    };
    Ok(format!("{tagged}@{digest}"))
}

pub fn image_registry(reference: &str) -> &str {
    let value = reference.split('@').next().unwrap_or(reference);
    match value.split_once('/') {
        Some((first, _)) if first.contains(['.', ':']) || first == "localhost" => first,
        _ => "docker.io",
    }
}

pub fn worker_bootstrap_commands(
    inputs: BootstrapInputs<'_>,
    database_count: i32,
) -> Result<Vec<String>, BuildError> {
    if !(0..=3).contains(&database_count) {
        return Err(BuildError::Bootstrap(
            "database count must be between zero and three".into(),
        ));
    }
    if inputs.generation.is_empty()
        || inputs.generation.contains('/')
        || inputs.generation == "."
        || inputs.generation == ".."
        || !inputs.cache_container_path.starts_with('/')
        || !inputs.storage_container_path.starts_with('/')
    {
        return Err(BuildError::Bootstrap(
            "invalid generation or container path".into(),
        ));
    }
    let mut archives = Vec::new();
    for key in REQUIRED_WORKER_IMAGE_KEYS {
        let mut matching = inputs.archives.iter().filter(|archive| archive.key == key);
        let archive = matching
            .next()
            .filter(|archive| archive.worker)
            .ok_or_else(|| BuildError::WorkerImage(key.into()))?;
        if matching.next().is_some() {
            return Err(BuildError::WorkerImage(key.into()));
        }
        if archive.path.is_empty()
            || archive.path.starts_with('/')
            || archive
                .path
                .split('/')
                .any(|part| part == ".." || part == "." || part.is_empty())
            || archive.sha256.len() != 64
            || !archive.sha256.bytes().all(|b| b.is_ascii_hexdigit())
            || archive.tagged.is_empty()
            || archive.tagged.contains('@')
        {
            return Err(BuildError::Bootstrap(format!("invalid archive {key}")));
        }
        archives.push(archive);
    }
    archives.sort_by(|left, right| left.key.cmp(&right.key));
    let mut commands = Vec::new();
    for archive in archives {
        let path = shell_quote(&format!(
            "{}/generations/{}/{}",
            inputs.cache_container_path.trim_end_matches('/'),
            inputs.generation,
            archive.path
        ));
        let tagged = shell_quote(&archive.tagged);
        commands.extend([
            format!(
                "printf '%s  %s\\n' {} {path} | sha256sum -c -",
                shell_quote(&archive.sha256)
            ),
            format!("ctr --namespace k8s.io images import --digests {path}"),
            format!(
                "ctr --namespace k8s.io images tag --force {tagged} {}",
                shell_quote(&archive.reference)
            ),
            format!(
                "ctr --namespace k8s.io images tag --force {tagged} {}",
                shell_quote(&canonical_exact_reference(archive)?)
            ),
            format!(
                "ctr --namespace k8s.io images tag --force {tagged} {}",
                shell_quote(&runtime_digest_reference(archive)?)
            ),
        ]);
    }
    for ordinal in 1..=database_count {
        let directory = shell_quote(&format!(
            "{}/volumes/cnpg/{ordinal}",
            inputs.storage_container_path
        ));
        commands.push(format!(
            "mkdir -p {directory} && chown 26:26 {directory} && chmod 0700 {directory}"
        ));
    }
    if !inputs.offline_enforced {
        return Ok(commands);
    }
    let registry = inputs
        .registry
        .filter(|registry| !registry.address.is_empty() && registry.port != 0)
        .ok_or(BuildError::Registry)?;
    let registries: BTreeSet<_> = inputs
        .archives
        .iter()
        .filter(|archive| archive.worker)
        .map(|archive| image_registry(&archive.tagged))
        .collect();
    for name in registries {
        let server = if name == "docker.io" {
            "https://registry-1.docker.io".into()
        } else {
            format!("https://{name}")
        };
        let mirror = format!("http://{}:{}", registry.address, registry.port);
        let content = format!(
            "server = {}\n\n[host.{}]\n  capabilities = [\"pull\", \"resolve\"]\n",
            serde_json::to_string(&server)?,
            serde_json::to_string(&mirror)?
        );
        let directory = format!("/etc/containerd/certs.d/{name}");
        commands.push(format!(
            "umask 077; mkdir -p {}; printf %s {} > {}",
            shell_quote(&directory),
            shell_quote(&content),
            shell_quote(&format!("{directory}/hosts.toml"))
        ));
    }
    commands.extend([
        "iptables -N CAPI_OFFLINE 2>/dev/null || true".into(),
        "iptables -F CAPI_OFFLINE".into(),
    ]);
    for subnet in inputs.allowed_subnets {
        commands.push(format!(
            "iptables -A CAPI_OFFLINE -d {} -j RETURN",
            shell_quote(subnet)
        ));
    }
    commands.extend([
        "iptables -A CAPI_OFFLINE -p tcp -m multiport --dports 80,443 -j REJECT".into(),
        "iptables -A CAPI_OFFLINE -j RETURN".into(),
        "iptables -C OUTPUT -j CAPI_OFFLINE 2>/dev/null || iptables -I OUTPUT 1 -j CAPI_OFFLINE"
            .into(),
    ]);
    Ok(commands)
}
