use std::collections::BTreeMap;

use k8s_openapi::api::{
    apps::v1::DaemonSet,
    core::v1::{ConfigMap, ServiceAccount},
    rbac::v1::{ClusterRoleBinding, RoleRef, Subject},
};
use kube::core::DynamicObject;
use serde_json::{Value, json};

use super::{
    BuildError, Context, decode_manifest, endpoint, manifest::replace_object_strings,
    mark_tenant_object, sort_objects, to_dynamic,
};

#[derive(Clone, Debug)]
pub struct NetworkImages {
    pub calico_cni: String,
    pub calico_cni_tagged: String,
    pub calico_node: String,
    pub calico_node_tagged: String,
    pub calico_controllers: String,
    pub calico_controllers_tagged: String,
    pub kube_proxy: String,
}

#[derive(Clone, Debug)]
pub struct NetworkBundle {
    pub objects: Vec<DynamicObject>,
}

pub fn build_network(
    context: &Context<'_>,
    calico: &[u8],
    images: &NetworkImages,
) -> Result<NetworkBundle, BuildError> {
    let mut objects = decode_manifest(calico)?;
    let mut counts = BTreeMap::new();
    let replacements = BTreeMap::from([
        (
            images.calico_cni_tagged.as_str(),
            images.calico_cni.as_str(),
        ),
        (
            images.calico_node_tagged.as_str(),
            images.calico_node.as_str(),
        ),
        (
            images.calico_controllers_tagged.as_str(),
            images.calico_controllers.as_str(),
        ),
    ]);
    for object in &mut objects {
        replace_object_strings(object, &replacements, &mut counts)?;
        if object
            .types
            .as_ref()
            .is_some_and(|types| types.kind == "DaemonSet")
            && object.metadata.name.as_deref() == Some("calico-node")
        {
            set_calico_pool(object, context.pod_cidr)?;
        }
        mark_tenant_object(context, object, "network-workload");
    }
    for (tagged, expected) in [
        (&images.calico_cni_tagged, 2),
        (&images.calico_node_tagged, 2),
        (&images.calico_controllers_tagged, 1),
    ] {
        if counts.get(tagged) != Some(&expected) {
            return Err(BuildError::Manifest(format!(
                "unexpected Calico image count for {tagged}"
            )));
        }
    }
    objects.push(to_dynamic(&endpoint_config_map(context)?)?);
    objects.extend(kube_proxy_objects(context, &images.kube_proxy)?);
    sort_objects(&mut objects);
    Ok(NetworkBundle { objects })
}

fn set_calico_pool(object: &mut DynamicObject, cidr: &str) -> Result<(), BuildError> {
    let containers = object
        .data
        .pointer_mut("/spec/template/spec/containers")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| BuildError::Manifest("calico-node containers are missing".into()))?;
    let container = containers
        .iter_mut()
        .find(|container| container.get("name").and_then(Value::as_str) == Some("calico-node"))
        .and_then(Value::as_object_mut)
        .ok_or_else(|| BuildError::Manifest("calico-node container is missing".into()))?;
    let env = container
        .entry("env")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .ok_or_else(|| BuildError::Manifest("calico-node env must be an array".into()))?;
    let mut updated = false;
    for entry in env.iter_mut() {
        let entry = entry.as_object_mut().ok_or_else(|| {
            BuildError::Manifest("calico-node env entry must be an object".into())
        })?;
        if entry.get("name").and_then(Value::as_str) == Some("CALICO_IPV4POOL_CIDR") {
            entry.insert("value".into(), json!(cidr));
            updated = true;
        }
    }
    if !updated {
        env.push(json!({"name":"CALICO_IPV4POOL_CIDR","value":cidr}));
    }
    Ok(())
}

pub fn endpoint_config_map(context: &Context<'_>) -> Result<ConfigMap, BuildError> {
    let (host, _) = endpoint(context.endpoint)?;
    Ok(ConfigMap {
        metadata: context.metadata(
            "kubernetes-services-endpoint",
            "kube-system",
            "network-endpoint",
        ),
        data: Some(BTreeMap::from([
            ("KUBERNETES_SERVICE_HOST".into(), host.into()),
            (
                "KUBERNETES_SERVICE_PORT".into(),
                context.inputs.api_port.to_string(),
            ),
            (
                "KUBERNETES_SERVICE_PORT_HTTPS".into(),
                context.inputs.api_port.to_string(),
            ),
        ])),
        ..Default::default()
    })
}

