use std::collections::BTreeMap;

use k8s_openapi::api::core::v1::ConfigMap;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tenant_controller::{
    api::{Tenant, TenantPhase, TenantProviderSpec, TenantSpec, TenantStatus, spec_hash},
    azure::{AzureConfiguration, CONFIG_KEY},
    azure_allocation::{
        self, APPROVED_SHA256_ANNOTATION, AzureAllocationCatalog, AzureAllocationDocument,
        AzureClaimIdentity, AzureNetworkSlot,
    },
    management::{self, AZURE_MANAGEMENT_RESOURCES, ResourceClass},
    reconcile::{AzureProvider, Config, ReconcileError, Reconciler},
};

use crate::creation_support::{FakeAccess, Server};

const TENANT_PATH: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha3/tenants/tenant-a";
const PROVIDER_CONFIG_PATH: &str =
    "/api/v1/namespaces/tenant-system/configmaps/tenant-azure-provider";
const ALLOCATION_CONFIG_PATH: &str =
    "/api/v1/namespaces/tenant-system/configmaps/tenant-azure-allocation";

struct Fixture {
    management: Server,
    workload: Server,
    configuration: AzureConfiguration,
    allocation: AzureAllocationCatalog,
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
                provider: TenantProviderSpec::Azure,
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
        management.allow_typed_list(
            "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases",
            "coordination.k8s.io/v1",
            "Lease",
        );
        workload.allow_typed_list("/api/v1/nodes", "v1", "Node");
        let config = provider_config();
        management.insert(PROVIDER_CONFIG_PATH, config.clone());
        let configuration = AzureConfiguration::from_config_map(&config).unwrap();
        let allocation_config = allocation_config();
        management.insert(ALLOCATION_CONFIG_PATH, allocation_config.clone());
        let allocation = AzureAllocationCatalog::from_config_map(&allocation_config).unwrap();
        Self {
            management,
            workload,
            configuration,
            allocation,
        }
    }

    fn current(&self) -> Tenant {
        serde_json::from_value(self.management.get(TENANT_PATH)).unwrap()
    }

    async fn step(&self) {
        self.try_step().await.unwrap();
    }

    async fn try_step(&self) -> Result<(), ReconcileError> {
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
                allocation: self.allocation.clone(),
                access: FakeAccess(self.workload.client()),
            },
        );
        reconciler.reconcile_name("tenant-a").await.map(|_| ())
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
            "kind":"AzureMachinePool","name":"tenant-a-worker","uid":azure_owner});
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

fn allocation_config() -> ConfigMap {
    let values = AzureAllocationDocument {
        schema: 1,
        reserved_cidrs: vec![
            "10.220.0.0/16".into(),
            "10.221.0.0/16".into(),
            "10.222.0.0/16".into(),
        ],
        slots: vec![AzureNetworkSlot {
            slot_id: "azure-01".into(),
            pod_cidr: "10.244.0.0/16".into(),
            service_cidr: "10.96.0.0/16".into(),
        }],
    };
    let raw = serde_json::to_string(&values).unwrap();
    let hash = hex::encode(Sha256::digest(
        serde_json::to_vec(&serde_json::to_value(&values).unwrap()).unwrap(),
    ));
    ConfigMap {
        metadata: kube::api::ObjectMeta {
            name: Some("tenant-azure-allocation".into()),
            namespace: Some("tenant-system".into()),
            uid: Some("catalog-uid".into()),
            annotations: Some(BTreeMap::from([(APPROVED_SHA256_ANNOTATION.into(), hash)])),
            ..Default::default()
        },
        data: Some(BTreeMap::from([("slots.json".into(), raw)])),
        ..Default::default()
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
            name: Some("tenant-azure-provider".into()),
            namespace: Some("tenant-system".into()),
            uid: Some("provider-config-uid".into()),
            resource_version: Some("1".into()),
            ..Default::default()
        },
        data: Some(BTreeMap::from([(
            CONFIG_KEY.into(),
            serde_json::to_string(&value).unwrap(),
        )])),
        ..Default::default()
    }
}

