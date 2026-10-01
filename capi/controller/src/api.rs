use std::collections::BTreeMap;

use kube::{CustomResource, CustomResourceExt};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const GROUP: &str = "tenancy.cnpg-vcluster.io";
pub const VERSION: &str = "v1alpha4";
pub const SUPPORTED_KUBERNETES_VERSION: &str = "1.36.4";
pub const FINALIZER: &str = "tenancy.cnpg-vcluster.io/finalizer";

#[rustfmt::skip]
#[expect(clippy::duplicated_attributes, reason = "each printer column declares its own type")]
#[derive(CustomResource, Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[kube(
    group = "tenancy.cnpg-vcluster.io", version = "v1alpha4", kind = "Tenant",
    plural = "tenants", shortname = "tn", status = "TenantStatus", derive = "PartialEq",
    printcolumn(name = "Phase", type_ = "string", json_path = ".status.phase"),
    printcolumn(name = "Ready", type_ = "string", json_path = ".status.conditions[?(@.type==\"Ready\")].status"),
    printcolumn(name = "Endpoint", type_ = "string", json_path = ".status.provider.allocation.endpoint"),
    printcolumn(name = "Workers", type_ = "integer", json_path = ".spec.workers")
)]
#[serde(rename_all = "camelCase")]
pub struct TenantSpec {
    pub kubernetes_version: String, pub workers: i32,
    #[schemars(with = "TenantProviderSpecSchema")] pub provider: TenantProviderSpec,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum TenantProviderSpec {
    Local,
    Azure,
}
#[rustfmt::skip]
#[derive(JsonSchema)]
#[allow(dead_code)]
struct TenantProviderSpecSchema {
    #[schemars(rename = "type")] provider_type: ProviderTypeSchema,
}
#[rustfmt::skip]
#[derive(JsonSchema)]
#[schemars(rename_all = "lowercase")]
#[allow(dead_code)]
enum ProviderTypeSchema { Local, Azure }
#[rustfmt::skip]
impl TenantSpec {
    pub fn local(kubernetes_version: impl Into<String>, workers: i32) -> Self {
        Self { kubernetes_version: kubernetes_version.into(), workers, provider: TenantProviderSpec::Local }
    }
}

