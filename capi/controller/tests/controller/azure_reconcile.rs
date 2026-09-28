use std::collections::BTreeMap;

use k8s_openapi::api::core::v1::ConfigMap;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tenant_controller::{
    api::{Tenant, TenantPhase, TenantProviderSpec, TenantSpec, TenantStatus},
    azure::{AzureConfiguration, CONFIG_KEY},
    management::{self, AZURE_MANAGEMENT_RESOURCES, ResourceClass},
    reconcile::{AzureProvider, Config, Reconciler},
};

use crate::creation_support::{FakeAccess, Server};

const TENANT_PATH: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants/tenant-a";

struct Fixture {
    management: Server,
    workload: Server,
    configuration: AzureConfiguration,
}

impl Fixture {
    fn new() -> Self {
        let management = Server::default();
        let workload = Server::default();
        let mut tenant = Tenant::new(
            "tenant-a",
            TenantSpec {
                kubernetes_version: "1.32.13".into(),
                workers: 3,
                provider: TenantProviderSpec::Azure {
                    pod_cidr: "10.244.0.0/16".into(),
                    service_cidr: "10.96.0.0/16".into(),
                },
            },
        );
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.resource_version = Some("1".into());
        tenant.metadata.generation = Some(1);
        management.insert(TENANT_PATH, tenant);
        let mut lists = std::collections::BTreeSet::new();
        for resource in AZURE_MANAGEMENT_RESOURCES
            .iter()
            .filter(|resource| resource.namespaced)
            .filter(|resource| lists.insert((resource.api_version, resource.plural)))
        {
            management.allow_typed_list(
                &list_path(resource.api_version, resource.plural, "tenant-a"),
                resource.api_version,
                resource.kind,
            );
        }
        workload.allow_typed_list("/api/v1/nodes", "v1", "Node");
        let config = provider_config();
        let configuration = AzureConfiguration::from_config_map(&config).unwrap();
        Self {
            management,
            workload,
            configuration,
        }
    }

    fn current(&self) -> Tenant {
        serde_json::from_value(self.management.get(TENANT_PATH)).unwrap()
    }

    async fn step(&self) {
        let client = self.management.client();
        let reconciler = Reconciler::new(
            client.clone(),
            Config {
                supported_version: "1.32.13".into(),
                watch_management_resources: false,
                management_resources: AZURE_MANAGEMENT_RESOURCES,
            },
            AzureProvider {
                client,
                configuration: self.configuration.clone(),
                access: FakeAccess(self.workload.client()),
            },
        );
        reconciler.reconcile_name("tenant-a").await.unwrap();
    }

