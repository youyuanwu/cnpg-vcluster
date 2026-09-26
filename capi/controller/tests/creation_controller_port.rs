//! Go parity: tenant_controller, phase2, readiness, reconcile_cnpg and status.
//! This Tower service drives the real creation pipeline across every barrier.
mod creation_support;

use creation_support::*;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference;
use kube::{ResourceExt, core::DynamicObject, runtime::controller::Action};
use serde_json::{Value, json};
use tenant_controller::{
    api::{Tenant, TenantStatus, canonical_spec, spec_hash},
    docker::{DockerContainer, WORKER_CLUSTER_LABEL, WORKER_ROLE_LABEL},
    foundation::{Foundation, canonical_hash},
    ownership::Identity,
    reconcile::{Assets, Config, FINALIZER, PROGRESS_INTERVAL, Reconciler},
};

const TENANT: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants/tenant-a";
const FOUNDATION: &str = "/api/v1/namespaces/tenant-system/configmaps/tenant-foundation";
const LEASES: &str = "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases";
const CLUSTER: &str = "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/clusters/tenant-a";
const DEPLOYMENT: &str =
    "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/machinedeployments/tenant-a-worker";

struct Fixture {
    management: Server,
    workload: Server,
    reconciler: Reconciler<FakeDocker, FakeAccess, FakeDeletion>,
    foundation: Foundation,
    hash: String,
}

