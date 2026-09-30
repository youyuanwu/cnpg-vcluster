//! Go parity: tenant_controller, phase2, readiness, reconcile_cnpg and status.
//! This Tower service drives the real creation pipeline across every barrier.
use std::sync::Arc;

use crate::creation_support::*;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference;
use kube::{ResourceExt, core::DynamicObject, runtime::controller::Action};
use serde_json::{Value, json};
use tenant_controller::{
    api::{
        CanonicalSpec, LocalProviderStatus, Tenant, TenantPhase, TenantProviderSpec,
        TenantProviderStatus, TenantStatus, canonical_spec, spec_hash,
    },
    docker::{DockerContainer, WORKER_CLUSTER_LABEL, WORKER_ROLE_LABEL},
    foundation::{Foundation, FoundationError, canonical_hash, parse_runtime},
    management,
    ownership::Identity,
    reconcile::{
        Assets, Config, FINALIZER, LocalProvider, PROGRESS_INTERVAL, ProviderLifecycle,
        ReconcileError, Reconciler,
    },
};

const TENANT: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a";
const FOUNDATION: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-foundation";
const LEASES: &str = "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases";
const CLUSTER: &str = "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/clusters/tenant-a";
const DEPLOYMENT: &str =
    "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/machinedeployments/tenant-a-worker";

struct Fixture {
    management: Server,
    workload: Server,
    reconciler: Reconciler<LocalProvider<FakeDocker, FakeAccess>>,
    foundation: Foundation,
    hash: String,
}

impl Fixture {
    fn new(_mutation: bool) -> Self {
        let management = Server::default();
        let workload = Server::default();
        management.insert(TENANT, tenant());
        let mut raw: Value =
            serde_json::from_str::<Value>(include_str!("../fixtures/foundation-schema3.json"))
                .unwrap()["foundation"]
                .clone();
        raw["inputs"]["cacheHostPath"] = json!("/cache");
        raw["inputs"]["konnectivityServerImage"] = json!("server:one@sha256:a");
        raw["inputs"]["konnectivityAgentImage"] = json!("agent:one@sha256:b");
        let encoded = serde_json::to_string(&raw).unwrap();
        let hash = canonical_hash(&encoded).unwrap();
        let foundation: Foundation = serde_json::from_value(raw).unwrap();
        let runtime_foundation =
            Arc::new(parse_runtime(&encoded, &hash, "1.36.4", "controller:one").unwrap());
        management.insert(FOUNDATION, json!({"apiVersion":"v1","kind":"ConfigMap",
            "metadata":{"name":"tenant-foundation","namespace":"tenant-system","uid":"foundation","resourceVersion":"1"},
            "data":{"foundation.json":encoded,"foundation.sha256":hash}}));
        management.allow_list(LEASES);
        let calico = [
            json!({"apiVersion":"apps/v1","kind":"DaemonSet","metadata":{"namespace":"kube-system","name":"calico-node"},
                "spec":{"template":{"spec":{"initContainers":[{"name":"cni-one","image":"docker.io/cni:one"},{"name":"cni-two","image":"docker.io/cni:one"},{"name":"node-init","image":"docker.io/node:one"}],
                    "containers":[{"name":"calico-node","image":"docker.io/node:one"}]}}}}),
            json!({"apiVersion":"apps/v1","kind":"Deployment","metadata":{"namespace":"kube-system","name":"calico-kube-controllers"},
                "spec":{"replicas":1,"template":{"spec":{"containers":[{"name":"controllers","image":"docker.io/controllers:one"}]}}}}),
        ].into_iter().map(|value| value.to_string()).collect::<Vec<_>>().join("\n---\n").into_bytes();
        let cnpg = json!({"apiVersion":"apps/v1","kind":"Deployment","metadata":{"namespace":"cnpg-system","name":"cnpg-controller-manager"},
            "spec":{"replicas":1,"template":{"spec":{"initContainers":[{"name":"init","image":"docker.io/cnpg:one"}],"containers":[{"name":"manager","image":"docker.io/cnpg:one"}]}}}}).to_string().into_bytes();
        let reconciler = Reconciler {
            client: management.client(),
            provider: LocalProvider {
                client: management.client(),
                docker: FakeDocker::default(),
                access: FakeAccess(workload.client()),
                assets: Assets { calico, cnpg },
                foundation: runtime_foundation,
            },
            config: Config::default(),
        };
        Self {
            management,
            workload,
            reconciler,
            foundation,
            hash,
        }
    }

    fn current(&self) -> Tenant {
        serde_json::from_value(self.management.get(TENANT)).unwrap()
    }

    fn clear(&self) {
        self.management.take_calls();
        self.workload.take_calls();
        self.reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .clear();
    }

    async fn step(&self) -> Action {
        self.reconciler.reconcile_name("tenant-a").await.unwrap()
    }

    fn settle_provider(&self) {
        let cluster = self
            .management
            .0
            .lock()
            .unwrap()
            .objects
            .get(CLUSTER)
            .cloned();
        if let Some(mut cluster) = cluster {
            cluster["status"] = json!({"observedGeneration":cluster["metadata"]["generation"],"conditions":[
                {"type":"ControlPlaneAvailable","status":"True","observedGeneration":cluster["metadata"]["generation"]},
                {"type":"Available","status":"True","observedGeneration":cluster["metadata"]["generation"]}
            ]});
            self.management.insert(CLUSTER, &cluster);
            let owner = json!({"apiVersion":"cluster.x-k8s.io/v1beta2","kind":"Cluster","name":"tenant-a","uid":cluster["metadata"]["uid"]});
            for (key, value) in &mut self.management.0.lock().unwrap().objects {
                if key != CLUSTER
                    && value["metadata"]["namespace"] == "tenant-a"
                    && matches!(
                        value["kind"].as_str(),
                        Some(
                            "DevCluster"
                                | "KamajiControlPlane"
                                | "MachineDeployment"
                                | "KubeadmConfigTemplate"
                                | "DevMachineTemplate"
                        )
                    )
                {
                    value["metadata"]["ownerReferences"] = json!([owner]);
                }
            }
        }
        for value in self.workload.0.lock().unwrap().objects.values_mut() {
            match value["kind"].as_str() {
                Some("DaemonSet") => {
                    value["status"] = json!({"observedGeneration":value["metadata"]["generation"],"desiredNumberScheduled":1,"numberAvailable":1})
                }
                Some("Deployment") => {
                    value["status"] = json!({"observedGeneration":value["metadata"]["generation"],"availableReplicas":1})
                }
                Some("Cluster") => {
                    value["status"] = json!({"phase":"Cluster in healthy state","readyInstances":1,
                        "conditions":[{"type":"Ready","status":"True","observedGeneration":value["metadata"]["generation"]}]})
                }
                _ => {}
            }
        }
        let deployment = self
            .management
            .0
            .lock()
            .unwrap()
            .objects
            .get(DEPLOYMENT)
            .cloned();
        if let Some(deployment) = deployment {
            let deployment: DynamicObject = serde_json::from_value(deployment).unwrap();
            self.seed_workers(&deployment);
        }
    }