    fn settle(&self) {
        let mut state = self.management.0.lock().unwrap();
        let cluster = find_kind(&state.objects, "Cluster");
        let pool = find_kind(&state.objects, "MachinePool");
        let azure_pool = find_kind(&state.objects, "AzureMachinePool");
        let control_plane = find_kind(&state.objects, "KamajiControlPlane");
        for value in state.objects.values_mut() {
            match value["kind"].as_str() {
                Some("AzureCluster") | Some("KamajiControlPlane") => {
                    if let Some(owner) = cluster.as_ref() {
                        value["metadata"]["ownerReferences"] = json!([owner]);
                    }
                }
                Some("MachinePool") => {
                    if let Some(owner) = cluster.as_ref() {
                        let mut owner = owner.clone();
                        owner.as_object_mut().unwrap().remove("controller");
                        value["metadata"]["ownerReferences"] = json!([owner]);
                    }
                }
                Some("KubeadmConfig") | Some("AzureMachinePool") => {
                    if let Some(owner) = pool.as_ref() {
                        value["metadata"]["ownerReferences"] = json!([owner]);
                    }
                }
                _ => {}
            }
            match value["kind"].as_str() {
                Some("Cluster") => {
                    value["status"]["controlPlaneReady"] = json!(true);
                    value["status"]["conditions"] = json!([
                        {"type":"ControlPlaneAvailable","status":"True","observedGeneration":1}
                    ]);
                }
                Some("AzureCluster") => {
                    value["status"] = json!({"observedGeneration":1,
                        "conditions":[{"type":"Ready","status":"True","observedGeneration":1}]});
                }
                Some("KamajiControlPlane") => {
                    value["spec"]["controlPlaneEndpoint"] = json!({"host":"10.0.0.8","port":6443});
                    value["status"]["ready"] = json!(true);
                }
                Some("MachinePool") => {
                    value["status"] = json!({"replicas":3,"readyReplicas":3,
                        "nodeRefs":[{"name":"node-0"},{"name":"node-1"},{"name":"node-2"}]});
                }
                Some("AzureMachinePool") => {
                    value["status"] = json!({"replicas":3,"ready":true});
                }
                Some("Deployment")
                    if value["metadata"]["name"]
                        .as_str()
                        .is_some_and(|name| name.ends_with("-status-probe")) =>
                {
                    value["status"] = json!({"observedGeneration":1,"availableReplicas":1});
                }
                Some("Job") => {
                    value["status"]["conditions"] = json!([{"type":"Complete","status":"True"}]);
                }
                _ => {}
            }
        }
        if let Some(control_plane) = control_plane {
            let secret_path = "/api/v1/namespaces/tenant-a/secrets/tenant-a-kubeconfig";
            state.objects.entry(secret_path.into()).or_insert_with(|| {
                json!({"apiVersion":"v1","kind":"Secret","type":"cluster.x-k8s.io/secret",
                    "metadata":{"name":"tenant-a-kubeconfig","namespace":"tenant-a",
                        "uid":"kubeconfig-uid","resourceVersion":"1",
                        "ownerReferences":[control_plane]},
                    "data":{"value":"a3ViZWNvbmZpZw=="}})
            });
        }
        drop(state);
        self.seed_provider_workers(azure_pool);
        self.seed_workload();
    }

    fn seed_provider_workers(&self, azure_pool: Option<Value>) {
        let tenant = self.current();
        let Some(status) = tenant.status.as_ref().and_then(TenantStatus::azure) else {
            return;
        };
        let Some(management) = status.management.as_ref() else {
            return;
        };
        let (Some(pool_uid), Some(azure_pool_uid)) = (
            management.machine_pool_uid.as_deref(),
            management.azure_machine_pool_uid.as_deref(),
        ) else {
            return;
        };
        let pool_owner = json!({"apiVersion":"cluster.x-k8s.io/v1beta1","kind":"MachinePool",
            "name":"tenant-a-worker","uid":pool_uid,"controller":true});
        let binding = status.binding.as_ref().unwrap();
        let markers = json!({
            "lifecycle.cnpg-vcluster.capi/tenant":"tenant-a",
            "lifecycle.cnpg-vcluster.capi/profile":"azure",
            "lifecycle.cnpg-vcluster.capi/specification-sha256":binding.specification_sha256,
            "lifecycle.cnpg-vcluster.capi/foundation-sha256":binding.foundation_sha256,
            "lifecycle.cnpg-vcluster.capi/operation-id":binding.operation_id,
        });
        let azure_owner = azure_pool
            .and_then(|value| {
                value
                    .get("uid")
                    .cloned()
                    .or_else(|| value.pointer("/metadata/uid").cloned())
            })
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_else(|| azure_pool_uid.into());
        let azure_owner = json!({"apiVersion":"infrastructure.cluster.x-k8s.io/v1beta1",
            "kind":"AzureMachinePool","name":"tenant-a-worker","uid":azure_owner,"controller":true});
        for index in 0..3 {
            self.management.insert(
                &format!(
                    "/apis/cluster.x-k8s.io/v1beta1/namespaces/tenant-a/machines/machine-{index}"
                ),
                json!({"apiVersion":"cluster.x-k8s.io/v1beta1","kind":"Machine",
                    "metadata":{"name":format!("machine-{index}"),"namespace":"tenant-a",
                        "uid":format!("machine-{index}-uid"),"resourceVersion":"1",
                        "annotations":markers.clone(),
                        "ownerReferences":[pool_owner.clone()]}}),
            );
            self.management.insert(
                &format!("/apis/infrastructure.cluster.x-k8s.io/v1beta1/namespaces/tenant-a/azuremachinepoolmachines/azure-machine-{index}"),
                json!({"apiVersion":"infrastructure.cluster.x-k8s.io/v1beta1",
                    "kind":"AzureMachinePoolMachine",
                    "metadata":{"name":format!("azure-machine-{index}"),"namespace":"tenant-a",
                        "uid":format!("azure-machine-{index}-uid"),"resourceVersion":"1",
                        "ownerReferences":[azure_owner.clone()]},
                    "status":{"providerID":format!("azure:///subscriptions/subscription/resourceGroups/group/providers/Microsoft.Compute/virtualMachineScaleSets/tenant-a-worker/virtualMachines/{index}")}}),
            );
        }
    }

