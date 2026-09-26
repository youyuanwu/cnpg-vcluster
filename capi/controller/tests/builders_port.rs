//! Go parity: internal/resources/{resources,phase3}_test.go and
//! internal/controller/tenantclient_test.go. Golden bodies also cover fields
//! previously checked only by the live endpoint/network/storage scenarios.

use k8s_openapi::api::core::v1::Namespace;
use kube::core::DynamicObject;
use serde_json::{Value, json};
use tenant_controller::{
    api::{CanonicalSpec, Tenant, TenantSpec},
    ownership::*,
    resources::*,
};

struct Fixture {
    tenant: Tenant,
    spec: CanonicalSpec,
    inputs: Inputs,
}

impl Fixture {
    fn new() -> Self {
        let mut tenant = Tenant::new(
            "tenant-a",
            TenantSpec {
                kubernetes_version: "1.36.4".into(),
                workers: 2,
                databases: 1,
            },
        );
        tenant.metadata.uid = Some("uid-a".into());
        Self {
            tenant,
            spec: CanonicalSpec {
                kubernetes_version: "1.36.4".into(),
                workers: 2,
                databases: 1,
            },
            inputs: Inputs {
                ownership_label: "example.io/owned".into(),
                lab_prefix: "example".into(),
                api_port: 6443,
                cluster_domain: "example.local".into(),
                node_image: format!("kindest/node:v1@sha256:{}", "a".repeat(64)),
                cache_host_path: "/cache".into(),
                cache_container_path: "/var/lib/capi-image-cache".into(),
                storage_container_path: "/var/lib/storage".into(),
                konnectivity_server_image: format!(
                    "registry.k8s.io/server:v1@sha256:{}",
                    "b".repeat(64)
                ),
                konnectivity_agent_image: format!(
                    "registry.k8s.io/agent:v1@sha256:{}",
                    "c".repeat(64)
                ),
            },
        }
    }

    fn context(&self) -> Context<'_> {
        Context {
            tenant: &self.tenant,
            spec: &self.spec,
            spec_hash: "spec-hash",
            foundation_hash: "foundation-hash",
            endpoint: "172.18.255.1:6443",
            pod_cidr: "10.20.0.0/16",
            service_cidr: "10.21.0.0/16",
            volume_path: "/var/lib/docker/volumes/tenant/_data",
            worker_bootstrap_commands: &[],
            inputs: &self.inputs,
        }
    }
}

fn value(object: &impl serde::Serialize) -> Value {
    serde_json::to_value(object).unwrap()
}

fn assert_markers(object: &DynamicObject, resource: &str) {
    let meta = &object.metadata;
    assert_eq!(
        meta.labels
            .as_ref()
            .unwrap()
            .get("example.io/owned")
            .unwrap(),
        "example"
    );
    assert_eq!(
        meta.annotations,
        Some(
            [
                (TENANT_ANNOTATION, "tenant-a"),
                (TENANT_UID_ANNOTATION, "uid-a"),
                (SPEC_HASH_ANNOTATION, "spec-hash"),
                (FOUNDATION_ANNOTATION, "foundation-hash"),
                (RESOURCE_ANNOTATION, resource),
            ]
            .into_iter()
            .map(|(k, v)| (k.into(), v.into()))
            .collect()
        )
    );
    assert!(meta.owner_references.is_none());
    assert!(meta.uid.is_none());
    assert!(meta.resource_version.is_none());
    assert!(object.data.get("status").is_none());
}

#[test]
fn management_cluster_golden_and_namespace_identity() {
    let fixture = Fixture::new();
    let context = fixture.context();
    let object = cluster(&context).unwrap();
    let golden: Value =
        serde_json::from_str(include_str!("fixtures/builders_cluster.json")).unwrap();
    assert_eq!(value(&object), golden);
    assert_markers(&object, "cluster");
    let namespace: Namespace = namespace(&context);
    assert_eq!(namespace.metadata.name.as_deref(), Some("tenant-a"));
    assert!(namespace.metadata.namespace.is_none());
    assert_markers(&to_dynamic(&namespace).unwrap(), "namespace");
    assert_eq!(
        encode_documents(std::slice::from_ref(&object)).unwrap(),
        encode_documents(&[cluster(&context).unwrap()]).unwrap()
    );
}

