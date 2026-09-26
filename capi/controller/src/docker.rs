//! Direct Unix-socket Docker observations. Container mutations remain provider-owned.

use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;

use bollard::models::{ContainerInspectResponse, Volume, VolumeCreateRequest};
use bollard::query_parameters::{ListContainersOptionsBuilder, RemoveVolumeOptions};
use bollard::{API_DEFAULT_VERSION, Docker};

pub const WORKER_CLUSTER_LABEL: &str = "io.x-k8s.kind.cluster";
pub const WORKER_ROLE_LABEL: &str = "io.x-k8s.kind.role";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DockerContainer {
    pub id: String,
    pub name: String,
    pub labels: BTreeMap<String, String>,
    /// Network name to immutable network ID.
    pub networks: BTreeMap<String, String>,
    /// Immutable network ID to the container's IPv4 address.
    pub network_addresses: BTreeMap<String, String>,
    pub state: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DockerNetwork {
    pub id: String,
    pub subnets: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DockerVolume {
    pub name: String,
    pub created_at: String,
    pub mountpoint: String,
    pub labels: BTreeMap<String, String>,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum DockerError {
    #[error("Docker {operation} returned HTTP {status}")]
    Daemon {
        operation: &'static str,
        status: u16,
    },
    #[error("Docker {operation} transport or response decoding failed")]
    Transport { operation: &'static str },
    #[error("Docker identity is invalid: {0}")]
    Identity(&'static str),
}

impl DockerError {
    fn request(operation: &'static str, error: bollard::errors::Error) -> Self {
        match error {
            bollard::errors::Error::DockerResponseServerError { status_code, .. } => Self::Daemon {
                operation,
                status: status_code,
            },
            _ => Self::Transport { operation },
        }
    }
}

/// These are observations, not a container creation/deletion interface.
pub trait DockerClient: Send + Sync {
    fn inspect_container(
        &self,
        id: &str,
    ) -> impl Future<Output = Result<Option<DockerContainer>, DockerError>> + Send;
    fn inspect_network(
        &self,
        id: &str,
    ) -> impl Future<Output = Result<DockerNetwork, DockerError>> + Send;
    fn inspect_volume(
        &self,
        name: &str,
    ) -> impl Future<Output = Result<Option<DockerVolume>, DockerError>> + Send;
    fn create_volume(
        &self,
        name: &str,
        labels: &BTreeMap<String, String>,
    ) -> impl Future<Output = Result<DockerVolume, DockerError>> + Send;
    fn remove_volume(&self, name: &str) -> impl Future<Output = Result<(), DockerError>> + Send;
    fn list_containers(
        &self,
    ) -> impl Future<Output = Result<Vec<DockerContainer>, DockerError>> + Send;

    fn list_worker_containers(
        &self,
        identity: WorkerIdentity<'_>,
    ) -> impl Future<Output = Result<Vec<DockerContainer>, DockerError>> + Send {
        async move { worker_containers(self.list_containers().await?, identity) }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct WorkerIdentity<'a> {
    pub tenant_name: &'a str,
    pub network_id: &'a str,
    /// Exact names from the ownership-validated provider inventory, not a prefix.
    pub machine_names: &'a BTreeSet<String>,
}

/// Validate candidates before checking counts. A same-name container with foreign
/// labels and a same-tenant container with an unexpected name both block progress.
pub fn worker_containers(
    containers: Vec<DockerContainer>,
    identity: WorkerIdentity<'_>,
) -> Result<Vec<DockerContainer>, DockerError> {
    if identity.tenant_name.is_empty() || identity.network_id.is_empty() {
        return Err(DockerError::Identity("worker identity is incomplete"));
    }
    let mut selected = Vec::new();
    let mut ids = BTreeSet::new();
    let mut names = BTreeSet::new();
    let load_balancer_name = format!("{}-lb", identity.tenant_name);
    for container in containers {
        let tenant = container
            .labels
            .get(WORKER_CLUSTER_LABEL)
            .map(String::as_str);
        if tenant != Some(identity.tenant_name)
            && !identity.machine_names.contains(&container.name)
            && container.name != load_balancer_name
        {
            continue;
        }
        let load_balancer = container.name == load_balancer_name
            && !identity.machine_names.contains(&container.name)
            && container.labels.get(WORKER_ROLE_LABEL).map(String::as_str)
                == Some("external-load-balancer");
        if container.id.is_empty()
            || tenant != Some(identity.tenant_name)
            || (!load_balancer
                && (container.labels.get(WORKER_ROLE_LABEL).map(String::as_str) != Some("worker")
                    || !identity.machine_names.contains(&container.name)))
            || container.networks.len() != 1
            || !container
                .networks
                .values()
                .any(|network| network == identity.network_id)
            || !ids.insert(container.id.clone())
            || !names.insert(container.name.clone())
        {
            return Err(DockerError::Identity("worker ownership cannot be proven"));
        }
        if !load_balancer {
            selected.push(container);
        }
    }
    Ok(selected)
}

pub fn validate_volume(
    volume: &DockerVolume,
    name: &str,
    labels: &BTreeMap<String, String>,
) -> Result<(), DockerError> {
    if name.is_empty()
        || volume.name != name
        || volume.labels != *labels
        || volume.created_at.is_empty()
        || !volume.mountpoint.starts_with('/')
    {
        return Err(DockerError::Identity("volume ownership cannot be proven"));
    }
    Ok(())
}

#[derive(Clone)]
pub struct BollardDockerClient {
    docker: Docker,
}

impl BollardDockerClient {
    pub fn connect(socket: &str) -> Result<Self, DockerError> {
        if socket.is_empty() || socket.starts_with("tcp:") || socket.starts_with("http") {
            return Err(DockerError::Identity("a Unix socket path is required"));
        }
        let docker = Docker::connect_with_unix(socket, 120, API_DEFAULT_VERSION)
            .map_err(|error| DockerError::request("connect", error))?;
        Ok(Self { docker })
    }
}

fn path_identifier(value: &str) -> Result<(), DockerError> {
    if value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
        || value == "."
        || value == ".."
    {
        return Err(DockerError::Identity("invalid Docker path identifier"));
    }
    Ok(())
}

fn volume_identity(volume: Volume, expected_name: &str) -> Result<DockerVolume, DockerError> {
    let result = DockerVolume {
        name: volume.name,
        created_at: volume.created_at.unwrap_or_default(),
        mountpoint: volume.mountpoint,
        labels: volume.labels.into_iter().collect(),
    };
    if result.name != expected_name
        || result.created_at.is_empty()
        || !result.mountpoint.starts_with('/')
    {
        return Err(DockerError::Identity("volume inspection is incomplete"));
    }
    Ok(result)
}

fn container_identity(
    container: ContainerInspectResponse,
    expected_id: &str,
) -> Result<DockerContainer, DockerError> {
    let id = container.id.unwrap_or_default();
    let name = container.name.unwrap_or_default();
    let name = name.strip_prefix('/').unwrap_or(&name).to_owned();
    if id != expected_id || name.is_empty() {
        return Err(DockerError::Identity(
            "container inspection identity changed",
        ));
    }
    let labels = container
        .config
        .and_then(|config| config.labels)
        .unwrap_or_default()
        .into_iter()
        .collect();
    let endpoints = container
        .network_settings
        .and_then(|settings| settings.networks)
        .unwrap_or_default();
    let mut networks = BTreeMap::new();
    let mut network_addresses = BTreeMap::new();
    for (name, endpoint) in endpoints {
        let network_id = endpoint.network_id.unwrap_or_default();
        if network_id.is_empty() {
            return Err(DockerError::Identity("container network ID is missing"));
        }
        network_addresses.insert(network_id.clone(), endpoint.ip_address.unwrap_or_default());
        networks.insert(name, network_id);
    }
    Ok(DockerContainer {
        id,
        name,
        labels,
        networks,
        network_addresses,
        state: container
            .state
            .and_then(|state| state.status)
            .map(|status| status.to_string())
            .unwrap_or_default(),
    })
}

impl DockerClient for BollardDockerClient {
    async fn inspect_container(&self, id: &str) -> Result<Option<DockerContainer>, DockerError> {
        path_identifier(id)?;
        match self.docker.inspect_container(id, None).await {
            Ok(container) => container_identity(container, id).map(Some),
            Err(bollard::errors::Error::DockerResponseServerError {
                status_code: 404, ..
            }) => Ok(None),
            Err(error) => Err(DockerError::request("inspect container", error)),
        }
    }

    async fn inspect_network(&self, id: &str) -> Result<DockerNetwork, DockerError> {
        path_identifier(id)?;
        let network = self
            .docker
            .inspect_network(id, None)
            .await
            .map_err(|error| DockerError::request("inspect network", error))?;
        if network.id.as_deref() != Some(id) {
            return Err(DockerError::Identity("network inspection identity changed"));
        }
        Ok(DockerNetwork {
            id: id.into(),
            subnets: network
                .ipam
                .and_then(|ipam| ipam.config)
                .unwrap_or_default()
                .into_iter()
                .filter_map(|config| config.subnet.filter(|subnet| !subnet.is_empty()))
                .collect(),
        })
    }

    async fn inspect_volume(&self, name: &str) -> Result<Option<DockerVolume>, DockerError> {
        path_identifier(name)?;
        match self.docker.inspect_volume(name).await {
            Ok(volume) => volume_identity(volume, name).map(Some),
            Err(bollard::errors::Error::DockerResponseServerError {
                status_code: 404, ..
            }) => Ok(None),
            Err(error) => Err(DockerError::request("inspect volume", error)),
        }
    }

    async fn create_volume(
        &self,
        name: &str,
        labels: &BTreeMap<String, String>,
    ) -> Result<DockerVolume, DockerError> {
        path_identifier(name)?;
        let volume = self
            .docker
            .create_volume(VolumeCreateRequest {
                name: Some(name.into()),
                labels: Some(labels.clone().into_iter().collect()),
                ..Default::default()
            })
            .await
            .map_err(|error| DockerError::request("create volume", error))?;
        let volume = volume_identity(volume, name)?;
        validate_volume(&volume, name, labels)?;
        Ok(volume)
    }

    async fn remove_volume(&self, name: &str) -> Result<(), DockerError> {
        path_identifier(name)?;
        self.docker
            .remove_volume(name, None::<RemoveVolumeOptions>)
            .await
            .map_err(|error| DockerError::request("remove volume", error))
    }

    async fn list_containers(&self) -> Result<Vec<DockerContainer>, DockerError> {
        let listed = self
            .docker
            .list_containers(Some(
                ListContainersOptionsBuilder::default().all(true).build(),
            ))
            .await
            .map_err(|error| DockerError::request("list containers", error))?;
        let mut result = Vec::new();
        let mut seen = BTreeSet::new();
        for entry in listed {
            let id = entry
                .id
                .filter(|id| !id.is_empty())
                .ok_or(DockerError::Identity("listed container has no ID"))?;
            if !seen.insert(id.clone()) {
                return Err(DockerError::Identity("duplicate listed container ID"));
            }
            if let Some(container) = DockerClient::inspect_container(self, &id).await? {
                result.push(container);
            }
        }
        Ok(result)
    }
}

impl crate::effects::DockerEffects for BollardDockerClient {
    type Error = DockerError;

    async fn inspect_volume(
        &self,
        name: &str,
    ) -> Result<Option<crate::effects::DockerVolume>, Self::Error> {
        Ok(DockerClient::inspect_volume(self, name)
            .await?
            .map(|volume| crate::effects::DockerVolume {
                name: volume.name,
                mountpoint: volume.mountpoint,
                labels: volume.labels,
            }))
    }

    async fn remove_volume(&self, name: &str) -> Result<(), Self::Error> {
        DockerClient::remove_volume(self, name).await
    }

    async fn list_worker_containers(
        &self,
    ) -> Result<Vec<crate::effects::DockerContainer>, Self::Error> {
        // This legacy narrow seam has no Tenant identity. Never use it as an
        // ownership proof; the richer DockerClient seam validates names/network.
        Ok(self
            .list_containers()
            .await?
            .into_iter()
            .filter(|container| {
                container.labels.get(WORKER_ROLE_LABEL).map(String::as_str) == Some("worker")
            })
            .map(Into::into)
            .collect())
    }

    async fn inspect_container(
        &self,
        id: &str,
    ) -> Result<Option<crate::effects::DockerContainer>, Self::Error> {
        Ok(DockerClient::inspect_container(self, id)
            .await?
            .map(Into::into))
    }
}

impl From<DockerContainer> for crate::effects::DockerContainer {
    fn from(container: DockerContainer) -> Self {
        Self {
            id: container.id,
            name: container.name,
            labels: container.labels,
        }
    }
}