    fn seed_workers(&self, deployment: &DynamicObject) {
        let tenant = self.current();
        let canonical = canonical_spec("tenant-a", &tenant.spec, "1.36.4").unwrap();
        let spec_hash = spec_hash(&canonical);
        let identity = Identity {
            spec_hash: &spec_hash,
            foundation_hash: &self.hash,
            ..identity()
        };
        let mut set = object(
            "cluster.x-k8s.io/v1beta2",
            "MachineSet",
            "tenant-a",
            "worker-set",
            "machine",
        );
        set.metadata.owner_references = Some(vec![owner(deployment)]);
        let mut machine = object(
            "cluster.x-k8s.io/v1beta2",
            "Machine",
            "tenant-a",
            "worker-a",
            "machine",
        );
        machine.metadata.annotations = Some(identity.annotations("machine"));
        machine.metadata.owner_references = Some(vec![owner(&set)]);
        machine.data = json!({"status":{"conditions":[{"type":"Ready","status":"True","observedGeneration":2}]}});
        let mut dev = object(
            "infrastructure.cluster.x-k8s.io/v1beta2",
            "DevMachine",
            "tenant-a",
            "worker-a",
            "machine",
        );
        dev.metadata.uid = Some("dev-uid".into());
        dev.metadata.owner_references = Some(vec![owner(&machine)]);
        dev.data = machine.data.clone();
        for value in [&set, &machine, &dev] {
            self.management.insert(&path(value), value);
            self.management
                .allow_list(path(value).rsplit_once('/').unwrap().0);
        }
        let mut node = object("v1", "Node", "", "worker-a", "machine");
        node.data = machine.data.clone();
        self.workload.insert(&path(&node), node);
        self.workload.allow_list("/api/v1/nodes");
        let mut coredns = object(
            "apps/v1",
            "Deployment",
            "kube-system",
            "coredns",
            "provider",
        );
        coredns.data =
            json!({"spec":{"replicas":1},"status":{"observedGeneration":2,"availableReplicas":1}});
        self.workload.insert(&path(&coredns), coredns);
        *self.reconciler.provider.docker.containers.lock().unwrap() = vec![DockerContainer {
            id: "container-a".into(),
            name: "worker-a".into(),
            state: "running".into(),
            labels: [
                (WORKER_CLUSTER_LABEL.into(), "tenant-a".into()),
                (WORKER_ROLE_LABEL.into(), "worker".into()),
            ]
            .into(),
            networks: [("kind".into(), self.foundation.network_id.clone())].into(),
            network_addresses: Default::default(),
        }];
    }

    async fn until_ready(&self) {
        for _ in 0..25 {
            self.step().await;
            if self.current().status.as_ref().is_some_and(|status| {
                status.phase == Some(TenantPhase::Ready)
                    && status
                        .database_capability
                        .as_ref()
                        .is_some_and(|cap| cap.available)
            }) {
                return;
            }
            self.settle_provider();
        }
        panic!("did not converge: {:?}", self.current().status);
    }
}

fn owner(object: &DynamicObject) -> OwnerReference {
    OwnerReference {
        api_version: object.types.as_ref().unwrap().api_version.clone(),
        kind: object.types.as_ref().unwrap().kind.clone(),
        name: object.name_any(),
        uid: object.uid().unwrap(),
        ..Default::default()
    }
}