    fn seed_workload(&self) {
        for index in 0..3 {
            self.workload.insert(
                &format!("/api/v1/nodes/node-{index}"),
                json!({"apiVersion":"v1","kind":"Node",
                    "metadata":{"name":format!("node-{index}"),"uid":format!("node-{index}-uid"),
                        "resourceVersion":"1","generation":1},
                    "spec":{"providerID":format!("azure:///subscriptions/subscription/resourceGroups/group/providers/Microsoft.Compute/virtualMachineScaleSets/tenant-a-worker/virtualMachines/{index}")},
                    "status":{"addresses":[{"type":"InternalIP","address":format!("10.1.0.{}",index+4)}],
                        "conditions":[{"type":"Ready","status":"True"}]}}),
            );
        }
        for (kind, namespace, name) in [
            ("Deployment", "kube-system", "cloud-controller-manager"),
            ("DaemonSet", "kube-system", "cloud-node-manager"),
            ("DaemonSet", "calico-system", "calico-node"),
            ("Deployment", "calico-system", "calico-kube-controllers"),
        ] {
            let plural = if kind == "Deployment" {
                "deployments"
            } else {
                "daemonsets"
            };
            let status = if kind == "Deployment" {
                json!({"observedGeneration":1,"availableReplicas":1,"updatedReplicas":1})
            } else {
                json!({"observedGeneration":1,"desiredNumberScheduled":1,"numberAvailable":1,
                    "numberReady":1,"updatedNumberScheduled":1})
            };
            self.workload.insert(
                &format!("/apis/apps/v1/namespaces/{namespace}/{plural}/{name}"),
                json!({"apiVersion":"apps/v1","kind":kind,
                    "metadata":{"name":name,"namespace":namespace,"uid":format!("{name}-uid"),
                        "resourceVersion":"1","generation":1},
                    "spec":{"replicas":1},"status":status}),
            );
        }
    }

    async fn until_ready(&self) {
        for _ in 0..50 {
            self.step().await;
            self.settle();
            if self
                .current()
                .status
                .as_ref()
                .and_then(|status| status.phase)
                == Some(TenantPhase::Ready)
            {
                return;
            }
        }
        panic!(
            "Azure fixture did not converge: {:?}",
            self.current().status
        );
    }

    fn mark_deleting(&self) {
        let mut tenant = self.management.get(TENANT_PATH);
        tenant["metadata"]["deletionTimestamp"] = json!("2026-09-28T00:00:00Z");
        self.management.insert(TENANT_PATH, tenant);
    }

    fn remove_kinds(&self, kinds: &[&str]) {
        let mut state = self.management.0.lock().unwrap();
        state.objects.retain(|_, value| {
            !value["kind"]
                .as_str()
                .is_some_and(|kind| kinds.contains(&kind))
        });
    }

    fn start_azure_cluster_deletion_without_lb(&self) {
        let mut state = self.management.0.lock().unwrap();
        let value = state
            .objects
            .values_mut()
            .find(|value| value["kind"] == "AzureCluster")
            .unwrap();
        value["metadata"]["deletionTimestamp"] = json!("2026-09-28T00:00:01Z");
        value["spec"]["networkSpec"]
            .as_object_mut()
            .unwrap()
            .remove("apiServerLB");
    }
}

