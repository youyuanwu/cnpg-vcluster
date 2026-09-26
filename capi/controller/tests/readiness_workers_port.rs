//! Go parity: postcni_test.go. Also rejects unlabeled/extra inventory before
//! count-based pending and verifies that the complete pass lists Nodes once.
mod creation_support;

use creation_support::*;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference;
use kube::{ResourceExt, core::DynamicObject};
use serde_json::json;
use tenant_controller::{
    docker::{DockerContainer, WORKER_CLUSTER_LABEL, WORKER_ROLE_LABEL},
    reconcile::workers::{WorkerInputs, WorkerObservation, observe_workers},
};

struct Fixture {
    management: Server,
    tenant: Server,
    docker: FakeDocker,
    deployment: DynamicObject,
    machine: DynamicObject,
    dev: DynamicObject,
    node: DynamicObject,
    network: Vec<DynamicObject>,
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

fn ready(object: &mut DynamicObject) {
    object.data =
        json!({"status":{"conditions":[{"type":"Ready","status":"True","observedGeneration":2}]}});
}

impl Fixture {
    fn new() -> Self {
        let management = Server::default();
        let tenant = Server::default();
        let docker = FakeDocker::default();
        let deployment = object(
            "cluster.x-k8s.io/v1beta2",
            "MachineDeployment",
            "tenant-a",
            "tenant-a-worker",
            "machine-deployment",
        );
        let mut set = object(
            "cluster.x-k8s.io/v1beta2",
            "MachineSet",
            "tenant-a",
            "set-a",
            "machine",
        );
        set.metadata.owner_references = Some(vec![owner(&deployment)]);
        let mut machine = object(
            "cluster.x-k8s.io/v1beta2",
            "Machine",
            "tenant-a",
            "worker-a",
            "machine",
        );
        machine.metadata.owner_references = Some(vec![owner(&set)]);
        ready(&mut machine);
        let mut dev = object(
            "infrastructure.cluster.x-k8s.io/v1beta2",
            "DevMachine",
            "tenant-a",
            "worker-a",
            "machine",
        );
        dev.metadata.uid = Some("dev-uid".into());
        dev.metadata.owner_references = Some(vec![owner(&machine)]);
        ready(&mut dev);
        let mut node = object("v1", "Node", "", "worker-a", "machine");
        ready(&mut node);
        for value in [&set, &machine, &dev] {
            management.insert(&path(value), value);
            management.allow_list(path(value).rsplit_once('/').unwrap().0);
        }
        tenant.insert(&path(&node), &node);
        tenant.allow_list("/api/v1/nodes");
        docker.containers.lock().unwrap().push(DockerContainer {
            id: "container-a".into(),
            name: "worker-a".into(),
            state: "running".into(),
            labels: [
                (WORKER_CLUSTER_LABEL.into(), "tenant-a".into()),
                (WORKER_ROLE_LABEL.into(), "worker".into()),
            ]
            .into(),
            networks: [("kind".into(), "network-id".into())].into(),
            network_addresses: [("network-id".into(), "172.18.0.10".into())].into(),
        });
        let mut network = vec![];
        for (kind, name) in [
            ("DaemonSet", "calico-node"),
            ("Deployment", "calico-kube-controllers"),
            ("DaemonSet", "capi-kube-proxy"),
            ("Deployment", "coredns"),
        ] {
            let mut workload = object("apps/v1", kind, "kube-system", name, "network");
            workload.data = json!({"spec":{"replicas":1},"status":{"observedGeneration":2,"availableReplicas":1,"desiredNumberScheduled":1,"numberAvailable":1}});
            tenant.insert(&path(&workload), &workload);
            network.push(workload);
        }
        Self {
            management,
            tenant,
            docker,
            deployment,
            machine,
            dev,
            node,
            network,
        }
    }