#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct TenantStatus {
    #[serde(skip_serializing_if = "Option::is_none")] pub observed_generation: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")] pub phase: Option<TenantPhase>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")] pub conditions: Vec<k8s_openapi::apimachinery::pkg::apis::meta::v1::Condition>,
    #[serde(skip_serializing_if = "Option::is_none")] pub database_capability: Option<DatabaseCapability>,
    #[serde(skip_serializing_if = "Option::is_none")] pub catalog_create_intent: Option<CatalogCreateIntent>,
    #[serde(skip_serializing_if = "Option::is_none")] #[schemars(with = "Option<TenantProviderStatusSchema>")] pub provider: Option<TenantProviderStatus>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct DatabaseCapability {
    pub available: bool, pub reason: String,
    pub namespace: String,
    #[serde(rename = "namespaceUID")] pub namespace_uid: String,
    #[serde(rename = "catalogUID")] pub catalog_uid: String,
    #[serde(skip_serializing_if = "Option::is_none", rename = "storageNamespaceUID")]
    pub storage_namespace_uid: Option<String>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct CatalogCreateIntent {
    pub namespace: String,
    pub name: String,
    #[serde(rename = "tenantUID")] pub tenant_uid: String,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum TenantProviderStatus {
    Local(LocalProviderStatus),
    Azure(Box<AzureProviderStatus>),
}
#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LocalProviderStatus {
    #[serde(skip_serializing_if = "Option::is_none")] pub allocation: Option<AllocationStatus>,
    #[serde(skip_serializing_if = "Option::is_none")] pub foundation_hash: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "clusterUID")] pub cluster_uid: Option<String>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureProviderStatus {
    #[serde(skip_serializing_if = "Option::is_none")] pub binding: Option<AzureBindingStatus>,
    #[serde(skip_serializing_if = "Option::is_none")] pub network_allocation: Option<AzureAllocationStatus>,
    #[serde(skip_serializing_if = "Option::is_none")] pub endpoint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")] pub management: Option<AzureManagementStatus>,
    #[serde(skip_serializing_if = "Option::is_none")] pub kubeconfig: Option<AzureKubeconfigStatus>,
    #[serde(skip_serializing_if = "Option::is_none")] pub vmss: Option<AzureVmssStatus>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")] pub nodes: Vec<AzureNodeIdentity>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")] pub addon_components: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")] pub provider_resources: Vec<AzureProviderResourceIdentity>,
    #[serde(skip_serializing_if = "Option::is_none")] pub deletion: Option<AzureDeletionStatus>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureAllocationStatus {
    pub slot_id: String, #[serde(rename = "podCIDR")] pub pod_cidr: String,
    #[serde(rename = "serviceCIDR")] pub service_cidr: String,
    #[serde(rename = "catalogUID")] pub catalog_uid: String, pub catalog_sha256: String,
    pub lease_name: String, #[serde(rename = "leaseUID")] pub lease_uid: String,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureBindingStatus {
    #[serde(rename = "tenantUID")] pub tenant_uid: String, pub specification_sha256: String,
    #[serde(rename = "providerConfigUID")] pub provider_config_uid: String, pub provider_config_sha256: String,
    pub foundation_sha256: String, pub foundation_defaults_sha256: String, pub controller_image: String,
    pub resource_group_id: String, pub virtual_network_id: String, pub tenant_subnet_id: String,
    pub identity_id: String, pub operation_id: String,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureManagementStatus {
    #[serde(skip_serializing_if = "Option::is_none", rename = "namespaceUID")] pub namespace_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "azureClusterIdentityUID")] pub azure_cluster_identity_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "clusterUID")] pub cluster_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "azureClusterUID")] pub azure_cluster_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "kamajiControlPlaneUID")] pub kamaji_control_plane_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "kubeadmConfigUID")] pub kubeadm_config_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "azureMachinePoolUID")] pub azure_machine_pool_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "machinePoolUID")] pub machine_pool_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "cloudValuesConfigMapUID")] pub cloud_values_config_map_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "networkValuesConfigMapUID")] pub network_values_config_map_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "statusProbeDeploymentUID")] pub status_probe_deployment_uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "addonJobUID")] pub addon_job_uid: Option<String>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureKubeconfigStatus { #[serde(rename = "secretUID")] pub secret_uid: String, pub content_sha256: String }

#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureVmssStatus {
    #[serde(skip_serializing_if = "Option::is_none")] pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")] pub instance_ids: Vec<String>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureNodeIdentity {
    pub name: String, #[serde(rename = "uid")] pub uid: String,
    #[serde(rename = "providerID")] pub provider_id: String,
    #[serde(rename = "internalIP")] pub internal_ip: String,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureProviderResourceIdentity {
    pub api_version: String, pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")] pub namespace: Option<String>,
    pub name: String, #[serde(rename = "uid")] pub uid: String,
    #[serde(skip_serializing_if = "Option::is_none")] pub resource_id: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty", rename = "ownerUIDs")] pub owner_uids: Vec<String>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AzureDeletionStatus {
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty", rename = "resourceVersions")] pub resource_versions: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")] pub verified_azure_resource_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty", rename = "verifiedProviderUIDs")] pub verified_provider_uids: Vec<String>,
}

