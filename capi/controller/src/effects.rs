use std::collections::BTreeMap;
use std::future::Future;

use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::ConfigMap;

use crate::api::{Tenant, TenantStatus};

pub trait KubernetesEffects {
    type Error: std::error::Error + Send + Sync + 'static;

    fn get_tenant(
        &self,
        name: &str,
    ) -> impl Future<Output = Result<Option<Tenant>, Self::Error>> + Send;
    fn get_foundation(&self) -> impl Future<Output = Result<ConfigMap, Self::Error>> + Send;
    fn get_allocation_lease(
        &self,
        name: &str,
    ) -> impl Future<Output = Result<Option<Lease>, Self::Error>> + Send;
    fn create_allocation_lease(
        &self,
        lease: Lease,
    ) -> impl Future<Output = Result<Lease, Self::Error>> + Send;
    fn delete_allocation_lease(
        &self,
        name: &str,
        uid: &str,
        resource_version: &str,
    ) -> impl Future<Output = Result<(), Self::Error>> + Send;
    fn update_tenant_status(
        &self,
        name: &str,
        resource_version: &str,
        status: TenantStatus,
    ) -> impl Future<Output = Result<Tenant, Self::Error>> + Send;
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DockerVolume {
    pub name: String,
    pub mountpoint: String,
    pub labels: BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DockerContainer {
    pub id: String,
    pub name: String,
    pub labels: BTreeMap<String, String>,
}

pub trait DockerEffects {
    type Error: std::error::Error + Send + Sync + 'static;

    fn inspect_volume(
        &self,
        name: &str,
    ) -> impl Future<Output = Result<Option<DockerVolume>, Self::Error>> + Send;
    fn remove_volume(&self, name: &str) -> impl Future<Output = Result<(), Self::Error>> + Send;
    fn list_worker_containers(
        &self,
    ) -> impl Future<Output = Result<Vec<DockerContainer>, Self::Error>> + Send;
    fn inspect_container(
        &self,
        id: &str,
    ) -> impl Future<Output = Result<Option<DockerContainer>, Self::Error>> + Send;
}
