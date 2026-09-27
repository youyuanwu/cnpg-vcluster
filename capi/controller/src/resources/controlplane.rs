use k8s_openapi::api::core::v1::Namespace;
use kube::core::DynamicObject;
use serde_json::json;

use super::{BuildError, Context, dns_service_ip, endpoint, split_image};

pub fn namespace(context: &Context<'_>) -> Namespace {
    Namespace {
        metadata: context.metadata(context.name(), "", "namespace"),
        ..Default::default()
    }
}

pub fn cluster(context: &Context<'_>) -> Result<DynamicObject, BuildError> {
    let (host, port) = endpoint(context.endpoint)?;
    let mut object = context.management_object(
        "Cluster",
        json!({
            "controlPlaneEndpoint":{"host":host,"port":port},
            "clusterNetwork":{
                "apiServerPort":port,
                "services":{"cidrBlocks":[context.service_cidr]},
                "pods":{"cidrBlocks":[context.pod_cidr]},
                "serviceDomain":context.inputs.cluster_domain
            },
            "infrastructureRef":context.management_ref("DevCluster"),
            "controlPlaneRef":context.management_ref("KamajiControlPlane")
        }),
    );
    object
        .metadata
        .labels
        .get_or_insert_default()
        .insert("cnpg-vcluster.capi/addons".into(), context.name().into());
    Ok(object)
}

pub fn dev_cluster(context: &Context<'_>) -> Result<DynamicObject, BuildError> {
    let (host, port) = endpoint(context.endpoint)?;
    Ok(context.management_object(
        "DevCluster",
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
    Ok(context.management_object("KamajiControlPlane", json!({
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