fn changed_provider_config(uid: &str) -> ConfigMap {
    let mut config = provider_config();
    let mut value: Value =
        serde_json::from_str(config.data.as_ref().unwrap().get(CONFIG_KEY).unwrap()).unwrap();
    value["workerSku"] = json!("Standard_D2s_v5");
    value.as_object_mut().unwrap().remove("foundationSha256");
    let hash = hex::encode(Sha256::digest(serde_json::to_vec(&value).unwrap()));
    value["foundationSha256"] = json!(hash);
    config.metadata.uid = Some(uid.into());
    config
        .data
        .as_mut()
        .unwrap()
        .insert(CONFIG_KEY.into(), serde_json::to_string(&value).unwrap());
    config
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
async fn allocation_is_durable_before_any_provider_write() {
    let fixture = Fixture::new();
    fixture.step().await;
    fixture.step().await;
    fixture.management.take_calls();
    fixture.step().await;
    let tenant = fixture.current();
    let allocation = tenant
        .status
        .as_ref()
        .and_then(TenantStatus::azure)
        .and_then(|status| status.network_allocation.as_ref())
        .unwrap();
    assert_eq!(allocation.slot_id, "azure-01");
    assert_eq!(allocation.catalog_uid, "catalog-uid");
    let calls = fixture.management.take_calls();
    assert!(calls.iter().any(|call| {
        call.method == "POST"
            && call.path == "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases"
    }));
    assert!(!calls.iter().any(|call| {
        call.method == "POST"
            && call.path != format!("{TENANT_PATH}/status")
            && !call.path.contains("/leases")
    }));
}

#[tokio::test]
async fn concurrent_azure_claims_never_share_the_only_slot() {
    let fixture = Fixture::new();
    let first_hash = "a".repeat(64);
    let second_hash = "b".repeat(64);
    let (first, second) = tokio::join!(
        azure_allocation::claim(
            fixture.management.client(),
            &fixture.allocation,
            AzureClaimIdentity {
                tenant_name: "tenant-a",
                tenant_uid: "uid-a",
                spec_hash: &first_hash,
            },
        ),
        azure_allocation::claim(
            fixture.management.client(),
            &fixture.allocation,
            AzureClaimIdentity {
                tenant_name: "tenant-b",
                tenant_uid: "uid-b",
                spec_hash: &second_hash,
            },
        )
    );
    assert_eq!(usize::from(first.is_ok()) + usize::from(second.is_ok()), 1);
    assert!([first, second].into_iter().any(|result| matches!(
        result,
        Err(tenant_controller::allocation::AllocationError::Exhausted)
    )));
}

#[tokio::test]
async fn released_azure_slot_retry_never_touches_successor_claim() {
    let fixture = Fixture::new();
    let first_hash = "a".repeat(64);
    let first = AzureClaimIdentity {
        tenant_name: "tenant-a",
        tenant_uid: "uid-a",
        spec_hash: &first_hash,
    };
    let first_status =
        azure_allocation::claim(fixture.management.client(), &fixture.allocation, first)
            .await
            .unwrap();
    assert!(matches!(
        azure_allocation::release(
            fixture.management.client(),
            first,
            Some(&first_status),
            true,
        )
        .await
        .unwrap(),
        tenant_controller::allocation::ReleaseDecision::Pending
    ));
    let second_hash = "b".repeat(64);
    let second = AzureClaimIdentity {
        tenant_name: "tenant-b",
        tenant_uid: "uid-b",
        spec_hash: &second_hash,
    };
    let second_status =
        azure_allocation::claim(fixture.management.client(), &fixture.allocation, second)
            .await
            .unwrap();
    assert!(matches!(
        azure_allocation::release(
            fixture.management.client(),
            first,
            Some(&first_status),
            true,
        )
        .await
        .unwrap(),
        tenant_controller::allocation::ReleaseDecision::Complete
    ));
    azure_allocation::validate_recorded(fixture.management.client(), second, &second_status)
        .await
        .unwrap();
}

#[tokio::test]
async fn deleting_tenant_recovers_claim_created_before_status() {
    let fixture = Fixture::new();
    fixture.step().await;
    fixture.step().await;
    let tenant = fixture.current();
    let uid = tenant.metadata.uid.as_deref().unwrap();
    let hash = spec_hash(&tenant.spec);
    azure_allocation::claim(
        fixture.management.client(),
        &fixture.allocation,
        AzureClaimIdentity {
            tenant_name: "tenant-a",
            tenant_uid: uid,
            spec_hash: &hash,
        },
    )
    .await
    .unwrap();
    fixture.mark_deleting();
    for _ in 0..6 {
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
    assert!(
        !fixture
            .current()
            .metadata
            .finalizers
            .as_deref()
            .unwrap_or_default()
            .iter()
            .any(|value| value == tenant_controller::api::FINALIZER)
    );
}

#[tokio::test]
async fn ready_tenant_observation_survives_invalid_current_catalog() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    let mut config = allocation_config();
    config
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .insert(APPROVED_SHA256_ANNOTATION.into(), "0".repeat(64));
    fixture.management.insert(ALLOCATION_CONFIG_PATH, config);
    fixture.step().await;
    assert_eq!(
        fixture
            .current()
            .status
            .as_ref()
            .and_then(|status| status.phase),
        Some(TenantPhase::Ready)
    );
}

#[tokio::test]
async fn invalid_current_catalog_blocks_ready_tenant_repair() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    let mut config = allocation_config();
    config
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .insert(APPROVED_SHA256_ANNOTATION.into(), "0".repeat(64));
    fixture.management.insert(ALLOCATION_CONFIG_PATH, config);
    fixture.remove_kinds(&["Job"]);
    fixture.management.take_calls();
    fixture.try_step().await.unwrap();
    assert!(!fixture.management.calls().iter().any(|call| {
        call.method == "POST" && call.path == "/apis/batch/v1/namespaces/tenant-a/jobs"
    }));
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
async fn rollout_window_config_change_blocks_before_first_mutation() {
    let fixture = Fixture::new();
    fixture.management.insert(
        PROVIDER_CONFIG_PATH,
        changed_provider_config("replacement-provider-config-uid"),
    );
    fixture.management.take_calls();
    fixture.step().await;
    assert!(fixture.current().status.is_none());
    assert!(
        fixture
            .management
            .take_calls()
            .iter()
            .all(|call| !matches!(call.method.as_str(), "PATCH" | "POST" | "PUT" | "DELETE"))
    );
}

#[tokio::test]
async fn out_of_band_config_change_blocks_ready_tenant_without_status_or_resource_writes() {
    let fixture = Fixture::new();
    fixture.until_ready().await;
    let before = fixture.current().status;
    fixture.management.insert(
        PROVIDER_CONFIG_PATH,
        changed_provider_config("provider-config-uid"),
    );
    fixture.management.take_calls();
    fixture.step().await;
    assert_eq!(fixture.current().status, before);
    assert!(
        fixture
            .management
            .take_calls()
            .iter()
            .all(|call| !matches!(call.method.as_str(), "PATCH" | "POST" | "PUT" | "DELETE"))
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
    let mut invalid_catalog = allocation_config();
    invalid_catalog
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .insert(APPROVED_SHA256_ANNOTATION.into(), "0".repeat(64));
    fixture
        .management
        .insert(ALLOCATION_CONFIG_PATH, invalid_catalog);
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
    fixture.step().await;
    let config_map_deletes: Vec<_> = fixture
        .management
        .calls()
        .into_iter()
        .filter(|call| call.method == "DELETE" && call.path.contains("/configmaps/"))
        .collect();
    assert_eq!(config_map_deletes.len(), 2);
    for _ in 0..3 {
        fixture.step().await;
    }
    fixture.remove_kinds(&["Pod", "ReplicaSet"]);
    for _ in 0..9 {
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
            "/apis/coordination.k8s.io/v1/namespaces/tenant-system/leases/tenant-azure-slot-7b50dafae5a3a57dff1f006158a88bbffd0ac92301b48",
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
async fn finalization_rejects_explicit_desired_spec_drift_before_record_or_delete() {
    let cases = [
        (
            "AzureCluster",
            "/spec/networkSpec/vnet/resourceGroup",
            json!("foreign-network-group"),
        ),
        (
            "AzureCluster",
            "/spec/resourceGroup",
            json!("foreign-foundation-group"),
        ),
        ("MachinePool", "/spec/replicas", json!(4)),
        (
            "MachinePool",
            "/spec/template/spec/infrastructureRef/name",
            json!("foreign-pool"),
        ),
        (
            "AzureMachinePool",
            "/spec/template/vmSize",
            json!("Standard_D8s_v5"),
        ),
    ];
    for (kind, pointer, replacement) in cases {
        let fixture = Fixture::new();
        fixture.until_ready().await;
        {
            let mut state = fixture.management.0.lock().unwrap();
            let object = state
                .objects
                .values_mut()
                .find(|value| value["kind"] == kind)
                .unwrap();
            *object.pointer_mut(pointer).unwrap() = replacement;
        }
        fixture.mark_deleting();
        fixture.management.take_calls();
        fixture.step().await;
        let tenant = fixture.current();
        let status = tenant.status.unwrap();
        assert_eq!(
            status.phase,
            Some(TenantPhase::OwnershipInvalid),
            "{kind} {pointer}"
        );
        assert!(
            status.azure().unwrap().deletion.is_none(),
            "{kind} {pointer}"
        );
        assert!(
            fixture
                .management
                .take_calls()
                .iter()
                .all(|call| call.method != "DELETE"),
            "{kind} {pointer}"
        );
    }
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
