mod support;

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex, OnceLock};

use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::{ConfigMap, Namespace, Secret};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::{ObjectMeta, OwnerReference};
use kube::Client;
use kube::core::{DynamicObject, TypeMeta};
use serde_json::{Value, json};
use support::Server;
use tenant_controller::allocation::{ClaimContext, new_lease};
use tenant_controller::api::{
    SUPPORTED_KUBERNETES_VERSION, Tenant, TenantSpec, TenantStatus, canonical_spec, spec_hash,
};
use tenant_controller::docker::{
    DockerClient, DockerContainer, DockerError, DockerNetwork, DockerVolume,
};
use tenant_controller::error::ControllerError;
use tenant_controller::finalize::{FinalizationKube, Finalizer, LiveKube};
use tenant_controller::foundation::{AllocationSlot, canonical_hash};
use tenant_controller::ownership::{CLUSTER_API_VERSION, CONTROL_PLANE_API_VERSION, Identity};

const FINALIZER: &str = "tenancy.cnpg-vcluster.io/finalizer";
const NAME: &str = "tenant-a";
const UID: &str = "old-tenant";

fn fixture() -> (String, String, AllocationSlot) {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/foundation-schema3.json")).unwrap();
    let raw = fixture["foundation"].to_string();
    let hash = canonical_hash(&raw).unwrap();
    let slot = serde_json::from_value(fixture["foundation"]["slots"][0].clone()).unwrap();
    (raw, hash, slot)
}

fn tenant() -> Tenant {
    let mut tenant = Tenant::new(
        NAME,
        TenantSpec {
            kubernetes_version: "v1.36.4".into(),
            workers: 1,
            databases: 1,
        },
    );
    tenant.metadata.uid = Some(UID.into());
    tenant.metadata.resource_version = Some("1".into());
    tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_str("\"2026-09-25T00:00:00Z\"").unwrap());
    tenant
}

fn hash_spec() -> &'static str {
    static HASH: OnceLock<String> = OnceLock::new();
    HASH.get_or_init(|| {
        spec_hash(&canonical_spec(NAME, &tenant().spec, SUPPORTED_KUBERNETES_VERSION).unwrap())
    })
}

fn identity<'a>(hash: &'a str) -> Identity<'a> {
    Identity {
        tenant_name: NAME,
        tenant_uid: UID,
        spec_hash: hash_spec(),
        foundation_hash: hash,
        ownership_label: "example.io/owned",
        lab_prefix: "example",
    }
}

fn root(kind: &str, hash: &str) -> DynamicObject {
    let (api_version, resource, name) = match kind {
        "Cluster" => (CLUSTER_API_VERSION, "cluster", NAME),
        "DevCluster" => (
            "infrastructure.cluster.x-k8s.io/v1beta2",
            "dev-cluster",
            NAME,
        ),
        "KamajiControlPlane" => (CONTROL_PLANE_API_VERSION, "kamaji-control-plane", NAME),
        "KubeadmConfigTemplate" => (
            "bootstrap.cluster.x-k8s.io/v1beta2",
            "kubeadm-config-template",
            "tenant-a-worker",
        ),
        "DevMachineTemplate" => (
            "infrastructure.cluster.x-k8s.io/v1beta2",
            "dev-machine-template",
            "tenant-a-worker",
        ),
        "MachineDeployment" => (CLUSTER_API_VERSION, "machine-deployment", "tenant-a-worker"),
        "Machine" => (CLUSTER_API_VERSION, "machine", "tenant-a-worker-one"),
        "DevMachine" => (
            "infrastructure.cluster.x-k8s.io/v1beta2",
            "machine",
            "tenant-a-worker-one",
        ),
        "MachineSet" => (CLUSTER_API_VERSION, "machine", "tenant-a-worker-one"),
        "KubeadmConfig" => (
            "bootstrap.cluster.x-k8s.io/v1beta2",
            "machine",
            "tenant-a-worker-one",
        ),
        other => panic!("unexpected kind: {other}"),
    };
    DynamicObject {
        types: Some(TypeMeta {
            api_version: api_version.into(),
            kind: kind.into(),
        }),
        metadata: ObjectMeta {
            name: Some(name.into()),
            namespace: Some(NAME.into()),
            uid: Some(format!("{kind}-uid")),
            resource_version: Some("1".into()),
            annotations: Some(identity(hash).annotations(resource).into_iter().collect()),
            labels: Some(identity(hash).labels().into_iter().collect()),
            ..Default::default()
        },
        data: json!({}),
    }
}

fn namespace(hash: &str) -> Namespace {
    Namespace {
        metadata: ObjectMeta {
            name: Some(NAME.into()),
            uid: Some("namespace-uid".into()),
            resource_version: Some("1".into()),
            annotations: Some(
                identity(hash)
                    .annotations("namespace")
                    .into_iter()
                    .collect(),
            ),
            labels: Some(identity(hash).labels().into_iter().collect()),
            ..Default::default()
        },
        ..Default::default()
    }
}

fn volume(hash: &str) -> DockerVolume {
    DockerVolume {
        name: "example-tenant-a-storage".into(),
        created_at: "2026-09-25T00:00:00Z".into(),
        mountpoint: "/var/lib/docker/volumes/example-tenant-a-storage".into(),
        labels: BTreeMap::from([
            ("example.io/owned".into(), "example".into()),
            ("cnpg-vcluster.capi/role".into(), "tenant-storage".into()),
            ("cnpg-vcluster.capi/tenant".into(), NAME.into()),
            ("tenancy.cnpg-vcluster.io/tenant-uid".into(), UID.into()),
            (
                "tenancy.cnpg-vcluster.io/spec-hash".into(),
                hash_spec().into(),
            ),
            (
                "tenancy.cnpg-vcluster.io/foundation-hash".into(),
                hash.into(),
            ),
        ]),
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
        spec_hash: hash_spec(),
        foundation_hash: hash,
        slots: &slots,
    };
    let mut result = new_lease(&context, slot);
    result.metadata.uid = Some("lease-uid".into());
    result.metadata.resource_version = Some("1".into());
    result
}