#[tokio::test]
async fn absent_tenant_only_uses_one_uncached_get() {
    let fixture = Fixture::new(true);
    fixture.management.0.lock().unwrap().objects.remove(TENANT);
    assert_eq!(fixture.step().await, Action::await_change());
    assert_eq!(fixture.management.calls().len(), 1);
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn invalid_spec_has_no_foundation_or_external_calls() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.spec.workers = 4;
    fixture.management.insert(TENANT, tenant);
    fixture.step().await;
    let calls = fixture.management.calls();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].path, TENANT);
    assert_eq!(calls[1].path, format!("{TENANT}/status"));
    assert!(fixture.current().finalizers().is_empty());
    assert_eq!(
        fixture
            .current()
            .status
            .unwrap()
            .conditions
            .iter()
            .find(|condition| condition.type_ == "Ready")
            .unwrap()
            .reason,
        "InvalidSpec"
    );
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn valid_azure_spec_stops_before_finalizer_or_local_dependencies() {
    let mut fixture = Fixture::new(true);
    Arc::make_mut(&mut fixture.reconciler.provider.foundation).creation =
        Err(FoundationError::Invalid("must not be observed".into()));
    let mut tenant = fixture.current();
    tenant.spec.provider = TenantProviderSpec::Azure;
    fixture.management.insert(TENANT, tenant);
    fixture.step().await;
    let current = fixture.current();
    assert!(current.finalizers().is_empty());
    let status = current.status.unwrap();
    assert_eq!(status.phase, Some(TenantPhase::Failed));
    assert_eq!(
        status.provider,
        Some(TenantProviderStatus::Azure(Default::default()))
    );
    for condition_type in ["Accepted", "Ready"] {
        assert_eq!(
            status
                .conditions
                .iter()
                .find(|condition| condition.type_ == condition_type)
                .unwrap()
                .reason,
            "ProviderUnsupported"
        );
    }
    assert_eq!(
        fixture.management.calls().len(),
        2,
        "Azure status must be the only mutation"
    );
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn deleting_azure_without_controller_finalizer_is_read_only() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.spec.provider = TenantProviderSpec::Azure;
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-28T00:00:00Z")).unwrap());
    tenant.metadata.finalizers = Some(vec!["example.com/third-party".into()]);
    fixture.management.insert(TENANT, tenant);

    assert_eq!(fixture.step().await, Action::await_change());
    assert_eq!(
        fixture
            .management
            .calls()
            .iter()
            .map(|call| (call.method.as_str(), call.path.as_str()))
            .collect::<Vec<_>>(),
        [("GET", TENANT)]
    );
    assert!(fixture.current().status.is_none());
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[derive(Clone, Copy)]
struct UnsupportedProvider;

impl ProviderLifecycle for UnsupportedProvider {
    fn supports(&self, _provider: &TenantProviderSpec) -> bool {
        false
    }

    async fn reconcile(
        &self,
        _tenant: &Tenant,
        _spec: &CanonicalSpec,
    ) -> Result<Action, ReconcileError> {
        panic!("unsupported provider reconcile must not run")
    }

    async fn finalize(
        &self,
        _tenant: &Tenant,
        _supported_version: &str,
    ) -> Result<Action, ReconcileError> {
        panic!("unsupported provider finalize must not run")
    }
}

#[tokio::test]
async fn unsupported_reporting_uses_requested_provider_discriminator_and_name() {
    let management = Server::default();
    management.insert(TENANT, tenant());
    let reconciler = Reconciler::new(management.client(), Config::default(), UnsupportedProvider);

    assert_eq!(
        reconciler.reconcile_name("tenant-a").await.unwrap(),
        Action::await_change()
    );

    let current: Tenant = serde_json::from_value(management.get(TENANT)).unwrap();
    let status = current.status.unwrap();
    assert_eq!(
        status.provider,
        Some(TenantProviderStatus::Local(LocalProviderStatus::default()))
    );
    for condition_type in ["Accepted", "Ready"] {
        let condition = status
            .conditions
            .iter()
            .find(|condition| condition.type_ == condition_type)
            .unwrap();
        assert_eq!(condition.reason, "ProviderUnsupported");
        assert_eq!(
            condition.message,
            "Local provider reconciliation is not implemented"
        );
    }
    assert_eq!(management.calls().len(), 2);
}

fn assert_ownership_invalid(status: &TenantStatus) {
    assert_eq!(status.phase, Some(TenantPhase::OwnershipInvalid));
    for condition_type in ["Ready", "OwnershipValid"] {
        assert_eq!(
            status
                .conditions
                .iter()
                .find(|condition| condition.type_ == condition_type)
                .unwrap()
                .reason,
            "OwnershipInvalid"
        );
    }
}

#[tokio::test]
async fn provider_status_mismatch_blocks_creation_as_invalid_ownership() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.status = Some(TenantStatus {
        provider: Some(TenantProviderStatus::Azure(Default::default())),
        ..Default::default()
    });
    fixture.management.insert(TENANT, tenant);

    assert_eq!(
        fixture.step().await,
        Action::requeue(tenant_controller::reconcile::READY_INTERVAL)
    );

    let current = fixture.current();
    assert!(current.finalizers().is_empty());
    assert_ownership_invalid(current.status.as_ref().unwrap());
    assert_eq!(fixture.management.calls().len(), 2);
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn provider_status_mismatch_blocks_deletion_and_retains_finalizer() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-28T00:00:00Z")).unwrap());
    tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
    tenant.status = Some(TenantStatus {
        provider: Some(TenantProviderStatus::Azure(Default::default())),
        ..Default::default()
    });
    fixture.management.insert(TENANT, tenant);

    assert_eq!(
        fixture.step().await,
        Action::requeue(tenant_controller::reconcile::READY_INTERVAL)
    );

    let current = fixture.current();
    assert!(current.finalizers().iter().any(|value| value == FINALIZER));
    assert_ownership_invalid(current.status.as_ref().unwrap());
    assert_eq!(fixture.management.calls().len(), 2);
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn azure_status_mismatch_blocks_before_unsupported_reporting() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.spec.provider = TenantProviderSpec::Azure;
    tenant.status = Some(TenantStatus {
        provider: Some(TenantProviderStatus::Local(LocalProviderStatus::default())),
        ..Default::default()
    });
    fixture.management.insert(TENANT, tenant);

    assert_eq!(
        fixture.step().await,
        Action::requeue(tenant_controller::reconcile::READY_INTERVAL)
    );
    assert_ownership_invalid(fixture.current().status.as_ref().unwrap());
    assert_eq!(fixture.management.calls().len(), 2);
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn azure_spec_with_controller_finalizer_fails_closed_without_local_cleanup() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.spec.provider = TenantProviderSpec::Azure;
    tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
    tenant.status = Some(TenantStatus {
        provider: Some(TenantProviderStatus::Azure(Default::default())),
        ..Default::default()
    });
    fixture.management.insert(TENANT, tenant);

    assert_eq!(fixture.step().await, Action::await_change());

    let current = fixture.current();
    assert!(current.finalizers().iter().any(|value| value == FINALIZER));
    let status = current.status.unwrap();
    assert_eq!(status.phase, Some(TenantPhase::Failed));
    assert_eq!(
        status.provider,
        Some(TenantProviderStatus::Azure(Default::default()))
    );
    let accepted = status
        .conditions
        .iter()
        .find(|condition| condition.type_ == "Accepted")
        .unwrap();
    assert_eq!(accepted.reason, "ProviderFinalizerUnsupported");
    assert_eq!(
        accepted.message,
        "Azure provider carries the controller finalizer; lifecycle is not implemented"
    );
    let ready = status
        .conditions
        .iter()
        .find(|condition| condition.type_ == "Ready")
        .unwrap();
    assert_eq!(ready.reason, "ProviderFinalizerUnsupported");
    assert_eq!(
        ready.message,
        "Azure provider carries the controller finalizer; lifecycle is not implemented"
    );
    assert_eq!(
        fixture.management.calls().len(),
        2,
        "blocked Azure finalization must only publish status"
    );
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn deleting_azure_with_controller_finalizer_reports_blocked_deletion() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.spec.provider = TenantProviderSpec::Azure;
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-28T00:00:00Z")).unwrap());
    tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
    tenant.status = Some(TenantStatus {
        provider: Some(TenantProviderStatus::Azure(Default::default())),
        ..Default::default()
    });
    fixture.management.insert(TENANT, tenant);

    assert_eq!(fixture.step().await, Action::await_change());

    let current = fixture.current();
    assert!(current.finalizers().iter().any(|value| value == FINALIZER));
    let status = current.status.unwrap();
    assert_eq!(status.phase, Some(TenantPhase::Deleting));
    for condition_type in ["Accepted", "Ready"] {
        assert_eq!(
            status
                .conditions
                .iter()
                .find(|condition| condition.type_ == condition_type)
                .unwrap()
                .reason,
            "ProviderFinalizerUnsupported"
        );
    }
    assert_eq!(fixture.management.calls().len(), 2);
    assert!(fixture.workload.calls().is_empty());
}