impl Fixture {
    fn new(mutation: bool) -> Self {
        let management = Server::default();
        let workload = Server::default();
        management.insert(TENANT, tenant());
        let mut raw: Value =
            serde_json::from_str::<Value>(include_str!("fixtures/foundation-schema3.json"))
                .unwrap()["foundation"]
                .clone();
        raw["inputs"]["cacheHostPath"] = json!("/cache");
        raw["inputs"]["konnectivityServerImage"] = json!("server:one@sha256:a");
        raw["inputs"]["konnectivityAgentImage"] = json!("agent:one@sha256:b");
        let encoded = serde_json::to_string(&raw).unwrap();
        let hash = canonical_hash(&encoded).unwrap();
        let foundation: Foundation = serde_json::from_value(raw).unwrap();
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
            docker: FakeDocker::default(),
            access: FakeAccess(workload.client()),
            deletion: FakeDeletion::default(),
            config: Config {
                mutation_enabled: mutation,
                controller_image: "controller:one".into(),
                ..Default::default()
            },
            assets: Assets { calico, cnpg },
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
        self.reconciler.docker.calls.lock().unwrap().clear();
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
        *self.reconciler.docker.containers.lock().unwrap() = vec![DockerContainer {
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
            if self
                .current()
                .status
                .as_ref()
                .and_then(|status| status.phase)
                == Some(tenant_controller::api::TenantPhase::Ready)
            {
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
    assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn mutation_disabled_and_invalid_spec_have_no_foundation_or_external_calls() {
    for invalid in [false, true] {
        let fixture = Fixture::new(false);
        if invalid {
            let mut tenant = fixture.current();
            tenant.spec.workers = 4;
            fixture.management.insert(TENANT, tenant);
        }
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
            if invalid {
                "InvalidSpec"
            } else {
                "MutationDisabled"
            }
        );
        assert!(fixture.workload.calls().is_empty());
        assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
    }
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
async fn deletion_receives_the_runtime_supported_version() {
    let mut fixture = Fixture::new(false);
    fixture.reconciler.config.supported_version = "1.36.5".into();
    let mut tenant = fixture.current();
    tenant.spec.kubernetes_version = "v1.36.5".into();
    tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-25T00:00:00Z")).unwrap());
    fixture.management.insert(TENANT, tenant);
    fixture.step().await;
    assert_eq!(
        *fixture.reconciler.deletion.0.lock().unwrap(),
        [("tenant-a".into(), "1.36.5".into())]
    );
}

#[tokio::test]
async fn managed_deletion_bypasses_both_creation_mutation_gates_and_assets() {
    for managed in [false, true] {
        let mut fixture = Fixture::new(false);
        let mut tenant = fixture.current();
        tenant.metadata.deletion_timestamp =
            Some(serde_json::from_value(json!("2026-09-25T00:00:00Z")).unwrap());
        if managed {
            tenant.metadata.finalizers = Some(vec![FINALIZER.into()]);
        }
        fixture.management.insert(TENANT, tenant);
        fixture
            .management
            .0
            .lock()
            .unwrap()
            .objects
            .remove(FOUNDATION);
        fixture.reconciler.assets = Assets::default();
        fixture.step().await;
        assert_eq!(
            fixture.reconciler.deletion.0.lock().unwrap().len(),
            usize::from(managed)
        );
        assert_eq!(fixture.management.calls().len(), 1);
        assert!(fixture.workload.calls().is_empty());
    }
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
        ["GET", "GET", "PUT"]
    );
    assert_eq!(
        calls[2].path, TENANT,
        "finalizer uses main resource, not status"
    );
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
        ["GET", "GET", "PATCH"]
    );
    assert_eq!(
        fixture
            .current()
            .status
            .as_ref()
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
        ["GET", "GET", "GET", "POST", "GET", "PATCH"]
    );
    assert_eq!(calls[3].path, LEASES);
    assert!(
        fixture
            .current()
            .status
            .as_ref()
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
            .cluster_uid
            .is_some()
    );
    assert!(!calls.iter().any(
        |call| call.path.contains("/devclusters") || call.path.contains("/kamajicontrolplanes")
    ));
    assert!(fixture.workload.calls().is_empty());
    assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn foundation_replacement_or_disabled_foundation_never_adds_finalizer_or_claims() {
    for changed in [false, true] {
        let fixture = Fixture::new(true);
        if changed {
            let mut tenant = fixture.current();
            tenant.status = Some(TenantStatus {
                foundation_hash: Some("old-foundation".into()),
                ..Default::default()
            });
            fixture.management.insert(TENANT, tenant);
        } else {
            let mut config = fixture.management.get(FOUNDATION);
            let mut raw: Value =
                serde_json::from_str(config["data"]["foundation.json"].as_str().unwrap()).unwrap();
            raw["mutationEnabled"] = json!(false);
            config["data"]["foundation.json"] = json!(raw.to_string());
            fixture.management.insert(FOUNDATION, config);
        }
        fixture.step().await;
        assert!(fixture.current().finalizers().is_empty());
        assert_eq!(fixture.management.calls().len(), 3);
        assert!(fixture.workload.calls().is_empty());
        assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
    }
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
    assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
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
    assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
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
    assert_eq!(
        calls
            .iter()
            .filter(|call| call.method == "GET"
                && call.path
                    == "/apis/postgresql.cnpg.io/v1/namespaces/database/clusters/capi-postgres")
            .count(),
        1
    );
    assert!(calls.iter().all(
        |call| !call.path.ends_with("/pods") && !call.path.ends_with("/persistentvolumeclaims")
    ));
    let patches: Vec<_> = calls.iter().filter(|call| call.method == "PATCH").collect();
    assert_eq!(
        patches.len(),
        1,
        "only the CNPG Cluster is dynamic in the tenant API"
    );
    assert!(patches[0].path.ends_with("/clusters/capi-postgres"));
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
async fn database_health_trusts_aggregate_phase_and_ready_instances() {
    for evidence in [
        "missing-condition",
        "false-condition",
        "stale-condition",
        "stale-status",
        "missing-generation",
    ] {
        let fixture = Fixture::new(true);
        fixture.until_ready().await;
        let path = "/apis/postgresql.cnpg.io/v1/namespaces/database/clusters/capi-postgres";
        let mut database = fixture.workload.get(path);
        match evidence {
            "missing-condition" => {
                database["status"]
                    .as_object_mut()
                    .unwrap()
                    .remove("conditions");
            }
            "false-condition" => database["status"]["conditions"][0]["status"] = json!("False"),
            "stale-condition" => {
                database["status"]["conditions"][0]["observedGeneration"] = json!(0)
            }
            "stale-status" => database["status"]["observedGeneration"] = json!(0),
            "missing-generation" => {
                database["status"]["conditions"][0]
                    .as_object_mut()
                    .unwrap()
                    .remove("observedGeneration");
            }
            _ => unreachable!(),
        }
        fixture.workload.insert(path, database);
        assert_eq!(
            fixture.step().await,
            Action::requeue(std::time::Duration::from_secs(300))
        );
        let status = fixture.current().status.unwrap();
        assert_eq!(
            status.phase,
            Some(tenant_controller::api::TenantPhase::Ready),
            "{evidence}"
        );
    }
}

#[tokio::test]
async fn established_worker_and_database_recovery_remain_degraded_and_use_short_retry() {
    for component in ["worker", "database"] {
        let fixture = Fixture::new(true);
        fixture.until_ready().await;
        let path = if component == "worker" {
            "/api/v1/nodes/worker-a"
        } else {
            "/apis/postgresql.cnpg.io/v1/namespaces/database/clusters/capi-postgres"
        };
        let mut value = fixture.workload.get(path);
        if component == "worker" {
            value["status"]["conditions"][0]["status"] = json!("False");
        } else {
            value["status"]["readyInstances"] = json!(0);
        }
        fixture.workload.insert(path, value);
        fixture.clear();
        assert_eq!(
            fixture.step().await,
            Action::requeue(std::time::Duration::from_secs(5))
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
    assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
    assert!(
        !fixture
            .management
            .calls()
            .iter()
            .any(|call| call.path.ends_with("/status"))
    );
}

#[tokio::test]
async fn foundation_read_failures_are_classified_without_external_mutation() {
    for code in [404, 403, 503] {
        let fixture = Fixture::new(true);
        fixture
            .management
            .respond("GET", FOUNDATION, code, status(code, "Unavailable"));
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
        assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
    }
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
    assert!(fixture.reconciler.docker.calls.lock().unwrap().is_empty());
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
    assert_eq!(
        status
            .conditions
            .iter()
            .find(|condition| condition.type_ == "DatabaseReady")
            .unwrap()
            .status,
        "True"
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
async fn worker_and_database_spec_drift_is_repaired_only_on_the_live_bound_identities() {
    let fixture = Fixture::new(true);
    fixture.until_ready().await;
    let database_path = "/apis/postgresql.cnpg.io/v1/namespaces/database/clusters/capi-postgres";
    let mut deployment = fixture.management.get(DEPLOYMENT);
    deployment["spec"]["replicas"] = json!(3);
    fixture.management.insert(DEPLOYMENT, &deployment);
    let mut database = fixture.workload.get(database_path);
    database["spec"]["instances"] = json!(3);
    fixture.workload.insert(database_path, &database);
    fixture.clear();
    fixture.step().await;
    assert_eq!(fixture.management.get(DEPLOYMENT)["spec"]["replicas"], 1);
    assert_eq!(fixture.workload.get(database_path)["spec"]["instances"], 1);
    for (calls, original, path) in [
        (fixture.management.calls(), deployment, DEPLOYMENT),
        (fixture.workload.calls(), database, database_path),
    ] {
        let apply = calls
            .iter()
            .find(|call| call.method == "PATCH" && call.path == path)
            .unwrap();
        assert_eq!(apply.body["metadata"]["uid"], original["metadata"]["uid"]);
        assert_eq!(
            apply.body["metadata"]["resourceVersion"],
            original["metadata"]["resourceVersion"]
        );
    }
}
