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
        for resource in AZURE_MANAGEMENT_RESOURCES
            .iter()
            .filter(|resource| resource.class == ResourceClass::Descendant)
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
                    value["status"] = json!({"replicas":3,"readyReplicas":3});
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