#[derive(Default)]
struct State {
    tenant: Option<Tenant>,
    foundation: Option<ConfigMap>,
    namespace: Option<Namespace>,
    secret: Option<Secret>,
    roots: HashMap<String, DynamicObject>,
    descendants: HashMap<String, Vec<DynamicObject>>,
    leases: Vec<Lease>,
    replace_lease_on_get: Option<Lease>,
    volume: Option<DockerVolume>,
    containers: Vec<DockerContainer>,
    requests: Vec<String>,
    discovery_error: Option<String>,
    docker_error: bool,
    volume_error: bool,
    status_conflict: bool,
    finalizer_conflict: bool,
    hold_cluster: bool,
}

fn state() -> Arc<Mutex<State>> {
    let (raw, hash, _) = fixture();
    Arc::new(Mutex::new(State {
        tenant: Some(tenant()),
        foundation: Some(ConfigMap {
            metadata: ObjectMeta {
                name: Some("tenant-foundation".into()),
                namespace: Some("tenant-system".into()),
                ..Default::default()
            },
            data: Some(BTreeMap::from([
                ("foundation.json".into(), raw),
                ("foundation.sha256".into(), hash),
            ])),
            ..Default::default()
        }),
        ..Default::default()
    }))
}

#[derive(Clone)]
struct Kube(Arc<Mutex<State>>);
#[derive(Clone)]
struct Docker(Arc<Mutex<State>>);

fn failure(reason: &str) -> ControllerError {
    ControllerError::Task(reason.into())
}

impl FinalizationKube for Kube {
    async fn foundation(&self) -> Result<ConfigMap, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("get foundation".into());
        state
            .foundation
            .clone()
            .ok_or_else(|| failure("foundation absent"))
    }
    async fn tenant(&self, _: &str) -> Result<Option<Tenant>, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("get Tenant".into());
        Ok(state.tenant.clone())
    }
    async fn namespace(&self, _: &str) -> Result<Option<Namespace>, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("get Namespace".into());
        Ok(state.namespace.clone())
    }
    async fn secret(&self, _: &str, _: &str) -> Result<Option<Secret>, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("get Secret".into());
        Ok(state.secret.clone())
    }
    async fn root(
        &self,
        kind: &str,
        _: &str,
        _: &str,
    ) -> Result<Option<DynamicObject>, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push(format!("get {kind}"));
        Ok(state.roots.get(kind).cloned())
    }
    async fn descendants(
        &self,
        kind: &str,
        _: &str,
    ) -> Result<Vec<DynamicObject>, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push(format!("list {kind}"));
        if state.discovery_error.as_deref() == Some(kind) {
            return Err(failure("discovery failed"));
        }
        Ok(state.descendants.get(kind).cloned().unwrap_or_default())
    }
    async fn leases(&self) -> Result<Vec<Lease>, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("list Leases".into());
        Ok(state.leases.clone())
    }
    async fn lease(&self, name: &str) -> Result<Option<Lease>, ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("get Lease".into());
        if let Some(replacement) = state.replace_lease_on_get.take() {
            state.leases = vec![replacement];
        }
        Ok(state
            .leases
            .iter()
            .find(|lease| lease.metadata.name.as_deref() == Some(name))
            .cloned())
    }
    async fn delete_root(
        &self,
        kind: &str,
        _: &str,
        _: &str,
        uid: &str,
        rv: &str,
    ) -> Result<(), ControllerError> {
        let mut state = self.0.lock().unwrap();
        let item = state
            .roots
            .get(kind)
            .ok_or_else(|| failure("root absent"))?;
        assert_eq!(item.metadata.uid.as_deref(), Some(uid));
        assert_eq!(item.metadata.resource_version.as_deref(), Some(rv));
        state
            .requests
            .push(format!("delete {kind} exact Background"));
        if !state.hold_cluster || kind != "Cluster" {
            state.roots.remove(kind);
            if kind == "KamajiControlPlane" {
                state.secret = None;
            }
        } else if let Some(item) = state.roots.get_mut(kind) {
            item.metadata.deletion_timestamp =
                Some(serde_json::from_str("\"2026-09-25T00:00:00Z\"").unwrap());
        }
        Ok(())
    }
    async fn delete_namespace(&self, _: &str, uid: &str, rv: &str) -> Result<(), ControllerError> {
        let mut state = self.0.lock().unwrap();
        let item = state
            .namespace
            .as_ref()
            .ok_or_else(|| failure("namespace absent"))?;
        assert_eq!(item.metadata.uid.as_deref(), Some(uid));
        assert_eq!(item.metadata.resource_version.as_deref(), Some(rv));
        state
            .requests
            .push("delete Namespace exact Background".into());
        state.namespace = None;
        Ok(())
    }
    async fn delete_lease(&self, _: &str, uid: &str, rv: &str) -> Result<(), ControllerError> {
        let mut state = self.0.lock().unwrap();
        assert_eq!(state.leases[0].metadata.uid.as_deref(), Some(uid));
        assert_eq!(
            state.leases[0].metadata.resource_version.as_deref(),
            Some(rv)
        );
        state.requests.push("delete Lease exact".into());
        state.leases.clear();
        Ok(())
    }
    async fn status(&self, _: &Tenant, value: Value) -> Result<(), ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("patch status".into());
        if state.status_conflict {
            state.status_conflict = false;
            return Err(failure("status conflict"));
        }
        let current = state.tenant.as_mut().unwrap();
        if current
            .status
            .as_ref()
            .is_some_and(|status| status.allocation.is_some())
            && value.get("allocation").is_none_or(Value::is_null)
        {
            assert_eq!(
                value.get("allocation"),
                Some(&Value::Null),
                "merge patch must explicitly clear an allocation instead of omitting it"
            );
        }
        current.status = Some(serde_json::from_value(value).unwrap());
        current.metadata.resource_version = Some("2".into());
        Ok(())
    }
    async fn remove_finalizer(&self, _: &Tenant) -> Result<(), ControllerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("remove finalizer".into());
        if state.finalizer_conflict {
            state.finalizer_conflict = false;
            return Err(failure("finalizer conflict"));
        }
        state.tenant.as_mut().unwrap().metadata.finalizers = Some(vec![]);
        Ok(())
    }
}

