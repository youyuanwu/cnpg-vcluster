use ipnet::Ipv4Net;
use kube::{CustomResource, CustomResourceExt};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const GROUP: &str = "tenancy.cnpg-vcluster.io";
pub const VERSION: &str = "v1alpha2";
pub const SUPPORTED_KUBERNETES_VERSION: &str = "1.36.4";
pub const FINALIZER: &str = "tenancy.cnpg-vcluster.io/finalizer";

#[rustfmt::skip]
#[expect(clippy::duplicated_attributes, reason = "each printer column declares its own type")]
#[derive(CustomResource, Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[kube(
    group = "tenancy.cnpg-vcluster.io", version = "v1alpha2", kind = "Tenant",
    plural = "tenants", shortname = "tn", status = "TenantStatus", derive = "PartialEq",
    printcolumn(name = "Phase", type_ = "string", json_path = ".status.phase"),
    printcolumn(name = "Ready", type_ = "string", json_path = ".status.conditions[?(@.type==\"Ready\")].status"),
    printcolumn(name = "Endpoint", type_ = "string", json_path = ".status.provider.allocation.endpoint"),
    printcolumn(name = "Workers", type_ = "integer", json_path = ".spec.workers"),
    printcolumn(name = "Databases", type_ = "integer", json_path = ".spec.provider.databases")
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
    Local { databases: i32 },
    Azure { #[serde(rename = "podCIDR")] pod_cidr: String, #[serde(rename = "serviceCIDR")] service_cidr: String },
}
#[rustfmt::skip]
#[derive(JsonSchema)]
#[allow(dead_code)]
struct TenantProviderSpecSchema {
    #[schemars(rename = "type")] provider_type: ProviderTypeSchema, databases: Option<i32>,
    #[schemars(rename = "podCIDR")] pod_cidr: Option<String>, #[schemars(rename = "serviceCIDR")] service_cidr: Option<String>,
}
#[rustfmt::skip]
#[derive(JsonSchema)]
#[schemars(rename_all = "lowercase")]
#[allow(dead_code)]
enum ProviderTypeSchema { Local, Azure }
#[rustfmt::skip]
impl TenantSpec {
    pub fn local(kubernetes_version: impl Into<String>, workers: i32, databases: i32) -> Self {
        Self { kubernetes_version: kubernetes_version.into(), workers, provider: TenantProviderSpec::Local { databases } }
    }
    pub fn local_databases(&self) -> Option<i32> { match self.provider { TenantProviderSpec::Local { databases } => Some(databases), TenantProviderSpec::Azure { .. } => None } }
}

#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct TenantStatus {
    #[serde(skip_serializing_if = "Option::is_none")] pub observed_generation: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")] pub phase: Option<TenantPhase>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")] pub conditions: Vec<k8s_openapi::apimachinery::pkg::apis::meta::v1::Condition>,
    #[serde(skip_serializing_if = "Option::is_none")] #[schemars(with = "Option<TenantProviderStatusSchema>")] pub provider: Option<TenantProviderStatus>,
}

