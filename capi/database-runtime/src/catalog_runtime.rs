use std::collections::BTreeMap;

use k8s_openapi::{
    api::{
        core::v1::Namespace,
        rbac::v1::{PolicyRule, Role, RoleBinding, RoleRef, Subject},
    },
    apimachinery::pkg::apis::meta::v1::{ObjectMeta, OwnerReference},
};
use kube::{
    Api, Client, ResourceExt,
    api::{ApiResource, DeleteParams, ListParams, Patch, PatchParams, PostParams},
    core::{DynamicObject, GroupVersionKind},
};
use serde_json::{Value, json};

pub const CREDENTIAL_ROLE: &str = "tenant-database-credentials";
pub const TENANT_UID: &str = "tenancy.cnpg-vcluster.io/tenant-uid";
pub const CATALOG_FINALIZER: &str = "tenancy.cnpg-vcluster.io/database-catalog-finalizer";

#[derive(Debug, thiserror::Error)]
pub enum CatalogRuntimeError {
    #[error("database runtime resource is missing, replaced, or has foreign ownership")]
    Identity,
    #[error("catalog creation or deletion has an unresolved outcome")]
    Pending,
    #[error("resource CREATE was definitely rejected by the API server")]
    Rejected,
    #[error(transparent)]
    Api(#[from] kube::Error),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CatalogIdentity {
    pub namespace_uid: String,
    pub catalog_uid: String,
    pub storage_namespace_uid: Option<String>,
}

fn catalogs(client: Client, namespace: &str) -> Api<DynamicObject> {
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
        "tenancy.cnpg-vcluster.io",
        "v1alpha1",
        "TenantDatabaseCatalog",
    ));
    resource.plural = "tenantdatabasecatalogs".into();
    Api::namespaced_with(client, namespace, &resource)
}

fn catalog_identity(
    catalog: &DynamicObject,
    tenant_name: &str,
    tenant_uid: &str,
    expected_uid: Option<&str>,
    require_open: bool,
) -> Result<String, CatalogRuntimeError> {
    let uid = catalog
        .uid()
        .filter(|uid| !uid.is_empty())
        .ok_or(CatalogRuntimeError::Identity)?;
    let namespace = database_namespace(tenant_name);
    let owners = catalog
        .metadata
        .owner_references
        .as_deref()
        .ok_or(CatalogRuntimeError::Identity)?;
    if catalog.name_any() != tenant_name
        || catalog.namespace().as_deref() != Some(namespace.as_str())
        || catalog.metadata.deletion_timestamp.is_some()
        || expected_uid.is_some_and(|expected| expected != uid)
        || owners.len() != 1
        || owners[0].api_version != "tenancy.cnpg-vcluster.io/v1alpha4"
        || owners[0].kind != "Tenant"
        || owners[0].name != tenant_name
        || owners[0].uid != tenant_uid
        || catalog
            .metadata
            .finalizers
            .as_deref()
            .is_none_or(|finalizers| {
                !finalizers
                    .iter()
                    .any(|finalizer| finalizer == CATALOG_FINALIZER)
            })
        || catalog
            .data
            .pointer("/spec/tenantName")
            .and_then(Value::as_str)
            != Some(tenant_name)
        || catalog
            .data
            .pointer("/spec/tenantUID")
            .and_then(Value::as_str)
            != Some(tenant_uid)
        || catalog
            .data
            .pointer("/spec/closed")
            .and_then(Value::as_bool)
            .is_none()
        || !catalog
            .data
            .pointer("/spec/entries")
            .is_some_and(Value::is_object)
        || (require_open && catalog.data.pointer("/spec/closed") != Some(&json!(false)))
    {
        return Err(CatalogRuntimeError::Identity);
    }
    Ok(uid)
}