impl DockerClient for Docker {
    async fn inspect_container(&self, _: &str) -> Result<Option<DockerContainer>, DockerError> {
        Ok(None)
    }
    async fn inspect_network(&self, _: &str) -> Result<DockerNetwork, DockerError> {
        Err(DockerError::Identity("tenant network inspection forbidden"))
    }
    async fn inspect_volume(&self, _: &str) -> Result<Option<DockerVolume>, DockerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("inspect volume".into());
        if state.docker_error {
            return Err(DockerError::Transport {
                operation: "inspect volume",
            });
        }
        Ok(state.volume.clone())
    }
    async fn create_volume(
        &self,
        _: &str,
        _: &BTreeMap<String, String>,
    ) -> Result<DockerVolume, DockerError> {
        Err(DockerError::Identity(
            "volume creation forbidden during deletion",
        ))
    }
    async fn remove_volume(&self, name: &str) -> Result<(), DockerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("remove volume".into());
        if state.volume_error {
            return Err(DockerError::Transport {
                operation: "remove volume",
            });
        }
        assert_eq!(state.volume.as_ref().unwrap().name, name);
        state.volume = None;
        Ok(())
    }
    async fn list_containers(&self) -> Result<Vec<DockerContainer>, DockerError> {
        let mut state = self.0.lock().unwrap();
        state.requests.push("list containers".into());
        if state.docker_error {
            return Err(DockerError::Transport {
                operation: "list containers",
            });
        }
        Ok(state.containers.clone())
    }
}

async fn tick(shared: &Arc<Mutex<State>>) -> Result<(), ControllerError> {
    let tenant = shared.lock().unwrap().tenant.clone().unwrap();
    Finalizer::with_adapters(
        Kube(shared.clone()),
        Docker(shared.clone()),
        SUPPORTED_KUBERNETES_VERSION,
    )
    .reconcile(&tenant)
    .await?;
    Ok(())
}

async fn finish(shared: &Arc<Mutex<State>>) {
    for _ in 0..32 {
        if shared
            .lock()
            .unwrap()
            .tenant
            .as_ref()
            .unwrap()
            .metadata
            .finalizers
            .as_deref()
            == Some(&[])
        {
            return;
        }
        tick(shared).await.unwrap();
    }
    panic!(
        "deletion did not converge: {:?}",
        shared.lock().unwrap().requests
    );
}

#[tokio::test]
async fn exact_capd_load_balancer_blocks_volume_and_lease_release_until_provider_removes_it() {
    let (_, hash, slot) = fixture();
    let shared = state();
    {
        let mut s = shared.lock().unwrap();
        s.volume = Some(volume(&hash));
        s.leases.push(lease(&hash, &slot));
        let network_id = serde_json::from_str::<Value>(&fixture().0).unwrap()["networkId"]
            .as_str()
            .unwrap()
            .to_owned();
        s.containers.push(DockerContainer {
            id: "load-balancer-id".into(),
            name: format!("{NAME}-lb"),
            labels: BTreeMap::from([
                ("io.x-k8s.kind.cluster".into(), NAME.into()),
                ("io.x-k8s.kind.role".into(), "external-load-balancer".into()),
            ]),
            networks: BTreeMap::from([("kind".into(), network_id)]),
            network_addresses: BTreeMap::new(),
            state: "running".into(),
        });
    }
    for _ in 0..5 {
        tick(&shared).await.unwrap();
    }
    {
        let mut s = shared.lock().unwrap();
        assert!(s.volume.is_some());
        assert_eq!(s.leases.len(), 1);
        assert!(
            !s.requests
                .iter()
                .any(|request| request == "remove finalizer")
        );
        s.containers.clear();
    }
    finish(&shared).await;
}

#[tokio::test]
async fn partial_creation_and_each_crash_window_converge_across_fresh_finalizers() {
    let (_, hash, slot) = fixture();
    let kinds = [
        "Cluster",
        "DevCluster",
        "KamajiControlPlane",
        "KubeadmConfigTemplate",
        "DevMachineTemplate",
        "MachineDeployment",
    ];
    for stage in 0..=kinds.len() + 1 {
        let shared = state();
        {
            let mut state = shared.lock().unwrap();
            if stage > 0 {
                state.namespace = Some(namespace(&hash));
                state.leases.push(lease(&hash, &slot));
                state.volume = Some(volume(&hash));
            }
            for kind in kinds.iter().take(stage.saturating_sub(1)) {
                state.roots.insert((*kind).into(), root(kind, &hash));
            }
        }
        finish(&shared).await;
        let state = shared.lock().unwrap();
        assert!(
            state.namespace.is_none()
                && state.roots.is_empty()
                && state.volume.is_none()
                && state.leases.is_empty(),
            "stage {stage}"
        );
        assert!(
            state
                .requests
                .last()
                .is_some_and(|r| r == "remove finalizer"),
            "stage {stage}"
        );
        for expected in [
            "list MachineSet",
            "list Machine",
            "list DevMachine",
            "list KubeadmConfig",
            "list Leases",
        ] {
            assert!(state.requests.iter().any(|request| request == expected));
        }
        assert!(
            !state
                .requests
                .iter()
                .any(|request| request.contains("tenant API"))
        );
    }
}

