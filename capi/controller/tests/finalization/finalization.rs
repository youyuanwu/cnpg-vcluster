use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

use crate::support::Server;
use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::Namespace;
use kube::{ResourceExt, core::DynamicObject, runtime::controller::Action};
use serde_json::{Value, json};
use tenant_controller::{
    allocation::{ClaimContext, new_lease},
    api::{
        SUPPORTED_KUBERNETES_VERSION, Tenant, TenantPhase, TenantSpec, TenantStatus,
        canonical_spec, spec_hash,
    },
    docker::{DockerClient, DockerContainer, DockerError, DockerNetwork, DockerVolume},
    error::ControllerError,
    finalize::Finalizer,
    foundation::{AllocationSlot, RuntimeFoundation, canonical_hash, parse_runtime},
    management::MANAGEMENT_RESOURCES,
    ownership::Identity,
};

const NAME: &str = "tenant-a";
const UID: &str = "tenant-uid";
const FINALIZER: &str = "tenancy.cnpg-vcluster.io/finalizer";
const TENANT_PATH: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants/tenant-a";
const LEASES: &str = "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases";
const NAMESPACE: &str = "/api/v1/namespaces/tenant-a";

fn fixture() -> (Arc<RuntimeFoundation>, String, AllocationSlot) {
    let value: Value =
        serde_json::from_str(include_str!("../fixtures/foundation-schema3.json")).unwrap();
    let raw = value["foundation"].to_string();
    let hash = canonical_hash(&raw).unwrap();
    let slot = serde_json::from_value(value["foundation"]["slots"][0].clone()).unwrap();
    (
        Arc::new(
            parse_runtime(&raw, &hash, SUPPORTED_KUBERNETES_VERSION, "controller:one").unwrap(),
        ),
        hash,
        slot,
    )
}

fn tenant(hash: &str) -> Tenant {
    let mut tenant = Tenant::new(
        NAME,
        TenantSpec {
            kubernetes_version: "1.36.4".into(),
            workers: 1,
            databases: 1,
        },
    );
    tenant.metadata.uid = Some(UID.into());
    tenant.metadata.resource_version = Some("1".into());
    tenant.metadata.generation = Some(1);
    tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-25T00:00:00Z")).unwrap());
    tenant.status = Some(TenantStatus {
        phase: Some(TenantPhase::Deleting),
        foundation_hash: Some(hash.into()),
        ..Default::default()
    });
    tenant
}

fn identity<'a>(hash: &'a str) -> Identity<'a> {
    Identity {
        tenant_name: NAME,
        tenant_uid: UID,
        spec_hash: Box::leak(
            spec_hash(
                &canonical_spec(NAME, &tenant(hash).spec, SUPPORTED_KUBERNETES_VERSION).unwrap(),
            )
            .into_boxed_str(),
        ),
        foundation_hash: hash,
        ownership_label: "example.io/owned",
        lab_prefix: "example",
    }
}

fn path(object: &DynamicObject) -> String {
    crate::creation_support::path(object)
}

fn root(hash: &str) -> DynamicObject {
    let resource = MANAGEMENT_RESOURCES
        .iter()
        .find(|resource| resource.kind == "Cluster")
        .unwrap();
    let mut object = crate::creation_support::object(
        resource.api_version,
        resource.kind,
        NAME,
        NAME,
        resource.role,
    );
    object.metadata.uid = Some("cluster-uid".into());
    object.metadata.annotations = Some(identity(hash).annotations(resource.role));
    object
}

fn namespace(hash: &str) -> Namespace {
    Namespace {
        metadata: k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta {
            name: Some(NAME.into()),
            uid: Some("namespace-uid".into()),
            resource_version: Some("1".into()),
            annotations: Some(identity(hash).annotations("namespace")),
            labels: Some(identity(hash).labels()),
            ..Default::default()
        },
        ..Default::default()
    }
}

fn lease(hash: &str, slot: &AllocationSlot) -> Lease {
    let slots = [slot.clone()];
    let context = ClaimContext {
        namespace: "tenant-system",
        ownership_label: "example.io/owned",
        lab_prefix: "example",
        tenant_name: NAME,
        tenant_uid: UID,
        spec_hash: identity(hash).spec_hash,
        foundation_hash: hash,
        slots: &slots,
    };
    let mut lease = new_lease(&context, slot);
    lease.metadata.uid = Some("lease-uid".into());
    lease.metadata.resource_version = Some("1".into());
    lease
}

