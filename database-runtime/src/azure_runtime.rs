use k8s_openapi::{api::storage::v1::StorageClass, apimachinery::pkg::apis::meta::v1::ObjectMeta};
use kube::{
    Api, Client, ResourceExt,
    api::PostParams,
    core::{ApiResource, DynamicObject, GroupVersionKind},
};
use serde_json::Value;

pub const CNPG_VERSION: &str = "1.30.0";
pub const AZURE_DISK_VERSION: &str = "v1.32.12";
pub const CNPG_IMAGE: &str = "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0@sha256:a2701eb97cdd2a34b1fdb2cb51987f544b706e40bec72ae7146cd8580efefebb";
pub const AZURE_DISK_IMAGE: &str = "mcr.microsoft.com/oss/v2/kubernetes-csi/azuredisk-csi:v1.32.12@sha256:96ed94bea5da1fc6bc1e9a75f8a666c467e95a4fe06079a7bbacf469cf6a4cbd";
pub const STORAGE_CLASS: &str = "cnpg-azure-disk";
const DEFAULT_CLASS: &str = "storageclass.kubernetes.io/is-default-class";

fn expected_class() -> StorageClass {
    StorageClass {
        metadata: ObjectMeta {
            name: Some(STORAGE_CLASS.into()),
            annotations: Some([(DEFAULT_CLASS.into(), "false".into())].into()),
            ..Default::default()
        },
        provisioner: "disk.csi.azure.com".into(),
        reclaim_policy: Some("Retain".into()),
        volume_binding_mode: Some("WaitForFirstConsumer".into()),
        ..Default::default()
    }
}

pub fn valid_class(class: &StorageClass) -> bool {
    let expected = expected_class();
    class.metadata.deletion_timestamp.is_none()
        && class
            .metadata
            .uid
            .as_deref()
            .is_some_and(|uid| !uid.is_empty())
        && class.metadata.name == expected.metadata.name
        && class.metadata.annotations == expected.metadata.annotations
        && class.provisioner == expected.provisioner
        && class.reclaim_policy == expected.reclaim_policy
        && class.volume_binding_mode == expected.volume_binding_mode
        && class.allow_volume_expansion.is_none_or(|enabled| !enabled)
        && class
            .parameters
            .as_ref()
            .is_none_or(|parameters| parameters.is_empty())
        && class
            .mount_options
            .as_ref()
            .is_none_or(|options| options.is_empty())
        && class
            .allowed_topologies
            .as_ref()
            .is_none_or(|topologies| topologies.is_empty())
}

pub async fn ensure_class(client: Client) -> Result<bool, kube::Error> {
    let classes: Api<StorageClass> = Api::all(client);
    match classes.get_opt(STORAGE_CLASS).await? {
        Some(class) => Ok(valid_class(&class)),
        None => {
            classes
                .create(&PostParams::default(), &expected_class())
                .await?;
            Ok(false)
        }
    }
}

fn workload_ready(object: &DynamicObject, container_name: &str, image: &str) -> bool {
    if object.metadata.deletion_timestamp.is_some() {
        return false;
    }
    let generation = object.metadata.generation.unwrap_or_default();
    let desired = object
        .data
        .pointer("/spec/replicas")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    let daemon = object
        .data
        .pointer("/status/desiredNumberScheduled")
        .and_then(Value::as_i64);
    let count = daemon.unwrap_or(desired);
    count > 0
        && object
            .data
            .pointer("/status/observedGeneration")
            .and_then(Value::as_i64)
            .is_some_and(|value| value >= generation)
        && object
            .data
            .pointer(if daemon.is_some() {
                "/status/numberReady"
            } else {
                "/status/availableReplicas"
            })
            .and_then(Value::as_i64)
            == Some(count)
        && object
            .data
            .pointer(if daemon.is_some() {
                "/status/updatedNumberScheduled"
            } else {
                "/status/updatedReplicas"
            })
            .and_then(Value::as_i64)
            == Some(count)
        && object
            .data
            .pointer("/spec/template/spec/containers")
            .and_then(Value::as_array)
            .is_some_and(|containers| {
                containers.iter().any(|container| {
                    container["name"] == container_name && container["image"] == image
                })
            })
}

async fn workload(
    client: Client,
    namespace: &str,
    name: &str,
    kind: &str,
    container_name: &str,
    image: &str,
) -> Result<bool, kube::Error> {
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk("apps", "v1", kind));
    resource.plural = match kind {
        "DaemonSet" => "daemonsets",
        _ => "deployments",
    }
    .into();
    let api: Api<DynamicObject> = Api::namespaced_with(client, namespace, &resource);
    Ok(api
        .get_opt(name)
        .await?
        .as_ref()
        .is_some_and(|object| workload_ready(object, container_name, image)))
}