pub async fn ensure_catalog(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    expected_namespace_uid: Option<&str>,
    expected_catalog_uid: Option<&str>,
    expected_storage_uid: Option<&str>,
    azure: bool,
) -> Result<CatalogIdentity, CatalogRuntimeError> {
    let namespace = database_namespace(tenant_name);
    let namespace_uid = ensure_namespace(
        client.clone(),
        &namespace,
        tenant_uid,
        expected_namespace_uid,
    )
    .await?;
    let storage_namespace_uid = if azure {
        Some(
            ensure_namespace(
                client.clone(),
                &storage_namespace(tenant_name),
                tenant_uid,
                expected_storage_uid,
            )
            .await?,
        )
    } else {
        None
    };
    let api = catalogs(client, &namespace);
    let catalog = match api.get_opt(tenant_name).await? {
        Some(catalog) => catalog,
        None if expected_catalog_uid.is_some() => return Err(CatalogRuntimeError::Identity),
        None => {
            let mut desired = DynamicObject::new(
                tenant_name,
                &ApiResource::from_gvk(&GroupVersionKind::gvk(
                    "tenancy.cnpg-vcluster.io",
                    "v1alpha1",
                    "TenantDatabaseCatalog",
                )),
            );
            desired.metadata.namespace = Some(namespace.clone());
            desired.metadata.finalizers = Some(vec![CATALOG_FINALIZER.into()]);
            desired.metadata.owner_references = Some(vec![OwnerReference {
                api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
                kind: "Tenant".into(),
                name: tenant_name.into(),
                uid: tenant_uid.into(),
                ..Default::default()
            }]);
            desired.data = json!({"spec":{
                "tenantName":tenant_name,"tenantUID":tenant_uid,"closed":false,"entries":{}
            }});
            match api.create(&PostParams::default(), &desired).await {
                Ok(catalog) => catalog,
                Err(kube::Error::Api(error)) if error.code == 409 => api.get(tenant_name).await?,
                Err(kube::Error::Api(error))
                    if matches!(error.code, 400 | 403 | 404 | 405 | 422) =>
                {
                    return Err(CatalogRuntimeError::Rejected);
                }
                Err(error) => return Err(error.into()),
            }
        }
    };
    let catalog_uid = catalog_identity(
        &catalog,
        tenant_name,
        tenant_uid,
        expected_catalog_uid,
        true,
    )?;
    Ok(CatalogIdentity {
        namespace_uid,
        catalog_uid,
        storage_namespace_uid,
    })
}

pub async fn observe_catalog(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    expected: Option<&CatalogIdentity>,
) -> Result<Option<CatalogIdentity>, CatalogRuntimeError> {
    let namespace = database_namespace(tenant_name);
    let Some(catalog) = catalogs(client.clone(), &namespace)
        .get_opt(tenant_name)
        .await?
    else {
        return Ok(None);
    };
    let catalog_uid = catalog_identity(
        &catalog,
        tenant_name,
        tenant_uid,
        expected.map(|identity| identity.catalog_uid.as_str()),
        false,
    )?;
    let namespace_object = Api::<Namespace>::all(client.clone())
        .get(&namespace)
        .await?;
    let namespace_uid = owned(
        &namespace_object.metadata,
        &namespace,
        tenant_uid,
        expected.map(|identity| identity.namespace_uid.as_str()),
    )?;
    let storage_namespace_uid = if expected
        .and_then(|identity| identity.storage_namespace_uid.as_ref())
        .is_some()
        || Api::<Namespace>::all(client.clone())
            .get_opt(&storage_namespace(tenant_name))
            .await?
            .is_some()
    {
        let name = storage_namespace(tenant_name);
        let object = Api::<Namespace>::all(client).get(&name).await?;
        Some(owned(
            &object.metadata,
            &name,
            tenant_uid,
            expected.and_then(|identity| identity.storage_namespace_uid.as_deref()),
        )?)
    } else {
        None
    };
    Ok(Some(CatalogIdentity {
        namespace_uid,
        catalog_uid,
        storage_namespace_uid,
    }))
}

async fn delete_namespace(
    client: Client,
    name: &str,
    tenant_uid: &str,
    recorded_uid: &str,
) -> Result<bool, CatalogRuntimeError> {
    let api = Api::<Namespace>::all(client);
    let Some(namespace) = api.get_opt(name).await? else {
        return Ok(true);
    };
    if namespace.metadata.deletion_timestamp.is_some()
        && namespace.uid().as_deref() == Some(recorded_uid)
    {
        return Ok(false);
    }
    owned(&namespace.metadata, name, tenant_uid, Some(recorded_uid))?;
    api.delete(
        name,
        &DeleteParams {
            preconditions: Some(kube::api::Preconditions {
                uid: Some(recorded_uid.into()),
                resource_version: namespace.resource_version(),
            }),
            ..Default::default()
        },
    )
    .await?;
    Ok(false)
}