fn provider_config() -> ConfigMap {
    let mut value = json!({
        "schema":1,"subscriptionId":"subscription","tenantId":"tenant","location":"region",
        "resourceGroupName":"group",
        "resourceGroupId":"/subscriptions/subscription/resourceGroups/group",
        "vnetName":"vnet",
        "vnetId":"/subscriptions/subscription/resourceGroups/group/providers/Microsoft.Network/virtualNetworks/vnet",
        "tenantSubnetName":"subnet",
        "tenantSubnetId":"/subscriptions/subscription/resourceGroups/group/providers/Microsoft.Network/virtualNetworks/vnet/subnets/subnet",
        "identityName":"identity",
        "identityId":"/subscriptions/subscription/resourceGroups/group/providers/Microsoft.ManagedIdentity/userAssignedIdentities/identity",
        "identityClientId":"client-id","supportedKubernetesVersion":"1.32.13",
        "workerSku":"Standard_B2s","capiVersion":"v1.10.7","capzVersion":"v1.21.1",
        "kamajiCapiVersion":"v0.19.0","kamajiChartVersion":"26.8.6-edge",
        "asoVersion":"v2.11.0","cloudProviderVersion":"v1.32.3","calicoVersion":"v3.32.2",
        "controllerImage":"example.azurecr.io/controller@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "foundationDefaultsSha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    });
    let hash = hex::encode(Sha256::digest(serde_json::to_vec(&value).unwrap()));
    value["foundationSha256"] = json!(hash);
    ConfigMap {
        metadata: kube::core::ObjectMeta {
            uid: Some("provider-config-uid".into()),
            ..Default::default()
        },
        data: Some(BTreeMap::from([(
            CONFIG_KEY.into(),
            serde_json::to_string(&value).unwrap(),
        )])),
        ..Default::default()
    }
}

fn list_path(api_version: &str, plural: &str, namespace: &str) -> String {
    let base = api_version.split_once('/').map_or_else(
        || format!("/api/{api_version}"),
        |(group, version)| format!("/apis/{group}/{version}"),
    );
    format!("{base}/namespaces/{namespace}/{plural}")
}

fn find_kind(objects: &BTreeMap<String, Value>, kind: &str) -> Option<Value> {
    objects
        .values()
        .find(|value| value["kind"] == kind)
        .map(|value| {
            json!({"apiVersion":value["apiVersion"],"kind":kind,
            "name":value["metadata"]["name"],"uid":value["metadata"]["uid"],"controller":true})
        })
}

fn management_count(tenant: &Tenant) -> usize {
    tenant
        .status
        .as_ref()
        .and_then(TenantStatus::azure)
        .and_then(|status| status.management.as_ref())
        .map_or(0, |status| status.recorded_uids().len())
}

#[tokio::test]
async fn every_write_barrier_survives_restart_and_conflict_retry() {
    let fixture = Fixture::new();
    fixture.management.respond(
        "PATCH",
        &format!("{TENANT_PATH}/status"),
        409,
        crate::creation_support::status(409, "Conflict"),
    );
    let mut previous = 0;
    for _ in 0..50 {
        fixture.step().await;
        fixture.settle();
        let current = fixture.current();
        let count = management_count(&current);
        assert!(count <= previous + 1, "{previous} -> {count}");
        previous = count;
        if current.status.as_ref().and_then(|status| status.phase) == Some(TenantPhase::Ready) {
            assert_eq!(count, 12);
            assert_eq!(
                current
                    .status
                    .as_ref()
                    .unwrap()
                    .azure()
                    .unwrap()
                    .nodes
                    .len(),
                3
            );
            let azure = current.status.as_ref().unwrap().azure().unwrap();
            assert_eq!(azure.endpoint.as_deref(), Some("10.0.0.8:6443"));
            assert_eq!(
                azure.kubeconfig.as_ref().unwrap().secret_uid,
                "kubeconfig-uid"
            );
            assert_eq!(azure.provider_resources.len(), 6);
            return;
        }
    }
    panic!("did not cross every Azure write barrier");
}