fn successor_lease(hash: &str, slot: &AllocationSlot) -> Lease {
    let slots = [slot.clone()];
    let context = ClaimContext {
        namespace: "tenant-system",
        ownership_label: "example.io/owned",
        lab_prefix: "example",
        tenant_name: "tenant-b",
        tenant_uid: "successor-uid",
        spec_hash: "successor-spec",
        foundation_hash: hash,
        slots: &slots,
    };
    let mut lease = new_lease(&context, slot);
    lease.metadata.uid = Some("successor-lease".into());
    lease.metadata.resource_version = Some("2".into());
    lease
}

fn server(tenant: &Tenant) -> Server {
    let server = Server::default();
    server.insert(TENANT_PATH, tenant);
    server.allow_list(LEASES);
    for resource in
        tenant_controller::management::descendants().filter(|resource| resource.role != "provider")
    {
        let (group, version) = resource.api_version.split_once('/').unwrap();
        server.allow_list(&format!(
            "/apis/{group}/{version}/namespaces/{NAME}/{}",
            resource.plural
        ));
    }
    server
}

#[derive(Clone, Default)]
struct Docker {
    volume: Arc<Mutex<Option<DockerVolume>>>,
    containers: Arc<Mutex<Vec<DockerContainer>>>,
    fail: Arc<Mutex<bool>>,
    fail_remove: Arc<Mutex<bool>>,
    calls: Arc<Mutex<Vec<String>>>,
}

impl DockerClient for Docker {
    async fn inspect_container(&self, _: &str) -> Result<Option<DockerContainer>, DockerError> {
        panic!("unexpected container inspection")
    }
    async fn inspect_network(&self, _: &str) -> Result<DockerNetwork, DockerError> {
        panic!("unexpected network inspection")
    }
    async fn inspect_volume(&self, _: &str) -> Result<Option<DockerVolume>, DockerError> {
        self.calls.lock().unwrap().push("inspect volume".into());
        Ok(self.volume.lock().unwrap().clone())
    }
    async fn create_volume(
        &self,
        _: &str,
        _: &BTreeMap<String, String>,
    ) -> Result<DockerVolume, DockerError> {
        panic!("finalization cannot create a volume")
    }
    async fn remove_volume(&self, _: &str) -> Result<(), DockerError> {
        if *self.fail_remove.lock().unwrap() {
            return Err(DockerError::Transport {
                operation: "remove volume",
            });
        }
        self.calls.lock().unwrap().push("remove volume".into());
        *self.volume.lock().unwrap() = None;
        Ok(())
    }
    async fn list_containers(&self) -> Result<Vec<DockerContainer>, DockerError> {
        if *self.fail.lock().unwrap() {
            return Err(DockerError::Transport {
                operation: "list containers",
            });
        }

        Ok(self.containers.lock().unwrap().clone())
    }

    async fn list_volumes(&self) -> Result<Vec<DockerVolume>, DockerError> {
        Ok(self.volume.lock().unwrap().iter().cloned().collect())
    }
}

fn volume(hash: &str) -> DockerVolume {
    DockerVolume {
        name: "example-tenant-a-storage".into(),
        created_at: "now".into(),
        mountpoint: "/volume".into(),
        labels: [
            ("example.io/owned".into(), "example".into()),
            ("cnpg-vcluster.capi/role".into(), "tenant-storage".into()),
            ("cnpg-vcluster.capi/tenant".into(), NAME.into()),
            ("tenancy.cnpg-vcluster.io/tenant-uid".into(), UID.into()),
            (
                "tenancy.cnpg-vcluster.io/spec-hash".into(),
                identity(hash).spec_hash.into(),
            ),
            (
                "tenancy.cnpg-vcluster.io/foundation-hash".into(),
                hash.into(),
            ),
        ]
        .into(),
    }
}

async fn finish(server: &Server, docker: &Docker, foundation: Arc<RuntimeFoundation>) {
    for _ in 0..16 {
        let current: Tenant = serde_json::from_value(server.get(TENANT_PATH)).unwrap();
        if current.finalizers().iter().all(|value| value != FINALIZER) {
            return;
        }
        Finalizer::with_docker(
            server.client(),
            docker.clone(),
            SUPPORTED_KUBERNETES_VERSION,
            foundation.clone(),
        )
        .reconcile(&current)
        .await
        .unwrap();
    }
    panic!("finalizer did not complete");
}