pub async fn drain_catalog(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    recorded: Option<&CatalogIdentity>,
) -> Result<bool, CatalogRuntimeError> {
    let namespace = database_namespace(tenant_name);
    let storage = storage_namespace(tenant_name);
    let api = catalogs(client.clone(), &namespace);
    if recorded.is_none_or(|identity| identity.catalog_uid.is_empty()) {
        if api.get_opt(tenant_name).await?.is_some() {
            return Err(CatalogRuntimeError::Identity);
        }
        let namespaces = Api::<Namespace>::all(client.clone());
        let namespace_uid = namespaces
            .get_opt(&namespace)
            .await?
            .map(|object| {
                owned(
                    &object.metadata,
                    &namespace,
                    tenant_uid,
                    recorded
                        .map(|identity| identity.namespace_uid.as_str())
                        .filter(|uid| !uid.is_empty()),
                )
            })
            .transpose()?;
        let storage_namespace_uid = namespaces
            .get_opt(&storage)
            .await?
            .map(|object| {
                owned(
                    &object.metadata,
                    &storage,
                    tenant_uid,
                    recorded.and_then(|identity| identity.storage_namespace_uid.as_deref()),
                )
            })
            .transpose()?;
        let observed = CatalogIdentity {
            namespace_uid: recorded
                .map(|identity| identity.namespace_uid.clone())
                .filter(|uid| !uid.is_empty())
                .or(namespace_uid)
                .unwrap_or_default(),
            catalog_uid: String::new(),
            storage_namespace_uid: recorded
                .and_then(|identity| identity.storage_namespace_uid.clone())
                .or(storage_namespace_uid),
        };
        return drain_catalog_without_catalog(client, tenant_name, tenant_uid, &observed).await;
    }
    let recorded = recorded.expect("checked above");
    if let Some(catalog) = api.get_opt(tenant_name).await? {
        if catalog.metadata.deletion_timestamp.is_some()
            && catalog.uid().as_deref() == Some(recorded.catalog_uid.as_str())
        {
            return Ok(false);
        }
        let catalog_uid = catalog_identity(
            &catalog,
            tenant_name,
            tenant_uid,
            Some(&recorded.catalog_uid),
            false,
        )?;
        let version = catalog
            .resource_version()
            .ok_or(CatalogRuntimeError::Identity)?;
        let entries = catalog
            .data
            .pointer("/spec/entries")
            .and_then(Value::as_object)
            .ok_or(CatalogRuntimeError::Identity)?;
        if catalog.data.pointer("/spec/closed") != Some(&json!(true))
            || entries
                .values()
                .any(|entry| entry.pointer("/deleting") != Some(&json!(true)))
        {
            let mut deleting = entries.clone();
            for entry in deleting.values_mut() {
                let Some(object) = entry.as_object_mut() else {
                    return Err(CatalogRuntimeError::Identity);
                };
                object.insert("deleting".into(), json!(true));
            }
            api.patch(
                tenant_name,
                &PatchParams::default(),
                &Patch::Merge(&json!({
                    "metadata":{"uid":catalog_uid,"resourceVersion":version},
                    "spec":{"closed":true,"entries":deleting}
                })),
            )
            .await?;
            return Ok(false);
        }
        if !entries.is_empty()
            || catalog
                .data
                .pointer("/status/entries")
                .and_then(Value::as_object)
                .is_some_and(|status| !status.is_empty())
        {
            return Ok(false);
        }
        let updated = api
            .patch(
                tenant_name,
                &PatchParams::default(),
                &Patch::Merge(&json!({
                    "metadata":{"uid":catalog_uid,"resourceVersion":version,"finalizers":null}
                })),
            )
            .await?;
        if updated.uid().as_deref() != Some(catalog_uid.as_str()) {
            return Err(CatalogRuntimeError::Identity);
        }
        api.delete(
            tenant_name,
            &DeleteParams {
                preconditions: Some(kube::api::Preconditions {
                    uid: Some(catalog_uid),
                    resource_version: updated.resource_version(),
                }),
                ..Default::default()
            },
        )
        .await?;
        return Ok(false);
    }
    drain_catalog_without_catalog(client, tenant_name, tenant_uid, recorded).await
}