#[test]
fn control_plane_golden_network_dns_images_and_tolerations() {
    let fixture = Fixture::new();
    let context = fixture.context();
    let dev = dev_cluster(&context).unwrap();
    assert_eq!(
        dev.types.as_ref().unwrap().api_version,
        "infrastructure.cluster.x-k8s.io/v1beta2"
    );
    assert_eq!(
        dev.data,
        json!({"spec":{
            "controlPlaneEndpoint":{"host":"172.18.255.1","port":6443},
            "backend":{"docker":{"loadBalancer":{}}}
        }})
    );
    assert_markers(&dev, "dev-cluster");
    let control = kamaji_control_plane(&context).unwrap();
    assert_eq!(
        control.types.as_ref().unwrap().api_version,
        CONTROL_PLANE_API_VERSION
    );
    assert_eq!(
        control.data,
        json!({"spec":{
            "version":"v1.36.4","replicas":1,"dataStoreName":"default",
            "network":{
                "serviceType":"LoadBalancer","serviceAddress":"172.18.255.1",
                "serviceAnnotations":{"metallb.io/loadBalancerIPs":"172.18.255.1"},
                "certSANs":["172.18.255.1","tenant-a"],"dnsServiceIPs":["10.21.0.10"]
            },
            "addons":{
                "coreDNS":{"dnsServiceIPs":["10.21.0.10"]},
                "konnectivity":{
                    "server":{"image":"registry.k8s.io/server","version":format!("v1@sha256:{}","b".repeat(64)),"port":8132},
                    "agent":{
                        "image":"registry.k8s.io/agent","version":format!("v1@sha256:{}","c".repeat(64)),
                        "mode":"DaemonSet","hostNetwork":true,
                        "tolerations":[
                            {"key":"CriticalAddonsOnly","operator":"Exists"},
                            {"key":"node.kubernetes.io/not-ready","operator":"Exists","effect":"NoSchedule"},
                            {"key":"node.kubernetes.io/not-ready","operator":"Exists","effect":"NoExecute"}
                        ]
                    }
                }
            }
        }})
    );
    assert_markers(&control, "kamaji-control-plane");
}

#[test]
fn dns_endpoint_and_image_errors_are_explicit() {
    for (cidr, expected) in [
        ("10.21.0.0/16", "10.21.0.10"),
        ("10.21.0.7/16", "10.21.0.10"),
        ("fd00::/64", "fd00::a"),
        ("0.0.0.0/0", "0.0.0.10"),
    ] {
        assert_eq!(dns_service_ip(cidr).unwrap(), expected);
    }
    for cidr in [
        "invalid",
        "10.21.0.0/30",
        "255.255.255.255/32",
        "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff/128",
    ] {
        assert!(dns_service_ip(cidr).is_err(), "{cidr}");
    }
    let mut fixture = Fixture::new();
    for bad in [
        "no-port",
        "10.0.0.1:not-a-port",
        "::1:6443",
        "10.0.0.1:0",
        "10.0.0.1:65536",
        ":6443",
    ] {
        let mut context = fixture.context();
        context.endpoint = bad;
        assert!(cluster(&context).is_err(), "{bad}");
        assert!(dev_cluster(&context).is_err(), "{bad}");
        assert!(kamaji_control_plane(&context).is_err(), "{bad}");
        assert!(endpoint_config_map(&context).is_err(), "{bad}");
    }
    let mut context = fixture.context();
    context.endpoint = "[fd00::1]:6443";
    assert_eq!(
        cluster(&context).unwrap().data["spec"]["controlPlaneEndpoint"]["host"],
        "fd00::1"
    );
    assert_eq!(
        endpoint_config_map(&context).unwrap().data.unwrap()["KUBERNETES_SERVICE_HOST"],
        "fd00::1"
    );
    for bad in [
        "image:v1",
        "image@sha256:abc",
        "registry:5000/image@sha256:abc",
        "image:@sha256:abc",
    ] {
        fixture.inputs.konnectivity_server_image = bad.into();
        assert!(kamaji_control_plane(&fixture.context()).is_err(), "{bad}");
    }
}

