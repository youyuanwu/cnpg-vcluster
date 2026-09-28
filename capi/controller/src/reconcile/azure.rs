use k8s_openapi::api::core::v1::ConfigMap;
use kube::runtime::controller::Action;

use crate::{
    api::{CanonicalSpec, Tenant, TenantProviderSpec},
    azure::AzureConfiguration,
    error::ControllerError,
};

use super::{ProviderLifecycle, ReconcileError};

pub use crate::azure::CONFIG_NAME;
const PHASE_TWO_PENDING: &str = "Azure tenant reconciliation is pending Phase 3 implementation";

#[derive(Clone)]
pub struct AzureProvider {
    pub configuration: AzureConfiguration,
}

impl AzureProvider {
    pub fn from_config_map(config: &ConfigMap) -> Result<Self, ControllerError> {
        Ok(Self {
            configuration: AzureConfiguration::from_config_map(config)
                .map_err(|error| ControllerError::Configuration(error.to_string()))?,
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
    use crate::azure::CONFIG_KEY;
    use sha2::{Digest, Sha256};
    use std::collections::BTreeMap;

    fn config(value: &str) -> ConfigMap {
        ConfigMap {
            data: Some(BTreeMap::from([(CONFIG_KEY.into(), value.into())])),
            metadata: kube::core::ObjectMeta {
                uid: Some("provider-config-uid".into()),
                ..Default::default()
            },
            ..Default::default()
        }
    }

    fn valid() -> String {
        let mut value = serde_json::json!({
            "schema":1,
            "subscriptionId":"subscription",
            "tenantId":"tenant",
            "location":"region",
            "resourceGroupName":"group",
            "resourceGroupId":"/subscriptions/subscription/resourceGroups/group",
            "vnetName":"vnet",
            "vnetId":"/subscriptions/subscription/resourceGroups/group/providers/Microsoft.Network/virtualNetworks/vnet",
            "tenantSubnetName":"subnet",
            "tenantSubnetId":"/subscriptions/subscription/resourceGroups/group/providers/Microsoft.Network/virtualNetworks/vnet/subnets/subnet",
            "identityName":"identity",
            "identityId":"/subscriptions/subscription/resourceGroups/group/providers/Microsoft.ManagedIdentity/userAssignedIdentities/identity",
            "identityClientId":"client-id",
            "supportedKubernetesVersion":"1.32.13",
            "workerSku":"Standard_B2s",
            "capiVersion":"v1.10.7",
            "capzVersion":"v1.21.1",
            "kamajiCapiVersion":"v0.19.0",
            "kamajiChartVersion":"26.8.6-edge",
            "asoVersion":"v2.11.0",
            "cloudProviderVersion":"v1.32.3",
            "calicoVersion":"v3.32.2",
            "controllerImage":"example.azurecr.io/controller@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "foundationDefaultsSha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        });
        let hash = hex::encode(Sha256::digest(serde_json::to_vec(&value).unwrap()));
        value["foundationSha256"] = serde_json::Value::String(hash);
        serde_json::to_string(&value).unwrap()
    }

    #[test]
    fn requires_non_secret_schema_one_provider_configuration() {
        let valid = valid();
        assert!(AzureProvider::from_config_map(&config(&valid)).is_ok());
        assert!(AzureProvider::from_config_map(&config(r#"{"schema":1}"#)).is_err());
        assert!(AzureProvider::from_config_map(&config(r#"{"schema":2}"#)).is_err());
        assert!(AzureProvider::from_config_map(&config("not-json")).is_err());
        assert!(AzureProvider::from_config_map(&ConfigMap::default()).is_err());
    }

    #[test]
    fn supports_only_azure_specs() {
        let provider = AzureProvider::from_config_map(&config(&valid())).unwrap();
        assert!(provider.supports(&TenantProviderSpec::Azure {
            pod_cidr: "10.0.0.0/16".into(),
            service_cidr: "10.1.0.0/16".into(),
        }));
        assert!(!provider.supports(&TenantProviderSpec::Local { databases: 1 }));
    }

    #[tokio::test]
    async fn reports_phase_two_dependency_without_acquiring_lifecycle_state() {
        let provider = AzureProvider::from_config_map(&config(&valid())).unwrap();
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
        assert!(error.to_string().contains("pending Phase 3"));
        assert!(tenant.metadata.finalizers.is_none());
    }
}