#[tokio::test]
async fn deleting_azure_status_mismatch_retains_finalizer_as_invalid_ownership() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.spec.provider = TenantProviderSpec::Azure;
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-28T00:00:00Z")).unwrap());
    tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
    tenant.status = Some(TenantStatus {
        provider: Some(TenantProviderStatus::Local(LocalProviderStatus::default())),
        ..Default::default()
    });
    fixture.management.insert(TENANT, tenant);

    assert_eq!(
        fixture.step().await,
        Action::requeue(tenant_controller::reconcile::READY_INTERVAL)
    );

    let current = fixture.current();
    assert!(current.finalizers().iter().any(|value| value == FINALIZER));
    assert_ownership_invalid(current.status.as_ref().unwrap());
    assert_eq!(fixture.management.calls().len(), 2);
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn validation_uses_runtime_supported_version_not_a_compiled_literal() {
    let mut fixture = Fixture::new(false);
    fixture.reconciler.config.supported_version = "1.36.5".into();
    fixture.step().await;
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(tenant_controller::api::TenantPhase::Failed)
    );
    assert_eq!(fixture.management.calls().len(), 2);
}

#[tokio::test]
async fn finalizer_foundation_allocation_namespace_cluster_and_uid_writes_are_separate_barriers() {
    let fixture = Fixture::new(true);
    fixture.step().await;
    let calls = fixture.management.take_calls();
    assert_eq!(
        calls
            .iter()
            .map(|call| call.method.as_str())
            .collect::<Vec<_>>(),
        ["GET", "PATCH"]
    );
    assert_eq!(
        calls[1].path, TENANT,
        "finalizer uses main resource, not status"
    );
    assert_eq!(calls[1].body["metadata"]["uid"], "tenant-uid");
    assert_eq!(calls[1].body["metadata"]["resourceVersion"], "1");
    assert!(
        fixture
            .current()
            .finalizers()
            .iter()
            .any(|value| value == FINALIZER)
    );
    fixture.step().await;
    assert_eq!(
        fixture
            .management
            .take_calls()
            .iter()
            .map(|call| call.method.as_str())
            .collect::<Vec<_>>(),
        ["GET", "PATCH"]
    );
    assert_eq!(
        fixture
            .current()
            .status
            .as_ref()
            .unwrap()
            .local()
            .unwrap()
            .foundation_hash
            .as_deref(),
        Some(fixture.hash.as_str())
    );
    fixture.step().await;
    let calls = fixture.management.take_calls();
    assert_eq!(
        calls
            .iter()
            .map(|call| call.method.as_str())
            .collect::<Vec<_>>(),
        ["GET", "GET", "POST", "GET", "PATCH"]
    );
    assert_eq!(calls[2].path, LEASES);
    assert!(
        fixture
            .current()
            .status
            .as_ref()
            .unwrap()
            .local()
            .unwrap()
            .allocation
            .is_some()
    );
    fixture.step().await;
    let calls = fixture.management.take_calls();
    assert!(
        calls
            .iter()
            .any(|call| call.method == "POST" && call.path == "/api/v1/namespaces")
    );
    assert!(!calls.iter().any(|call| call.path.contains("/clusters/")));
    fixture.step().await;
    let calls = fixture.management.take_calls();
    assert!(
        calls
            .iter()
            .any(|call| call.method == "POST" && call.path.ends_with("/clusters"))
    );
    assert!(
        fixture
            .current()
            .status
            .as_ref()
            .unwrap()
            .local()
            .unwrap()
            .cluster_uid
            .is_none()
    );
    fixture.step().await;
    let calls = fixture.management.take_calls();
    assert!(
        fixture
            .current()
            .status
            .as_ref()
            .unwrap()
            .local()
            .unwrap()
            .cluster_uid
            .is_some()
    );
    assert!(!calls.iter().any(
        |call| call.path.contains("/devclusters") || call.path.contains("/kamajicontrolplanes")
    ));
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn creation_finalizer_conflict_requeues_without_failure_status_patch() {
    let fixture = Fixture::new(true);
    fixture.management.respond(
        "PATCH",
        TENANT,
        409,
        crate::support::kube::status(409, "Conflict"),
    );
    fixture.reconciler.reconcile_name("tenant-a").await.unwrap();
    let calls = fixture.management.calls();
    assert_eq!(
        calls
            .iter()
            .map(|call| (call.method.as_str(), call.path.as_str()))
            .collect::<Vec<_>>(),
        [("GET", TENANT), ("PATCH", TENANT)]
    );
}

#[tokio::test]
async fn finalizer_patch_conflict_requeues_without_failure_status_patch() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.management.get(TENANT);
    tenant["metadata"]["deletionTimestamp"] = json!("2026-09-27T00:00:00Z");
    tenant["metadata"]["finalizers"] = json!([FINALIZER]);
    tenant["status"] = json!({
        "phase":"Deleting",
        "foundationHash":fixture.hash,
        "conditions":[]
    });
    fixture.management.insert(TENANT, tenant);
    for resource in management::descendants().filter(|resource| resource.role != "provider") {
        let (group, version) = resource.api_version.split_once('/').unwrap();
        fixture.management.allow_list(&format!(
            "/apis/{group}/{version}/namespaces/tenant-a/{}",
            resource.plural
        ));
    }
    fixture.management.respond(
        "PATCH",
        TENANT,
        409,
        crate::support::kube::status(409, "Conflict"),
    );

    fixture.reconciler.reconcile_name("tenant-a").await.unwrap();

    assert_eq!(
        fixture
            .management
            .calls()
            .iter()
            .filter(|call| call.method == "PATCH" && call.path == TENANT)
            .count(),
        1
    );
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| !(call.method == "PATCH" && call.path == format!("{TENANT}/status")))
    );
}

