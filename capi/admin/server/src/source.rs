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
        if !definition.namespaced
            && let Some(expected_name) = definition.expected_name(&tenant_name)
        {
            return match api.get_opt(&expected_name).await {
                Ok(Some(mut object)) => {
                    object.types.get_or_insert_with(|| kube::core::TypeMeta {
                        api_version: definition.api_version.into(),
                        kind: definition.kind.into(),
                    });
                    Ok(vec![object])
                }
                Ok(None) => Ok(Vec::new()),
                Err(error) => {
                    tracing::warn!(
                        api_version = definition.api_version,
                        kind = definition.kind,
                        name = expected_name,
                        error = %tenant_controller::sanitize::text(&error.to_string()),
                        "management resource read failed"
                    );
                    Err(SourceError::KubernetesUnavailable)
                }
            };
        }
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

#[cfg(test)]
mod tests {
    use std::{
        convert::Infallible,
        sync::{Arc, Mutex},
    };

    use axum::http::{Request, Response};
    use kube::client::Body;
    use serde_json::json;
    use tower::service_fn;

    use super::*;

    #[tokio::test]
    async fn deterministic_cluster_resource_uses_exact_get() {
        let calls = Arc::new(Mutex::new(Vec::<(String, String)>::new()));
        let service_calls = calls.clone();
        let client = Client::new(
            service_fn(move |request: Request<Body>| {
                let calls = service_calls.clone();
                async move {
                    let path = request.uri().path().to_owned();
                    let query = request.uri().query().unwrap_or_default().to_owned();
                    calls
                        .lock()
                        .expect("call log lock")
                        .push((path.clone(), query));
                    let (status, body) = if path == "/api/v1/namespaces/tenant-a" {
                        (
                            200,
                            json!({
                                "metadata": {
                                    "name": "tenant-a",
                                    "uid": "namespace-uid"
                                }
                            }),
                        )
                    } else {
                        (
                            404,
                            json!({
                                "apiVersion": "v1",
                                "kind": "Status",
                                "status": "Failure",
                                "code": 404,
                                "reason": "NotFound",
                                "message": "not found"
                            }),
                        )
                    };
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "application/json")
                            .body(Body::from(
                                serde_json::to_vec(&body).expect("response body"),
                            ))
                            .expect("response"),
                    )
                }
            }),
            "default",
        );
        let source = KubeDataSource::new(client);

        let resources = source
            .list_management_resources(ProviderMode::Local, "tenant-a")
            .await
            .expect("resource scan");

        let namespace = resources
            .iter()
            .find(|resource| resource.metadata.uid.as_deref() == Some("namespace-uid"))
            .expect("exact namespace");
        let types = namespace.types.as_ref().expect("restored type metadata");
        assert_eq!(types.api_version, "v1");
        assert_eq!(types.kind, "Namespace");

        let calls = calls.lock().expect("call log lock");
        assert!(
            calls
                .iter()
                .any(|(path, _)| path == "/api/v1/namespaces/tenant-a")
        );
        assert!(
            calls.iter().all(|(path, _)| path != "/api/v1/namespaces"),
            "Namespace inventory must not list every cluster Namespace"
        );
    }
}