#[tokio::test]
async fn deletion_uses_only_minimal_foundation_and_durable_allocation_identity() {
    let (_, _, slot) = fixture();
    for slots in [
        None,
        Some(json!([])),
        Some(json!("broken creation catalog")),
    ] {
        for stage in [
            "pre-allocation",
            "recover-claim",
            "bound-claim",
            "released-claim",
        ] {
            let shared = state();
            let mut raw = json!({
                "schema":3, "networkId":"network-one",
                "inputs":{"ownershipLabel":"example.io/owned","labPrefix":"example",
                    "storageContainerPath":"/var/local/tenant-storage"}
            });
            if let Some(slots) = &slots {
                raw["slots"] = slots.clone();
            }
            let raw = raw.to_string();
            let hash = canonical_hash(&raw).unwrap();
            {
                let mut s = shared.lock().unwrap();
                s.foundation.as_mut().unwrap().data = Some(BTreeMap::from([
                    ("foundation.json".into(), raw),
                    ("foundation.sha256".into(), hash.clone()),
                ]));
                let mut status = TenantStatus::default();
                if stage != "pre-allocation" {
                    status.foundation_hash = Some(hash.clone());
                }
                if stage == "bound-claim" || stage == "released-claim" {
                    status.allocation = Some((&slot).into());
                }
                if stage == "recover-claim" || stage == "bound-claim" {
                    s.leases.push(lease(&hash, &slot));
                    s.namespace = Some(namespace(&hash));
                    s.roots.insert("Cluster".into(), root("Cluster", &hash));
                }
                s.tenant.as_mut().unwrap().status = Some(status);
            }
            finish(&shared).await;
            let s = shared.lock().unwrap();
            assert!(
                s.leases.is_empty() && s.namespace.is_none() && s.roots.is_empty(),
                "{stage}"
            );
            assert!(
                s.tenant
                    .as_ref()
                    .unwrap()
                    .status
                    .as_ref()
                    .unwrap()
                    .allocation
                    .is_none()
            );
            assert!(
                !s.requests
                    .iter()
                    .any(|request| request.starts_with("create "))
            );
            if stage == "recover-claim" || stage == "bound-claim" {
                assert!(
                    s.requests
                        .iter()
                        .any(|request| request == "delete Lease exact")
                );
            }
        }
    }
}

#[tokio::test]
async fn finalization_validates_and_hashes_with_the_runtime_supported_version() {
    let (_, hash, _) = fixture();
    for supported in ["1.36.5", SUPPORTED_KUBERNETES_VERSION] {
        let shared = state();
        let tenant = {
            let mut s = shared.lock().unwrap();
            let tenant = s.tenant.as_mut().unwrap();
            tenant.spec.kubernetes_version = "v1.36.5".into();
            tenant.status = Some(TenantStatus {
                phase: Some(tenant_controller::api::TenantPhase::Deleting),
                foundation_hash: Some(hash.clone()),
                cluster_uid: Some("Cluster-uid".into()),
                ..Default::default()
            });
            let tenant = tenant.clone();
            let mut cluster = root("Cluster", &hash);
            cluster.metadata.annotations.as_mut().unwrap().insert(
                "tenancy.cnpg-vcluster.io/spec-hash".into(),
                spec_hash(&canonical_spec(NAME, &tenant.spec, "1.36.5").unwrap()),
            );
            s.roots.insert("Cluster".into(), cluster);
            tenant
        };
        let result =
            Finalizer::with_adapters(Kube(shared.clone()), Docker(shared.clone()), supported)
                .reconcile(&tenant)
                .await;
        let s = shared.lock().unwrap();
        if supported == "1.36.5" {
            result.unwrap();
            assert!(
                s.requests
                    .iter()
                    .any(|request| request == "delete Cluster exact Background")
            );
        } else {
            assert!(matches!(result, Err(ControllerError::InvalidInput(_))));
            assert!(s.requests.is_empty());
        }
    }
}

