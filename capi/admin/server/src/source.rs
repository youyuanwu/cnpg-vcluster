use std::{collections::BTreeSet, future::Future, pin::Pin, sync::Arc};

use futures::{StreamExt, stream};
use kube::{
    Api, Client,
    api::{ListParams, ObjectList},
    core::DynamicObject,
};
use tenant_admin_shared::query::ProviderMode;
use tenant_controller::{
    api::Tenant,
    management::{AZURE_MANAGEMENT_RESOURCES, MANAGEMENT_RESOURCES, ManagementResource},
};

use crate::SourceError;

const MAX_TENANTS: u32 = 500;
const MAX_RESOURCES_PER_KIND: u32 = 500;
const MAX_RESOURCES_TOTAL: usize = 2_000;
const RESOURCE_LIST_CONCURRENCY: usize = 8;

pub type SourceFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, SourceError>> + Send + 'a>>;

pub trait DataSource: Send + Sync {
    fn list_tenants(&self) -> SourceFuture<'_, Vec<Tenant>>;
    fn get_tenant(&self, name: &str) -> SourceFuture<'_, Option<Tenant>>;
    fn list_management_resources(
        &self,
        provider: ProviderMode,
        tenant_name: &str,
    ) -> SourceFuture<'_, Vec<DynamicObject>>;
    fn check_ready(&self) -> SourceFuture<'_, ()>;
}

#[derive(Clone)]
pub struct KubeDataSource {
    client: Client,
}

impl KubeDataSource {
    pub const fn new(client: Client) -> Self {
        Self { client }
    }

    async fn tenant_list(&self, limit: u32) -> Result<ObjectList<Tenant>, SourceError> {
        Api::<Tenant>::all(self.client.clone())
            .list(&ListParams::default().limit(limit))
            .await
            .map_err(|error| {
                tracing::warn!(error = %tenant_controller::sanitize::text(&error.to_string()), "Tenant list failed");
                SourceError::KubernetesUnavailable
            })
    }

    async fn list_definition(
        client: Client,
        definition: ManagementResource,
        tenant_name: Arc<str>,
    ) -> Result<Vec<DynamicObject>, SourceError> {
        let api_resource = definition.api_resource();
        let namespace = match definition.inventory_namespace {
            Some(namespace) => namespace,
            None => tenant_name.as_ref(),
        };
        let api = if definition.namespaced {
            Api::<DynamicObject>::namespaced_with(client, namespace, &api_resource)
        } else {
            Api::<DynamicObject>::all_with(client, &api_resource)
        };
        match api
            .list(&ListParams::default().limit(MAX_RESOURCES_PER_KIND + 1))
            .await
        {
            Ok(mut list) => {
                if list.items.len() > MAX_RESOURCES_PER_KIND as usize
                    || list
                        .metadata
                        .continue_
                        .as_deref()
                        .is_some_and(|value| !value.is_empty())
                {
                    return Err(SourceError::ResponseTooLarge);
                }
                for item in &mut list.items {
                    item.types.get_or_insert_with(|| kube::core::TypeMeta {
                        api_version: definition.api_version.into(),
                        kind: definition.kind.into(),
                    });
                }
                Ok(list.items)
            }
            Err(kube::Error::Api(response)) if response.code == 404 => Ok(Vec::new()),
            Err(error) => {
                tracing::warn!(
                    api_version = definition.api_version,
                    kind = definition.kind,
                    error = %tenant_controller::sanitize::text(&error.to_string()),
                    "management resource list failed"
                );
                Err(SourceError::KubernetesUnavailable)
            }
        }
    }
}

impl DataSource for KubeDataSource {
    fn list_tenants(&self) -> SourceFuture<'_, Vec<Tenant>> {
        Box::pin(async move {
            let list = self.tenant_list(MAX_TENANTS + 1).await?;
            if list.items.len() > MAX_TENANTS as usize
                || list
                    .metadata
                    .continue_
                    .as_deref()
                    .is_some_and(|value| !value.is_empty())
            {
                return Err(SourceError::ResponseTooLarge);
            }
            Ok(list.items)
        })
    }

    fn get_tenant(&self, name: &str) -> SourceFuture<'_, Option<Tenant>> {
        let name = name.to_owned();
        Box::pin(async move {
            Api::<Tenant>::all(self.client.clone())
                .get_opt(&name)
                .await
                .map_err(|error| {
                    tracing::warn!(
                        tenant = name,
                        error = %tenant_controller::sanitize::text(&error.to_string()),
                        "Tenant read failed"
                    );
                    SourceError::KubernetesUnavailable
                })
        })
    }

    fn list_management_resources(
        &self,
        provider: ProviderMode,
        tenant_name: &str,
    ) -> SourceFuture<'_, Vec<DynamicObject>> {
        let client = self.client.clone();
        let tenant_name: Arc<str> = Arc::from(tenant_name);
        Box::pin(async move {
            let catalog = match provider {
                ProviderMode::Local => MANAGEMENT_RESOURCES,
                ProviderMode::Azure => AZURE_MANAGEMENT_RESOURCES,
            };
            let mut unique = BTreeSet::new();
            let definitions: Vec<_> = catalog
                .iter()
                .copied()
                .filter(|definition| definition.kind != "Secret")
                .filter(|definition| {
                    unique.insert((
                        definition.api_version,
                        definition.plural,
                        definition.namespaced,
                        definition.inventory_namespace,
                    ))
                })
                .collect();
            let chunks = stream::iter(definitions.into_iter().map(|definition| {
                Self::list_definition(client.clone(), definition, tenant_name.clone())
            }))
            .buffer_unordered(RESOURCE_LIST_CONCURRENCY)
            .collect::<Vec<_>>()
            .await;
            let mut resources = Vec::new();
            for chunk in chunks {
                resources.extend(chunk?);
                if resources.len() > MAX_RESOURCES_TOTAL {
                    return Err(SourceError::ResponseTooLarge);
                }
            }
            Ok(resources)
        })
    }

    fn check_ready(&self) -> SourceFuture<'_, ()> {
        Box::pin(async move {
            self.tenant_list(1).await?;
            Ok(())
        })
    }
}