#[tokio::test]
async fn foreign_uid_or_marker_fails_closed() {
    let fixture = Fixture::new();
    for _ in 0..4 {
        fixture.step().await;
        fixture.settle();
    }
    let path = "/api/v1/namespaces/tenant-a";
    let mut namespace = fixture.management.get(path);
    namespace["metadata"]["annotations"]["lifecycle.cnpg-vcluster.capi/operation-id"] =
        json!("foreign");
    fixture.management.insert(path, namespace);
    fixture.step().await;
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(TenantPhase::OwnershipInvalid)
    );
}

#[tokio::test]
async fn readiness_requires_all_workloads_and_recovers_one_worker() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    fixture
        .workload
        .remove("/apis/apps/v1/namespaces/calico-system/deployments/calico-kube-controllers");
    fixture.step().await;
    assert_ne!(
        fixture.current().status.unwrap().phase,
        Some(TenantPhase::Ready)
    );
    fixture.seed_workload();
    fixture.until_ready().await;
}

#[test]
fn catalog_represents_twelve_writes_with_two_named_configmaps() {
    assert_eq!(
        AZURE_MANAGEMENT_RESOURCES
            .iter()
            .filter(|resource| resource.class != ResourceClass::Descendant)
            .filter(|resource| resource.kind != "Secret")
            .count(),
        11
    );
    assert_eq!(
        AZURE_MANAGEMENT_RESOURCES
            .iter()
            .filter(|resource| resource.kind == "ConfigMap")
            .count(),
        1
    );
    assert!(management::azure_by_kind("AzureMachinePool").is_some());
}