#[test]
fn worker_templates_golden_and_live_volume_mounts() {
    let fixture = Fixture::new();
    let mut context = fixture.context();
    let commands = vec!["first command".into(), "second command".into()];
    assert!(
        kubeadm_config_template(&context).data["spec"]["template"]["spec"]
            .get("preKubeadmCommands")
            .is_none()
    );
    context.worker_bootstrap_commands = &commands;
    let bootstrap = kubeadm_config_template(&context);
    assert_eq!(
        bootstrap.data,
        json!({"spec":{"template":{"spec":{
            "preKubeadmCommands":commands,
            "joinConfiguration":{"nodeRegistration":{"kubeletExtraArgs":[{
                "name":"eviction-hard","value":"nodefs.available<0%,nodefs.inodesFree<0%,imagefs.available<0%"
            }]}}
        }}}})
    );
    assert_markers(&bootstrap, "kubeadm-config-template");
    let machine = dev_machine_template(&context);
    assert_eq!(
        machine.data,
        json!({"spec":{"template":{"spec":{"backend":{"docker":{
            "customImage":fixture.inputs.node_image,"bootstrapTimeout":"5m",
            "extraMounts":[
                {"hostPath":"/cache","containerPath":"/var/lib/capi-image-cache","readOnly":true},
                {"hostPath":"/var/lib/docker/volumes/tenant/_data","containerPath":"/var/lib/storage","readOnly":false}
            ]
        }}}}}})
    );
    assert_markers(&machine, "dev-machine-template");
    context.volume_path = "/different/inspected/live/mount";
    assert_eq!(
        dev_machine_template(&context).data["spec"]["template"]["spec"]["backend"]["docker"]["extraMounts"]
            [1]["hostPath"],
        context.volume_path
    );
    assert_eq!(machine.metadata.name.as_deref(), Some("tenant-a-worker"));
    assert_eq!(machine.metadata.namespace.as_deref(), Some("tenant-a"));
    assert_eq!(storage_volume_name(&context), "example-tenant-a-storage");
    assert_eq!(
        value(&storage_volume_labels(&context)),
        json!({
            "example.io/owned":"example","cnpg-vcluster.capi/role":"tenant-storage",
            "cnpg-vcluster.capi/tenant":"tenant-a","tenancy.cnpg-vcluster.io/tenant-uid":"uid-a",
            "tenancy.cnpg-vcluster.io/spec-hash":"spec-hash","tenancy.cnpg-vcluster.io/foundation-hash":"foundation-hash"
        })
    );
}

#[test]
fn deployment_golden_preserves_machine_markers_references_and_replicas() {
    let fixture = Fixture::new();
    let context = fixture.context();
    let deployment = machine_deployment(&context);
    assert_eq!(deployment.metadata.name.as_deref(), Some("tenant-a-worker"));
    assert_markers(&deployment, "machine-deployment");
    assert_eq!(
        deployment.data,
        json!({"spec":{
            "clusterName":"tenant-a","replicas":2,
            "machineNaming":{"template":"{{ .cluster.name }}-worker-{{ .random }}"},
            "selector":{"matchLabels":{"cluster.x-k8s.io/cluster-name":"tenant-a","cnpg-vcluster.capi/nodepool":"worker"}},
            "template":{
                "metadata":{
                    "labels":{"example.io/owned":"example","cluster.x-k8s.io/cluster-name":"tenant-a","cnpg-vcluster.capi/nodepool":"worker"},
                    "annotations":{
                        "tenancy.cnpg-vcluster.io/tenant":"tenant-a","tenancy.cnpg-vcluster.io/tenant-uid":"uid-a",
                        "tenancy.cnpg-vcluster.io/spec-hash":"spec-hash","tenancy.cnpg-vcluster.io/foundation-hash":"foundation-hash",
                        "tenancy.cnpg-vcluster.io/resource":"machine"
                    }
                },
                "spec":{
                    "clusterName":"tenant-a","version":"v1.36.4",
                    "bootstrap":{"configRef":{"apiGroup":"bootstrap.cluster.x-k8s.io","kind":"KubeadmConfigTemplate","name":"tenant-a-worker"}},
                    "infrastructureRef":{"apiGroup":"infrastructure.cluster.x-k8s.io","kind":"DevMachineTemplate","name":"tenant-a-worker"}
                }
            }
        }})
    );
}