#[tokio::test]
async fn identity_inspection_and_replacement_matrix_blocks_all_destructive_requests() {
    let (_, hash, slot) = fixture();
    let failures = [
        "foundation",
        "spec",
        "namespace",
        "namespace-owner",
        "cluster",
        "cluster-owner",
        "control-plane-owner",
        "volume-label",
        "volume-name",
        "docker",
        "discovery",
        "missing-lease",
        "foreign-lease",
        "secret-owner",
        "secret-dangling",
    ];
    for failure in failures {
        let shared = state();
        {
            let mut s = shared.lock().unwrap();
            s.namespace = Some(namespace(&hash));
            s.roots.insert("Cluster".into(), root("Cluster", &hash));
            s.volume = Some(volume(&hash));
            s.leases.push(lease(&hash, &slot));
            let status = TenantStatus {
                foundation_hash: Some(hash.clone()),
                cluster_uid: Some("Cluster-uid".into()),
                allocation: Some(tenant_controller::api::AllocationStatus {
                    slot_id: slot.slot_id.clone(),
                    endpoint: slot.endpoint.clone(),
                    pod_cidr: slot.pod_cidr.clone(),
                    service_cidr: slot.service_cidr.clone(),
                }),
                ..Default::default()
            };
            s.tenant.as_mut().unwrap().status = Some(status);
            match failure {
                "foundation" => {
                    s.tenant
                        .as_mut()
                        .unwrap()
                        .status
                        .as_mut()
                        .unwrap()
                        .foundation_hash = Some("other".into())
                }
                "spec" => s.tenant.as_mut().unwrap().spec.workers = 9,
                "namespace" => {
                    *s.namespace
                        .as_mut()
                        .unwrap()
                        .metadata
                        .annotations
                        .as_mut()
                        .unwrap()
                        .get_mut("tenancy.cnpg-vcluster.io/tenant-uid")
                        .unwrap() = "foreign".into()
                }
                "namespace-owner" => {
                    s.namespace.as_mut().unwrap().metadata.owner_references =
                        Some(vec![owner("Cluster", "Cluster-uid")])
                }
                "cluster" => {
                    s.roots.get_mut("Cluster").unwrap().metadata.uid = Some("replacement".into())
                }
                "cluster-owner" => {
                    s.roots
                        .get_mut("Cluster")
                        .unwrap()
                        .metadata
                        .owner_references = Some(vec![owner("Foreign", "foreign")])
                }
                "control-plane-owner" => {
                    let mut cp = root("KamajiControlPlane", &hash);
                    cp.metadata.owner_references = Some(vec![owner("Cluster", "foreign")]);
                    s.roots.insert("KamajiControlPlane".into(), cp);
                }
                "volume-label" => {
                    *s.volume
                        .as_mut()
                        .unwrap()
                        .labels
                        .get_mut("tenancy.cnpg-vcluster.io/tenant-uid")
                        .unwrap() = "foreign".into()
                }
                "volume-name" => s.volume.as_mut().unwrap().name = "foreign".into(),
                "docker" => s.docker_error = true,
                "discovery" => s.discovery_error = Some("MachineSet".into()),
                "missing-lease" => s.leases.clear(),
                "foreign-lease" => {
                    *s.leases[0]
                        .metadata
                        .annotations
                        .as_mut()
                        .unwrap()
                        .get_mut("tenancy.cnpg-vcluster.io/tenant-uid")
                        .unwrap() = "successor".into()
                }
                "secret-owner" | "secret-dangling" => {
                    let mut cp = root("KamajiControlPlane", &hash);
                    cp.metadata.owner_references = Some(vec![owner("Cluster", "Cluster-uid")]);
                    if failure == "secret-owner" {
                        s.roots.insert("KamajiControlPlane".into(), cp);
                    }
                    s.secret = Some(Secret {
                        metadata: ObjectMeta {
                            name: Some("tenant-a-kubeconfig".into()),
                            namespace: Some(NAME.into()),
                            uid: Some("secret-uid".into()),
                            owner_references: Some(vec![owner(
                                "KamajiControlPlane",
                                if failure == "secret-owner" {
                                    "foreign"
                                } else {
                                    "KamajiControlPlane-uid"
                                },
                            )]),
                            ..Default::default()
                        },
                        ..Default::default()
                    });
                }
                _ => unreachable!(),
            }
        }
        assert!(
            tick(&shared).await.is_err(),
            "failure {failure} did not block"
        );
        let s = shared.lock().unwrap();
        assert!(
            s.requests.iter().all(|r| !r.starts_with("delete ")
                && r != "remove volume"
                && r != "remove finalizer"),
            "failure {failure}: {:?}",
            s.requests
        );
    }
}

fn owner(kind: &str, uid: &str) -> OwnerReference {
    OwnerReference {
        api_version: match kind {
            "Cluster" | "MachineDeployment" | "MachineSet" | "Machine" => CLUSTER_API_VERSION,
            "KamajiControlPlane" => CONTROL_PLANE_API_VERSION,
            _ => "example.io/v1",
        }
        .into(),
        kind: kind.into(),
        name: match kind {
            "MachineDeployment" => "tenant-a-worker",
            "MachineSet" | "Machine" => "tenant-a-worker-one",
            _ => NAME,
        }
        .into(),
        uid: uid.into(),
        ..Default::default()
    }
}

fn deletion_chain() -> Arc<Mutex<State>> {
    let shared = state();
    let (_, hash, slot) = fixture();
    {
        let mut s = shared.lock().unwrap();
        s.namespace = Some(namespace(&hash));
        s.volume = Some(volume(&hash));
        s.leases.push(lease(&hash, &slot));
        s.tenant.as_mut().unwrap().status = Some(TenantStatus {
            phase: Some(tenant_controller::api::TenantPhase::Deleting),
            foundation_hash: Some(hash.clone()),
            cluster_uid: Some("Cluster-uid".into()),
            allocation: Some((&slot).into()),
            ..Default::default()
        });
        s.roots.insert("Cluster".into(), root("Cluster", &hash));
        let mut deployment = root("MachineDeployment", &hash);
        deployment.metadata.owner_references = Some(vec![owner("Cluster", "Cluster-uid")]);
        s.roots.insert("MachineDeployment".into(), deployment);
        for (kind, parent) in [
            ("MachineSet", "MachineDeployment"),
            ("Machine", "MachineSet"),
            ("DevMachine", "Machine"),
            ("KubeadmConfig", "Machine"),
        ] {
            let mut child = root(kind, &hash);
            child.metadata.owner_references = Some(vec![owner(parent, &format!("{parent}-uid"))]);
            if kind == "MachineSet" || kind == "KubeadmConfig" {
                child.metadata.labels = None;
                child.metadata.annotations = None;
            }
            s.descendants.insert(kind.into(), vec![child]);
        }
    }
    shared
}

