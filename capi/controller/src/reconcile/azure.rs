use k8s_openapi::api::core::v1::ConfigMap;
use kube::runtime::controller::Action;

use crate::{
    api::{CanonicalSpec, Tenant, TenantProviderSpec},
    error::ControllerError,
};

use super::{ProviderLifecycle, ReconcileError};

pub const CONFIG_NAME: &str = "tenant-azure-provider";
pub const CONFIG_KEY: &str = "provider.json";
const PHASE_TWO_PENDING: &str =
    "Azure tenant lifecycle dependencies are pending Phase 2 implementation";

#[derive(Clone)]
pub struct AzureProvider {
    _configuration: serde_json::Value,
}

impl AzureProvider {
    pub fn from_config_map(config: &ConfigMap) -> Result<Self, ControllerError> {
        let raw = config
            .data
            .as_ref()
            .and_then(|data| data.get(CONFIG_KEY))
            .ok_or_else(|| {
                ControllerError::Configuration(
                    "Azure provider ConfigMap provider.json is missing".into(),
                )
            })?;
        let configuration: serde_json::Value = serde_json::from_str(raw).map_err(|error| {
            ControllerError::Configuration(format!(
                "Azure provider ConfigMap provider.json is invalid: {error}"
            ))
        })?;
        if configuration
            .get("schema")
            .and_then(serde_json::Value::as_u64)
            != Some(1)
        {
            return Err(ControllerError::Configuration(
                "Azure provider ConfigMap schema must be 1".into(),
            ));
        }
        for key in [
            "subscriptionId",
            "tenantId",
            "location",
            "resourceGroupName",
            "resourceGroupId",
            "vnetName",
            "vnetId",
            "tenantSubnetName",
            "tenantSubnetId",
            "identityName",
            "identityId",
            "identityClientId",
            "supportedKubernetesVersion",
            "workerSku",
            "cloudProviderVersion",
            "calicoVersion",
        ] {
            if configuration
                .get(key)
                .and_then(serde_json::Value::as_str)
                .is_none_or(str::is_empty)
            {
                return Err(ControllerError::Configuration(format!(
                    "Azure provider ConfigMap {key} is missing"
                )));
            }
        }
        Ok(Self {
            _configuration: configuration,
        })
    }
}

impl ProviderLifecycle for AzureProvider {
    fn supports(&self, provider: &TenantProviderSpec) -> bool {
        matches!(provider, TenantProviderSpec::Azure { .. })
    }

    async fn reconcile(
        &self,
        _tenant: &Tenant,
        _spec: &CanonicalSpec,
    ) -> Result<Action, ReconcileError> {
        Err(ReconcileError::Pending(PHASE_TWO_PENDING.into()))
    }

    async fn finalize(
        &self,
        _tenant: &Tenant,
        _supported_version: &str,
    ) -> Result<Action, ReconcileError> {
        Err(ReconcileError::Pending(PHASE_TWO_PENDING.into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    const VALID: &str = r#"{
        "schema":1,
        "subscriptionId":"subscription",
        "tenantId":"tenant",
        "location":"region",
        "resourceGroupName":"group",
        "resourceGroupId":"group-id",
        "vnetName":"vnet",
        "vnetId":"vnet-id",
        "tenantSubnetName":"subnet",
        "tenantSubnetId":"subnet-id",
        "identityName":"identity",
        "identityId":"identity-id",
        "identityClientId":"client-id",
        "supportedKubernetesVersion":"1.32.13",
        "workerSku":"Standard_B2s",
        "cloudProviderVersion":"v1.32.3",
        "calicoVersion":"v3.32.2"
    }"#;

    fn config(value: &str) -> ConfigMap {
        ConfigMap {
            data: Some(BTreeMap::from([(CONFIG_KEY.into(), value.into())])),
            ..Default::default()
        }
    }

    #[test]
    fn requires_non_secret_schema_one_provider_configuration() {
        assert!(AzureProvider::from_config_map(&config(VALID)).is_ok());
        assert!(AzureProvider::from_config_map(&config(r#"{"schema":1}"#)).is_err());
        assert!(AzureProvider::from_config_map(&config(r#"{"schema":2}"#)).is_err());
        assert!(AzureProvider::from_config_map(&config("not-json")).is_err());
        assert!(AzureProvider::from_config_map(&ConfigMap::default()).is_err());
    }

    #[test]
    fn supports_only_azure_specs() {
        let provider = AzureProvider::from_config_map(&config(VALID)).unwrap();
        assert!(provider.supports(&TenantProviderSpec::Azure {
            pod_cidr: "10.0.0.0/16".into(),
            service_cidr: "10.1.0.0/16".into(),
        }));
        assert!(!provider.supports(&TenantProviderSpec::Local { databases: 1 }));
    }

    #[tokio::test]
    async fn reports_phase_two_dependency_without_acquiring_lifecycle_state() {
        let provider = AzureProvider::from_config_map(&config(VALID)).unwrap();
        let spec = crate::api::TenantSpec {
            kubernetes_version: "1.32.13".into(),
            workers: 1,
            provider: TenantProviderSpec::Azure {
                pod_cidr: "10.0.0.0/16".into(),
                service_cidr: "10.1.0.0/16".into(),
            },
        };
        let tenant = Tenant::new("azure", spec.clone());
        let error = provider.reconcile(&tenant, &spec).await.unwrap_err();
        assert!(error.pending());
        assert!(error.to_string().contains("pending Phase 2"));
        assert!(tenant.metadata.finalizers.is_none());
    }
}