#[test]
fn bootstrap_rbac_golden_and_subject_order() {
    let roles = bootstrap_rbac();
    assert_eq!(roles.len(), 2);
    for (pair, (name, resource)) in roles.iter().zip([
        ("kubeadm:nodes-kubeadm-config", "kubeadm-config"),
        ("kubeadm:kubelet-config", "kubelet-config"),
    ]) {
        assert_eq!(
            value(&pair.role),
            json!({
                "apiVersion":"rbac.authorization.k8s.io/v1","kind":"Role",
                "metadata":{"name":name,"namespace":"kube-system"},
                "rules":[{"apiGroups":[""],"resources":["configmaps"],"resourceNames":[resource],"verbs":["get"]}]
            })
        );
        assert_eq!(
            value(&pair.binding),
            json!({
                "apiVersion":"rbac.authorization.k8s.io/v1","kind":"RoleBinding",
                "metadata":{"name":name,"namespace":"kube-system"},
                "roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"Role","name":name},
                "subjects":[
                    {"apiGroup":"rbac.authorization.k8s.io","kind":"Group","name":"system:bootstrappers:kubeadm:default-node-token"},
                    {"apiGroup":"rbac.authorization.k8s.io","kind":"Group","name":"system:nodes"}
                ]
            })
        );
        let expected = pair.binding.subjects.as_ref().unwrap();
        let mut subjects = expected.clone();
        subjects.reverse();
        assert!(bootstrap_subjects_match(&subjects, expected));
        subjects[0].name = "foreign".into();
        assert!(!bootstrap_subjects_match(&subjects, expected));
        subjects = expected.clone();
        subjects[0] = subjects[1].clone();
        assert!(!bootstrap_subjects_match(&subjects, expected));
    }
}

fn images() -> NetworkImages {
    NetworkImages {
        calico_cni: "calico/cni:exact".into(),
        calico_cni_tagged: "calico/cni:tag".into(),
        calico_node: "calico/node:exact".into(),
        calico_node_tagged: "calico/node:tag".into(),
        calico_controllers: "calico/controllers:exact".into(),
        calico_controllers_tagged: "calico/controllers:tag".into(),
        kube_proxy: "kube-proxy:exact".into(),
    }
}

fn find<'a>(objects: &'a [DynamicObject], kind: &str, name: &str) -> &'a DynamicObject {
    objects
        .iter()
        .find(|o| {
            o.types.as_ref().unwrap().kind == kind && o.metadata.name.as_deref() == Some(name)
        })
        .unwrap()
}

