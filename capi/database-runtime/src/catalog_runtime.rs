use std::collections::BTreeMap;

use k8s_openapi::{
    api::{
        core::v1::Namespace,
        rbac::v1::{PolicyRule, Role, RoleBinding, RoleRef, Subject},
    },
    apimachinery::pkg::apis::meta::v1::ObjectMeta,
};
use kube::{Api, Client, api::PostParams};

pub const CREDENTIAL_ROLE: &str = "tenant-database-credentials";
pub const TENANT_UID: &str = "tenancy.cnpg-vcluster.io/tenant-uid";

#[derive(Debug, thiserror::Error)]
pub enum CatalogRuntimeError {
    #[error("database runtime resource is missing, replaced, or has foreign ownership")]
    Identity,
    #[error(transparent)]
    Api(#[from] kube::Error),
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
                Err(error) => return Err(error.into()),
            }
        }
    };
    owned(&namespace.metadata, name, tenant_uid, expected)
}

pub async fn ensure_credentials(
    client: Client,
    tenant_name: &str,
    tenant_uid: &str,
    azure: bool,
) -> Result<(), CatalogRuntimeError> {
    let secret = if azure {
        format!("{tenant_name}-admin-kubeconfig")
    } else {
        format!("{tenant_name}-kubeconfig")
    };
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