pub async fn observe(client: Client) -> Result<&'static str, kube::Error> {
    let operator = workload(
        client.clone(),
        "cnpg-system",
        "cnpg-cloudnative-pg",
        "Deployment",
        "manager",
        CNPG_IMAGE,
    )
    .await;
    let controller = workload(
        client.clone(),
        "kube-system",
        "csi-azuredisk-controller",
        "Deployment",
        "azuredisk",
        AZURE_DISK_IMAGE,
    )
    .await;
    let node = workload(
        client.clone(),
        "kube-system",
        "csi-azuredisk-node",
        "DaemonSet",
        "azuredisk",
        AZURE_DISK_IMAGE,
    )
    .await;
    let disk_reason = match (controller, node) {
        (Ok(true), Ok(true)) => observe_disk(client).await,
        (Err(error), _) | (_, Err(error)) => Err(error),
        _ => Ok("DiskCSINotReady"),
    };
    if !operator? {
        return Ok("OperatorNotReady");
    }
    disk_reason
}

async fn observe_disk(client: Client) -> Result<&'static str, kube::Error> {
    let mut resource =
        ApiResource::from_gvk(&GroupVersionKind::gvk("storage.k8s.io", "v1", "CSIDriver"));
    resource.plural = "csidrivers".into();
    let drivers: Api<DynamicObject> = Api::all_with(client.clone(), &resource);
    if drivers
        .get_opt("disk.csi.azure.com")
        .await?
        .as_ref()
        .is_none_or(|driver| driver.uid().is_none() || driver.metadata.deletion_timestamp.is_some())
    {
        return Ok("DiskCSINotReady");
    }
    if !ensure_class(client).await? {
        return Ok("StoragePolicyNotReady");
    }
    Ok("Ready")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn storage_policy_rejects_drift_and_recovers() {
        let mut class = expected_class();
        let missing: Option<&StorageClass> = None;
        assert!(!missing.is_some_and(valid_class));
        class.metadata.uid = Some("class-uid".into());
        assert!(valid_class(&class));
        class.provisioner = "kubernetes.io/azure-disk".into();
        assert!(!valid_class(&class));
        class = expected_class();
        class.metadata.uid = Some("class-uid".into());
        class.reclaim_policy = Some("Delete".into());
        assert!(!valid_class(&class));
        class.reclaim_policy = Some("Retain".into());
        class.volume_binding_mode = Some("Immediate".into());
        assert!(!valid_class(&class));
        class.volume_binding_mode = Some("WaitForFirstConsumer".into());
        assert!(valid_class(&class));
        class
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .insert(DEFAULT_CLASS.into(), "true".into());
        assert!(!valid_class(&class));
    }

    #[test]
    fn workloads_require_current_rollout_and_pinned_image() {
        let mut object: DynamicObject = serde_json::from_value(serde_json::json!({
            "apiVersion":"apps/v1", "kind":"Deployment", "metadata":{"name":"cnpg-cloudnative-pg","generation":2},
            "spec":{"replicas":1,"template":{"spec":{"containers":[
                {"name":"manager","image":"ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0"},
                {"name":"sidecar","image":CNPG_IMAGE}
            ]}}},
            "status":{"observedGeneration":1,"availableReplicas":1,"updatedReplicas":1}
        })).unwrap();
        assert!(!workload_ready(&object, "manager", CNPG_IMAGE));
        object.data["status"]["observedGeneration"] = 2.into();
        assert!(!workload_ready(&object, "manager", CNPG_IMAGE));
        object.data["spec"]["template"]["spec"]["containers"][0]["image"] = CNPG_IMAGE.into();
        assert!(workload_ready(&object, "manager", CNPG_IMAGE));
        assert!(!workload_ready(
            &object,
            "manager",
            "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.0"
        ));
        object.data["status"]["availableReplicas"] = 0.into();
        assert!(!workload_ready(&object, "manager", CNPG_IMAGE));
    }

    #[test]
    fn disk_driver_requires_pinned_main_container_on_both_workloads() {
        for kind in ["Deployment", "DaemonSet"] {
            let daemon = kind == "DaemonSet";
            let mut object: DynamicObject = serde_json::from_value(serde_json::json!({
                "apiVersion":"apps/v1", "kind":kind, "metadata":{"generation":1},
                "spec":{"replicas":2,"template":{"spec":{"containers":[
                    {"name":"azuredisk","image":"mcr.microsoft.com/oss/v2/kubernetes-csi/azuredisk-csi:v1.32.12"},
                    {"name":"sidecar","image":AZURE_DISK_IMAGE}
                ]}}},
                "status":{"observedGeneration":1,"availableReplicas":2,"updatedReplicas":2,
                    "desiredNumberScheduled":2,"numberReady":2,"updatedNumberScheduled":2}
            })).unwrap();
            if !daemon {
                object.data["status"]
                    .as_object_mut()
                    .unwrap()
                    .remove("desiredNumberScheduled");
            }
            assert!(!workload_ready(&object, "azuredisk", AZURE_DISK_IMAGE));
            object.data["spec"]["template"]["spec"]["containers"][0]["image"] =
                AZURE_DISK_IMAGE.into();
            assert!(workload_ready(&object, "azuredisk", AZURE_DISK_IMAGE));
            object.data["status"][if daemon {
                "numberReady"
            } else {
                "availableReplicas"
            }] = 1.into();
            assert!(!workload_ready(&object, "azuredisk", AZURE_DISK_IMAGE));
        }
    }
}