#[test]
fn network_builder_pins_images_updates_pool_merges_metadata_and_sorts() {
    let fixture = Fixture::new();
    let context = fixture.context();
    let calico = include_bytes!("fixtures/builders_calico.yaml");
    let bundle = build_network(&context, calico, &images()).unwrap();
    assert_eq!(bundle.objects.len(), 8);
    let node = find(&bundle.objects, "DaemonSet", "calico-node");
    assert_eq!(
        node.metadata.labels.as_ref().unwrap()["k8s-app"],
        "calico-node"
    );
    assert_eq!(
        node.metadata.annotations.as_ref().unwrap()["upstream.example/keep"],
        "preserved"
    );
    assert_eq!(
        node.data["spec"]["template"]["spec"]["containers"][0]["env"],
        json!([
            {"name":"OTHER","value":"preserved"},{"name":"CALICO_IPV4POOL_CIDR","value":"10.20.0.0/16"}
        ])
    );
    let encoded = encode_documents(&bundle.objects).unwrap();
    for tagged in [
        "calico/cni:tag",
        "calico/node:tag",
        "calico/controllers:tag",
    ] {
        assert!(!encoded.contains(tagged));
    }
    for (image, count) in [
        ("calico/cni:exact", 2),
        ("calico/node:exact", 2),
        ("calico/controllers:exact", 1),
    ] {
        assert_eq!(encoded.matches(image).count(), count);
    }
    for object in &bundle.objects {
        assert_eq!(
            object.metadata.annotations.as_ref().unwrap()[TENANT_UID_ANNOTATION],
            "uid-a"
        );
        assert!(object.metadata.owner_references.is_none());
    }
    let mut reversed = bundle.objects.clone();
    reversed.reverse();
    sort_objects(&mut reversed);
    assert_eq!(encode_documents(&reversed).unwrap(), encoded);
    assert_eq!(decode_manifest(encoded.as_bytes()).unwrap().len(), 8);
    let replaced = String::from_utf8(calico.to_vec()).unwrap().replace(
        "- name: OTHER\n          value: preserved",
        "- name: CALICO_IPV4POOL_CIDR\n          value: stale",
    );
    let objects = build_network(&context, replaced.as_bytes(), &images())
        .unwrap()
        .objects;
    assert_eq!(
        find(&objects, "DaemonSet", "calico-node").data["spec"]["template"]["spec"]["containers"]
            [0]["env"],
        json!([
            {"name":"CALICO_IPV4POOL_CIDR","value":"10.20.0.0/16"}
        ])
    );
}

#[test]
fn network_rejects_changed_upstream_image_counts_and_malformed_pools() {
    let fixture = Fixture::new();
    let calico = include_str!("fixtures/builders_calico.yaml");
    for image in [
        "calico/cni:tag",
        "calico/node:tag",
        "calico/controllers:tag",
    ] {
        let changed = calico.replacen(image, "unexpected", 1);
        assert!(build_network(&fixture.context(), changed.as_bytes(), &images()).is_err());
    }
    for changed in [
        calico.replacen(
            "name: calico-node\n        image",
            "name: missing\n        image",
            1,
        ),
        calico.replacen("containers:\n", "containers: invalid\n      ignored:\n", 1),
        calico.replace(
            "env:\n        - name: OTHER\n          value: preserved",
            "env: malformed",
        ),
    ] {
        assert!(build_network(&fixture.context(), changed.as_bytes(), &images()).is_err());
    }
}

