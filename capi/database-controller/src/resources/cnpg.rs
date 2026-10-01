use std::path::Path;

use kube::core::DynamicObject;
use serde_json::json;

use crate::{
    api::TenantDatabaseCatalog, ownership, reconcile::ObserveError, tenant_access::LocalAccess,
};

const STORAGE_CLASS: &str = "capi-hostpath";

pub fn volume(
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster: &str,
    ordinal: i32,
    access: &LocalAccess,
    path: &str,
) -> Result<DynamicObject, ObserveError> {
    let relative = Path::new(path)
        .strip_prefix(&access.root)
        .map_err(|_| ObserveError::Foreign)?;
    let host_path = access.worker_root.join(relative);
    let name = format!("pv-{cluster}-{ordinal}");
    serde_json::from_value(json!({
        "apiVersion":"v1","kind":"PersistentVolume",
        "metadata":{"name":name,"labels":ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?},
        "spec":{
            "capacity":{"storage":"1Gi"},"accessModes":["ReadWriteOnce"],"persistentVolumeReclaimPolicy":"Retain",
            "storageClassName":STORAGE_CLASS,"volumeMode":"Filesystem","hostPath":{"path":host_path,"type":"Directory"},
            "claimRef":{"namespace":namespace,"name":format!("{cluster}-{ordinal}")}
        }
    })).map_err(|_| ObserveError::Identity)
}

pub fn claim(
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster: &str,
    ordinal: i32,
) -> Result<DynamicObject, ObserveError> {
    let mut labels = ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?;
    labels.extend([
        ("cnpg.io/cluster".into(), cluster.into()),
        (
            "cnpg.io/instanceName".into(),
            format!("{cluster}-{ordinal}"),
        ),
        ("cnpg.io/pvcRole".into(), "PG_DATA".into()),
        (
            "app.kubernetes.io/managed-by".into(),
            "cloudnative-pg".into(),
        ),
        ("app.kubernetes.io/name".into(), "postgresql".into()),
        ("app.kubernetes.io/component".into(), "database".into()),
    ]);
    serde_json::from_value(json!({
        "apiVersion":"v1","kind":"PersistentVolumeClaim",
        "metadata":{"name":format!("{cluster}-{ordinal}"),"namespace":namespace,"labels":labels},
        "spec":{"accessModes":["ReadWriteOnce"],"storageClassName":STORAGE_CLASS,"volumeMode":"Filesystem",
            "volumeName":format!("pv-{cluster}-{ordinal}"),"resources":{"requests":{"storage":"1Gi"}}}
    })).map_err(|_| ObserveError::Identity)
}

pub fn cluster(
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    name: &str,
    instances: i32,
    image: &str,
) -> Result<DynamicObject, ObserveError> {
    serde_json::from_value(json!({
        "apiVersion":"postgresql.cnpg.io/v1","kind":"Cluster",
        "metadata":{"name":name,"namespace":namespace,"labels":ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?},
        "spec":{"instances":instances,"imageName":image,"enableSuperuserAccess":true,
            "postgresUID":26,"postgresGID":26,
            "inheritedMetadata":{"labels":ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?},
            "storage":{"size":"1Gi","storageClass":STORAGE_CLASS,"pvcTemplate":{
                "storageClassName":STORAGE_CLASS,"accessModes":["ReadWriteOnce"],"volumeMode":"Filesystem"
            }}}
    })).map_err(|_| ObserveError::Identity)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{CatalogEntry, TenantDatabaseCatalogSpec};
    use std::{collections::BTreeSet, path::PathBuf};

    const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const ENTRIES: [&str; 3] = [
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    ];

    #[tokio::test]
    async fn three_clusters_have_nine_separate_gib_ordinals() {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant-uid".into(),
                closed: false,
                entries: ENTRIES
                    .iter()
                    .enumerate()
                    .map(|(index, uid)| {
                        (
                            uid.to_string(),
                            CatalogEntry {
                                name: format!("name-{index}"),
                                instances: 3,
                                deleting: false,
                            },
                        )
                    })
                    .collect(),
            },
        );
        catalog.metadata.uid = Some(CATALOG.into());
        let access = LocalAccess {
            client: {
                use std::convert::Infallible;
                use tower::service_fn;
                kube::Client::new(
                    service_fn(|_| async {
                        Ok::<_, Infallible>(axum::http::Response::new(axum::body::Body::empty()))
                    }),
                    "default",
                )
            },
            root: PathBuf::from("/var/lib/docker/volumes/tenant-storage/_data"),
            worker_root: PathBuf::from("/mnt/tenant-storage"),
            image: "postgres@sha256:abc".into(),
        };
        let mut namespaces = BTreeSet::new();
        let mut names = BTreeSet::new();
        let mut paths = BTreeSet::new();
        for uid in ENTRIES {
            let (namespace, cluster_name) = ownership::names(CATALOG, uid).unwrap();
            assert!(namespaces.insert(namespace.clone()));
            let cnpg = cluster(&catalog, uid, &namespace, &cluster_name, 3, &access.image).unwrap();
            assert_eq!(cnpg.data["spec"]["instances"], 3);
            assert_eq!(cnpg.data["spec"]["postgresUID"], 26);
            assert_eq!(cnpg.data["spec"]["postgresGID"], 26);
            assert_eq!(cnpg.data["spec"]["storage"]["size"], "1Gi");
            assert_eq!(
                cnpg.data["spec"]["storage"]["pvcTemplate"]["storageClassName"],
                STORAGE_CLASS
            );
            assert_eq!(
                cnpg.data["spec"]["inheritedMetadata"]["labels"][ownership::ENTRY_LABEL],
                uid
            );
            for ordinal in 1..=3 {
                let path = access
                    .root
                    .join("volumes/cnpg")
                    .join(CATALOG)
                    .join(uid)
                    .join(ordinal.to_string());
                assert!(paths.insert(path.clone()));
                let volume = volume(
                    &catalog,
                    uid,
                    &namespace,
                    &cluster_name,
                    ordinal,
                    &access,
                    path.to_str().unwrap(),
                )
                .unwrap();
                let claim = claim(&catalog, uid, &namespace, &cluster_name, ordinal).unwrap();
                assert!(names.insert(volume.metadata.name.clone().unwrap()));
                assert!(names.insert(claim.metadata.name.clone().unwrap()));
                assert_eq!(volume.data["spec"]["capacity"]["storage"], "1Gi");
                assert_eq!(
                    claim.data["spec"]["resources"]["requests"]["storage"],
                    "1Gi"
                );
                assert_eq!(
                    claim.data["spec"]["volumeName"],
                    volume.metadata.name.unwrap()
                );
                assert_eq!(
                    claim.metadata.labels.as_ref().unwrap()["cnpg.io/instanceName"],
                    format!("{cluster_name}-{ordinal}")
                );
                assert_eq!(
                    claim.metadata.labels.as_ref().unwrap()["cnpg.io/pvcRole"],
                    "PG_DATA"
                );
                assert_eq!(
                    volume.data["spec"]["hostPath"]["path"],
                    access
                        .worker_root
                        .join(path.strip_prefix(&access.root).unwrap())
                        .to_str()
                        .unwrap()
                );
            }
        }
        assert_eq!((namespaces.len(), names.len(), paths.len()), (3, 18, 9));
    }
}