pub fn kube_proxy_config_map(context: &Context<'_>) -> ConfigMap {
    ConfigMap {
        metadata: context.metadata("capi-kube-proxy", "kube-system", "kube-proxy"),
        data: Some(BTreeMap::from([
            (
                "config.conf".into(),
                format!(
                    "apiVersion: kubeproxy.config.k8s.io/v1alpha1\nkind: KubeProxyConfiguration\nbindAddress: 0.0.0.0\nclientConnection:\n  kubeconfig: /var/lib/kube-proxy/kubeconfig.conf\nclusterCIDR: {}\nconntrack:\n  maxPerCore: 0\n  min: 0\nmode: iptables\n",
                    context.pod_cidr
                ),
            ),
            (
                "kubeconfig.conf".into(),
                format!(
                    "apiVersion: v1\nkind: Config\nclusters:\n- cluster:\n    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt\n    server: https://{}\n  name: default\ncontexts:\n- context:\n    cluster: default\n    namespace: default\n    user: default\n  name: default\ncurrent-context: default\nusers:\n- name: default\n  user:\n    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token\n",
                    context.endpoint
                ),
            ),
        ])),
        ..Default::default()
    }
}

pub fn kube_proxy_daemon_set(context: &Context<'_>, image: &str) -> Result<DaemonSet, BuildError> {
    Ok(serde_json::from_value(json!({
        "metadata":context.metadata("capi-kube-proxy","kube-system","kube-proxy"),
        "spec":{
            "selector":{"matchLabels":{"k8s-app":"capi-kube-proxy"}},
            "template":{
                "metadata":{"labels":{"k8s-app":"capi-kube-proxy"}},
                "spec":{
                    "priorityClassName":"system-node-critical","serviceAccountName":"capi-kube-proxy","hostNetwork":true,
                    "tolerations":[{"operator":"Exists"}],
                    "containers":[{
                        "name":"kube-proxy","image":image,
                        "command":["/usr/local/bin/kube-proxy","--config=/var/lib/kube-proxy/config.conf","--v=2"],
                        "securityContext":{"privileged":true},
                        "volumeMounts":[
                            {"name":"kube-proxy","mountPath":"/var/lib/kube-proxy"},
                            {"name":"xtables-lock","mountPath":"/run/xtables.lock"},
                            {"name":"lib-modules","mountPath":"/lib/modules","readOnly":true}
                        ]
                    }],
                    "volumes":[
                        {"name":"kube-proxy","configMap":{"name":"capi-kube-proxy"}},
                        {"name":"xtables-lock","hostPath":{"path":"/run/xtables.lock","type":"FileOrCreate"}},
                        {"name":"lib-modules","hostPath":{"path":"/lib/modules","type":"Directory"}}
                    ]
                }
            }
        }
    }))?)
}

pub fn kube_proxy_objects(
    context: &Context<'_>,
    image: &str,
) -> Result<Vec<DynamicObject>, BuildError> {
    let account = ServiceAccount {
        metadata: context.metadata("capi-kube-proxy", "kube-system", "kube-proxy"),
        ..Default::default()
    };
    let binding = ClusterRoleBinding {
        metadata: context.metadata("capi-system:node-proxier", "", "kube-proxy"),
        role_ref: RoleRef {
            api_group: Some("rbac.authorization.k8s.io".into()),
            kind: "ClusterRole".into(),
            name: "system:node-proxier".into(),
        },
        subjects: Some(vec![Subject {
            kind: "ServiceAccount".into(),
            name: "capi-kube-proxy".into(),
            namespace: Some("kube-system".into()),
            ..Default::default()
        }]),
    };
    Ok(vec![
        to_dynamic(&account)?,
        to_dynamic(&binding)?,
        to_dynamic(&kube_proxy_config_map(context))?,
        to_dynamic(&kube_proxy_daemon_set(context, image)?)?,
    ])
}