    async fn observe(
        &self,
        count: i32,
        observed: &[DynamicObject],
    ) -> Result<WorkerObservation, tenant_controller::reconcile::ReconcileError> {
        observe_workers(
            self.management.client(),
            self.tenant.client(),
            &self.docker,
            WorkerInputs {
                identity: identity(),
                network_id: "network-id",
                desired_count: count,
                deployment: &self.deployment,
                network_objects: observed,
            },
        )
        .await
    }
}

#[tokio::test]
async fn exact_topology_reuses_network_snapshot_with_one_node_list_and_no_extra_root_get() {
    let fixture = Fixture::new();
    let result = fixture.observe(1, &fixture.network[..3]).await.unwrap();
    assert_eq!(
        result,
        WorkerObservation {
            inventory_complete: true,
            all_ready: true,
            network_ready: true
        }
    );
    let calls = fixture.tenant.calls();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].path, "/api/v1/nodes");
    assert!(calls[1].path.ends_with("/deployments/coredns"));
    assert_eq!(fixture.management.calls().len(), 3);
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| call.method == "GET" && call.query.is_empty())
    );
    assert_eq!(
        fixture.docker.calls.lock().unwrap().as_slice(),
        ["containers"]
    );
}

#[tokio::test]
async fn worker_readiness_and_network_availability_are_independent() {
    let fixture = Fixture::new();
    let mut node = fixture.node.clone();
    node.data["status"]["conditions"][0]["status"] = json!("False");
    fixture.tenant.insert(&path(&node), node);
    let result = fixture.observe(1, &[]).await.unwrap();
    assert!(result.inventory_complete && !result.all_ready && result.network_ready);
    assert_eq!(fixture.tenant.calls().len(), 5);
    let fixture = Fixture::new();
    let mut workload = fixture.network[0].clone();
    workload.data["status"]["numberAvailable"] = json!(0);
    fixture.tenant.insert(&path(&workload), workload);
    let result = fixture.observe(1, &[]).await.unwrap();
    assert!(result.inventory_complete && result.all_ready && !result.network_ready);
}

#[tokio::test]
async fn provider_and_node_readiness_allow_missing_but_reject_stale_generation_evidence() {
    for kind in ["Machine", "DevMachine", "Node"] {
        for evidence in ["absent", "stale", "current-status", "current-condition"] {
            let fixture = Fixture::new();
            let (mut object, server) = match kind {
                "Machine" => (fixture.machine.clone(), &fixture.management),
                "DevMachine" => (fixture.dev.clone(), &fixture.management),
                _ => (fixture.node.clone(), &fixture.tenant),
            };
            object.data = json!({"status":{"conditions":[{"type":"Ready","status":"True"}]}});
            match evidence {
                "absent" => {}
                "stale" => object.data["status"]["conditions"][0]["observedGeneration"] = json!(1),
                "current-status" => object.data["status"]["observedGeneration"] = json!(2),
                "current-condition" => {
                    object.data["status"]["conditions"][0]["observedGeneration"] = json!(2)
                }
                _ => unreachable!(),
            }
            server.insert(&path(&object), object);
            let observation = fixture.observe(1, &fixture.network).await.unwrap();
            assert!(observation.inventory_complete && observation.network_ready);
            assert_eq!(
                observation.all_ready,
                evidence != "stale",
                "{kind}: {evidence}"
            );
        }
    }
}

#[tokio::test]
async fn counts_and_stopped_containers_are_pending_only_after_all_identity_checks() {
    for desired in [1, 2] {
        let fixture = Fixture::new();
        fixture.docker.containers.lock().unwrap()[0].state = "created".into();
        let result = fixture.observe(desired, &[]).await.unwrap();
        assert!(!result.inventory_complete && !result.all_ready && !result.network_ready);
        assert_eq!(fixture.tenant.calls().len(), 1);
        assert_eq!(fixture.tenant.calls()[0].path, "/api/v1/nodes");
    }
}

#[tokio::test]
async fn extra_and_unlabeled_foreign_inventory_is_never_hidden_by_count_mismatch() {
    for kind in ["Machine", "MachineSet", "DevMachine", "Node"] {
        let fixture = Fixture::new();
        let (mut extra, server) = match kind {
            "Machine" => (fixture.machine.clone(), &fixture.management),
            "MachineSet" => {
                let extra = object(
                    "cluster.x-k8s.io/v1beta2",
                    kind,
                    "tenant-a",
                    "foreign",
                    "machine",
                );
                (extra, &fixture.management)
            }
            "DevMachine" => (fixture.dev.clone(), &fixture.management),
            _ => (fixture.node.clone(), &fixture.tenant),
        };
        extra.metadata.name = Some("foreign".into());
        extra.metadata.uid = Some("foreign-uid".into());
        extra.metadata.labels = None;
        extra.metadata.annotations = None;
        server.insert(&path(&extra), extra);
        let error = fixture.observe(3, &[]).await.unwrap_err();
        assert!(error.ownership_invalid(), "{kind}: {error}");
    }
}