#[test]
fn kube_proxy_endpoint_config_rbac_and_privileged_mount_contract() {
    let fixture = Fixture::new();
    let context = fixture.context();
    assert_eq!(
        endpoint_config_map(&context).unwrap().data.unwrap(),
        [
            ("KUBERNETES_SERVICE_HOST", "172.18.255.1"),
            ("KUBERNETES_SERVICE_PORT", "6443"),
            ("KUBERNETES_SERVICE_PORT_HTTPS", "6443"),
        ]
        .into_iter()
        .map(|(k, v)| (k.into(), v.into()))
        .collect()
    );
    let objects = kube_proxy_objects(&context, "proxy:exact").unwrap();
    assert_eq!(objects.len(), 4);
    for object in &objects {
        assert_markers(object, "kube-proxy");
    }
    let binding = find(&objects, "ClusterRoleBinding", "capi-system:node-proxier");
    assert!(binding.metadata.namespace.is_none());
    assert_eq!(
        binding.data,
        json!({
            "roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"ClusterRole","name":"system:node-proxier"},
            "subjects":[{"kind":"ServiceAccount","name":"capi-kube-proxy","namespace":"kube-system"}]
        })
    );
    let config = kube_proxy_config_map(&context).data.unwrap();
    let proxy_config: Value = serde_yaml::from_str(&config["config.conf"]).unwrap();
    assert_eq!(
        proxy_config,
        json!({
            "apiVersion":"kubeproxy.config.k8s.io/v1alpha1","kind":"KubeProxyConfiguration",
            "bindAddress":"0.0.0.0","clientConnection":{"kubeconfig":"/var/lib/kube-proxy/kubeconfig.conf"},
            "clusterCIDR":"10.20.0.0/16","conntrack":{"maxPerCore":0,"min":0},"mode":"iptables"
        })
    );
    let kubeconfig: Value = serde_yaml::from_str(&config["kubeconfig.conf"]).unwrap();
    assert_eq!(
        kubeconfig["clusters"][0]["cluster"],
        json!({
            "certificate-authority":"/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            "server":"https://172.18.255.1:6443"
        })
    );
    assert_eq!(
        kubeconfig["users"][0]["user"]["tokenFile"],
        "/var/run/secrets/kubernetes.io/serviceaccount/token"
    );
    let daemon = value(&kube_proxy_daemon_set(&context, "proxy:exact").unwrap());
    assert_eq!(
        daemon["spec"]["selector"]["matchLabels"],
        json!({"k8s-app":"capi-kube-proxy"})
    );
    let pod = &daemon["spec"]["template"]["spec"];
    assert_eq!(pod["hostNetwork"], true);
    assert_eq!(pod["priorityClassName"], "system-node-critical");
    assert_eq!(pod["serviceAccountName"], "capi-kube-proxy");
    assert_eq!(pod["tolerations"], json!([{"operator":"Exists"}]));
    assert_eq!(
        pod["containers"],
        json!([{
            "name":"kube-proxy","image":"proxy:exact",
            "command":["/usr/local/bin/kube-proxy","--config=/var/lib/kube-proxy/config.conf","--v=2"],
            "securityContext":{"privileged":true},
            "volumeMounts":[
                {"name":"kube-proxy","mountPath":"/var/lib/kube-proxy"},
                {"name":"xtables-lock","mountPath":"/run/xtables.lock"},
                {"name":"lib-modules","mountPath":"/lib/modules","readOnly":true}
            ]
        }])
    );
    assert_eq!(
        pod["volumes"],
        json!([
            {"name":"kube-proxy","configMap":{"name":"capi-kube-proxy"}},
            {"name":"xtables-lock","hostPath":{"path":"/run/xtables.lock","type":"FileOrCreate"}},
            {"name":"lib-modules","hostPath":{"path":"/lib/modules","type":"Directory"}}
        ])
    );
}

#[test]
fn storage_and_cnpg_all_count_pairs_affinity_and_exact_prebinding() {
    let mut fixture = Fixture::new();
    for workers in 1..=3 {
        for databases in 1..=3 {
            fixture.spec.workers = workers;
            fixture.spec.databases = databases;
            let context = fixture.context();
            let storage = to_dynamic(&storage_class(&context, "capi-hostpath")).unwrap();
            assert_markers(&storage, "storage");
            assert_eq!(
                storage.data,
                json!({"provisioner":"kubernetes.io/no-provisioner","volumeBindingMode":"Immediate","reclaimPolicy":"Retain"})
            );
            let objects = cnpg_objects(&context, "capi-hostpath", "postgres:exact").unwrap();
            assert_eq!(objects.len(), databases as usize + 2);
            for object in &objects {
                assert_markers(object, "cnpg");
            }
            assert_eq!(objects[0].metadata.name.as_deref(), Some("database"));
            assert_eq!(objects[0].types.as_ref().unwrap().kind, "Namespace");
            for ordinal in 1..=databases {
                let name = format!("capi-postgres-pv-{ordinal}");
                let volume = find(&objects, "PersistentVolume", &name);
                assert!(volume.metadata.namespace.is_none());
                assert_eq!(
                    volume.data,
                    json!({"spec":{
                        "capacity":{"storage":"1Gi"},"accessModes":["ReadWriteOnce"],
                        "persistentVolumeReclaimPolicy":"Retain","storageClassName":"capi-hostpath",
                        "claimRef":{"namespace":"database","name":format!("capi-postgres-{ordinal}")},
                        "hostPath":{"path":format!("/var/lib/storage/volumes/cnpg/{ordinal}"),"type":"DirectoryOrCreate"}
                    }})
                );
                assert!(volume.data["spec"].get("nodeAffinity").is_none());
            }
            let cluster = find(&objects, "Cluster", "capi-postgres");
            assert_eq!(
                cluster.types.as_ref().unwrap().api_version,
                "postgresql.cnpg.io/v1"
            );
            assert_eq!(cluster.metadata.namespace.as_deref(), Some("database"));
            assert_eq!(
                cluster.data,
                json!({"spec":{
                    "instances":databases,"imageName":"postgres:exact",
                    "affinity":{"enablePodAntiAffinity":true,"podAntiAffinityType":if databases > workers {"preferred"} else {"required"},"topologyKey":"kubernetes.io/hostname"},
                    "bootstrap":{"initdb":{"database":"app","owner":"app"}},
                    "storage":{"size":"1Gi","storageClass":"capi-hostpath"},
                    "resources":{"requests":{"cpu":"100m","memory":"256Mi"},"limits":{"cpu":"1","memory":"1Gi"}}
                }})
            );
        }
    }
}

