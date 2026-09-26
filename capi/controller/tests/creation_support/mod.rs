#![allow(dead_code)]

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

use kube::{Client, ResourceExt, core::DynamicObject, runtime::controller::Action};
use serde_json::json;
use tenant_controller::{
    api::{Tenant, TenantSpec},
    docker::{DockerClient, DockerContainer, DockerError, DockerNetwork, DockerVolume},
    ownership::Identity,
    reconcile::{DeletionHandler, ReconcileError, TenantAccess},
    tenant_client::TenantClientError,
};

pub use crate::support::Server;

pub fn status(code: u16, reason: &str) -> serde_json::Value {
    crate::support::kube::status(code, reason)
}

pub fn tenant() -> Tenant {
    let mut value = Tenant::new(
        "tenant-a",
        TenantSpec {
            kubernetes_version: "1.36.4".into(),
            workers: 1,
            databases: 1,
        },
    );
    value.metadata.uid = Some("tenant-uid".into());
    value.metadata.resource_version = Some("1".into());
    value.metadata.generation = Some(2);
    value
}

pub fn identity() -> Identity<'static> {
    Identity {
        tenant_name: "tenant-a",
        tenant_uid: "tenant-uid",
        spec_hash: "spec-hash",
        foundation_hash: "foundation-hash",
        ownership_label: "example.io/owned",
        lab_prefix: "example",
    }
}

pub fn object(api: &str, kind: &str, namespace: &str, name: &str, role: &str) -> DynamicObject {
    let mut value: DynamicObject = serde_json::from_value(json!({
        "apiVersion":api,"kind":kind,
        "metadata":{"name":name,"uid":format!("{name}-uid"),"resourceVersion":"1","generation":2,
            "annotations":identity().annotations(role),"labels":identity().labels()}
    }))
    .unwrap();
    if !namespace.is_empty() {
        value.metadata.namespace = Some(namespace.into());
    }
    value
}

pub fn path(object: &DynamicObject) -> String {
    let types = object.types.as_ref().unwrap();
    let resource = tenant_controller::reconcile::objects::resource(&types.api_version, &types.kind);
    let mut path = if resource.group.is_empty() {
        format!("/api/{}", resource.version)
    } else {
        format!("/apis/{}/{}", resource.group, resource.version)
    };
    if let Some(namespace) = &object.metadata.namespace {
        path.push_str(&format!("/namespaces/{namespace}"));
    }
    path.push_str(&format!("/{}/{}", resource.plural, object.name_any()));
    path
}

#[derive(Clone, Default)]
pub struct FakeDocker {
    pub containers: Arc<Mutex<Vec<DockerContainer>>>,
    pub volumes: Arc<Mutex<BTreeMap<String, DockerVolume>>>,
    pub calls: Arc<Mutex<Vec<String>>>,
}

impl DockerClient for FakeDocker {
    async fn inspect_container(&self, _: &str) -> Result<Option<DockerContainer>, DockerError> {
        panic!("unexpected single container inspection")
    }
    async fn inspect_network(&self, _: &str) -> Result<DockerNetwork, DockerError> {
        panic!("unexpected network inspection")
    }
    async fn inspect_volume(&self, name: &str) -> Result<Option<DockerVolume>, DockerError> {
        self.calls.lock().unwrap().push(format!("inspect {name}"));
        Ok(self.volumes.lock().unwrap().get(name).cloned())
    }
    async fn create_volume(
        &self,
        name: &str,
        labels: &BTreeMap<String, String>,
    ) -> Result<DockerVolume, DockerError> {
        self.calls.lock().unwrap().push(format!("create {name}"));
        let volume = DockerVolume {
            name: name.into(),
            created_at: "2026-09-25".into(),
            mountpoint: "/var/lib/docker/owned/_data".into(),
            labels: labels.clone(),
        };
        self.volumes
            .lock()
            .unwrap()
            .insert(name.into(), volume.clone());
        Ok(volume)
    }
    async fn remove_volume(&self, _: &str) -> Result<(), DockerError> {
        panic!("creation cannot remove a volume")
    }
    async fn list_containers(&self) -> Result<Vec<DockerContainer>, DockerError> {
        self.calls.lock().unwrap().push("containers".into());
        Ok(self.containers.lock().unwrap().clone())
    }
}

pub struct FakeAccess(pub Client);
impl TenantAccess for FakeAccess {
    async fn connect(
        &self,
        _: Client,
        _: &DynamicObject,
        _: &str,
        endpoint: &str,
    ) -> Result<Client, TenantClientError> {
        assert!(endpoint.ends_with(":6443"));
        Ok(self.0.clone())
    }
}

#[derive(Clone, Default)]
pub struct FakeDeletion(pub Arc<Mutex<Vec<(String, String)>>>);
impl DeletionHandler for FakeDeletion {
    async fn reconcile(
        &self,
        tenant: &Tenant,
        supported_version: &str,
    ) -> Result<Action, ReconcileError> {
        self.0
            .lock()
            .unwrap()
            .push((tenant.name_any(), supported_version.into()));
        Ok(Action::requeue(std::time::Duration::from_secs(5)))
    }
}