#[tokio::test]
async fn empty_partial_tenant_removes_only_status_and_finalizer() {
    let (foundation, hash, _) = fixture();
    let server = server(&tenant(&hash));
    finish(&server, &Docker::default(), foundation).await;
    let current: Tenant = serde_json::from_value(server.get(TENANT_PATH)).unwrap();
    assert!(current.finalizers().is_empty());
    assert!(server.calls().iter().all(|call| call.method != "DELETE"));
}

#[tokio::test]
async fn exact_cluster_namespace_and_lease_are_deleted_in_order() {
    let (foundation, hash, slot) = fixture();
    let mut tenant = tenant(&hash);
    tenant.status.as_mut().unwrap().cluster_uid = Some("cluster-uid".into());
    tenant.status.as_mut().unwrap().allocation = Some((&slot).into());
    let server = server(&tenant);
    let cluster = root(&hash);
    server.insert(&path(&cluster), cluster);
    server.insert(NAMESPACE, namespace(&hash));
    let claim = lease(&hash, &slot);
    server.insert(
        &format!("{LEASES}/{}", claim.metadata.name.as_deref().unwrap()),
        claim,
    );
    finish(&server, &Docker::default(), foundation).await;
    let deletes: Vec<_> = server
        .calls()
        .into_iter()
        .filter(|call| call.method == "DELETE")
        .collect();
    assert_eq!(deletes.len(), 3);
    assert!(deletes[0].path.ends_with("/clusters/tenant-a"));
    assert_eq!(deletes[1].path, NAMESPACE);
    assert!(deletes[2].path.contains("/leases/"));
    assert!(deletes.iter().all(|call| {
        call.body["preconditions"]["uid"].is_string()
            && call.body["preconditions"]["resourceVersion"].is_string()
            && call.body["propagationPolicy"] == "Background"
    }));
}

#[tokio::test]
async fn foreign_root_and_docker_uncertainty_never_delete() {
    let (foundation, hash, _) = fixture();
    for docker_failure in [false, true] {
        let server = server(&tenant(&hash));
        let docker = Docker::default();
        if docker_failure {
            *docker.fail.lock().unwrap() = true;
        } else {
            let mut cluster = root(&hash);
            cluster.metadata.annotations = None;
            server.insert(&path(&cluster), cluster);
        }
        assert!(
            Finalizer::with_docker(
                server.client(),
                docker,
                SUPPORTED_KUBERNETES_VERSION,
                foundation.clone(),
            )
            .reconcile(&tenant(&hash))
            .await
            .is_err()
        );
        assert!(server.calls().iter().all(|call| call.method != "DELETE"));
    }
}