#[test]
fn cnpg_operator_requires_exact_image_inventory_and_preserves_manifest_metadata() {
    let fixture = Fixture::new();
    let manifest = br#"apiVersion: apps/v1
kind: Deployment
metadata:
  name: cnpg-controller-manager
  namespace: cnpg-system
  labels:
    app: cnpg
spec:
  template:
    spec:
      containers:
      - name: manager
        image: cnpg:tag
        env:
        - name: OPERATOR_IMAGE_NAME
          value: cnpg:tag
"#;
    let objects = cnpg_operator(&fixture.context(), manifest, "cnpg:tag", "cnpg:exact").unwrap();
    assert_eq!(objects.len(), 1);
    assert_markers(&objects[0], "cnpg-operator");
    assert_eq!(objects[0].metadata.labels.as_ref().unwrap()["app"], "cnpg");
    assert_eq!(
        encode_documents(&objects)
            .unwrap()
            .matches("cnpg:exact")
            .count(),
        2
    );
    for changed in [
        String::from_utf8(manifest.to_vec())
            .unwrap()
            .replacen("cnpg:tag", "other", 1),
        String::from_utf8(manifest.to_vec()).unwrap()
            + "        - name: EXTRA\n          value: cnpg:tag\n",
    ] {
        assert!(
            cnpg_operator(
                &fixture.context(),
                changed.as_bytes(),
                "cnpg:tag",
                "cnpg:exact"
            )
            .is_err()
        );
    }
}

#[test]
fn manifest_lists_empty_documents_validation_and_stable_roundtrip() {
    let text = br#"---
---
{}
---
apiVersion: v1
kind: List
items:
- apiVersion: v1
  kind: ConfigMap
  metadata: {name: a, namespace: n}
  data: {keep: "value"}
- apiVersion: v1
  kind: Namespace
  metadata: {name: n}
"#;
    let objects = decode_manifest(text).unwrap();
    assert_eq!(objects.len(), 2);
    let encoded = encode_documents(&objects).unwrap();
    assert_eq!(
        encoded,
        encode_documents(&decode_manifest(encoded.as_bytes()).unwrap()).unwrap()
    );
    for text in [
        "kind: List\nitems: [not-an-object]",
        "kind: List\nitems: {}",
        "[unterminated",
        "name: missing-kind",
        "42",
    ] {
        assert!(decode_manifest(text.as_bytes()).is_err(), "{text}");
    }
    assert!(decode_manifest(b"").unwrap().is_empty());
    assert!(encode_documents(&[]).unwrap().is_empty());
}

#[test]
fn maximum_tenant_name_produces_stable_dns_safe_names() {
    let mut fixture = Fixture::new();
    fixture.tenant.metadata.name = Some("a".repeat(30));
    let context = fixture.context();
    for object in [
        cluster(&context).unwrap(),
        dev_cluster(&context).unwrap(),
        kamaji_control_plane(&context).unwrap(),
        kubeadm_config_template(&context),
        dev_machine_template(&context),
        machine_deployment(&context),
    ] {
        let name = object.metadata.name.unwrap();
        assert!(
            name.len() <= 63
                && name
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte == b'-')
        );
        assert_eq!(object.metadata.namespace.as_deref(), Some(context.name()));
    }
}