#[tokio::test]
async fn every_descendant_chain_is_proven_before_any_destructive_root_delete() {
    for cluster_present in [false, true] {
        let shared = deletion_chain();
        if !cluster_present {
            shared.lock().unwrap().roots.remove("Cluster");
        }
        tick(&shared).await.unwrap();
        assert!(shared.lock().unwrap().requests.iter().any(|request| {
            request
                == if cluster_present {
                    "delete Cluster exact Background"
                } else {
                    "delete MachineDeployment exact Background"
                }
        }));

        for kind in ["MachineSet", "Machine", "DevMachine", "KubeadmConfig"] {
            for mutation in [
                "unowned",
                "foreign-uid",
                "wrong-name",
                "wrong-version",
                "wrong-kind",
                "multiple-owners",
                "missing-parent",
                "missing-uid",
                "duplicate-uid",
                "wrong-api",
                "wrong-namespace",
                "skip-parent",
                "extra-foreign",
            ] {
                let shared = deletion_chain();
                {
                    let mut s = shared.lock().unwrap();
                    if !cluster_present {
                        s.roots.remove("Cluster");
                    }
                    let child = &mut s.descendants.get_mut(kind).unwrap()[0];
                    let parent = child.metadata.owner_references.as_ref().unwrap()[0]
                        .kind
                        .clone();
                    match mutation {
                        "unowned" => child.metadata.owner_references = None,
                        "foreign-uid" => {
                            child.metadata.owner_references.as_mut().unwrap()[0].uid =
                                "foreign".into()
                        }
                        "wrong-name" => {
                            child.metadata.owner_references.as_mut().unwrap()[0].name =
                                "foreign".into()
                        }
                        "wrong-version" => {
                            child.metadata.owner_references.as_mut().unwrap()[0].api_version =
                                "cluster.x-k8s.io/v1beta1".into()
                        }
                        "wrong-kind" => {
                            child.metadata.owner_references.as_mut().unwrap()[0].kind =
                                "Cluster".into()
                        }
                        "multiple-owners" => child
                            .metadata
                            .owner_references
                            .as_mut()
                            .unwrap()
                            .push(owner("Cluster", "Cluster-uid")),
                        "missing-uid" => child.metadata.uid = None,
                        "duplicate-uid" => {
                            child.metadata.uid = Some("MachineDeployment-uid".into())
                        }
                        "wrong-api" => {
                            child.types.as_mut().unwrap().api_version = "example.io/v1".into()
                        }
                        "wrong-namespace" => child.metadata.namespace = Some("foreign".into()),
                        "skip-parent" => {
                            child.metadata.owner_references =
                                Some(vec![owner("Cluster", "Cluster-uid")])
                        }
                        "missing-parent" => {
                            s.roots.remove(&parent);
                            s.descendants.remove(&parent);
                        }
                        "extra-foreign" => {
                            let mut extra = child.clone();
                            extra.metadata.name = Some("foreign".into());
                            extra.metadata.uid = Some("foreign-uid".into());
                            extra.metadata.owner_references = None;
                            s.descendants.get_mut(kind).unwrap().push(extra);
                        }
                        _ => unreachable!(),
                    }
                }
                assert!(tick(&shared).await.is_err(), "{kind}: {mutation}");
                let s = shared.lock().unwrap();
                assert!(
                    s.requests.iter().all(|request| {
                        !request.starts_with("delete ")
                            && request != "remove volume"
                            && request != "remove finalizer"
                            && request != "patch status"
                    }),
                    "{kind}: {mutation}: {:?}",
                    s.requests
                );
                assert!(s.namespace.is_some() && s.volume.is_some() && s.leases.len() == 1);
            }
        }
    }
}

#[tokio::test]
async fn provider_finalizers_descendants_and_orphan_workers_hold_storage() {
    let (_, hash, _) = fixture();
    let shared = state();
    {
        let mut s = shared.lock().unwrap();
        s.namespace = Some(namespace(&hash));
        s.volume = Some(volume(&hash));
        s.roots.insert("Cluster".into(), root("Cluster", &hash));
        s.hold_cluster = true;
    }
    for _ in 0..5 {
        tick(&shared).await.unwrap();
    }
    {
        let s = shared.lock().unwrap();
        assert_eq!(
            s.requests
                .iter()
                .filter(|r| *r == "delete Cluster exact Background")
                .count(),
            1
        );
        assert!(s.volume.is_some() && s.namespace.is_some());
    }
    {
        let mut s = shared.lock().unwrap();
        s.roots.clear();
        s.descendants
            .insert("MachineSet".into(), vec![root("MachineSet", &hash)]);
    }
    assert!(
        tick(&shared).await.is_err(),
        "dangling MachineSet must block"
    );
    assert!(shared.lock().unwrap().volume.is_some());
    {
        let mut s = shared.lock().unwrap();
        s.descendants.clear();
        s.containers.push(DockerContainer {
            id: "worker-id".into(),
            name: "tenant-a-worker-orphan".into(),
            labels: BTreeMap::from([
                ("io.x-k8s.kind.cluster".into(), NAME.into()),
                ("io.x-k8s.kind.role".into(), "worker".into()),
            ]),
            networks: BTreeMap::from([("kind".into(), "network-one".into())]),
            network_addresses: BTreeMap::default(),
            state: "running".into(),
        });
    }
    assert!(tick(&shared).await.is_err());
    assert!(shared.lock().unwrap().volume.is_some());
}

#[tokio::test]
async fn unfiltered_provider_inventories_and_each_discovery_failure_block_host_cleanup() {
    let (_, hash, _) = fixture();
    for kind in ["MachineSet", "Machine", "DevMachine", "KubeadmConfig"] {
        let shared = state();
        {
            let mut s = shared.lock().unwrap();
            s.namespace = Some(namespace(&hash));
            s.volume = Some(volume(&hash));
            s.discovery_error = Some(kind.into());
        }
        assert!(tick(&shared).await.is_err(), "discovery: {kind}");
        {
            let s = shared.lock().unwrap();
            assert!(s.volume.is_some() && s.namespace.is_some());
            assert!(!s.requests.iter().any(|r| r.starts_with("delete ")));
        }

        let shared = state();
        {
            let mut s = shared.lock().unwrap();
            s.namespace = Some(namespace(&hash));
            s.volume = Some(volume(&hash));
            s.descendants.insert(kind.into(), vec![root(kind, &hash)]);
        }
        assert!(tick(&shared).await.is_err(), "unproven {kind} chain");
        let s = shared.lock().unwrap();
        assert!(s.volume.is_some() && s.namespace.is_some());
        assert!(!s.requests.iter().any(|r| r.starts_with("delete ")));
    }
}

