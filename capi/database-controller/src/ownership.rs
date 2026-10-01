use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use sha2::{Digest, Sha256};

use crate::api::{ResourceIdentity, TenantDatabaseCatalog, valid_logical_uid};

pub const CATALOG_LABEL: &str = "tenancy.cnpg-vcluster.io/catalog-uid";
pub const ENTRY_LABEL: &str = "tenancy.cnpg-vcluster.io/database-uid";
pub const TENANT_LABEL: &str = "tenancy.cnpg-vcluster.io/tenant-uid";

#[derive(Debug, thiserror::Error)]
#[error("database resource identity is foreign, missing, or replaced")]
pub struct Foreign;

pub fn names(catalog_uid: &str, entry_uid: &str) -> Result<(String, String), Foreign> {
    if !valid_logical_uid(catalog_uid) || !valid_logical_uid(entry_uid) {
        return Err(Foreign);
    }
    let digest = Sha256::digest(format!("{catalog_uid}/{entry_uid}"));
    let hex = format!("{digest:x}");
    Ok((format!("db-{}", &hex[..48]), format!("pg-{}", &hex[..47])))
}

pub fn labels(
    catalog: &TenantDatabaseCatalog,
    entry_uid: &str,
) -> Result<std::collections::BTreeMap<String, String>, Foreign> {
    Ok([
        (
            CATALOG_LABEL.into(),
            catalog
                .metadata
                .uid
                .clone()
                .filter(|v| valid_logical_uid(v))
                .ok_or(Foreign)?,
        ),
        (ENTRY_LABEL.into(), entry_uid.into()),
        (TENANT_LABEL.into(), catalog.spec.tenant_uid.clone()),
    ]
    .into())
}

pub fn check(
    meta: &ObjectMeta,
    catalog: &TenantDatabaseCatalog,
    entry_uid: &str,
    expected: Option<&ResourceIdentity>,
) -> Result<ResourceIdentity, Foreign> {
    let uid = meta
        .uid
        .as_deref()
        .filter(|uid| !uid.is_empty())
        .ok_or(Foreign)?;
    let name = meta.name.as_deref().ok_or(Foreign)?;
    if !labels(catalog, entry_uid)?
        .iter()
        .all(|(key, value)| meta.labels.as_ref().and_then(|labels| labels.get(key)) == Some(value))
        || expected.is_some_and(|id| id.name != name || id.uid != uid)
    {
        return Err(Foreign);
    }
    Ok(ResourceIdentity {
        name: name.into(),
        uid: uid.into(),
    })
}

#[cfg(test)]
mod tests {
    use super::names;

    #[test]
    fn generated_cluster_name_respects_cnpg_fifty_character_limit() {
        let first = names(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        .unwrap();
        let second = names(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        )
        .unwrap();
        assert_eq!(first.0.len(), 51);
        assert_eq!(first.1.len(), 50);
        assert_ne!(first, second);
    }
}