#[tokio::test]
async fn finalization_records_then_deletes_in_exact_order() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    let management = fixture
        .current()
        .status
        .as_ref()
        .unwrap()
        .azure()
        .unwrap()
        .management
        .as_ref()
        .unwrap()
        .clone();
    fixture.management.insert(
        "/apis/apps/v1/namespaces/tenant-a/replicasets/status-probe-rs",
        json!({"apiVersion":"apps/v1","kind":"ReplicaSet","metadata":{
            "name":"status-probe-rs","namespace":"tenant-a","uid":"status-probe-rs-uid",
            "resourceVersion":"1","ownerReferences":[{
                "apiVersion":"apps/v1","kind":"Deployment","name":"tenant-a-status-probe",
                "uid":management.status_probe_deployment_uid.unwrap(),"controller":true
            }]
        }}),
    );
    fixture.management.insert(
        "/api/v1/namespaces/tenant-a/pods/status-probe-pod",
        json!({"apiVersion":"v1","kind":"Pod","metadata":{
            "name":"status-probe-pod","namespace":"tenant-a","uid":"status-probe-pod-uid",
            "resourceVersion":"1","ownerReferences":[{
                "apiVersion":"apps/v1","kind":"ReplicaSet","name":"status-probe-rs",
                "uid":"status-probe-rs-uid","controller":true
            }]
        }}),
    );
    fixture.management.insert(
        "/api/v1/namespaces/tenant-a/pods/addon-pod",
        json!({"apiVersion":"v1","kind":"Pod","metadata":{
            "name":"addon-pod","namespace":"tenant-a","uid":"addon-pod-uid",
            "resourceVersion":"1","ownerReferences":[{
                "apiVersion":"batch/v1","kind":"Job","name":"tenant-a-install-addons",
                "uid":management.addon_job_uid.unwrap(),"controller":true
            }]
        }}),
    );
    fixture.mark_deleting();
    fixture.management.take_calls();

    fixture.step().await;
    let current = fixture.current();
    let status = current.status.as_ref().unwrap();
    let azure = status.azure().unwrap();
    let pool_uid = azure
        .management
        .as_ref()
        .unwrap()
        .machine_pool_uid
        .clone()
        .unwrap();
    assert_eq!(status.phase, Some(TenantPhase::Deleting));
    assert!(azure.deletion.is_some());
    assert!(
        fixture
            .management
            .take_calls()
            .iter()
            .all(|call| call.method != "DELETE")
    );

    for _ in 0..4 {
        fixture.step().await;
    }
    let first = fixture.management.take_calls();
    let machine_patches: Vec<_> = first
        .iter()
        .filter(|call| call.method == "PATCH" && call.path.contains("/machines/"))
        .collect();
    assert_eq!(machine_patches.len(), 3);
    assert!(machine_patches.iter().all(|call| {
        call.body["metadata"]["annotations"]["machine.cluster.x-k8s.io/exclude-node-draining"]
            == "true"
    }));
    let pool_delete = first
        .iter()
        .find(|call| call.method == "DELETE" && call.path.contains("/machinepools/"))
        .unwrap();
    assert_eq!(pool_delete.body["preconditions"]["uid"], pool_uid);
    assert!(pool_delete.body["preconditions"]["resourceVersion"].is_string());

    fixture.remove_kinds(&["Machine", "AzureMachinePool", "AzureMachinePoolMachine"]);
    fixture.step().await;
    let cluster_delete = fixture
        .management
        .take_calls()
        .into_iter()
        .find(|call| call.method == "DELETE" && call.path.ends_with("/clusters/tenant-a"))
        .unwrap();
    assert!(cluster_delete.body["preconditions"]["uid"].is_string());
    assert!(cluster_delete.body["preconditions"]["resourceVersion"].is_string());

    fixture.remove_kinds(&["Cluster"]);
    fixture.start_azure_cluster_deletion_without_lb();
    fixture.step().await;
    let workaround = fixture
        .management
        .take_calls()
        .into_iter()
        .find(|call| call.method == "PATCH" && call.path.ends_with("/azureclusters/tenant-a"))
        .unwrap();
    assert_eq!(
        workaround.body["spec"]["networkSpec"]["apiServerLB"]["type"],
        "Public"
    );

    fixture.remove_kinds(&[
        "AzureCluster",
        "KamajiControlPlane",
        "KubeadmConfig",
        "TenantControlPlane",
        "Certificate",
        "CertificateRequest",
        "Issuer",
        "Service",
        "Endpoints",
        "StatefulSet",
        "PersistentVolumeClaim",
        "PodDisruptionBudget",
        "Role",
        "RoleBinding",
        "ResourceGroup",
        "VirtualNetwork",
        "VirtualNetworksSubnet",
        "NatGateway",
        "PublicIPAddress",
        "Secret",
    ]);
    fixture.management.take_calls();
    for _ in 0..4 {
        fixture.step().await;
    }
    fixture.remove_kinds(&["Pod", "ReplicaSet"]);
    for _ in 0..5 {
        fixture.step().await;
        if !fixture
            .current()
            .metadata
            .finalizers
            .as_deref()
            .unwrap_or_default()
            .iter()
            .any(|value| value == tenant_controller::api::FINALIZER)
        {
            break;
        }
    }
    let calls = fixture.management.take_calls();
    let deletes: Vec<_> = calls
        .iter()
        .filter(|call| call.method == "DELETE")
        .map(|call| call.path.as_str())
        .collect();
    assert_eq!(
        deletes,
        [
            "/api/v1/namespaces/tenant-a/configmaps/tenant-a-azure-cloud-provider-values",
            "/api/v1/namespaces/tenant-a/configmaps/tenant-a-calico-values",
            "/apis/batch/v1/namespaces/tenant-a/jobs/tenant-a-install-addons",
            "/apis/apps/v1/namespaces/tenant-a/deployments/tenant-a-status-probe",
            "/apis/infrastructure.cluster.x-k8s.io/v1beta1/namespaces/tenant-a/azureclusteridentities/tenant-a-identity",
            "/api/v1/namespaces/tenant-a",
        ]
    );
    let finalizer_patch = calls
        .iter()
        .rposition(|call| call.method == "PATCH" && call.path == TENANT_PATH)
        .unwrap();
    let namespace_delete = calls
        .iter()
        .position(|call| call.method == "DELETE" && call.path == "/api/v1/namespaces/tenant-a")
        .unwrap();
    assert!(finalizer_patch > namespace_delete);
    assert!(
        !fixture
            .current()
            .metadata
            .finalizers
            .unwrap_or_default()
            .iter()
            .any(|value| value == tenant_controller::api::FINALIZER)
    );
}