#[tokio::test]
async fn recorded_dangling_cluster_owner_is_proven_and_wrong_uid_is_not() {
    let (_, hash, _) = fixture();
    let shared = state();
    {
        let mut s = shared.lock().unwrap();
        s.tenant.as_mut().unwrap().status = Some(TenantStatus {
            foundation_hash: Some(hash.clone()),
            cluster_uid: Some("Cluster-uid".into()),
            ..Default::default()
        });
        let mut control_plane = root("KamajiControlPlane", &hash);
        control_plane.metadata.owner_references = Some(vec![owner("Cluster", "Cluster-uid")]);
        s.roots.insert("KamajiControlPlane".into(), control_plane);
    }
    finish(&shared).await;
    assert!(
        shared
            .lock()
            .unwrap()
            .requests
            .iter()
            .any(|request| request == "delete KamajiControlPlane exact Background")
    );

    let shared = state();
    {
        let mut s = shared.lock().unwrap();
        s.tenant.as_mut().unwrap().status = Some(TenantStatus {
            foundation_hash: Some(hash.clone()),
            cluster_uid: Some("Cluster-uid".into()),
            ..Default::default()
        });
        let mut control_plane = root("KamajiControlPlane", &hash);
        control_plane.metadata.owner_references = Some(vec![owner("Cluster", "replacement-uid")]);
        s.roots.insert("KamajiControlPlane".into(), control_plane);
    }
    assert!(tick(&shared).await.is_err());
    assert!(
        shared
            .lock()
            .unwrap()
            .roots
            .contains_key("KamajiControlPlane")
    );
}

#[tokio::test]
async fn failed_volume_removal_is_not_mistaken_for_observed_absence() {
    let (_, hash, _) = fixture();
    let shared = state();
    {
        let mut s = shared.lock().unwrap();
        s.namespace = Some(namespace(&hash));
        s.volume = Some(volume(&hash));
        s.volume_error = true;
    }
    for _ in 0..3 {
        let _ = tick(&shared).await;
    }
    let s = shared.lock().unwrap();
    assert!(s.volume.is_some() && s.namespace.is_some());
    assert!(
        !s.requests
            .iter()
            .any(|request| request.starts_with("delete "))
    );
    assert!(
        !s.requests
            .iter()
            .any(|request| request == "remove finalizer")
    );
}

#[tokio::test]
async fn successor_lease_and_conflicted_status_finalizer_do_not_shortcut_barriers() {
    let (_, hash, slot) = fixture();
    let shared = state();
    {
        let mut s = shared.lock().unwrap();
        s.namespace = Some(namespace(&hash));
        s.volume = Some(volume(&hash));
        s.leases.push(lease(&hash, &slot));
        s.status_conflict = true;
    }
    assert!(tick(&shared).await.is_err());
    assert!(shared.lock().unwrap().volume.is_some());
    finish(&shared).await;
    let requests = shared.lock().unwrap().requests.clone();
    assert!(
        requests
            .iter()
            .position(|r| r == "delete Lease exact")
            .unwrap()
            < requests
                .iter()
                .position(|r| r == "remove finalizer")
                .unwrap()
    );

    let shared = state();
    {
        let mut s = shared.lock().unwrap();
        let mut successor = lease(&hash, &slot);
        successor.metadata.uid = Some("successor-uid".into());
        successor.metadata.annotations.as_mut().unwrap().insert(
            "tenancy.cnpg-vcluster.io/tenant-uid".into(),
            "successor".into(),
        );
        s.leases.push(successor);
        s.tenant.as_mut().unwrap().status = Some(TenantStatus {
            phase: Some(tenant_controller::api::TenantPhase::Deleting),
            foundation_hash: Some(hash),
            allocation: Some(tenant_controller::api::AllocationStatus {
                slot_id: slot.slot_id,
                endpoint: slot.endpoint,
                pod_cidr: slot.pod_cidr,
                service_cidr: slot.service_cidr,
            }),
            ..Default::default()
        });
        s.finalizer_conflict = true;
    }
    tick(&shared).await.unwrap();
    assert!(tick(&shared).await.is_err());
    finish(&shared).await;
    let s = shared.lock().unwrap();
    assert_eq!(s.leases.len(), 1);
    assert!(!s.requests.iter().any(|r| r == "delete Lease exact"));
}

#[tokio::test]
async fn malformed_successor_keeps_allocation_and_finalizer_and_is_never_modified() {
    let (_, hash, slot) = fixture();
    for reread in [false, true] {
        for mutation in ["markers", "uid", "resource-version", "spec"] {
            let shared = state();
            let malformed = {
                let mut s = shared.lock().unwrap();
                let mut successor = lease(&hash, &slot);
                successor.metadata.uid = Some("successor-lease-uid".into());
                successor.metadata.annotations.as_mut().unwrap().insert(
                    "tenancy.cnpg-vcluster.io/tenant-uid".into(),
                    "successor-tenant-uid".into(),
                );
                match mutation {
                    "markers" => successor.metadata.labels = None,
                    "uid" => successor.metadata.uid = None,
                    "resource-version" => successor.metadata.resource_version = None,
                    "spec" => {
                        successor.spec = Some(k8s_openapi::api::coordination::v1::LeaseSpec {
                            holder_identity: Some("foreign".into()),
                            ..Default::default()
                        })
                    }
                    _ => unreachable!(),
                }
                if reread {
                    s.leases.push(lease(&hash, &slot));
                    s.replace_lease_on_get = Some(successor.clone());
                } else {
                    s.leases.push(successor.clone());
                }
                s.tenant.as_mut().unwrap().status = Some(TenantStatus {
                    phase: Some(tenant_controller::api::TenantPhase::Deleting),
                    foundation_hash: Some(hash.clone()),
                    allocation: Some((&slot).into()),
                    ..Default::default()
                });
                successor
            };
            assert!(tick(&shared).await.is_err(), "{mutation}, reread={reread}");
            let s = shared.lock().unwrap();
            assert_eq!(s.leases, [malformed]);
            let tenant = s.tenant.as_ref().unwrap();
            assert_eq!(
                tenant.metadata.finalizers.as_deref(),
                Some(&[FINALIZER.to_owned()][..])
            );
            assert_eq!(
                tenant.status.as_ref().unwrap().allocation,
                Some((&slot).into())
            );
            assert!(s.requests.iter().all(|request| {
                !request.starts_with("delete ")
                    && request != "remove finalizer"
                    && request != "patch status"
            }));
        }
    }
}