#[tokio::test]
async fn runtime_version_mismatch_fails_before_api_or_docker_calls() {
    let (foundation, hash, _) = fixture();
    let server = server(&tenant(&hash));
    let docker = Docker::default();
    let error = Finalizer::with_docker(server.client(), docker.clone(), "1.36.5", foundation)
        .reconcile(&tenant(&hash))
        .await
        .unwrap_err();
    assert!(matches!(error, ControllerError::InvalidInput(_)));
    assert!(server.calls().is_empty());
    assert!(docker.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn generation_change_and_status_conflict_never_mutate_resources() {
    let (foundation, hash, _) = fixture();
    for generation_change in [false, true] {
        let mut original = tenant(&hash);
        if !generation_change {
            original.status.as_mut().unwrap().phase = None;
        }
        let server = server(&original);
        if generation_change {
            let mut replacement = original.clone();
            replacement.metadata.generation = Some(2);
            replacement.metadata.resource_version = Some("2".into());
            server.replace_on(
                "GET",
                TENANT_PATH,
                Some(serde_json::to_value(replacement).unwrap()),
            );
        } else {
            server.respond(
                "PATCH",
                &format!("{TENANT_PATH}/status"),
                409,
                crate::support::kube::status(409, "Conflict"),
            );
        }
        assert!(
            Finalizer::with_docker(
                server.client(),
                Docker::default(),
                SUPPORTED_KUBERNETES_VERSION,
                foundation.clone(),
            )
            .reconcile(&original)
            .await
            .is_err()
        );
        assert!(server.calls().iter().all(|call| call.method != "DELETE"));
    }
}

#[tokio::test]
async fn discovery_failure_and_successor_race_retain_finalizer() {
    let (foundation, hash, slot) = fixture();
    for discovery_failure in [false, true] {
        let mut original = tenant(&hash);
        original.status.as_mut().unwrap().allocation = Some((&slot).into());
        let server = server(&original);
        let old = lease(&hash, &slot);
        let lease_path = format!("{LEASES}/{}", old.name_any());
        server.insert(&lease_path, old);
        if discovery_failure {
            let first = tenant_controller::management::descendants()
                .find(|resource| resource.role != "provider")
                .unwrap();
            let (group, version) = first.api_version.split_once('/').unwrap();
            server.respond(
                "GET",
                &format!("/apis/{group}/{version}/namespaces/{NAME}/{}", first.plural),
                503,
                crate::support::kube::status(503, "Unavailable"),
            );
        } else {
            server.replace_on(
                "GET",
                &lease_path,
                Some(serde_json::to_value(successor_lease(&hash, &slot)).unwrap()),
            );
        }
        let result = Finalizer::with_docker(
            server.client(),
            Docker::default(),
            SUPPORTED_KUBERNETES_VERSION,
            foundation.clone(),
        )
        .reconcile(&original)
        .await;
        if discovery_failure {
            assert!(result.is_err());
        } else {
            assert_eq!(
                result.unwrap(),
                Action::requeue(std::time::Duration::from_secs(5))
            );
        }
        let current: Tenant = serde_json::from_value(server.get(TENANT_PATH)).unwrap();
        assert!(current.finalizers().iter().any(|value| value == FINALIZER));
        assert!(current.status.as_ref().unwrap().allocation.is_some());
        assert!(server.calls().iter().all(|call| call.method != "DELETE"));
    }
}

#[tokio::test]
async fn successor_between_initial_and_release_lists_requires_next_pass() {
    let (foundation, hash, slot) = fixture();
    let mut original = tenant(&hash);
    original.status.as_mut().unwrap().allocation = Some((&slot).into());
    let server = server(&original);
    let old = lease(&hash, &slot);
    let lease_path = format!("{LEASES}/{}", old.name_any());
    server.insert(&lease_path, &old);
    server.mutate_on(
        "GET",
        LEASES,
        &lease_path,
        Some(serde_json::to_value(&old).unwrap()),
    );
    server.mutate_on(
        "GET",
        LEASES,
        &lease_path,
        Some(serde_json::to_value(successor_lease(&hash, &slot)).unwrap()),
    );
    Finalizer::with_docker(
        server.client(),
        Docker::default(),
        SUPPORTED_KUBERNETES_VERSION,
        foundation,
    )
    .reconcile(&original)
    .await
    .unwrap();
    let current: Tenant = serde_json::from_value(server.get(TENANT_PATH)).unwrap();
    assert!(current.status.as_ref().unwrap().allocation.is_some());
    assert!(current.finalizers().iter().any(|value| value == FINALIZER));
    assert!(server.calls().iter().all(|call| call.method != "DELETE"));
}

#[tokio::test]
async fn malformed_descendant_and_failed_volume_removal_hold_all_roots() {
    let (foundation, hash, _) = fixture();
    for volume_failure in [false, true] {
        let server = server(&tenant(&hash));
        let docker = Docker::default();
        if volume_failure {
            *docker.volume.lock().unwrap() = Some(volume(&hash));
            *docker.fail_remove.lock().unwrap() = true;
        } else {
            let deployment_resource = MANAGEMENT_RESOURCES
                .iter()
                .find(|resource| resource.kind == "MachineDeployment")
                .unwrap();
            let mut deployment = crate::creation_support::object(
                deployment_resource.api_version,
                deployment_resource.kind,
                NAME,
                &deployment_resource.expected_name(NAME).unwrap(),
                deployment_resource.role,
            );
            deployment.metadata.annotations =
                Some(identity(&hash).annotations(deployment_resource.role));
            server.insert(&path(&deployment), deployment);
            let set_resource = MANAGEMENT_RESOURCES
                .iter()
                .find(|resource| resource.kind == "MachineSet")
                .unwrap();
            let mut set = crate::creation_support::object(
                set_resource.api_version,
                set_resource.kind,
                NAME,
                "set-a",
                set_resource.role,
            );
            set.metadata.owner_references = None;
            let (group, version) = set_resource.api_version.split_once('/').unwrap();
            server.insert(
                &format!(
                    "/apis/{group}/{version}/namespaces/{NAME}/{}/set-a",
                    set_resource.plural
                ),
                set,
            );
        }
        assert!(
            Finalizer::with_docker(
                server.client(),
                docker,
                SUPPORTED_KUBERNETES_VERSION,
                foundation.clone(),
            )
            .reconcile(&tenant(&hash))
            .await
            .is_err()
        );
        assert!(server.calls().iter().all(|call| call.method != "DELETE"));
    }
}