#[tokio::test]
async fn provider_name_uid_owner_version_and_node_names_must_match_exactly() {
    for mutation in [
        "dev-name",
        "owner-uid",
        "owner-name",
        "owner-version",
        "owner-kind",
        "node-name",
        "machine-markers",
    ] {
        let fixture = Fixture::new();
        if mutation == "node-name" {
            fixture
                .tenant
                .0
                .lock()
                .unwrap()
                .objects
                .remove(&path(&fixture.node));
            let mut node = fixture.node.clone();
            node.metadata.name = Some("wrong".into());
            fixture.tenant.insert(&path(&node), node);
        } else if mutation == "machine-markers" {
            let mut machine = fixture.machine.clone();
            machine.metadata.annotations = None;
            fixture.management.insert(&path(&machine), machine);
        } else {
            let mut dev = fixture.dev.clone();
            let owners = dev.metadata.owner_references.as_mut().unwrap();
            match mutation {
                "dev-name" => dev.metadata.name = Some("wrong".into()),
                "owner-uid" => owners[0].uid = "replacement".into(),
                "owner-name" => owners[0].name = "wrong".into(),
                "owner-version" => owners[0].api_version = "cluster.x-k8s.io/v1beta1".into(),
                "owner-kind" => owners[0].kind = "MachineSet".into(),
                _ => unreachable!(),
            }
            fixture
                .management
                .0
                .lock()
                .unwrap()
                .objects
                .remove(&path(&fixture.dev));
            fixture.management.insert(&path(&dev), dev);
        }
        assert!(
            fixture
                .observe(2, &[])
                .await
                .unwrap_err()
                .ownership_invalid(),
            "{mutation}"
        );
    }
}

#[tokio::test]
async fn foreign_container_name_labels_network_duplicates_and_extra_workers_fail_closed() {
    for mutation in ["name", "role", "tenant", "network", "duplicate", "extra"] {
        let fixture = Fixture::new();
        {
            let mut containers = fixture.docker.containers.lock().unwrap();
            match mutation {
                "name" => containers[0].name = "foreign".into(),
                "role" => {
                    containers[0]
                        .labels
                        .insert(WORKER_ROLE_LABEL.into(), "control-plane".into());
                }
                "tenant" => {
                    containers[0]
                        .labels
                        .insert(WORKER_CLUSTER_LABEL.into(), "foreign".into());
                }
                "network" => containers[0].networks.clear(),
                "duplicate" => {
                    let clone = containers[0].clone();
                    containers.push(clone);
                }
                "extra" => {
                    let mut clone = containers[0].clone();
                    clone.name = "foreign".into();
                    clone.id = "extra".into();
                    containers.push(clone);
                }
                _ => unreachable!(),
            }
        }
        assert!(
            fixture
                .observe(3, &[])
                .await
                .unwrap_err()
                .ownership_invalid(),
            "{mutation}"
        );
    }
}

#[tokio::test]
async fn list_failure_is_an_error_not_absence_and_no_node_list_is_repeated() {
    for (server_kind, list_path) in [
        (
            "management",
            "/apis/cluster.x-k8s.io/v1beta2/namespaces/tenant-a/machines",
        ),
        ("tenant", "/api/v1/nodes"),
    ] {
        let fixture = Fixture::new();
        let server = if server_kind == "management" {
            &fixture.management
        } else {
            &fixture.tenant
        };
        server.respond("GET", list_path, 503, status(503, "ServiceUnavailable"));
        assert!(fixture.observe(1, &[]).await.is_err());
        assert!(
            fixture
                .tenant
                .calls()
                .iter()
                .filter(|call| call.path == "/api/v1/nodes")
                .count()
                <= 1
        );
    }
}