async fn drain_catalog_without_catalog(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    recorded: &CatalogIdentity,
) -> Result<bool, CatalogRuntimeError> {
    let namespace = database_namespace(tenant_name);
    let storage = storage_namespace(tenant_name);
    let roles = Api::<Role>::namespaced(client.clone(), tenant_name);
    let bindings = Api::<RoleBinding>::namespaced(client.clone(), tenant_name);
    let role = roles.get_opt(CREDENTIAL_ROLE).await?;
    let binding = bindings.get_opt(CREDENTIAL_ROLE).await?;
    for metadata in [
        role.as_ref().map(|role| &role.metadata),
        binding.as_ref().map(|binding| &binding.metadata),
    ]
    .into_iter()
    .flatten()
    {
        owned(metadata, CREDENTIAL_ROLE, tenant_uid, None)?;
    }
    if let Some(binding) = binding {
        bindings
            .delete(
                CREDENTIAL_ROLE,
                &credential_delete_params(&binding.metadata)?,
            )
            .await?;
        return Ok(false);
    }
    if let Some(role) = role {
        roles
            .delete(CREDENTIAL_ROLE, &credential_delete_params(&role.metadata)?)
            .await?;
        return Ok(false);
    }
    if !recorded.namespace_uid.is_empty()
        && !delete_namespace(
            client.clone(),
            &namespace,
            tenant_uid,
            &recorded.namespace_uid,
        )
        .await?
    {
        return Ok(false);
    }

    if let Some(uid) = recorded.storage_namespace_uid.as_deref() {
        let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
            "compute.azure.com",
            "v1api20240302",
            "Disk",
        ));
        resource.plural = "disks".into();
        if !Api::<DynamicObject>::namespaced_with(client.clone(), &storage, &resource)
            .list(&ListParams::default().limit(1))
            .await?
            .items
            .is_empty()
        {
            return Ok(false);
        }
        if !delete_namespace(client, &storage, tenant_uid, uid).await? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn credential_delete_params(metadata: &ObjectMeta) -> Result<DeleteParams, CatalogRuntimeError> {
    let uid = metadata
        .uid
        .as_ref()
        .filter(|value| !value.is_empty())
        .ok_or(CatalogRuntimeError::Identity)?;
    let version = metadata
        .resource_version
        .as_ref()
        .filter(|value| !value.is_empty())
        .ok_or(CatalogRuntimeError::Identity)?;
    Ok(DeleteParams {
        preconditions: Some(kube::api::Preconditions {
            uid: Some(uid.clone()),
            resource_version: Some(version.clone()),
        }),
        ..Default::default()
    })
}

pub fn database_namespace(tenant_name: &str) -> String {
    format!("tenant-db-{tenant_name}")
}

pub fn storage_namespace(tenant_name: &str) -> String {
    format!("tenant-db-storage-{tenant_name}")
}

fn markers(name: &str, tenant_uid: &str) -> ObjectMeta {
    ObjectMeta {
        name: Some(name.into()),
        labels: Some(BTreeMap::from([(TENANT_UID.into(), tenant_uid.into())])),
        ..Default::default()
    }
}

fn owned(
    metadata: &ObjectMeta,
    name: &str,
    tenant_uid: &str,
    expected: Option<&str>,
) -> Result<String, CatalogRuntimeError> {
    let uid = metadata
        .uid
        .as_deref()
        .filter(|uid| !uid.is_empty())
        .ok_or(CatalogRuntimeError::Identity)?;
    if metadata.name.as_deref() != Some(name)
        || metadata.deletion_timestamp.is_some()
        || metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(TENANT_UID))
            .map(String::as_str)
            != Some(tenant_uid)
        || metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || expected.is_some_and(|expected| expected != uid)
    {
        return Err(CatalogRuntimeError::Identity);
    }
    Ok(uid.into())
}

pub async fn ensure_namespace(
    client: Client,
    name: &str,
    tenant_uid: &str,
    expected: Option<&str>,
) -> Result<String, CatalogRuntimeError> {
    let api = Api::<Namespace>::all(client);
    let namespace = match api.get_opt(name).await? {
        Some(namespace) => namespace,
        None if expected.is_some() => return Err(CatalogRuntimeError::Identity),
        None => {
            let desired = Namespace {
                metadata: markers(name, tenant_uid),
                ..Default::default()
            };
            match api.create(&PostParams::default(), &desired).await {
                Ok(namespace) => namespace,
                Err(kube::Error::Api(error)) if error.code == 409 => api.get(name).await?,
                Err(kube::Error::Api(error))
                    if matches!(error.code, 400 | 403 | 404 | 405 | 422) =>
                {
                    return Err(CatalogRuntimeError::Rejected);
                }
                Err(error) => return Err(error.into()),
            }
        }
    };
    owned(&namespace.metadata, name, tenant_uid, expected)
}