#[tokio::test]
async fn finalization_blocks_foreign_residue_and_same_name_recreation() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    fixture.mark_deleting();
    fixture.step().await;
    fixture.management.insert(
        "/api/v1/namespaces/tenant-a/configmaps/foreign",
        json!({"apiVersion":"v1","kind":"ConfigMap","metadata":{
            "name":"foreign","namespace":"tenant-a","uid":"foreign-uid","resourceVersion":"1"
        }}),
    );
    fixture.step().await;
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(TenantPhase::OwnershipInvalid)
    );
    fixture
        .management
        .remove("/api/v1/namespaces/tenant-a/configmaps/foreign");
    let path = "/apis/cluster.x-k8s.io/v1beta1/namespaces/tenant-a/machinepools/tenant-a-worker";
    let mut pool = fixture.management.get(path);
    pool["metadata"]["uid"] = json!("recreated-pool");
    fixture.management.insert(path, pool);
    fixture.step().await;
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(TenantPhase::OwnershipInvalid)
    );
}

#[tokio::test]
async fn finalization_recovers_missing_identity_in_one_barrier() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    let mut tenant = fixture.current();
    let azure = tenant.status.as_mut().unwrap().azure_mut().unwrap();
    azure.management.as_mut().unwrap().machine_pool_uid = None;
    azure.kubeconfig = None;
    tenant.metadata.deletion_timestamp =
        Some(serde_json::from_value(json!("2026-09-28T00:00:00Z")).unwrap());
    fixture.management.insert(TENANT_PATH, tenant);
    fixture.management.take_calls();
    fixture.step().await;
    let current = fixture.current();
    let azure = current.status.as_ref().unwrap().azure().unwrap();
    assert!(
        azure
            .management
            .as_ref()
            .unwrap()
            .machine_pool_uid
            .is_some()
    );
    assert!(azure.kubeconfig.is_some());
    assert!(azure.deletion.is_some());
    let calls = fixture.management.take_calls();
    assert_eq!(
        calls
            .iter()
            .filter(|call| call.method == "PATCH" && call.path.ends_with("/status"))
            .count(),
        1
    );
    assert!(calls.iter().all(|call| call.method != "DELETE"));
}

#[tokio::test]
async fn finalization_never_strips_provider_finalizers() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    fixture.mark_deleting();
    fixture.step().await;
    fixture.remove_kinds(&[
        "MachinePool",
        "Machine",
        "AzureMachinePool",
        "AzureMachinePoolMachine",
        "Cluster",
    ]);
    fixture.start_azure_cluster_deletion_without_lb();
    {
        let mut state = fixture.management.0.lock().unwrap();
        let azure_cluster = state
            .objects
            .values_mut()
            .find(|value| value["kind"] == "AzureCluster")
            .unwrap();
        azure_cluster["metadata"]["finalizers"] = json!(["infrastructure.cluster.x-k8s.io"]);
        azure_cluster["spec"]["networkSpec"]["apiServerLB"] = json!({"type":"Public"});
    }
    fixture.management.take_calls();
    fixture.step().await;
    let calls = fixture.management.take_calls();
    assert!(
        calls
            .iter()
            .all(|call| { call.body.pointer("/metadata/finalizers").is_none() })
    );
    let state = fixture.management.0.lock().unwrap();
    let azure_cluster = state
        .objects
        .values()
        .find(|value| value["kind"] == "AzureCluster")
        .unwrap();
    assert_eq!(
        azure_cluster["metadata"]["finalizers"],
        json!(["infrastructure.cluster.x-k8s.io"])
    );
}

#[tokio::test]
async fn finalization_refuses_missing_recorded_provider_descendant() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    fixture
        .management
        .remove("/apis/cluster.x-k8s.io/v1beta1/namespaces/tenant-a/machines/machine-0");
    fixture.mark_deleting();
    fixture.step().await;
    assert_eq!(
        fixture.current().status.unwrap().phase,
        Some(TenantPhase::OwnershipInvalid)
    );
    assert!(
        fixture
            .management
            .calls()
            .iter()
            .all(|call| call.method != "DELETE")
    );
}
