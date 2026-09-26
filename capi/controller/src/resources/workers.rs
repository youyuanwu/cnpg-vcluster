use kube::core::DynamicObject;
use serde_json::json;
use std::collections::BTreeMap;

use super::Context;
use crate::ownership::{
    CLUSTER_API_VERSION, FOUNDATION_ANNOTATION, SPEC_HASH_ANNOTATION, TENANT_UID_ANNOTATION,
};

pub fn kubeadm_config_template(context: &Context<'_>) -> DynamicObject {
    let mut spec = json!({"template":{"spec":{"joinConfiguration":{"nodeRegistration":{"kubeletExtraArgs":[{
        "name":"eviction-hard","value":"nodefs.available<0%,nodefs.inodesFree<0%,imagefs.available<0%"
    }]}}}}});
    if !context.worker_bootstrap_commands.is_empty() {
        spec["template"]["spec"]["preKubeadmCommands"] = json!(context.worker_bootstrap_commands);
    }
    context.object(
        "bootstrap.cluster.x-k8s.io/v1beta2",
        "KubeadmConfigTemplate",
        &format!("{}-worker", context.name()),
        context.name(),
        "kubeadm-config-template",
        spec,
    )
}

pub fn dev_machine_template(context: &Context<'_>) -> DynamicObject {
    context.object("infrastructure.cluster.x-k8s.io/v1beta2", "DevMachineTemplate",
        &format!("{}-worker",context.name()), context.name(), "dev-machine-template", json!({
            "template":{"spec":{"backend":{"docker":{
                "customImage":context.inputs.node_image,"bootstrapTimeout":"5m",
                "extraMounts":[
                    {"hostPath":context.inputs.cache_host_path,"containerPath":context.inputs.cache_container_path,"readOnly":true},
                    {"hostPath":context.volume_path,"containerPath":context.inputs.storage_container_path,"readOnly":false}
                ]
            }}}}
        }))
}

pub fn machine_deployment(context: &Context<'_>) -> DynamicObject {
    let name = format!("{}-worker", context.name());
    let mut labels = context.identity().labels();
    labels.insert(
        "cluster.x-k8s.io/cluster-name".into(),
        context.name().into(),
    );
    labels.insert("cnpg-vcluster.capi/nodepool".into(), "worker".into());
    context.object(CLUSTER_API_VERSION, "MachineDeployment", &name, context.name(), "machine-deployment", json!({
        "clusterName":context.name(),"replicas":context.spec.workers,
        "machineNaming":{"template":"{{ .cluster.name }}-worker-{{ .random }}"},
        "selector":{"matchLabels":{"cluster.x-k8s.io/cluster-name":context.name(),"cnpg-vcluster.capi/nodepool":"worker"}},
        "template":{
            "metadata":{"labels":labels,"annotations":context.identity().annotations("machine")},
            "spec":{
                "clusterName":context.name(),"version":format!("v{}",context.spec.kubernetes_version),
                "bootstrap":{"configRef":{"apiGroup":"bootstrap.cluster.x-k8s.io","kind":"KubeadmConfigTemplate","name":name}},
                "infrastructureRef":{"apiGroup":"infrastructure.cluster.x-k8s.io","kind":"DevMachineTemplate","name":name}
            }
        }
    }))
}

pub fn storage_volume_name(context: &Context<'_>) -> String {
    format!("{}-{}-storage", context.inputs.lab_prefix, context.name())
}

pub fn storage_volume_labels(context: &Context<'_>) -> BTreeMap<String, String> {
    let mut labels = context.identity().labels();
    for (key, value) in [
        ("cnpg-vcluster.capi/role", "tenant-storage"),
        ("cnpg-vcluster.capi/tenant", context.name()),
        (TENANT_UID_ANNOTATION, context.identity().tenant_uid),
        (SPEC_HASH_ANNOTATION, context.spec_hash),
        (FOUNDATION_ANNOTATION, context.foundation_hash),
    ] {
        labels.insert(key.into(), value.into());
    }
    labels
}