#[tokio::test]
async fn foundation_replacement_never_adds_finalizer_or_claims() {
    let fixture = Fixture::new(true);
    let mut tenant = fixture.current();
    tenant.status = Some(TenantStatus {
        provider: Some(tenant_controller::api::TenantProviderStatus::Local(
            tenant_controller::api::LocalProviderStatus {
                foundation_hash: Some("old-foundation".into()),
                ..Default::default()
            },
        )),
        ..Default::default()
    });
    fixture.management.insert(TENANT, tenant);
    fixture.step().await;
    assert!(fixture.current().finalizers().is_empty());
    assert_eq!(fixture.management.calls().len(), 2);
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn missing_status_bound_lease_fails_closed_before_namespace_or_docker() {
    let fixture = Fixture::new(true);
    for _ in 0..3 {
        fixture.step().await;
    }
    fixture
        .management
        .0
        .lock()
        .unwrap()
        .objects
        .retain(|key, _| !key.starts_with(&format!("{LEASES}/")));
    fixture.clear();
    fixture.step().await;
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(tenant_controller::api::TenantPhase::OwnershipInvalid)
    );
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| call.method != "POST")
    );
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn control_plane_aggregate_gates_credentials_volume_and_workers() {
    let fixture = Fixture::new(true);
    for _ in 0..8 {
        fixture.step().await;
        fixture.settle_provider();
    }
    let mut cluster = fixture.management.get(CLUSTER);
    cluster["status"]["conditions"] = json!([{"type":"Available","status":"True","observedGeneration":cluster["metadata"]["generation"]}]);
    fixture.management.insert(CLUSTER, cluster);
    fixture.clear();
    fixture.step().await;
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
    assert!(
        !fixture
            .management
            .calls()
            .iter()
            .any(|call| call.path.contains("/machinedeployments"))
    );
}

#[tokio::test]
async fn full_pipeline_converges_then_observes_once_and_preserves_static_content_drift() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let storage = fixture
        .workload
        .get("/apis/storage.k8s.io/v1/storageclasses/capi-hostpath");
    assert_eq!(storage["volumeBindingMode"], "Immediate");
    assert_eq!(storage["reclaimPolicy"], "Retain");
    assert_eq!(
        fixture
            .current()
            .status
            .unwrap()
            .database_capability
            .unwrap()
            .reason,
        "Ready"
    );
    let config_path = "/api/v1/namespaces/kube-system/configmaps/capi-kube-proxy";
    let mut config = fixture.workload.get(config_path);
    config["data"]["config.conf"] = json!("owned drift is intentionally not repaired");
    fixture.workload.insert(config_path, config);
    fixture.clear();
    let action = fixture.step().await;
    assert_eq!(action, Action::requeue(std::time::Duration::from_secs(300)));
    let calls = fixture.workload.calls();
    assert_eq!(
        calls
            .iter()
            .filter(|call| call.path == "/api/v1/nodes")
            .count(),
        1
    );
    assert!(
        calls
            .iter()
            .all(|call| !call.path.contains("/postgresql.cnpg.io/"))
    );
    assert!(calls.iter().all(
        |call| !call.path.ends_with("/pods") && !call.path.ends_with("/persistentvolumeclaims")
    ));
    assert!(calls.iter().all(|call| call.method != "PATCH"));
    assert_eq!(
        fixture.workload.get(config_path)["data"]["config.conf"],
        "owned drift is intentionally not repaired"
    );
    let status = fixture.current().status.unwrap();
    assert!(
        status
            .conditions
            .iter()
            .all(|condition| condition.observed_generation == Some(2))
    );
    assert_eq!(
        status.phase,
        Some(tenant_controller::api::TenantPhase::Ready)
    );
}

#[tokio::test]
async fn network_resources_are_created_without_waiting_for_any_node_inventory() {
    let fixture = Fixture::new(true);
    for _ in 0..20 {
        fixture.clear();
        fixture.step().await;
        let calls = fixture.workload.calls();
        if calls
            .iter()
            .any(|call| call.method == "POST" && call.body["metadata"]["name"] == "calico-node")
        {
            assert!(!calls.iter().any(|call| call.path == "/api/v1/nodes"));
            assert!(!fixture.management.calls().iter().any(
                |call| call.path.ends_with("/machines") || call.path.ends_with("/devmachines")
            ));
            return;
        }
        fixture.settle_provider();
    }
    panic!("network bundle was not created");
}

