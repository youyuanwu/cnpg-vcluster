use k8s_openapi::api::core::v1::Namespace;
use kube::core::DynamicObject;
use serde_json::json;

use super::{BuildError, Context, dns_service_ip, endpoint, split_image};
use crate::ownership::{CLUSTER_API_VERSION, CONTROL_PLANE_API_VERSION};

pub fn namespace(context: &Context<'_>) -> Namespace {
    Namespace {
        metadata: context.metadata(context.name(), "", "namespace"),
        ..Default::default()
    }
}

pub fn cluster(context: &Context<'_>) -> Result<DynamicObject, BuildError> {
    let (host, port) = endpoint(context.endpoint)?;
    let mut object = context.object(CLUSTER_API_VERSION, "Cluster", context.name(), context.name(), "cluster", json!({
        "controlPlaneEndpoint":{"host":host,"port":port},
        "clusterNetwork":{
            "apiServerPort":port,
            "services":{"cidrBlocks":[context.service_cidr]},
            "pods":{"cidrBlocks":[context.pod_cidr]},
            "serviceDomain":context.inputs.cluster_domain
        },
        "infrastructureRef":{"apiGroup":"infrastructure.cluster.x-k8s.io","kind":"DevCluster","name":context.name()},
        "controlPlaneRef":{"apiGroup":"controlplane.cluster.x-k8s.io","kind":"KamajiControlPlane","name":context.name()}
    }));
    object
        .metadata
        .labels
        .get_or_insert_default()
        .insert("cnpg-vcluster.capi/addons".into(), context.name().into());
    Ok(object)
}

pub fn dev_cluster(context: &Context<'_>) -> Result<DynamicObject, BuildError> {
    let (host, port) = endpoint(context.endpoint)?;
    Ok(context.object(
        "infrastructure.cluster.x-k8s.io/v1beta2",
        "DevCluster",
        context.name(),
        context.name(),
        "dev-cluster",
        json!({
            "controlPlaneEndpoint":{"host":host,"port":port},
            "backend":{"docker":{"loadBalancer":{}}}
        }),
    ))
}

pub fn kamaji_control_plane(context: &Context<'_>) -> Result<DynamicObject, BuildError> {
    let (host, _) = endpoint(context.endpoint)?;
    let dns = dns_service_ip(context.service_cidr)?;
    let (server, server_version) = split_image(&context.inputs.konnectivity_server_image)?;
    let (agent, agent_version) = split_image(&context.inputs.konnectivity_agent_image)?;
    Ok(context.object(CONTROL_PLANE_API_VERSION, "KamajiControlPlane", context.name(), context.name(), "kamaji-control-plane", json!({
        "version":format!("v{}",context.spec.kubernetes_version),"replicas":1,"dataStoreName":"default",
        "network":{
            "serviceType":"LoadBalancer","serviceAddress":host,
            "serviceAnnotations":{"metallb.io/loadBalancerIPs":host},
            "certSANs":[host,context.name()],"dnsServiceIPs":[dns]
        },
        "addons":{
            "coreDNS":{"dnsServiceIPs":[dns]},
            "konnectivity":{
                "server":{"image":server,"version":server_version,"port":8132},
                "agent":{
                    "image":agent,"version":agent_version,"mode":"DaemonSet","hostNetwork":true,
                    "tolerations":[
                        {"key":"CriticalAddonsOnly","operator":"Exists"},
                        {"key":"node.kubernetes.io/not-ready","operator":"Exists","effect":"NoSchedule"},
                        {"key":"node.kubernetes.io/not-ready","operator":"Exists","effect":"NoExecute"}
                    ]
                }
            }
        }
    })))
}