pub async fn observe_namespace(
    client: Client,
    name: &str,
    tenant_uid: &str,
    expected: Option<&str>,
) -> Result<Option<String>, CatalogRuntimeError> {
    Api::<Namespace>::all(client)
        .get_opt(name)
        .await?
        .map(|namespace| owned(&namespace.metadata, name, tenant_uid, expected))
        .transpose()
}

pub async fn ensure_credentials(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    _azure: bool,
) -> Result<(), CatalogRuntimeError> {
    let secret = format!("{tenant_name}-kubeconfig");
    let roles = Api::<Role>::namespaced(client.clone(), tenant_name);
    let desired = Role {
        metadata: ObjectMeta {
            namespace: Some(tenant_name.into()),
            ..markers(CREDENTIAL_ROLE, tenant_uid)
        },
        rules: Some(vec![PolicyRule {
            api_groups: Some(vec![String::new()]),
            resources: Some(vec!["secrets".into()]),
            resource_names: Some(vec![secret]),
            verbs: vec!["get".into()],
            ..Default::default()
        }]),
    };
    let role = match roles.get_opt(CREDENTIAL_ROLE).await? {
        Some(role) => role,
        None => match roles.create(&PostParams::default(), &desired).await {
            Ok(role) => role,
            Err(kube::Error::Api(error)) if error.code == 409 => roles.get(CREDENTIAL_ROLE).await?,
            Err(error) => return Err(error.into()),
        },
    };
    owned(&role.metadata, CREDENTIAL_ROLE, tenant_uid, None)?;
    if role.metadata.namespace.as_deref() != Some(tenant_name) || role.rules != desired.rules {
        return Err(CatalogRuntimeError::Identity);
    }
    let bindings = Api::<RoleBinding>::namespaced(client, tenant_name);
    let binding = RoleBinding {
        metadata: desired.metadata,
        role_ref: RoleRef {
            api_group: Some("rbac.authorization.k8s.io".into()),
            kind: "Role".into(),
            name: CREDENTIAL_ROLE.into(),
        },
        subjects: Some(
            ["tenant-admin", "database-controller"]
                .map(|name| Subject {
                    kind: "ServiceAccount".into(),
                    name: name.into(),
                    namespace: Some("tenant-system".into()),
                    ..Default::default()
                })
                .to_vec(),
        ),
    };
    let current = match bindings.get_opt(CREDENTIAL_ROLE).await? {
        Some(current) => current,
        None => match bindings.create(&PostParams::default(), &binding).await {
            Ok(current) => current,
            Err(kube::Error::Api(error)) if error.code == 409 => {
                bindings.get(CREDENTIAL_ROLE).await?
            }
            Err(error) => return Err(error.into()),
        },
    };
    owned(&current.metadata, CREDENTIAL_ROLE, tenant_uid, None)?;
    if current.metadata.namespace.as_deref() != Some(tenant_name)
        || current.role_ref != binding.role_ref
        || current.subjects != binding.subjects
    {
        return Err(CatalogRuntimeError::Identity);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn helpers_keep_tenant_scoped_names_and_exact_ownership() {
        assert_eq!(database_namespace("tenant-a"), "tenant-db-tenant-a");
        assert_eq!(storage_namespace("tenant-a"), "tenant-db-storage-tenant-a");
        let mut meta = markers("tenant-db-tenant-a", "uid-1");
        meta.uid = Some("namespace-uid".into());
        assert_eq!(
            owned(&meta, "tenant-db-tenant-a", "uid-1", Some("namespace-uid")).unwrap(),
            "namespace-uid"
        );
        assert!(owned(&meta, "tenant-db-tenant-a", "uid-2", None).is_err());
        assert!(owned(&meta, "tenant-db-tenant-a", "uid-1", Some("replacement")).is_err());
    }
}