#[tokio::test]
async fn worker_root_owners_are_optional_but_must_be_exact_when_present() {
    for (api, kind, plural) in [
        (
            "bootstrap.cluster.x-k8s.io/v1beta2",
            "KubeadmConfigTemplate",
            "kubeadmconfigtemplates",
        ),
        (
            "infrastructure.cluster.x-k8s.io/v1beta2",
            "DevMachineTemplate",
            "devmachinetemplates",
        ),
        (
            "cluster.x-k8s.io/v1beta2",
            "MachineDeployment",
            "machinedeployments",
        ),
    ] {
        for mutation in ["absent", "uid", "name", "kind", "api", "multiple"] {
            let fixture = Fixture::new(true);
            fixture.until_ready().await;
            let path = format!("/apis/{api}/namespaces/tenant-a/{plural}/tenant-a-worker");
            let mut root = fixture.management.get(&path);
            match mutation {
                "absent" => {
                    root["metadata"]
                        .as_object_mut()
                        .unwrap()
                        .remove("ownerReferences");
                }
                "uid" => root["metadata"]["ownerReferences"][0]["uid"] = json!("foreign"),
                "name" => root["metadata"]["ownerReferences"][0]["name"] = json!("foreign"),
                "kind" => root["metadata"]["ownerReferences"][0]["kind"] = json!("Foreign"),
                "api" => {
                    root["metadata"]["ownerReferences"][0]["apiVersion"] =
                        json!("cluster.x-k8s.io/v1beta1")
                }
                "multiple" => {
                    let owner = root["metadata"]["ownerReferences"][0].clone();
                    root["metadata"]["ownerReferences"]
                        .as_array_mut()
                        .unwrap()
                        .push(owner);
                }
                _ => unreachable!(),
            }
            fixture.management.insert(&path, root);
            fixture.clear();
            fixture.step().await;
            let status = fixture.current().status.unwrap();
            if mutation == "absent" {
                assert_eq!(
                    status.phase,
                    Some(tenant_controller::api::TenantPhase::Ready),
                    "{kind}: {mutation}"
                );
                continue;
            }
            assert_eq!(
                status.phase,
                Some(tenant_controller::api::TenantPhase::OwnershipInvalid),
                "{kind}: {mutation}"
            );
            assert!(
                status
                    .conditions
                    .iter()
                    .any(|condition| condition.type_ == "Ready" && condition.status == "False")
            );
            assert!(
                !fixture
                    .workload
                    .calls()
                    .iter()
                    .any(|call| call.path == "/api/v1/nodes"
                        || call.path.contains("/postgresql.cnpg.io/")
                        || call.path.contains("/daemonsets/")
                        || call.path.contains("/deployments/"))
            );
            assert!(!fixture.management.calls().iter().any(
                |call| call.path.ends_with("/machines") || call.path.ends_with("/devmachines")
            ));
        }
    }
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let deployment: DynamicObject =
        serde_json::from_value(fixture.management.get(DEPLOYMENT)).unwrap();
    for path in [
        "/apis/bootstrap.cluster.x-k8s.io/v1beta2/namespaces/tenant-a/kubeadmconfigtemplates/tenant-a-worker",
        "/apis/infrastructure.cluster.x-k8s.io/v1beta2/namespaces/tenant-a/devmachinetemplates/tenant-a-worker",
    ] {
        let mut template = fixture.management.get(path);
        template["metadata"]["ownerReferences"] = json!([owner(&deployment)]);
        fixture.management.insert(path, template);
    }
    assert_eq!(
        fixture.step().await,
        Action::requeue(std::time::Duration::from_secs(300))
    );
}

#[tokio::test]
async fn tenant_readiness_does_not_depend_on_database_workloads() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    fixture.clear();
    assert_eq!(
        fixture.step().await,
        Action::requeue(std::time::Duration::from_secs(300))
    );
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(TenantPhase::Ready)
    );
    assert!(fixture.workload.calls().iter().all(|call| {
        !call.path.contains("/postgresql.cnpg.io/") && !call.path.contains("/namespaces/database")
    }));
}

#[tokio::test]
async fn database_runtime_capability_loss_preserves_infrastructure_ready() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let credential_path =
        "/apis/rbac.authorization.k8s.io/v1/namespaces/tenant-a/roles/tenant-database-credentials";
    let mut credential = fixture.management.get(credential_path);
    assert_eq!(
        credential["rules"][0]["resourceNames"],
        json!(["tenant-a-kubeconfig"])
    );
    assert_eq!(credential["rules"][0]["verbs"], json!(["get"]));
    let binding = fixture.management.get(
        "/apis/rbac.authorization.k8s.io/v1/namespaces/tenant-a/rolebindings/tenant-database-credentials"
    );
    assert_eq!(
        binding["subjects"],
        json!([
            {"kind":"ServiceAccount","name":"tenant-admin","namespace":"tenant-system"},
            {"kind":"ServiceAccount","name":"database-controller","namespace":"tenant-system"}
        ])
    );
    credential["rules"][0]["resourceNames"] = json!(["unrelated-secret"]);
    fixture.management.insert(credential_path, &credential);
    fixture.step().await;
    assert_eq!(
        fixture.current().status.as_ref().unwrap().phase,
        Some(TenantPhase::Ready)
    );
    assert!(
        !fixture
            .current()
            .status
            .unwrap()
            .database_capability
            .unwrap()
            .available
    );
    credential["rules"][0]["resourceNames"] = json!(["tenant-a-kubeconfig"]);
    fixture.management.insert(credential_path, credential);
    let path = "/api/v1/namespaces/tenant-db-tenant-a/resourcequotas/tenant-database-limit";
    let mut quota = fixture.management.get(path);
    quota["spec"]["hard"]["count/tenantdatabases.tenancy.cnpg-vcluster.io"] = json!("4");
    fixture.management.insert(path, &quota);
    fixture.clear();
    assert_eq!(
        fixture.step().await,
        Action::requeue(std::time::Duration::from_secs(5))
    );
    let status = fixture.current().status.unwrap();
    assert_eq!(status.phase, Some(TenantPhase::Ready));
    assert!(!status.database_capability.unwrap().available);
    quota["spec"]["hard"]["count/tenantdatabases.tenancy.cnpg-vcluster.io"] = json!("3");
    fixture.management.insert(path, quota);
    fixture.step().await;
    assert!(
        fixture
            .current()
            .status
            .unwrap()
            .database_capability
            .unwrap()
            .available
    );
}

#[tokio::test]
async fn tenant_deletion_closes_gate_and_drains_database_namespace_before_infrastructure() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    fixture.management.allow_typed_list(
        "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabases",
        "tenancy.cnpg-vcluster.io/v1alpha1",
        "TenantDatabase",
    );
    let mut tenant = fixture.management.get(TENANT);
    tenant["metadata"]["deletionTimestamp"] = json!("2026-01-01T00:00:00Z");
    fixture.management.insert(TENANT, tenant);
    let gate_path = format!(
        "/api/v1/namespaces/tenant-database-gates/configmaps/{}",
        tenant_database_runtime::gate_name("tenant-uid"),
    );
    fixture.clear();
    fixture.step().await;
    assert_eq!(
        fixture.management.get(&gate_path)["data"]["state"],
        "closed"
    );
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| call.method != "DELETE")
    );
    fixture.clear();
    fixture.step().await;
    assert!(fixture.management.calls().iter().any(|call| {
        call.method == "DELETE" && call.path == "/api/v1/namespaces/tenant-db-tenant-a"
    }));
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| call.path != CLUSTER)
    );
    fixture.clear();
    fixture.step().await;
    assert_eq!(
        fixture
            .current()
            .status
            .unwrap()
            .database_capability
            .unwrap()
            .reason,
        "Retiring"
    );
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| call.method != "DELETE")
    );
    fixture.clear();
    fixture.step().await;
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .any(|call| { call.method == "DELETE" && call.path == gate_path })
    );
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| call.path != CLUSTER)
    );
}