#[rustfmt::skip]
#[derive(JsonSchema)]
#[allow(dead_code)]
struct TenantProviderStatusSchema {
    #[schemars(rename = "type")] provider_type: ProviderTypeSchema, allocation: Option<AllocationStatus>,
    #[schemars(rename = "foundationHash")] foundation_hash: Option<String>,
    #[schemars(rename = "clusterUID")] cluster_uid: Option<String>, binding: Option<AzureBindingStatus>,
    #[schemars(rename = "networkAllocation")] network_allocation: Option<AzureAllocationStatus>,
    endpoint: Option<String>, management: Option<AzureManagementStatus>, kubeconfig: Option<AzureKubeconfigStatus>,
    vmss: Option<AzureVmssStatus>, nodes: Option<Vec<AzureNodeIdentity>>,
    #[schemars(rename = "addonComponents")] addon_components: Option<BTreeMap<String, String>>,
    #[schemars(rename = "providerResources")] provider_resources: Option<Vec<AzureProviderResourceIdentity>>,
    deletion: Option<AzureDeletionStatus>,
}
#[rustfmt::skip]
impl TenantStatus {
    pub fn local(&self) -> Option<&LocalProviderStatus> { match self.provider.as_ref() { Some(TenantProviderStatus::Local(status)) => Some(status), _ => None } }
    pub fn local_mut(&mut self) -> Result<&mut LocalProviderStatus, crate::error::ControllerError> {
        if self.provider.is_none() { self.provider = Some(TenantProviderStatus::Local(LocalProviderStatus::default())); }
        match self.provider.as_mut() { Some(TenantProviderStatus::Local(status)) => Ok(status),
            Some(TenantProviderStatus::Azure(_)) => Err(crate::error::ControllerError::OwnershipInvalid(SpecError::ProviderStatus.to_string())), None => unreachable!("local provider status was initialized") }
    }
    pub fn azure(&self) -> Option<&AzureProviderStatus> { match self.provider.as_ref() { Some(TenantProviderStatus::Azure(status)) => Some(status), _ => None } }
    pub fn azure_mut(&mut self) -> Result<&mut AzureProviderStatus, crate::error::ControllerError> {
        if self.provider.is_none() { self.provider = Some(TenantProviderStatus::Azure(Default::default())); }
        match self.provider.as_mut() {
            Some(TenantProviderStatus::Azure(status)) => Ok(status),
            Some(TenantProviderStatus::Local(_)) => Err(crate::error::ControllerError::OwnershipInvalid(SpecError::ProviderStatus.to_string())),
            None => unreachable!("Azure provider status was initialized"),
        }
    }
    pub fn allocation(&self) -> Option<&AllocationStatus> { self.local().and_then(|status| status.allocation.as_ref()) }
    pub fn foundation_hash(&self) -> Option<&str> { self.local().and_then(|status| status.foundation_hash.as_deref()) }
    pub fn cluster_uid(&self) -> Option<&str> { self.local().and_then(|status| status.cluster_uid.as_deref()) }
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AllocationStatus {
    pub slot_id: String, pub endpoint: String,
    #[serde(rename = "podCIDR")] pub pod_cidr: String,
    #[serde(rename = "serviceCIDR")] pub service_cidr: String,
}

#[rustfmt::skip]
#[derive(Clone, Copy, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum TenantPhase { Pending, Progressing, Ready, Deleting, Degraded, Failed, OwnershipInvalid }

pub type CanonicalSpec = TenantSpec;

#[rustfmt::skip]
#[derive(Debug, PartialEq, Eq)]
pub enum SpecError { Name, Version, UnsupportedVersion, Workers, ProviderStatus }
#[rustfmt::skip]
impl std::fmt::Display for SpecError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Name => "tenant name must be a 1-30 character lowercase DNS label", Self::Version => "kubernetesVersion must be a three-component numeric version",
            Self::UnsupportedVersion => "kubernetesVersion is not supported by this controller", Self::Workers => "workers must be an integer from 1 through 3",
            Self::ProviderStatus => "Tenant provider status does not match the requested provider",
        })
    }
}

impl std::error::Error for SpecError {}

#[rustfmt::skip]
pub fn canonical_spec(
    name: &str, spec: &TenantSpec, supported_version: &str,
) -> Result<CanonicalSpec, SpecError> {
    let bytes = name.as_bytes();
    if !(1..=30).contains(&bytes.len())
        || !bytes.first().is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        || !bytes.last().is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        || !bytes.iter().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-') { return Err(SpecError::Name); }
    let version = spec.kubernetes_version.strip_prefix('v').unwrap_or(&spec.kubernetes_version);
    if version.split('.').count() != 3
        || !version.split('.').all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit())) { return Err(SpecError::Version); }
    if version != supported_version.strip_prefix('v').unwrap_or(supported_version) { return Err(SpecError::UnsupportedVersion); }
    if !(1..=3).contains(&spec.workers) { return Err(SpecError::Workers); }
    Ok(TenantSpec { kubernetes_version: version.to_owned(), workers: spec.workers, provider: spec.provider.clone() })
}

#[rustfmt::skip]
pub fn validate_provider_status(
    spec: &CanonicalSpec, status: Option<&TenantStatus>,
) -> Result<(), SpecError> {
    let Some(provider) = status.and_then(|status| status.provider.as_ref()) else { return Ok(()); };
    matches!((&spec.provider, provider), (TenantProviderSpec::Local, TenantProviderStatus::Local(_)) | (TenantProviderSpec::Azure, TenantProviderStatus::Azure(_)))
        .then_some(()).ok_or(SpecError::ProviderStatus)
}