#[tokio::test]
async fn lease_replacement_between_inventory_and_exact_get_never_deletes_successor() {
    let (_, hash, slot) = fixture();
    for successor_owned in [false, true] {
        let shared = state();
        {
            let mut s = shared.lock().unwrap();
            s.leases.push(lease(&hash, &slot));
            let mut replacement = lease(&hash, &slot);
            replacement.metadata.uid = Some("replacement-lease-uid".into());
            if successor_owned {
                replacement.metadata.annotations.as_mut().unwrap().insert(
                    "tenancy.cnpg-vcluster.io/tenant-uid".into(),
                    "successor-tenant-uid".into(),
                );
            }
            s.replace_lease_on_get = Some(replacement);
            s.tenant.as_mut().unwrap().status = Some(TenantStatus {
                phase: Some(tenant_controller::api::TenantPhase::Deleting),
                foundation_hash: Some(hash.clone()),
                allocation: Some(tenant_controller::api::AllocationStatus {
                    slot_id: slot.slot_id.clone(),
                    endpoint: slot.endpoint.clone(),
                    pod_cidr: slot.pod_cidr.clone(),
                    service_cidr: slot.service_cidr.clone(),
                }),
                ..Default::default()
            });
        }
        let result = tick(&shared).await;
        if successor_owned {
            assert!(result.is_ok());
            finish(&shared).await;
        } else {
            assert!(result.is_err());
            assert_eq!(
                shared
                    .lock()
                    .unwrap()
                    .tenant
                    .as_ref()
                    .unwrap()
                    .metadata
                    .finalizers
                    .as_ref()
                    .unwrap(),
                &[FINALIZER]
            );
        }
        let state = shared.lock().unwrap();
        assert_eq!(state.leases.len(), 1);
        assert_eq!(
            state.leases[0].metadata.uid.as_deref(),
            Some("replacement-lease-uid")
        );
        assert!(
            !state
                .requests
                .iter()
                .any(|request| request == "delete Lease exact")
        );
    }
}

#[tokio::test]
async fn live_kube_adapter_sends_direct_unfiltered_reads_and_exact_background_deletes() {
    const FOUNDATION: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-foundation";
    const MACHINESETS: &str = "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/machinesets";
    const CLUSTER: &str = "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/clusters/tenant-a";
    const NAMESPACE: &str = "/api/v1/namespaces/tenant-a";
    const LEASE: &str = "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases/slot";
    const TENANT: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants/tenant-a";

    let server = Server::default();
    server.insert(
        FOUNDATION,
        ConfigMap {
            metadata: ObjectMeta {
                name: Some("tenant-foundation".into()),
                namespace: Some("tenant-system".into()),
                ..Default::default()
            },
            ..Default::default()
        },
    );
    server.allow_list(MACHINESETS);
    for (path, uid, kind) in [
        (CLUSTER, "cluster-uid", "Cluster"),
        (NAMESPACE, "namespace-uid", "Namespace"),
        (LEASE, "lease-uid", "Lease"),
    ] {
        server.insert(
            path,
            json!({
                "apiVersion":"v1",
                "kind":kind,
                "metadata":{"name":path.rsplit('/').next().unwrap(),"uid":uid,"resourceVersion":
                    match uid {"cluster-uid"=>"rv-1","namespace-uid"=>"rv-2",_=>"rv-3"}}
            }),
        );
    }
    server.insert(TENANT, tenant());
    let client = server.client();
    let live = LiveKube::new(client.clone());
    let _constructor: fn(
        Client,
        tenant_controller::docker::BollardDockerClient,
        String,
    ) -> Finalizer = Finalizer::new;
    drop(client);
    live.foundation().await.unwrap();
    live.descendants("MachineSet", NAME).await.unwrap();
    live.delete_root("Cluster", NAME, NAME, "cluster-uid", "rv-1")
        .await
        .unwrap();
    live.delete_namespace(NAME, "namespace-uid", "rv-2")
        .await
        .unwrap();
    live.delete_lease("slot", "lease-uid", "rv-3")
        .await
        .unwrap();
    live.status(&tenant(), json!({"phase":"Deleting"}))
        .await
        .unwrap();
    live.remove_finalizer(&tenant()).await.unwrap();
    let requests = server.calls();
    assert_eq!(
        requests[0].path,
        "/api/v1/namespaces/tenant-system/configmaps/tenant-foundation"
    );
    assert_eq!(
        requests[1].path,
        "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/machinesets"
    );
    for (index, uid, rv) in [
        (2, "cluster-uid", "rv-1"),
        (3, "namespace-uid", "rv-2"),
        (4, "lease-uid", "rv-3"),
    ] {
        assert_eq!(requests[index].method, "DELETE");
        assert_eq!(requests[index].body["preconditions"]["uid"], uid);
        assert_eq!(requests[index].body["preconditions"]["resourceVersion"], rv);
        assert_eq!(requests[index].body["propagationPolicy"], "Background");
    }
    assert_eq!(requests[5].body["metadata"]["resourceVersion"], "1");
    assert_eq!(requests[6].body["metadata"]["uid"], UID);
    assert_eq!(requests[6].body["metadata"]["resourceVersion"], "1");
    assert_eq!(requests[6].body["metadata"]["finalizers"], json!([]));
}