#[tokio::test]
async fn unbound_gate_entry_waits_for_drain_deadline_and_two_absence_observations() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    fixture.management.allow_typed_list(
        "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabases",
        "tenancy.cnpg-vcluster.io/v1alpha1",
        "TenantDatabase",
    );
    let gate_path = format!(
        "/api/v1/namespaces/tenant-database-gates/configmaps/{}",
        tenant_database_runtime::gate_name("tenant-uid"),
    );
    let mut gate = fixture.management.get(&gate_path);
    let entry = tenant_database_runtime::GateEntry {
        namespace: "tenant-db-tenant-a".into(),
        name: "orders".into(),
        spec_sha256: "a".repeat(64),
        created_at: (chrono::Utc::now() - chrono::Duration::seconds(130)).to_rfc3339(),
        bound_uid: None,
        first_absent_at: None,
        absence_checks: 0,
    };
    gate["data"]["reservations"] = json!(
        serde_json::to_string(&std::collections::BTreeMap::from([("1234-5678", entry)])).unwrap()
    );
    fixture.management.insert(&gate_path, gate);
    let mut tenant = fixture.management.get(TENANT);
    tenant["metadata"]["deletionTimestamp"] = json!("2026-01-01T00:00:00Z");
    fixture.management.insert(TENANT, tenant);
    fixture.step().await;
    fixture.step().await;
    let gate = fixture.management.get(&gate_path);
    let entries: serde_json::Value =
        serde_json::from_str(gate["data"]["reservations"].as_str().unwrap()).unwrap();
    assert_eq!(entries["1234-5678"]["absenceChecks"], 1);
    assert!(
        fixture
            .management
            .0
            .lock()
            .unwrap()
            .objects
            .contains_key("/api/v1/namespaces/tenant-db-tenant-a")
    );
    fixture.step().await;
    let mut gate = fixture.management.get(&gate_path);
    let mut entries: serde_json::Value =
        serde_json::from_str(gate["data"]["reservations"].as_str().unwrap()).unwrap();
    assert_eq!(entries["1234-5678"]["absenceChecks"], 1);
    entries["1234-5678"]["firstAbsentAt"] =
        json!((chrono::Utc::now() - chrono::Duration::seconds(3)).to_rfc3339());
    gate["data"]["reservations"] = json!(entries.to_string());
    fixture.management.insert(&gate_path, gate);
    fixture.step().await;
    let gate = fixture.management.get(&gate_path);
    let entries: serde_json::Value =
        serde_json::from_str(gate["data"]["reservations"].as_str().unwrap()).unwrap();
    assert_eq!(entries["1234-5678"]["absenceChecks"], 2);
    fixture.step().await;
    let gate = fixture.management.get(&gate_path);
    assert_eq!(gate["data"]["reservations"], "{}");
    assert!(
        fixture
            .management
            .0
            .lock()
            .unwrap()
            .objects
            .contains_key("/api/v1/namespaces/tenant-db-tenant-a")
    );
}

#[tokio::test]
async fn accepted_database_is_deleted_exactly_before_infrastructure_or_replacement_refused() {
    for replacement in [false, true] {
        let fixture = Fixture::new(true);
        fixture.until_ready().await;
        fixture.management.allow_typed_list(
            "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabases",
            "tenancy.cnpg-vcluster.io/v1alpha1",
            "TenantDatabase",
        );
        let gate_path = format!(
            "/api/v1/namespaces/tenant-database-gates/configmaps/{}",
            tenant_database_runtime::gate_name("tenant-uid"),
        );
        let mut gate = fixture.management.get(&gate_path);
        let entry = tenant_database_runtime::GateEntry {
            namespace: "tenant-db-tenant-a".into(),
            name: "orders".into(),
            spec_sha256: "a".repeat(64),
            created_at: chrono::Utc::now().to_rfc3339(),
            bound_uid: Some("database-uid".into()),
            first_absent_at: None,
            absence_checks: 0,
        };
        gate["data"]["reservations"] = json!(
            serde_json::to_string(&std::collections::BTreeMap::from([("1234-5678", entry)]))
                .unwrap()
        );
        let gate_uid = gate["metadata"]["uid"].clone();
        fixture.management.insert(&gate_path, gate);
        let database_path = "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabases/orders";
        fixture.management.insert(
            database_path,
            json!({
                "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha1", "kind":"TenantDatabase",
                "metadata":{"namespace":"tenant-db-tenant-a", "name":"orders",
                    "uid":if replacement {"replacement-uid"} else {"database-uid"},
                    "resourceVersion":"5",
                    "finalizers":["tenancy.cnpg-vcluster.io/database-finalizer"],
                    "annotations":{
                        "tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid",
                        "tenancy.cnpg-vcluster.io/admission-request-uid":"1234-5678",
                        "tenancy.cnpg-vcluster.io/gate-uid":gate_uid,
                        "tenancy.cnpg-vcluster.io/database-spec-sha256":"a".repeat(64)
                    }},
                "spec":{"tenantName":"tenant-a","tenantUID":"tenant-uid","instances":1}
            }),
        );
        let mut tenant = fixture.management.get(TENANT);
        tenant["metadata"]["deletionTimestamp"] = json!("2026-01-01T00:00:00Z");
        fixture.management.insert(TENANT, tenant);
        fixture.step().await;
        fixture.clear();
        fixture.step().await;
        let deleted = fixture.management.calls().iter().any(|call| {
            call.method == "DELETE"
                && call.path == database_path
                && call.body["preconditions"]["uid"] == "database-uid"
                && call.body["preconditions"]["resourceVersion"] == "5"
        });
        assert_eq!(deleted, !replacement);
        assert!(
            fixture
                .management
                .calls()
                .iter()
                .all(|call| { !(call.method == "DELETE" && call.path == CLUSTER) })
        );
        if replacement {
            assert_eq!(
                fixture.current().status.unwrap().phase,
                Some(TenantPhase::OwnershipInvalid)
            );
        }
    }
}