#[rustfmt::skip]
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum TenantProviderStatus { Local(LocalProviderStatus), Azure }
#[rustfmt::skip]
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LocalProviderStatus {
    #[serde(skip_serializing_if = "Option::is_none")] pub allocation: Option<AllocationStatus>,
    #[serde(skip_serializing_if = "Option::is_none")] pub foundation_hash: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "clusterUID")] pub cluster_uid: Option<String>,
}
#[rustfmt::skip]
#[derive(JsonSchema)]
#[allow(dead_code)]
struct TenantProviderStatusSchema {
    #[schemars(rename = "type")] provider_type: ProviderTypeSchema, allocation: Option<AllocationStatus>,
    foundation_hash: Option<String>, #[schemars(rename = "clusterUID")] cluster_uid: Option<String>,
}
#[rustfmt::skip]
impl TenantStatus {
    pub fn local(&self) -> Option<&LocalProviderStatus> { match self.provider.as_ref() { Some(TenantProviderStatus::Local(status)) => Some(status), _ => None } }
    pub fn local_mut(&mut self) -> Result<&mut LocalProviderStatus, crate::error::ControllerError> {
        if self.provider.is_none() { self.provider = Some(TenantProviderStatus::Local(LocalProviderStatus::default())); }
        match self.provider.as_mut() { Some(TenantProviderStatus::Local(status)) => Ok(status),
            Some(TenantProviderStatus::Azure) => Err(crate::error::ControllerError::OwnershipInvalid(SpecError::ProviderStatus.to_string())), None => unreachable!("local provider status was initialized") }
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
pub enum SpecError { Name, Version, UnsupportedVersion, Workers, Databases, AzurePodCidr, AzureServiceCidr, AzureCidrOverlap, AzureServiceRange, ProviderStatus }
#[rustfmt::skip]
impl std::fmt::Display for SpecError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Name => "tenant name must be a 1-30 character lowercase DNS label", Self::Version => "kubernetesVersion must be a three-component numeric version",
            Self::UnsupportedVersion => "kubernetesVersion is not supported by this controller", Self::Workers => "workers must be an integer from 1 through 3",
            Self::Databases => "databases must be an integer from 1 through 3", Self::AzurePodCidr => "podCIDR must be a canonical IPv4 network",
            Self::AzureServiceCidr => "serviceCIDR must be a canonical IPv4 network", Self::AzureCidrOverlap => "Azure Pod and Service CIDRs must not overlap",
            Self::AzureServiceRange => "Azure Service CIDR is too small for the derived DNS service IP",
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
    let provider = match &spec.provider {
        TenantProviderSpec::Local { databases } => {
            if !(1..=3).contains(databases) { return Err(SpecError::Databases); }
            TenantProviderSpec::Local { databases: *databases }
        }
        TenantProviderSpec::Azure { pod_cidr, service_cidr } => {
            let pod = canonical_ipv4(pod_cidr).map_err(|_| SpecError::AzurePodCidr)?;
            let service = canonical_ipv4(service_cidr).map_err(|_| SpecError::AzureServiceCidr)?;
            if overlaps(&pod, &service) { return Err(SpecError::AzureCidrOverlap); }
            if service.prefix_len() > 28 { return Err(SpecError::AzureServiceRange); }
            TenantProviderSpec::Azure { pod_cidr: pod.to_string(), service_cidr: service.to_string() }
        }
    };
    Ok(TenantSpec { kubernetes_version: version.to_owned(), workers: spec.workers, provider })
}

#[rustfmt::skip]
fn canonical_ipv4(value: &str) -> Result<Ipv4Net, ()> {
    let network: Ipv4Net = value.parse().map_err(|_| ())?;
    if network.addr() == network.network() { Ok(network) } else { Err(()) }
}

#[rustfmt::skip]
fn overlaps(left: &Ipv4Net, right: &Ipv4Net) -> bool { left.contains(&right.network()) || right.contains(&left.network()) }

#[rustfmt::skip]
pub fn validate_provider_status(
    spec: &CanonicalSpec, status: Option<&TenantStatus>,
) -> Result<(), SpecError> {
    let Some(provider) = status.and_then(|status| status.provider.as_ref()) else { return Ok(()); };
    matches!((&spec.provider, provider), (TenantProviderSpec::Local { .. }, TenantProviderStatus::Local(_)) | (TenantProviderSpec::Azure { .. }, TenantProviderStatus::Azure))
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
    let provider = fields.get_mut("provider").expect("provider field").properties.as_mut().expect("provider has fields");
    let databases = provider.get_mut("databases").expect("local database field");
    (databases.minimum, databases.maximum) = (Some(1.0), Some(3.0));
    for field in ["podCIDR", "serviceCIDR"] {
        provider.get_mut(field).expect("Azure CIDR field").pattern =
            Some(r"^([0-9]{1,3}[.]){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$".into());
    }
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
        rule("self.spec.provider.type == 'local' ? has(self.spec.provider.databases) && !has(self.spec.provider.podCIDR) && !has(self.spec.provider.serviceCIDR) : !has(self.spec.provider.databases) && has(self.spec.provider.podCIDR) && has(self.spec.provider.serviceCIDR)", "provider fields must match the selected local or Azure provider"),
        rule("self.spec.provider.type != 'azure' || (isCIDR(self.spec.provider.podCIDR) && isCIDR(self.spec.provider.serviceCIDR) && cidr(self.spec.provider.podCIDR).ip().family() == 4 && cidr(self.spec.provider.serviceCIDR).ip().family() == 4 && !cidr(self.spec.provider.podCIDR).containsCIDR(cidr(self.spec.provider.serviceCIDR)) && !cidr(self.spec.provider.serviceCIDR).containsCIDR(cidr(self.spec.provider.podCIDR)) && cidr(self.spec.provider.serviceCIDR).prefixLength() <= 28)", "Azure Pod and Service CIDRs must be disjoint IPv4 networks and the Service CIDR must contain the derived DNS address"),
        rule("!has(self.status) || !has(self.status.provider) || self.status.provider.type == self.spec.provider.type", "Tenant provider status must match the requested provider"),
        rule("!has(self.status) || !has(self.status.provider) || self.status.provider.type != 'azure' || (!has(self.status.provider.allocation) && !has(self.status.provider.foundationHash) && !has(self.status.provider.clusterUID))", "Azure provider status cannot contain local durable identity"),
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
            provider: TenantProviderSpec::Local { databases: 3 },
        }
    }

    fn azure_spec() -> TenantSpec {
        TenantSpec {
            kubernetes_version: "1.36.4".into(),
            workers: 3,
            provider: TenantProviderSpec::Azure {
                pod_cidr: "10.244.0.0/16".into(),
                service_cidr: "10.96.0.0/16".into(),
            },
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
        assert_eq!(value["apiVersion"], "tenancy.cnpg-vcluster.io/v1alpha2");
        assert_eq!(value["kind"], "Tenant");
        assert_eq!(
            value["spec"],
            json!({
                "kubernetesVersion":"v1.36.4",
                "workers":2,
                "provider":{"type":"local","databases":3}
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
            provider: Some(TenantProviderStatus::Azure),
            ..Default::default()
        });
        let value = serde_json::to_value(&tenant).unwrap();
        assert_eq!(
            value["spec"]["provider"],
            json!({
                "type":"azure",
                "podCIDR":"10.244.0.0/16",
                "serviceCIDR":"10.96.0.0/16"
            })
        );
        assert_eq!(value["status"]["provider"], json!({"type":"azure"}));
        assert_eq!(serde_json::from_value::<Tenant>(value).unwrap(), tenant);
    }

    #[test]
    fn unknown_provider_fields_are_not_preserved_by_typed_api() {
        let spec: TenantSpec = serde_json::from_value(json!({
            "kubernetesVersion":"1.36.4",
            "workers":1,
            "provider":{"type":"local","databases":1,"unsupported":true}
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
            r#"{"kubernetesVersion":"1.36.4","workers":2,"provider":{"type":"local","databases":3}}"#
        );
        assert_eq!(spec_hash(&first), spec_hash(&second));
        assert_eq!(
            spec_hash(&first),
            "1bde3885f05188aa38dca07e80d5ce385b9520c7e51f09dbc5d71ebd9943191c"
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
            invalid.workers = 1;
            invalid.provider = TenantProviderSpec::Local { databases: count };
            assert_eq!(
                canonical_spec("valid", &invalid, "1.36.4"),
                Err(SpecError::Databases)
            );
        }
    }

    #[test]
    fn azure_cidrs_are_canonical_disjoint_ipv4_networks() {
        let canonical =
            canonical_spec("tenant-a", &azure_spec(), SUPPORTED_KUBERNETES_VERSION).unwrap();
        assert!(matches!(
            canonical.provider,
            TenantProviderSpec::Azure { .. }
        ));
        assert_eq!(
            serde_json::to_string(&canonical).unwrap(),
            r#"{"kubernetesVersion":"1.36.4","workers":3,"provider":{"type":"azure","podCIDR":"10.244.0.0/16","serviceCIDR":"10.96.0.0/16"}}"#
        );
        assert_eq!(
            spec_hash(&canonical),
            "61bf78756f6c9cc847f31de706048bae68b07b1cf8c0cd856142931854ac1885"
        );
        for (field, value, error) in [
            ("pod", "10.244.0.1/16", SpecError::AzurePodCidr),
            ("pod", "2001:db8::/64", SpecError::AzurePodCidr),
            ("service", "10.96.0.1/16", SpecError::AzureServiceCidr),
            ("service", "2001:db8::/64", SpecError::AzureServiceCidr),
        ] {
            let mut invalid = azure_spec();
            match &mut invalid.provider {
                TenantProviderSpec::Azure {
                    pod_cidr,
                    service_cidr,
                } => {
                    if field == "pod" {
                        *pod_cidr = value.into();
                    } else {
                        *service_cidr = value.into();
                    }
                }
                TenantProviderSpec::Local { .. } => unreachable!(),
            }
            assert_eq!(
                canonical_spec("tenant-a", &invalid, SUPPORTED_KUBERNETES_VERSION),
                Err(error)
            );
        }
        let mut overlap = azure_spec();
        if let TenantProviderSpec::Azure { service_cidr, .. } = &mut overlap.provider {
            *service_cidr = "10.244.128.0/17".into();
        }
        assert_eq!(
            canonical_spec("tenant-a", &overlap, SUPPORTED_KUBERNETES_VERSION),
            Err(SpecError::AzureCidrOverlap)
        );
        let mut too_small = azure_spec();
        if let TenantProviderSpec::Azure { service_cidr, .. } = &mut too_small.provider {
            *service_cidr = "10.96.0.0/29".into();
        }
        assert_eq!(
            canonical_spec("tenant-a", &too_small, SUPPORTED_KUBERNETES_VERSION),
            Err(SpecError::AzureServiceRange)
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
            provider: Some(TenantProviderStatus::Azure),
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
                ("Databases", ".spec.provider.databases"),
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
        assert_eq!(provider_fields["databases"].maximum, Some(3.0));
        assert!(
            provider_fields["podCIDR"]
                .pattern
                .as_deref()
                .is_some_and(|pattern| pattern.contains("[0-9]{1,3}"))
        );
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
        assert!(rules.iter().any(|r| r.contains("containsCIDR")));
        assert!(
            rules
                .iter()
                .any(|r| r.contains("status.provider.type == self.spec.provider.type"))
        );
        assert!(rules.iter().any(|r| r.contains("spec.kubernetesVersion")));
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
        assert!(status_provider_fields.contains_key("clusterUID"));
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
