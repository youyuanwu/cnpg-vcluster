use std::collections::{BTreeMap, BTreeSet};

use k8s_openapi::api::core::v1::ConfigMap;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use kube::core::{DynamicObject, TypeMeta};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::{
    api::{
        AzureBindingStatus, AzureManagementStatus, AzureProviderResourceIdentity, CanonicalSpec,
        Tenant, TenantProviderSpec,
    },
    resources::dns_service_ip,
};

pub const CONFIG_NAME: &str = "tenant-azure-provider";
pub const CONFIG_KEY: &str = "provider.json";
pub const FIELD_MANAGER: &str = "cnpg-vcluster-azure";
pub const EXTERNAL_CONTROL_PLANE_LABEL: &str = "cnpg-vcluster-external-control-plane";

const LABEL_EXPERIMENT: &str = "cnpg-vcluster-experiment";
pub const TENANT_LABEL: &str = "cnpg-vcluster-tenant";
const LABEL_PROFILE: &str = "cnpg-vcluster-profile";
pub const TENANT_ANNOTATION: &str = "lifecycle.cnpg-vcluster.capi/tenant";
const ANNOTATION_PROFILE: &str = "lifecycle.cnpg-vcluster.capi/profile";
const ANNOTATION_SPEC: &str = "lifecycle.cnpg-vcluster.capi/specification-sha256";
const ANNOTATION_FOUNDATION: &str = "lifecycle.cnpg-vcluster.capi/foundation-sha256";
const ANNOTATION_OPERATION: &str = "lifecycle.cnpg-vcluster.capi/operation-id";

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AzureProviderConfiguration {
    pub schema: u8,
    pub subscription_id: String,
    pub tenant_id: String,
    pub location: String,
    pub resource_group_name: String,
    pub resource_group_id: String,
    pub vnet_name: String,
    pub vnet_id: String,
    pub tenant_subnet_name: String,
    pub tenant_subnet_id: String,
    pub identity_name: String,
    pub identity_id: String,
    pub identity_client_id: String,
    pub supported_kubernetes_version: String,
    pub worker_sku: String,
    pub capi_version: String,
    pub capz_version: String,
    pub kamaji_capi_version: String,
    pub kamaji_chart_version: String,
    pub aso_version: String,
    pub cloud_provider_version: String,
    pub calico_version: String,
    pub controller_image: String,
    pub foundation_defaults_sha256: String,
    pub foundation_sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AzureConfiguration {
    pub values: AzureProviderConfiguration,
    pub config_map_uid: String,
    pub sha256: String,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum AzureConfigurationError {
    #[error("Azure provider ConfigMap provider.json is missing")]
    Missing,
    #[error("Azure provider ConfigMap provider.json is invalid: {0}")]
    Json(String),
    #[error("Azure provider ConfigMap UID is missing")]
    Uid,
    #[error("Azure provider configuration schema must be 1")]
    Schema,
    #[error("Azure provider configuration field {0} is invalid")]
    Field(&'static str),
    #[error("Azure provider foundation hash does not match canonical configuration")]
    FoundationHash,
}

impl AzureConfiguration {
    pub fn from_config_map(config: &ConfigMap) -> Result<Self, AzureConfigurationError> {
        let raw = config
            .data
            .as_ref()
            .and_then(|data| data.get(CONFIG_KEY))
            .ok_or(AzureConfigurationError::Missing)?;
        let values: AzureProviderConfiguration = serde_json::from_str(raw)
            .map_err(|error| AzureConfigurationError::Json(error.to_string()))?;
        let config_map_uid = config
            .metadata
            .uid
            .clone()
            .filter(|value| !value.is_empty())
            .ok_or(AzureConfigurationError::Uid)?;
        values.validate()?;
        let canonical = serde_json::to_vec(&values)
            .map_err(|error| AzureConfigurationError::Json(error.to_string()))?;
        Ok(Self {
            values,
            config_map_uid,
            sha256: hex::encode(Sha256::digest(canonical)),
        })
    }

    pub fn binding(
        &self,
        tenant_uid: impl Into<String>,
        specification_sha256: impl Into<String>,
        operation_id: impl Into<String>,
    ) -> AzureBindingStatus {
        AzureBindingStatus {
            tenant_uid: tenant_uid.into(),
            specification_sha256: specification_sha256.into(),
            provider_config_uid: self.config_map_uid.clone(),
            provider_config_sha256: self.sha256.clone(),
            foundation_sha256: self.values.foundation_sha256.clone(),
            foundation_defaults_sha256: self.values.foundation_defaults_sha256.clone(),
            controller_image: self.values.controller_image.clone(),
            resource_group_id: self.values.resource_group_id.clone(),
            virtual_network_id: self.values.vnet_id.clone(),
            tenant_subnet_id: self.values.tenant_subnet_id.clone(),
            identity_id: self.values.identity_id.clone(),
            operation_id: operation_id.into(),
        }
    }
}

impl AzureProviderConfiguration {
    fn validate(&self) -> Result<(), AzureConfigurationError> {
        if self.schema != 1 {
            return Err(AzureConfigurationError::Schema);
        }
        for (name, value) in [
            ("subscriptionId", self.subscription_id.as_str()),
            ("tenantId", self.tenant_id.as_str()),
            ("location", self.location.as_str()),
            ("resourceGroupName", self.resource_group_name.as_str()),
            ("resourceGroupId", self.resource_group_id.as_str()),
            ("vnetName", self.vnet_name.as_str()),
            ("vnetId", self.vnet_id.as_str()),
            ("tenantSubnetName", self.tenant_subnet_name.as_str()),
            ("tenantSubnetId", self.tenant_subnet_id.as_str()),
            ("identityName", self.identity_name.as_str()),
            ("identityId", self.identity_id.as_str()),
            ("identityClientId", self.identity_client_id.as_str()),
            (
                "supportedKubernetesVersion",
                self.supported_kubernetes_version.as_str(),
            ),
            ("workerSku", self.worker_sku.as_str()),
            ("capiVersion", self.capi_version.as_str()),
            ("capzVersion", self.capz_version.as_str()),
            ("kamajiCapiVersion", self.kamaji_capi_version.as_str()),
            ("kamajiChartVersion", self.kamaji_chart_version.as_str()),
            ("asoVersion", self.aso_version.as_str()),
            ("cloudProviderVersion", self.cloud_provider_version.as_str()),
            ("calicoVersion", self.calico_version.as_str()),
            ("controllerImage", self.controller_image.as_str()),
            (
                "foundationDefaultsSha256",
                self.foundation_defaults_sha256.as_str(),
            ),
            ("foundationSha256", self.foundation_sha256.as_str()),
        ] {
            if value.is_empty() || value.trim() != value {
                return Err(AzureConfigurationError::Field(name));
            }
        }
        if !three_part_version(&self.supported_kubernetes_version) {
            return Err(AzureConfigurationError::Field("supportedKubernetesVersion"));
        }
        for (name, value) in [
            (
                "foundationDefaultsSha256",
                self.foundation_defaults_sha256.as_str(),
            ),
            ("foundationSha256", self.foundation_sha256.as_str()),
        ] {
            if !sha256(value) {
                return Err(AzureConfigurationError::Field(name));
            }
        }
        if !self.controller_image.contains("@sha256:")
            || !self.resource_group_id.starts_with("/subscriptions/")
            || !self.vnet_id.starts_with("/subscriptions/")
            || !self.tenant_subnet_id.starts_with("/subscriptions/")
            || !self.identity_id.starts_with("/subscriptions/")
        {
            return Err(AzureConfigurationError::Field("Azure resource identity"));
        }
        let mut value = serde_json::to_value(self)
            .map_err(|error| AzureConfigurationError::Json(error.to_string()))?;
        value
            .as_object_mut()
            .expect("configuration serializes as an object")
            .remove("foundationSha256");
        let hash = hex::encode(Sha256::digest(
            serde_json::to_vec(&value)
                .map_err(|error| AzureConfigurationError::Json(error.to_string()))?,
        ));
        if hash != self.foundation_sha256 {
            return Err(AzureConfigurationError::FoundationHash);
        }
        Ok(())
    }
}

fn three_part_version(value: &str) -> bool {
    value
        .strip_prefix('v')
        .unwrap_or(value)
        .split('.')
        .collect::<Vec<_>>()
        .as_slice()
        .iter()
        .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
        && value.strip_prefix('v').unwrap_or(value).split('.').count() == 3
}

fn sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AzureNames {
    pub namespace: String,
    pub identity: String,
    pub cluster: String,
    pub azure_cluster: String,
    pub control_plane: String,
    pub pool: String,
    pub cloud_values: String,
    pub network_values: String,
    pub status_probe: String,
    pub addon_job: String,
    pub kubeconfig: String,
}

impl AzureNames {
    pub fn new(tenant: &str) -> Self {
        Self {
            namespace: tenant.into(),
            identity: format!("{tenant}-identity"),
            cluster: tenant.into(),
            azure_cluster: tenant.into(),
            control_plane: tenant.into(),
            pool: format!("{tenant}-worker"),
            cloud_values: format!("{tenant}-azure-cloud-provider-values"),
            network_values: format!("{tenant}-calico-values"),
            status_probe: format!("{tenant}-status-probe"),
            addon_job: format!("{tenant}-install-addons"),
            kubeconfig: format!("{tenant}-kubeconfig"),
        }
    }
}

pub struct AzureContext<'a> {
    pub tenant: &'a Tenant,
    pub spec: &'a CanonicalSpec,
    pub specification_sha256: &'a str,
    pub foundation_sha256: &'a str,
    pub operation_id: &'a str,
    pub configuration: &'a AzureProviderConfiguration,
}

impl AzureContext<'_> {
    pub fn name(&self) -> &str {
        self.tenant.metadata.name.as_deref().unwrap_or("")
    }

    pub fn names(&self) -> AzureNames {
        AzureNames::new(self.name())
    }

    fn labels(&self) -> BTreeMap<String, String> {
        [
            (LABEL_EXPERIMENT, "azure-capi"),
            (TENANT_LABEL, self.name()),
            (LABEL_PROFILE, "azure"),
        ]
        .into_iter()
        .map(|(key, value)| (key.into(), value.into()))
        .collect()
    }

    fn annotations(&self) -> BTreeMap<String, String> {
        [
            (TENANT_ANNOTATION, self.name()),
            (ANNOTATION_PROFILE, "azure"),
            (ANNOTATION_SPEC, self.specification_sha256),
            (ANNOTATION_FOUNDATION, self.foundation_sha256),
            (ANNOTATION_OPERATION, self.operation_id),
        ]
        .into_iter()
        .map(|(key, value)| (key.into(), value.into()))
        .collect()
    }

    fn metadata(&self, name: &str, namespace: Option<&str>) -> ObjectMeta {
        ObjectMeta {
            name: Some(name.into()),
            namespace: namespace.map(Into::into),
            labels: Some(self.labels()),
            annotations: Some(self.annotations()),
            ..Default::default()
        }
    }

    fn object(
        &self,
        api_version: &str,
        kind: &str,
        name: &str,
        namespace: Option<&str>,
        data: Value,
    ) -> DynamicObject {
        DynamicObject {
            types: Some(TypeMeta {
                api_version: api_version.into(),
                kind: kind.into(),
            }),
            metadata: self.metadata(name, namespace),
            data,
        }
    }

    fn template_metadata(&self, labels: BTreeMap<String, String>) -> Value {
        json!({"labels":labels,"annotations":self.annotations()})
    }

    fn azure_tags(&self) -> Value {
        json!({
            "cnpg-vcluster-tenant":self.name(),
            "cnpg-vcluster-profile":"azure",
            "cnpg-vcluster-spec-sha256":self.specification_sha256,
            "cnpg-vcluster-foundation-sha256":self.foundation_sha256,
            "cnpg-vcluster-operation-id":self.operation_id,
        })
    }
}

pub fn desired_objects(context: &AzureContext<'_>) -> Result<Vec<DynamicObject>, AzureBuildError> {
    let TenantProviderSpec::Azure {
        pod_cidr,
        service_cidr,
    } = &context.spec.provider
    else {
        return Err(AzureBuildError::Provider);
    };
    let names = context.names();
    let namespace = names.namespace.as_str();
    let config = context.configuration;
    let dns =
        dns_service_ip(service_cidr).map_err(|error| AzureBuildError::Dns(error.to_string()))?;
    let provider_id = format!(
        "azure:///subscriptions/{}/resourceGroups/{}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{}",
        config.subscription_id, config.resource_group_name, config.identity_name
    );
    let common_template_labels = context.labels();
    let cloud_values = json!({
        "cloudControllerManager":{
            "allocateNodeCidrs":"false",
            "clusterCIDR":pod_cidr,
            "configureCloudRoutes":"false",
            "nodeSelector":Value::Null,
            "replicas":1,
            "tolerations":[{"operator":"Exists"}]
        },
        "cloudNodeManager":{"cloudConfig":"/etc/kubernetes/azure.json"},
        "infra":{"clusterName":context.name()}
    });
    let calico_values = json!({
        "installation":{
            "calicoNetwork":{
                "bgp":"Disabled",
                "ipPools":[{"cidr":pod_cidr,"encapsulation":"VXLAN"}],
                "mtu":1350
            },
            "cni":{"ipam":{"type":"Calico"},"type":"Calico"}
        },
        "serviceCIDRs":[service_cidr],
        "tolerations":[{"operator":"Exists"}]
    });
    let install = format!(
        "helm repo add cloud-provider-azure https://raw.githubusercontent.com/kubernetes-sigs/cloud-provider-azure/master/helm/repo\n\
helm repo add projectcalico https://docs.tigera.io/calico/charts\n\
helm upgrade --install cloud-provider-azure cloud-provider-azure/cloud-provider-azure --kubeconfig /tenant/value --version {} --namespace kube-system --values /values/cloud-provider.yaml --wait --timeout 10m\n\
helm upgrade --install calico-crds projectcalico/crd.projectcalico.org.v1 --kubeconfig /tenant/value --version {} --namespace tigera-operator --create-namespace --wait --timeout 5m\n\
helm upgrade --install calico projectcalico/tigera-operator --kubeconfig /tenant/value --version {} --namespace tigera-operator --create-namespace --values /values/calico.yaml --wait --timeout 10m",
        config.cloud_provider_version.trim_start_matches('v'),
        config.calico_version,
        config.calico_version
    );
    let mut azure_cluster = context.object(
        "infrastructure.cluster.x-k8s.io/v1beta1",
        "AzureCluster",
        &names.azure_cluster,
        Some(namespace),
        json!({"spec":{
            "additionalTags":context.azure_tags(),
            "controlPlaneEnabled":false,
            "identityRef":{"apiVersion":"infrastructure.cluster.x-k8s.io/v1beta1","kind":"AzureClusterIdentity","name":names.identity},
            "location":config.location,
            "networkSpec":{
                "apiServerLB":{"type":"Public"},
                "subnets":[{"name":config.tenant_subnet_name,"role":"node"}],
                "vnet":{"name":config.vnet_name,"resourceGroup":config.resource_group_name}
            },
            "resourceGroup":config.resource_group_name,
            "subscriptionID":config.subscription_id
        }}),
    );
    azure_cluster
        .metadata
        .labels
        .get_or_insert_default()
        .insert(EXTERNAL_CONTROL_PLANE_LABEL.into(), "true".into());
    let mut status_labels = BTreeMap::new();
    status_labels.insert("cnpg-vcluster-status-probe".into(), context.name().into());
    status_labels.insert(TENANT_LABEL.into(), context.name().into());
    status_labels.insert(LABEL_PROFILE.into(), "azure".into());
    Ok(vec![
        context.object("v1", "Namespace", namespace, None, json!({})),
        context.object(
            "infrastructure.cluster.x-k8s.io/v1beta1",
            "AzureClusterIdentity",
            &names.identity,
            Some(namespace),
            json!({"spec":{
                "allowedNamespaces":{"list":[namespace]},
                "clientID":config.identity_client_id,
                "tenantID":config.tenant_id,
                "type":"WorkloadIdentity"
            }}),
        ),
        context.object(
            "cluster.x-k8s.io/v1beta1",
            "Cluster",
            &names.cluster,
            Some(namespace),
            json!({"spec":{
                "clusterNetwork":{
                    "apiServerPort":6443,
                    "pods":{"cidrBlocks":[pod_cidr]},
                    "serviceDomain":"cluster.local",
                    "services":{"cidrBlocks":[service_cidr]}
                },
                "controlPlaneRef":{"apiVersion":"controlplane.cluster.x-k8s.io/v1alpha1","kind":"KamajiControlPlane","name":names.control_plane},
                "infrastructureRef":{"apiVersion":"infrastructure.cluster.x-k8s.io/v1beta1","kind":"AzureCluster","name":names.azure_cluster}
            }}),
        ),
        azure_cluster,
        context.object(
            "controlplane.cluster.x-k8s.io/v1alpha1",
            "KamajiControlPlane",
            &names.control_plane,
            Some(namespace),
            json!({"spec":{
                "addons":{
                    "coreDNS":{"dnsServiceIPs":[dns]},
                    "konnectivity":{"agent":{
                        "hostNetwork":true,
                        "mode":"DaemonSet",
                        "tolerations":[
                            {"effect":"NoSchedule","key":"node.kubernetes.io/not-ready","operator":"Exists"},
                            {"effect":"NoExecute","key":"node.kubernetes.io/not-ready","operator":"Exists"},
                            {"effect":"NoSchedule","key":"node.cloudprovider.kubernetes.io/uninitialized","operator":"Exists"}
                        ]
                    },"server":{"port":8132}},
                    "kubeProxy":{}
                },
                "controllerManager":{"extraArgs":[
                    "--cloud-provider=external",
                    format!("--cluster-name={}",context.name()),
                    "--allocate-node-cidrs=false"
                ]},
                "dataStoreName":"default",
                "network":{
                    "certSANs":[],
                    "dnsServiceIPs":[dns],
                    "serviceAnnotations":{"service.beta.kubernetes.io/azure-load-balancer-internal":"true"},
                    "serviceType":"LoadBalancer"
                },
                "replicas":1,
                "version":context.spec.kubernetes_version
            }}),
        ),
        context.object(
            "bootstrap.cluster.x-k8s.io/v1beta1",
            "KubeadmConfig",
            &names.pool,
            Some(namespace),
            json!({"spec":{
                "files":[{"contentFrom":{"secret":{"key":"worker-node-azure.json","name":format!("{}-azure-json",names.pool)}},"owner":"root:root","path":"/etc/kubernetes/azure.json","permissions":"0644"}],
                "joinConfiguration":{"nodeRegistration":{
                    "kubeletExtraArgs":{"cloud-provider":"external","feature-gates":"KubeletCrashLoopBackOffMax=true"},
                    "name":"{{ ds.meta_data[\"local_hostname\"] }}"
                }}
            }}),
        ),
        context.object(
            "infrastructure.cluster.x-k8s.io/v1beta1",
            "AzureMachinePool",
            &names.pool,
            Some(namespace),
            json!({"spec":{
                "additionalTags":context.azure_tags(),
                "identity":"UserAssigned",
                "location":config.location,
                "orchestrationMode":"Uniform",
                "platformFaultDomainCount":1,
                "strategy":{"rollingUpdate":{"deletePolicy":"Oldest","maxSurge":1,"maxUnavailable":0},"type":"RollingUpdate"},
                "template":{
                    "image":{"computeGallery":{"gallery":"ClusterAPI-f72ceb4f-5159-4c26-a0fe-2ea738f0d019","name":"capi-ubun2-2404","version":context.spec.kubernetes_version}},
                    "networkInterfaces":[{"subnetName":config.tenant_subnet_name}],
                    "osDisk":{"diskSizeGB":30,"managedDisk":{"storageAccountType":"StandardSSD_LRS"},"osType":"Linux"},
                    "sshPublicKey":"",
                    "vmSize":config.worker_sku
                },
                "userAssignedIdentities":[{"providerID":provider_id}]
            }}),
        ),
        context.object(
            "cluster.x-k8s.io/v1beta1",
            "MachinePool",
            &names.pool,
            Some(namespace),
            json!({"spec":{
                "clusterName":context.name(),
                "replicas":context.spec.workers,
                "template":{
                    "metadata":context.template_metadata(common_template_labels.clone()),
                    "spec":{
                        "bootstrap":{"configRef":{"apiVersion":"bootstrap.cluster.x-k8s.io/v1beta1","kind":"KubeadmConfig","name":names.pool}},
                        "clusterName":context.name(),
                        "infrastructureRef":{"apiVersion":"infrastructure.cluster.x-k8s.io/v1beta1","kind":"AzureMachinePool","name":names.pool},
                        "nodeDrainTimeout":"2m",
                        "version":format!("v{}",context.spec.kubernetes_version)
                    }
                }
            }}),
        ),
        context.object(
            "v1",
            "ConfigMap",
            &names.cloud_values,
            Some(namespace),
            json!({"data":{"values.yaml":serde_json::to_string(&cloud_values).expect("values serialize")}}),
        ),
        context.object(
            "v1",
            "ConfigMap",
            &names.network_values,
            Some(namespace),
            json!({"data":{"values.yaml":serde_json::to_string(&calico_values).expect("values serialize")}}),
        ),
        context.object(
            "apps/v1",
            "Deployment",
            &names.status_probe,
            Some(namespace),
            json!({"spec":{
                "replicas":1,
                "selector":{"matchLabels":{"cnpg-vcluster-status-probe":context.name()}},
                "template":{
                    "metadata":context.template_metadata(status_labels),
                    "spec":{
                        "automountServiceAccountToken":false,
                        "containers":[{
                            "args":["proxy","--kubeconfig=/tenant/value","--address=127.0.0.1","--accept-hosts=^localhost$"],
                            "command":["kubectl"],
                            "image":format!("registry.k8s.io/kubectl:v{}",context.spec.kubernetes_version),
                            "name":"kubectl",
                            "readinessProbe":{"exec":{"command":["kubectl","--kubeconfig=/tenant/value","get","--raw=/readyz"]},"periodSeconds":10},
                            "volumeMounts":[{"mountPath":"/tenant","name":"tenant-kubeconfig","readOnly":true}]
                        }],
                        "volumes":[{"name":"tenant-kubeconfig","secret":{"secretName":names.kubeconfig}}]
                    }
                }
            }}),
        ),
        context.object(
            "batch/v1",
            "Job",
            &names.addon_job,
            Some(namespace),
            json!({"spec":{
                "backoffLimit":1,
                "template":{
                    "metadata":context.template_metadata(common_template_labels),
                    "spec":{
                        "automountServiceAccountToken":false,
                        "containers":[{
                            "args":[install],
                            "command":["sh","-ec"],
                            "image":"alpine/helm:3.19.0",
                            "name":"helm",
                            "volumeMounts":[
                                {"mountPath":"/tenant","name":"tenant-kubeconfig","readOnly":true},
                                {"mountPath":"/values/cloud-provider.yaml","name":"cloud-values","readOnly":true,"subPath":"values.yaml"},
                                {"mountPath":"/values/calico.yaml","name":"calico-values","readOnly":true,"subPath":"values.yaml"}
                            ]
                        }],
                        "restartPolicy":"Never",
                        "volumes":[
                            {"name":"tenant-kubeconfig","secret":{"secretName":names.kubeconfig}},
                            {"configMap":{"name":names.cloud_values},"name":"cloud-values"},
                            {"configMap":{"name":names.network_values},"name":"calico-values"}
                        ]
                    }
                }
            }}),
        ),
    ])
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum AzureBuildError {
    #[error("Azure builders require an Azure Tenant specification")]
    Provider,
    #[error("derive Azure DNS service address: {0}")]
    Dns(String),
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum AzureOwnershipError {
    #[error("desired and live Azure object identities differ")]
    Identity,
    #[error("live Azure object has no UID")]
    MissingUid,
    #[error("live Azure object UID differs from durable status")]
    Uid,
    #[error("live Azure object ownership markers differ from desired state")]
    Markers,
    #[error("live Azure object is deleting")]
    Deleting,
    #[error("live Azure object differs from desired state at {0}")]
    Desired(String),
    #[error("Azure provider resource identity is incomplete")]
    ProviderIdentity,
    #[error("Azure provider resource has no exact recorded owner")]
    ProviderOwner,
    #[error("Azure Tenant provider binding changed")]
    Binding,
}

pub fn validate_binding(
    actual: &AzureBindingStatus,
    expected: &AzureBindingStatus,
) -> Result<(), AzureOwnershipError> {
    if actual == expected {
        Ok(())
    } else {
        Err(AzureOwnershipError::Binding)
    }
}

pub fn validate_live_object(
    desired: &DynamicObject,
    live: &DynamicObject,
    recorded_uid: Option<&str>,
) -> Result<String, AzureOwnershipError> {
    if desired.types != live.types
        || desired.metadata.name != live.metadata.name
        || desired.metadata.namespace != live.metadata.namespace
    {
        return Err(AzureOwnershipError::Identity);
    }
    if live.metadata.deletion_timestamp.is_some() {
        return Err(AzureOwnershipError::Deleting);
    }
    for (key, value) in desired.metadata.labels.as_ref().into_iter().flatten() {
        if live
            .metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(key))
            != Some(value)
        {
            return Err(AzureOwnershipError::Markers);
        }
    }
    for (key, value) in desired.metadata.annotations.as_ref().into_iter().flatten() {
        if live
            .metadata
            .annotations
            .as_ref()
            .and_then(|annotations| annotations.get(key))
            != Some(value)
        {
            return Err(AzureOwnershipError::Markers);
        }
    }
    let uid = live
        .metadata
        .uid
        .as_deref()
        .filter(|value| !value.is_empty())
        .ok_or(AzureOwnershipError::MissingUid)?;
    if recorded_uid.is_some_and(|recorded| recorded != uid) {
        return Err(AzureOwnershipError::Uid);
    }
    desired_subset(&desired.data, &live.data, "$")?;
    Ok(uid.into())
}

fn desired_subset(desired: &Value, live: &Value, path: &str) -> Result<(), AzureOwnershipError> {
    match (desired, live) {
        (Value::Object(expected), Value::Object(actual)) => {
            for (key, value) in expected {
                let next = format!("{path}.{key}");
                let observed = actual
                    .get(key)
                    .ok_or_else(|| AzureOwnershipError::Desired(next.clone()))?;
                desired_subset(value, observed, &next)?;
            }
            Ok(())
        }
        (Value::Array(expected), Value::Array(actual)) if expected.len() == actual.len() => {
            for (index, (left, right)) in expected.iter().zip(actual).enumerate() {
                desired_subset(left, right, &format!("{path}[{index}]"))?;
            }
            Ok(())
        }
        (left, right) if left == right => Ok(()),
        _ => Err(AzureOwnershipError::Desired(path.into())),
    }
}

impl AzureManagementStatus {
    pub fn uid_for(&self, kind: &str, name: &str, tenant: &str) -> Option<&str> {
        let names = AzureNames::new(tenant);
        match (kind, name) {
            ("Namespace", value) if value == names.namespace => self.namespace_uid.as_deref(),
            ("AzureClusterIdentity", value) if value == names.identity => {
                self.azure_cluster_identity_uid.as_deref()
            }
            ("Cluster", value) if value == names.cluster => self.cluster_uid.as_deref(),
            ("AzureCluster", value) if value == names.azure_cluster => {
                self.azure_cluster_uid.as_deref()
            }
            ("KamajiControlPlane", value) if value == names.control_plane => {
                self.kamaji_control_plane_uid.as_deref()
            }
            ("KubeadmConfig", value) if value == names.pool => self.kubeadm_config_uid.as_deref(),
            ("AzureMachinePool", value) if value == names.pool => {
                self.azure_machine_pool_uid.as_deref()
            }
            ("MachinePool", value) if value == names.pool => self.machine_pool_uid.as_deref(),
            ("ConfigMap", value) if value == names.cloud_values => {
                self.cloud_values_config_map_uid.as_deref()
            }
            ("ConfigMap", value) if value == names.network_values => {
                self.network_values_config_map_uid.as_deref()
            }
            ("Deployment", value) if value == names.status_probe => {
                self.status_probe_deployment_uid.as_deref()
            }
            ("Job", value) if value == names.addon_job => self.addon_job_uid.as_deref(),
            _ => None,
        }
    }

    pub fn recorded_uids(&self) -> BTreeSet<&str> {
        [
            self.namespace_uid.as_deref(),
            self.azure_cluster_identity_uid.as_deref(),
            self.cluster_uid.as_deref(),
            self.azure_cluster_uid.as_deref(),
            self.kamaji_control_plane_uid.as_deref(),
            self.kubeadm_config_uid.as_deref(),
            self.azure_machine_pool_uid.as_deref(),
            self.machine_pool_uid.as_deref(),
            self.cloud_values_config_map_uid.as_deref(),
            self.network_values_config_map_uid.as_deref(),
            self.status_probe_deployment_uid.as_deref(),
            self.addon_job_uid.as_deref(),
        ]
        .into_iter()
        .flatten()
        .filter(|uid| !uid.is_empty())
        .collect()
    }
}

pub fn validate_provider_identity(
    identity: &AzureProviderResourceIdentity,
    management: &AzureManagementStatus,
) -> Result<(), AzureOwnershipError> {
    if identity.api_version.is_empty()
        || identity.kind.is_empty()
        || identity.name.is_empty()
        || identity.uid.is_empty()
    {
        return Err(AzureOwnershipError::ProviderIdentity);
    }
    let owners = management.recorded_uids();
    if !identity.owner_uids.is_empty()
        && !identity
            .owner_uids
            .iter()
            .any(|owner| owners.contains(owner.as_str()))
    {
        return Err(AzureOwnershipError::ProviderOwner);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{TenantSpec, spec_hash};
    use sha2::{Digest, Sha256};

    fn configuration_value() -> Value {
        let mut value = json!({
            "schema":1,
            "subscriptionId":"00000000-0000-0000-0000-000000000001",
            "tenantId":"00000000-0000-0000-0000-000000000002",
            "location":"westus2",
            "resourceGroupName":"yy-cv-rg",
            "resourceGroupId":"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/yy-cv-rg",
            "vnetName":"yy-cv-vnet",
            "vnetId":"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/yy-cv-rg/providers/Microsoft.Network/virtualNetworks/yy-cv-vnet",
            "tenantSubnetName":"tenant",
            "tenantSubnetId":"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/yy-cv-rg/providers/Microsoft.Network/virtualNetworks/yy-cv-vnet/subnets/tenant",
            "identityName":"yy-cv-identity",
            "identityId":"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/yy-cv-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/yy-cv-identity",
            "identityClientId":"00000000-0000-0000-0000-000000000003",
            "supportedKubernetesVersion":"1.32.13",
            "workerSku":"Standard_B2s",
            "capiVersion":"v1.10.7",
            "capzVersion":"v1.21.1",
            "kamajiCapiVersion":"v0.19.0",
            "kamajiChartVersion":"26.8.6-edge",
            "asoVersion":"v2.11.0",
            "cloudProviderVersion":"v1.32.3",
            "calicoVersion":"v3.32.2",
            "controllerImage":"yycvacr.azurecr.io/controller@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "foundationDefaultsSha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        });
        let hash = hex::encode(Sha256::digest(serde_json::to_vec(&value).unwrap()));
        value["foundationSha256"] = Value::String(hash);
        value
    }

    fn configuration() -> AzureConfiguration {
        let value = configuration_value();
        let config = ConfigMap {
            metadata: ObjectMeta {
                uid: Some("provider-config-uid".into()),
                ..Default::default()
            },
            data: Some(BTreeMap::from([(
                CONFIG_KEY.into(),
                serde_json::to_string(&value).unwrap(),
            )])),
            ..Default::default()
        };
        AzureConfiguration::from_config_map(&config).unwrap()
    }

    fn tenant() -> Tenant {
        Tenant::new(
            "tenant-c",
            TenantSpec {
                kubernetes_version: "1.32.13".into(),
                workers: 3,
                provider: TenantProviderSpec::Azure {
                    pod_cidr: "10.72.0.0/16".into(),
                    service_cidr: "10.142.0.0/16".into(),
                },
            },
        )
    }

    #[test]
    fn configuration_is_typed_canonical_and_fail_closed() {
        let parsed = configuration();
        assert_eq!(parsed.config_map_uid, "provider-config-uid");
        assert_eq!(parsed.values.capz_version, "v1.21.1");
        assert_eq!(parsed.values.aso_version, "v2.11.0");
        assert!(sha256(&parsed.sha256));
        let binding = parsed.binding("tenant-uid", "spec-sha", "operation");
        assert_eq!(validate_binding(&binding, &binding), Ok(()));
        let mut changed_binding = binding.clone();
        changed_binding.provider_config_uid = "replacement".into();
        assert_eq!(
            validate_binding(&changed_binding, &binding),
            Err(AzureOwnershipError::Binding)
        );

        let mut changed = configuration_value();
        changed["unknown"] = json!(true);
        let mut config = ConfigMap {
            metadata: ObjectMeta {
                uid: Some("uid".into()),
                ..Default::default()
            },
            data: Some(BTreeMap::from([(
                CONFIG_KEY.into(),
                serde_json::to_string(&changed).unwrap(),
            )])),
            ..Default::default()
        };
        assert!(AzureConfiguration::from_config_map(&config).is_err());
        changed.as_object_mut().unwrap().remove("unknown");
        changed["foundationSha256"] =
            json!("cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc");
        config
            .data
            .as_mut()
            .unwrap()
            .insert(CONFIG_KEY.into(), serde_json::to_string(&changed).unwrap());
        assert_eq!(
            AzureConfiguration::from_config_map(&config),
            Err(AzureConfigurationError::FoundationHash)
        );
        config.metadata.uid = None;
        assert_eq!(
            AzureConfiguration::from_config_map(&config),
            Err(AzureConfigurationError::Uid)
        );
    }

    #[test]
    fn desired_objects_match_authoritative_azure_contract() {
        let tenant = tenant();
        let config = configuration();
        let hash = spec_hash(&tenant.spec);
        let objects = desired_objects(&AzureContext {
            tenant: &tenant,
            spec: &tenant.spec,
            specification_sha256: &hash,
            foundation_sha256: &config.values.foundation_sha256,
            operation_id: "operation-1",
            configuration: &config.values,
        })
        .unwrap();
        let identities: Vec<_> = objects
            .iter()
            .map(|object| {
                (
                    object.types.as_ref().unwrap().api_version.as_str(),
                    object.types.as_ref().unwrap().kind.as_str(),
                    object.metadata.namespace.as_deref(),
                    object.metadata.name.as_deref().unwrap(),
                )
            })
            .collect();
        assert_eq!(
            identities,
            [
                ("v1", "Namespace", None, "tenant-c"),
                (
                    "infrastructure.cluster.x-k8s.io/v1beta1",
                    "AzureClusterIdentity",
                    Some("tenant-c"),
                    "tenant-c-identity"
                ),
                (
                    "cluster.x-k8s.io/v1beta1",
                    "Cluster",
                    Some("tenant-c"),
                    "tenant-c"
                ),
                (
                    "infrastructure.cluster.x-k8s.io/v1beta1",
                    "AzureCluster",
                    Some("tenant-c"),
                    "tenant-c"
                ),
                (
                    "controlplane.cluster.x-k8s.io/v1alpha1",
                    "KamajiControlPlane",
                    Some("tenant-c"),
                    "tenant-c"
                ),
                (
                    "bootstrap.cluster.x-k8s.io/v1beta1",
                    "KubeadmConfig",
                    Some("tenant-c"),
                    "tenant-c-worker"
                ),
                (
                    "infrastructure.cluster.x-k8s.io/v1beta1",
                    "AzureMachinePool",
                    Some("tenant-c"),
                    "tenant-c-worker"
                ),
                (
                    "cluster.x-k8s.io/v1beta1",
                    "MachinePool",
                    Some("tenant-c"),
                    "tenant-c-worker"
                ),
                (
                    "v1",
                    "ConfigMap",
                    Some("tenant-c"),
                    "tenant-c-azure-cloud-provider-values"
                ),
                (
                    "v1",
                    "ConfigMap",
                    Some("tenant-c"),
                    "tenant-c-calico-values"
                ),
                (
                    "apps/v1",
                    "Deployment",
                    Some("tenant-c"),
                    "tenant-c-status-probe"
                ),
                (
                    "batch/v1",
                    "Job",
                    Some("tenant-c"),
                    "tenant-c-install-addons"
                ),
            ]
        );
        for object in &objects {
            assert_eq!(
                object
                    .metadata
                    .labels
                    .as_ref()
                    .unwrap()
                    .get(LABEL_PROFILE)
                    .map(String::as_str),
                Some("azure")
            );
            assert_eq!(
                object
                    .metadata
                    .annotations
                    .as_ref()
                    .unwrap()
                    .get(ANNOTATION_OPERATION)
                    .map(String::as_str),
                Some("operation-1")
            );
        }
        let by_kind = |kind: &str| {
            objects
                .iter()
                .find(|object| object.types.as_ref().unwrap().kind == kind)
                .unwrap()
        };
        assert_eq!(
            by_kind("AzureCluster").metadata.labels.as_ref().unwrap()[EXTERNAL_CONTROL_PLANE_LABEL],
            "true"
        );
        assert_eq!(
            by_kind("AzureMachinePool").data["spec"]["orchestrationMode"],
            "Uniform"
        );
        assert_eq!(
            by_kind("AzureMachinePool").data["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"],
            0
        );
        assert_eq!(
            by_kind("MachinePool").data["spec"]["replicas"],
            tenant.spec.workers
        );
        assert_eq!(
            by_kind("MachinePool").data["spec"]["template"]["spec"]["nodeDrainTimeout"],
            "2m"
        );
        let deployment = by_kind("Deployment");
        let container = &deployment.data["spec"]["template"]["spec"]["containers"][0];
        assert_eq!(container["args"][2], "--address=127.0.0.1");
        assert!(container.get("ports").is_none());
        assert_eq!(
            container["readinessProbe"]["exec"]["command"][3],
            "--raw=/readyz"
        );
        let job = by_kind("Job").data["spec"]["template"]["spec"]["containers"][0]["args"][0]
            .as_str()
            .unwrap();
        assert!(job.contains("--version 1.32.3"));
        assert!(job.contains("--version v3.32.2"));
        assert!(
            !serde_json::to_string(&objects)
                .unwrap()
                .contains("yy-cv-tenant")
        );
    }

    #[test]
    fn ownership_validation_recovers_only_exact_live_identity() {
        let tenant = tenant();
        let config = configuration();
        let hash = spec_hash(&tenant.spec);
        let desired = desired_objects(&AzureContext {
            tenant: &tenant,
            spec: &tenant.spec,
            specification_sha256: &hash,
            foundation_sha256: &config.values.foundation_sha256,
            operation_id: "operation-1",
            configuration: &config.values,
        })
        .unwrap()
        .remove(2);
        let mut live = desired.clone();
        live.metadata.uid = Some("cluster-uid".into());
        live.metadata.resource_version = Some("12".into());
        live.data["status"] = json!({"phase":"Provisioned"});
        assert_eq!(
            validate_live_object(&desired, &live, None),
            Ok("cluster-uid".into())
        );
        assert_eq!(
            validate_live_object(&desired, &live, Some("foreign")),
            Err(AzureOwnershipError::Uid)
        );
        live.metadata
            .annotations
            .as_mut()
            .unwrap()
            .insert(TENANT_ANNOTATION.into(), "foreign".into());
        assert_eq!(
            validate_live_object(&desired, &live, None),
            Err(AzureOwnershipError::Markers)
        );
    }

    #[test]
    fn status_accessors_and_provider_owner_closure_are_exact() {
        let status = AzureManagementStatus {
            cluster_uid: Some("cluster-uid".into()),
            azure_cluster_uid: Some("azure-cluster-uid".into()),
            cloud_values_config_map_uid: Some("cloud-values-uid".into()),
            network_values_config_map_uid: Some("network-values-uid".into()),
            ..Default::default()
        };
        assert_eq!(
            status.uid_for("Cluster", "tenant-c", "tenant-c"),
            Some("cluster-uid")
        );
        assert_eq!(
            status.uid_for(
                "ConfigMap",
                "tenant-c-azure-cloud-provider-values",
                "tenant-c"
            ),
            Some("cloud-values-uid")
        );
        assert_eq!(status.uid_for("Cluster", "foreign", "tenant-c"), None);
        let owned = AzureProviderResourceIdentity {
            api_version: "network.azure.com/v1api20220701".into(),
            kind: "NatGateway".into(),
            namespace: Some("tenant-c".into()),
            name: "tenant-c-node-natgw-1".into(),
            uid: "nat-uid".into(),
            resource_id: Some("/subscriptions/x/natGateways/nat".into()),
            owner_uids: vec!["azure-cluster-uid".into()],
        };
        assert_eq!(validate_provider_identity(&owned, &status), Ok(()));
        let mut foreign = owned;
        foreign.owner_uids = vec!["foreign".into()];
        assert_eq!(
            validate_provider_identity(&foreign, &status),
            Err(AzureOwnershipError::ProviderOwner)
        );
    }
}