#[tokio::test]
async fn established_worker_recovery_remains_degraded_and_uses_short_retry() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let path = "/api/v1/nodes/worker-a";
    let mut value = fixture.workload.get(path);
    value["status"]["conditions"][0]["status"] = json!("False");
    fixture.workload.insert(path, value);
    fixture.clear();
    assert_eq!(
        fixture.step().await,
        Action::requeue(std::time::Duration::from_secs(5))
    );
    let status = fixture.current().status.unwrap();
    assert_eq!(status.phase, Some(TenantPhase::Degraded));
    assert_eq!(
        status
            .conditions
            .iter()
            .find(|condition| condition.type_ == "Ready")
            .unwrap()
            .reason,
        "Recovering"
    );
    assert_eq!(
        fixture
            .workload
            .calls()
            .iter()
            .filter(|call| call.path == "/api/v1/nodes")
            .count(),
        1
    );
}

#[tokio::test]
async fn root_apply_conflict_requeues_without_external_mutation_or_terminal_failure() {
    let fixture = Fixture::new(true);
    for _ in 0..6 {
        fixture.step().await;
    }
    fixture
        .management
        .respond("PATCH", CLUSTER, 409, status(409, "Conflict"));
    fixture.clear();
    assert_eq!(fixture.step().await, Action::requeue(PROGRESS_INTERVAL));
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
    assert!(
        !fixture
            .management
            .calls()
            .iter()
            .any(|call| call.path.ends_with("/status"))
    );
}

#[tokio::test]
async fn creation_invalid_snapshot_is_classified_without_external_mutation() {
    let mut fixture = Fixture::new(true);
    Arc::make_mut(&mut fixture.reconciler.provider.foundation).creation = Err(
        FoundationError::Invalid("creation inputs are invalid".into()),
    );
    fixture.step().await;
    let tenant = fixture.current();
    assert!(tenant.finalizers().is_empty());
    assert_eq!(
        tenant
            .status
            .unwrap()
            .conditions
            .iter()
            .find(|condition| condition.type_ == "Ready")
            .unwrap()
            .reason,
        "FoundationInvalid"
    );
    assert!(fixture.workload.calls().is_empty());
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn bootstrap_rbac_drift_is_degraded_and_blocks_volume_and_worker_mutations() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let roles = fixture
        .workload
        .0
        .lock()
        .unwrap()
        .objects
        .keys()
        .filter(|path| path.contains("/roles/"))
        .cloned()
        .collect::<Vec<_>>();
    assert!(!roles.is_empty());
    for role in roles {
        let mut value = fixture.workload.get(&role);
        value["rules"] = json!([]);
        fixture.workload.insert(&role, value);
    }
    fixture.clear();
    assert_eq!(
        fixture.step().await,
        Action::requeue(std::time::Duration::from_secs(300))
    );
    let status = fixture.current().status.unwrap();
    assert_eq!(
        status.phase,
        Some(tenant_controller::api::TenantPhase::Degraded)
    );
    assert_eq!(
        status
            .conditions
            .iter()
            .find(|condition| condition.type_ == "Ready")
            .unwrap()
            .reason,
        "BootstrapAccessMismatch"
    );
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .is_empty()
    );
    assert!(
        !fixture
            .management
            .calls()
            .iter()
            .any(|call| call.path == DEPLOYMENT)
    );
    assert!(
        fixture
            .workload
            .calls()
            .iter()
            .all(|call| call.method == "GET")
    );
}

#[tokio::test]
async fn foreign_volume_blocks_worker_mutations_and_is_not_adopted() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    for volume in fixture
        .reconciler
        .provider
        .docker
        .volumes
        .lock()
        .unwrap()
        .values_mut()
    {
        volume.labels.insert("foreign".into(), "true".into());
    }
    fixture.clear();
    fixture.step().await;
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(tenant_controller::api::TenantPhase::OwnershipInvalid)
    );
    assert!(
        !fixture
            .management
            .calls()
            .iter()
            .any(|call| call.path == DEPLOYMENT)
    );
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .volumes
            .lock()
            .unwrap()
            .values()
            .all(|volume| volume.labels.get("foreign").map(String::as_str) == Some("true"))
    );
    assert!(
        fixture
            .reconciler
            .provider
            .docker
            .calls
            .lock()
            .unwrap()
            .iter()
            .all(|call| call.starts_with("inspect "))
    );
}

#[tokio::test]
async fn final_availability_is_distinct_from_control_plane_access_and_observed_with_current_generation()
 {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let mut cluster = fixture.management.get(CLUSTER);
    cluster["status"]["conditions"][1]["status"] = json!("False");
    fixture.management.insert(CLUSTER, cluster);
    fixture.clear();
    assert_eq!(
        fixture.step().await,
        Action::requeue(std::time::Duration::from_secs(300))
    );
    let status = fixture.current().status.unwrap();
    assert_eq!(
        status.phase,
        Some(tenant_controller::api::TenantPhase::Degraded)
    );
    assert_eq!(
        status
            .conditions
            .iter()
            .find(|condition| condition.type_ == "ControlPlaneReady")
            .unwrap()
            .status,
        "False"
    );
    assert!(
        status
            .conditions
            .iter()
            .all(|condition| condition.type_ != "DatabaseReady")
    );
    assert_eq!(
        fixture
            .management
            .calls()
            .iter()
            .filter(|call| call.path == CLUSTER && call.method == "GET")
            .count(),
        1
    );
}

#[tokio::test]
async fn worker_spec_drift_is_repaired_only_on_the_live_bound_identity() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let mut deployment = fixture.management.get(DEPLOYMENT);
    deployment["spec"]["replicas"] = json!(3);
    fixture.management.insert(DEPLOYMENT, &deployment);
    fixture.clear();
    fixture.step().await;
    assert_eq!(fixture.management.get(DEPLOYMENT)["spec"]["replicas"], 1);
    let calls = fixture.management.calls();
    let apply = calls
        .iter()
        .find(|call| call.method == "PATCH" && call.path == DEPLOYMENT)
        .unwrap();
    assert_eq!(apply.body["metadata"]["uid"], deployment["metadata"]["uid"]);
    assert_eq!(
        apply.body["metadata"]["resourceVersion"],
        deployment["metadata"]["resourceVersion"]
    );
    assert!(
        fixture
            .workload
            .calls()
            .iter()
            .all(|call| !call.path.contains("/postgresql.cnpg.io/"))
    );
}