#[rustfmt::skip]
pub fn spec_hash(spec: &CanonicalSpec) -> String { hex::encode(Sha256::digest(serde_json::to_vec(spec).expect("canonical Tenant spec is serializable"))) }

#[rustfmt::skip]
pub fn tenant_crd() -> k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::CustomResourceDefinition {
    use k8s_openapi::apiextensions_apiserver::pkg::apis::apiextensions::v1::ValidationRule;

    let mut crd = Tenant::crd();
    let schema = crd.spec.versions[0].schema.as_mut().expect("Tenant has a derived schema")
        .open_api_v3_schema.as_mut().expect("Tenant has a structural schema");
    let properties = schema.properties.as_mut().expect("Tenant has properties");
    let fields = properties.get_mut("spec").expect("Tenant has a spec").properties.as_mut().expect("spec has fields");
    fields.get_mut("kubernetesVersion").expect("version field").pattern = Some(r"^v?[0-9]+\.[0-9]+\.[0-9]+$".into());
    let workers = fields.get_mut("workers").expect("workers field");
    (workers.minimum, workers.maximum) = (Some(1.0), Some(3.0));
    let conditions = properties.get_mut("status").and_then(|s| s.properties.as_mut())
        .and_then(|s| s.get_mut("conditions")).expect("status has conditions");
    (conditions.x_kubernetes_list_type, conditions.x_kubernetes_list_map_keys) =
        (Some("map".into()), Some(vec!["type".into()]));
    let rule = |rule: &str, message: &str| ValidationRule {
        rule: rule.into(), message: Some(message.into()), ..Default::default()
    };
    schema.x_kubernetes_validations = Some(vec![
        rule("self.spec == oldSelf.spec", "Tenant spec is immutable"),
        rule("self.metadata.name.matches('^[a-z0-9]([-a-z0-9]{0,28}[a-z0-9])?$')", "Tenant name must be a 1-30 character lowercase DNS label"),
        rule("self.spec.workers >= 1 && self.spec.workers <= 3", "workers must be between 1 and 3"),
        rule("!has(self.status) || !has(self.status.provider) || self.status.provider.type == self.spec.provider.type", "Tenant provider status must match the requested provider"),
        rule("!has(self.status) || !has(self.status.provider) || self.status.provider.type != 'azure' || (!has(self.status.provider.allocation) && !has(self.status.provider.foundationHash) && !has(self.status.provider.clusterUID))", "Azure provider status cannot contain local durable identity"),
        rule("!has(self.status) || !has(self.status.provider) || self.status.provider.type != 'local' || (!has(self.status.provider.binding) && !has(self.status.provider.networkAllocation) && !has(self.status.provider.endpoint) && !has(self.status.provider.management) && !has(self.status.provider.kubeconfig) && !has(self.status.provider.vmss) && !has(self.status.provider.nodes) && !has(self.status.provider.addonComponents) && !has(self.status.provider.providerResources) && !has(self.status.provider.deletion))", "Local provider status cannot contain Azure durable identity"),
        rule("!has(oldSelf.status) || !has(oldSelf.status.catalogCreateIntent) || (has(self.status) && has(self.status.catalogCreateIntent) && self.status.catalogCreateIntent == oldSelf.status.catalogCreateIntent)", "catalog creation intent cannot be removed or replaced"),
        rule("self.spec.kubernetesVersion.matches('^v?[0-9]+[.][0-9]+[.][0-9]+$')", "kubernetesVersion must be a three-component numeric version"),
    ]);
    crd
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn spec() -> TenantSpec {
        TenantSpec {
            kubernetes_version: "v1.36.4".into(),
            workers: 2,
            provider: TenantProviderSpec::Local,
        }
    }

    fn azure_spec() -> TenantSpec {
        TenantSpec {
            kubernetes_version: "1.36.4".into(),
            workers: 3,
            provider: TenantProviderSpec::Azure,
        }
    }

    #[test]
    fn local_spec_and_status_json_shape() {
        let mut tenant = Tenant::new("tenant-a", spec());
        let condition = serde_json::from_value(json!({
            "type":"Ready", "status":"True", "reason":"Reconciled",
            "message":"Ready", "lastTransitionTime":"2026-09-25T00:00:00Z",
            "observedGeneration":2
        }))
        .unwrap();
        tenant.status = Some(TenantStatus {
            observed_generation: Some(2),
            phase: Some(TenantPhase::Progressing),
            conditions: vec![condition],
            database_capability: None,
            catalog_create_intent: None,
            provider: Some(TenantProviderStatus::Local(LocalProviderStatus {
                allocation: Some(AllocationStatus {
                    slot_id: "slot-a".into(),
                    endpoint: "10.0.0.8".into(),
                    pod_cidr: "10.73.0.0/16".into(),
                    service_cidr: "10.143.0.0/16".into(),
                }),
                foundation_hash: Some("abc".into()),
                cluster_uid: None,
            })),
        });
        let value = serde_json::to_value(&tenant).unwrap();
        assert_eq!(value["apiVersion"], "tenancy.cnpg-vcluster.io/v1alpha4");
        assert_eq!(value["kind"], "Tenant");
        assert_eq!(
            value["spec"],
            json!({
                "kubernetesVersion":"v1.36.4",
                "workers":2,
                "provider":{"type":"local"}
            })
        );
        assert_eq!(value["status"]["provider"]["type"], "local");
        assert_eq!(
            value["status"]["provider"]["allocation"]["podCIDR"],
            "10.73.0.0/16"
        );
        assert_eq!(
            value["status"]["provider"]["allocation"]["serviceCIDR"],
            "10.143.0.0/16"
        );
        assert_eq!(value["status"]["observedGeneration"], 2);
        assert_eq!(value["status"]["conditions"][0]["type"], "Ready");
        assert_eq!(
            value["status"]["conditions"][0]["lastTransitionTime"],
            "2026-09-25T00:00:00Z"
        );
        assert_eq!(serde_json::from_value::<Tenant>(value).unwrap(), tenant);
        let mut status = tenant.status.unwrap();
        status.local_mut().unwrap().cluster_uid = Some("cluster-uid".into());
        assert_eq!(
            serde_json::to_value(status).unwrap()["provider"]["clusterUID"],
            "cluster-uid"
        );
    }

    #[test]
    fn azure_spec_and_placeholder_status_are_representable() {
        let mut tenant = Tenant::new("tenant-a", azure_spec());
        tenant.status = Some(TenantStatus {
            provider: Some(TenantProviderStatus::Azure(Default::default())),
            ..Default::default()
        });
        let value = serde_json::to_value(&tenant).unwrap();
        assert_eq!(value["spec"]["provider"], json!({"type":"azure"}));
        assert_eq!(value["status"]["provider"], json!({"type":"azure"}));
        assert_eq!(serde_json::from_value::<Tenant>(value).unwrap(), tenant);
    }

    #[test]
    fn azure_status_is_typed_durable_and_contains_no_secret_material() {
        let mut tenant = Tenant::new("tenant-a", azure_spec());
        tenant.status = Some(TenantStatus {
            provider: Some(TenantProviderStatus::Azure(Box::new(AzureProviderStatus {
                binding: Some(AzureBindingStatus {
                    tenant_uid: "tenant-uid".into(),
                    specification_sha256: "spec-sha".into(),
                    provider_config_uid: "config-uid".into(),
                    provider_config_sha256: "config-sha".into(),
                    foundation_sha256: "foundation-sha".into(),
                    foundation_defaults_sha256: "defaults-sha".into(),
                    controller_image: "registry/controller@sha256:digest".into(),
                    resource_group_id: "/subscriptions/s/resourceGroups/rg".into(),
                    virtual_network_id: "/subscriptions/s/virtualNetworks/vnet".into(),
                    tenant_subnet_id: "/subscriptions/s/subnets/tenant".into(),
                    identity_id: "/subscriptions/s/userAssignedIdentities/id".into(),
                    operation_id: "operation".into(),
                }),
                network_allocation: Some(AzureAllocationStatus {
                    slot_id: "azure-01".into(),
                    pod_cidr: "10.72.0.0/16".into(),
                    service_cidr: "10.142.0.0/16".into(),
                    catalog_uid: "catalog-uid".into(),
                    catalog_sha256: "catalog-sha".into(),
                    lease_name: "tenant-azure-slot-a".into(),
                    lease_uid: "lease-uid".into(),
                }),
                endpoint: Some("10.220.0.6:6443".into()),
                management: Some(AzureManagementStatus {
                    namespace_uid: Some("namespace-uid".into()),
                    cluster_uid: Some("cluster-uid".into()),
                    ..Default::default()
                }),
                kubeconfig: Some(AzureKubeconfigStatus {
                    secret_uid: "secret-uid".into(),
                    content_sha256: "content-sha".into(),
                }),
                vmss: Some(AzureVmssStatus {
                    id: Some("/subscriptions/s/virtualMachineScaleSets/tenant-a-worker".into()),
                    instance_ids: vec!["instance-0".into()],
                }),
                nodes: vec![AzureNodeIdentity {
                    name: "node-0".into(),
                    uid: "node-uid".into(),
                    provider_id: "azure:///instance-0".into(),
                    internal_ip: "10.30.0.4".into(),
                }],
                addon_components: BTreeMap::from([(
                    "cloudController".into(),
                    "component-uid".into(),
                )]),
                provider_resources: vec![AzureProviderResourceIdentity {
                    api_version: "network.azure.com/v1api20220701".into(),
                    kind: "NatGateway".into(),
                    namespace: Some("tenant-a".into()),
                    name: "tenant-a-nat".into(),
                    uid: "nat-uid".into(),
                    resource_id: Some("/subscriptions/s/natGateways/nat".into()),
                    owner_uids: vec!["cluster-uid".into()],
                }],
                deletion: Some(AzureDeletionStatus {
                    resource_versions: BTreeMap::from([("Cluster/tenant-a".into(), "42".into())]),
                    verified_azure_resource_ids: vec!["/subscriptions/s/natGateways/nat".into()],
                    verified_provider_uids: vec!["nat-uid".into()],
                }),
            }))),
            ..Default::default()
        });
        let value = serde_json::to_value(&tenant).unwrap();
        let provider = &value["status"]["provider"];
        assert_eq!(provider["type"], "azure");
        assert_eq!(provider["networkAllocation"]["podCIDR"], "10.72.0.0/16");
        assert_eq!(provider["management"]["clusterUID"], "cluster-uid");
        assert_eq!(provider["kubeconfig"]["secretUID"], "secret-uid");
        assert_eq!(provider["nodes"][0]["providerID"], "azure:///instance-0");
        let text = serde_json::to_string(&value).unwrap();
        assert!(!text.contains("kubeconfigBytes"));
        assert!(!text.contains("token"));
        assert!(!text.contains("clientSecret"));
        assert_eq!(serde_json::from_value::<Tenant>(value).unwrap(), tenant);
    }

    #[test]
    fn unknown_provider_fields_are_not_preserved_by_typed_api() {
        let spec: TenantSpec = serde_json::from_value(json!({
            "kubernetesVersion":"1.36.4",
            "workers":1,
            "provider":{"type":"local","unsupported":true}
        }))
        .unwrap();
        assert_eq!(
            serde_json::to_value(spec).unwrap()["provider"].get("unsupported"),
            None
        );
    }

    #[test]
    fn canonical_hash_normalizes_version_and_is_stable() {
        let first = canonical_spec("tenant-a", &spec(), SUPPORTED_KUBERNETES_VERSION).unwrap();
        let mut equivalent = spec();
        equivalent.kubernetes_version = "1.36.4".into();
        let second = canonical_spec("tenant-a", &equivalent, "v1.36.4").unwrap();
        assert_eq!(first, second);
        assert_eq!(
            serde_json::to_string(&first).unwrap(),
            r#"{"kubernetesVersion":"1.36.4","workers":2,"provider":{"type":"local"}}"#
        );
        assert_eq!(spec_hash(&first), spec_hash(&second));
        assert_eq!(
            spec_hash(&first),
            "0af1c9ca88047848e53b9419039e7821ab65a052d1da16eaae3dcd33092a7868"
        );
        equivalent.workers += 1;
        assert_ne!(
            spec_hash(&first),
            spec_hash(&canonical_spec("tenant-a", &equivalent, "1.36.4").unwrap())
        );
    }

    #[test]
    fn invalid_names_versions_and_counts_fail() {
        for name in ["", "-a", "a-", "UPPER", "a.b", "a".repeat(31).as_str()] {
            assert_eq!(
                canonical_spec(name, &spec(), "1.36.4"),
                Err(SpecError::Name)
            );
        }
        for version in ["", "1.36", "1.x.4", "1.36.4.0", "v1.36.4 "] {
            let mut invalid = spec();
            invalid.kubernetes_version = version.into();
            assert_eq!(
                canonical_spec("valid", &invalid, "1.36.4"),
                Err(SpecError::Version)
            );
        }
        assert_eq!(
            canonical_spec("valid", &spec(), "1.36.5"),
            Err(SpecError::UnsupportedVersion)
        );
        for count in [0, 4, -1] {
            let mut invalid = spec();
            invalid.workers = count;
            assert_eq!(
                canonical_spec("valid", &invalid, "1.36.4"),
                Err(SpecError::Workers)
            );
        }
    }

    #[test]
    fn azure_spec_has_no_administrator_selected_networks() {
        let canonical =
            canonical_spec("tenant-a", &azure_spec(), SUPPORTED_KUBERNETES_VERSION).unwrap();
        assert!(matches!(canonical.provider, TenantProviderSpec::Azure));
        assert_eq!(
            serde_json::to_string(&canonical).unwrap(),
            r#"{"kubernetesVersion":"1.36.4","workers":3,"provider":{"type":"azure"}}"#
        );
        assert_eq!(
            serde_json::to_value(canonical).unwrap()["provider"],
            json!({"type":"azure"})
        );
    }

    #[test]
    fn provider_status_must_match_spec_provider() {
        let local = canonical_spec("tenant-a", &spec(), "1.36.4").unwrap();
        let azure = canonical_spec("tenant-a", &azure_spec(), "1.36.4").unwrap();
        let local_status = TenantStatus {
            provider: Some(TenantProviderStatus::Local(LocalProviderStatus::default())),
            ..Default::default()
        };
        let azure_status = TenantStatus {
            provider: Some(TenantProviderStatus::Azure(Default::default())),
            ..Default::default()
        };
        assert_eq!(validate_provider_status(&local, None), Ok(()));
        assert_eq!(
            validate_provider_status(&local, Some(&local_status)),
            Ok(())
        );
        assert_eq!(
            validate_provider_status(&azure, Some(&azure_status)),
            Ok(())
        );
        assert_eq!(
            validate_provider_status(&local, Some(&azure_status)),
            Err(SpecError::ProviderStatus)
        );
        assert_eq!(
            validate_provider_status(&azure, Some(&local_status)),
            Err(SpecError::ProviderStatus)
        );
    }

    #[test]
    fn crd_has_structural_pruned_schema_and_transition_rules() {
        let crd = tenant_crd();
        assert_eq!(
            crd.metadata.name.as_deref(),
            Some("tenants.tenancy.cnpg-vcluster.io")
        );
        assert_eq!(crd.spec.group, GROUP);
        assert_eq!(crd.spec.scope, "Cluster");
        assert_eq!(crd.spec.names.kind, "Tenant");
        assert_eq!(crd.spec.names.plural, "tenants");
        assert_eq!(crd.spec.names.short_names.as_ref().unwrap(), &["tn"]);
        assert_eq!(crd.spec.versions.len(), 1);
        let v = &crd.spec.versions[0];
        assert_eq!(v.name, VERSION);
        assert!(v.served && v.storage);
        assert!(v.subresources.as_ref().unwrap().status.is_some());
        let columns: Vec<_> = v
            .additional_printer_columns
            .as_ref()
            .unwrap()
            .iter()
            .map(|c| (c.name.as_str(), c.json_path.as_str()))
            .collect();
        assert_eq!(
            columns,
            [
                ("Phase", ".status.phase"),
                ("Ready", ".status.conditions[?(@.type==\"Ready\")].status"),
                ("Endpoint", ".status.provider.allocation.endpoint"),
                ("Workers", ".spec.workers"),
            ]
        );
        let schema = v
            .schema
            .as_ref()
            .unwrap()
            .open_api_v3_schema
            .as_ref()
            .unwrap();
        let properties = schema.properties.as_ref().unwrap();
        assert_eq!(schema.type_.as_deref(), Some("object"));
        assert!(schema.x_kubernetes_preserve_unknown_fields.is_none());
        let spec = &properties["spec"];
        assert!(spec.x_kubernetes_preserve_unknown_fields.is_none());
        assert_eq!(
            spec.required.as_ref().unwrap(),
            &["kubernetesVersion", "provider", "workers"]
        );
        let fields = spec.properties.as_ref().unwrap();
        assert_eq!(fields.len(), 3);
        assert_eq!(fields["workers"].minimum, Some(1.0));
        assert_eq!(
            fields["kubernetesVersion"].pattern.as_deref(),
            Some(r"^v?[0-9]+\.[0-9]+\.[0-9]+$")
        );
        let provider_fields = fields["provider"].properties.as_ref().unwrap();
        assert!(!provider_fields.contains_key("databases"));
        assert!(!provider_fields.contains_key("podCIDR"));
        assert!(!provider_fields.contains_key("serviceCIDR"));
        let status_fields = properties["status"].properties.as_ref().unwrap();
        let capability_fields = status_fields["databaseCapability"]
            .properties
            .as_ref()
            .unwrap();
        for field in ["namespace", "namespaceUID", "catalogUID"] {
            assert!(capability_fields.contains_key(field));
        }
        assert!(!capability_fields.contains_key("gateUID"));
        assert!(!capability_fields.contains_key("quotaUID"));
        let intent_fields = status_fields["catalogCreateIntent"]
            .properties
            .as_ref()
            .unwrap();
        assert_eq!(intent_fields.len(), 3);
        for field in ["namespace", "name", "tenantUID"] {
            assert!(intent_fields.contains_key(field));
        }
        let status_provider = properties["status"].properties.as_ref().unwrap()["provider"]
            .properties
            .as_ref()
            .unwrap();
        assert!(status_provider.contains_key("foundationHash"));
        assert!(!status_provider.contains_key("foundation_hash"));
        for field in [
            "binding",
            "networkAllocation",
            "endpoint",
            "management",
            "kubeconfig",
            "vmss",
            "nodes",
            "addonComponents",
            "providerResources",
            "deletion",
        ] {
            assert!(status_provider.contains_key(field), "{field}");
        }
        let rules: Vec<_> = schema
            .x_kubernetes_validations
            .as_ref()
            .unwrap()
            .iter()
            .map(|r| r.rule.as_str())
            .collect();
        assert!(rules.contains(&"self.spec == oldSelf.spec"));
        assert!(rules.iter().any(|r| r.contains("metadata.name")));
        assert!(rules.iter().any(|r| r.contains("spec.workers")));
        assert!(!rules.iter().any(|r| r.contains("provider.databases")));
        assert!(
            rules
                .iter()
                .any(|r| r.contains("status.provider.type == self.spec.provider.type"))
        );
        assert!(rules.iter().any(|r| r.contains("spec.kubernetesVersion")));
        assert!(rules.iter().any(|r| {
            r.contains("self.status.catalogCreateIntent == oldSelf.status.catalogCreateIntent")
        }));
        let conditions = &properties["status"].properties.as_ref().unwrap()["conditions"];
        assert_eq!(conditions.x_kubernetes_list_type.as_deref(), Some("map"));
        assert_eq!(
            conditions.x_kubernetes_list_map_keys.as_ref().unwrap(),
            &["type"]
        );
        let status_provider = &properties["status"].properties.as_ref().unwrap()["provider"];
        let status_provider_fields = status_provider.properties.as_ref().unwrap();
        assert!(
            status_provider_fields["allocation"]
                .properties
                .as_ref()
                .is_some_and(|properties| properties.contains_key("podCIDR"))
        );
        assert!(
            status_provider_fields["networkAllocation"]
                .properties
                .as_ref()
                .is_some_and(|properties| properties.contains_key("catalogUID"))
        );
        assert!(status_provider_fields.contains_key("clusterUID"));
        assert!(
            status_provider_fields["management"]
                .properties
                .as_ref()
                .is_some_and(|properties| properties.contains_key("azureMachinePoolUID"))
        );
        assert!(
            status_provider_fields["kubeconfig"]
                .properties
                .as_ref()
                .is_some_and(|properties| properties.contains_key("contentSha256"))
        );
        assert!(
            properties["status"]
                .x_kubernetes_preserve_unknown_fields
                .is_none()
        );
        let json: Value = serde_json::to_value(crd).unwrap();
        assert!(
            json["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
                .get("x-kubernetes-preserve-unknown-fields")
                .is_none()
        );
    }
}
