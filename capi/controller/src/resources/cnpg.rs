use std::collections::BTreeMap;

use k8s_openapi::{
    api::{
        core::v1::{
            HostPathVolumeSource, Namespace, ObjectReference, PersistentVolume,
            PersistentVolumeSpec,
        },
        storage::v1::StorageClass,
    },
    apimachinery::pkg::api::resource::Quantity,
};
use kube::core::DynamicObject;
use serde_json::json;

use super::{
    BuildError, Context, decode_manifest, manifest::replace_object_strings, mark_tenant_object,
    to_dynamic,
};

pub fn storage_class(context: &Context<'_>, name: &str) -> StorageClass {
    StorageClass {
        metadata: context.metadata(name, "", "storage"),
        provisioner: "kubernetes.io/no-provisioner".into(),
        volume_binding_mode: Some("Immediate".into()),
        reclaim_policy: Some("Retain".into()),
        ..Default::default()
    }
}

pub fn cnpg_operator(
    context: &Context<'_>,
    manifest: &[u8],
    tagged_image: &str,
    exact_image: &str,
) -> Result<Vec<DynamicObject>, BuildError> {
    let mut objects = decode_manifest(manifest)?;
    let mut counts = BTreeMap::new();
    for object in &mut objects {
        replace_object_strings(
            object,
            &BTreeMap::from([(tagged_image, exact_image)]),
            &mut counts,
        )?;
        mark_tenant_object(context, object, "cnpg-operator");
    }
    if counts.get(tagged_image) != Some(&2) {
        return Err(BuildError::Manifest(
            "unexpected CNPG operator image count".into(),
        ));
    }
    Ok(objects)
}

pub fn database_namespace(context: &Context<'_>) -> Namespace {
    Namespace {
        metadata: context.metadata("database", "", "cnpg"),
        ..Default::default()
    }
}

pub fn persistent_volumes(context: &Context<'_>, storage_class: &str) -> Vec<PersistentVolume> {
    (1..=context.spec.databases)
        .map(|ordinal| PersistentVolume {
            metadata: context.metadata(&format!("capi-postgres-pv-{ordinal}"), "", "cnpg"),
            spec: Some(PersistentVolumeSpec {
                capacity: Some(BTreeMap::from([("storage".into(), Quantity("1Gi".into()))])),
                access_modes: Some(vec!["ReadWriteOnce".into()]),
                persistent_volume_reclaim_policy: Some("Retain".into()),
                storage_class_name: Some(storage_class.into()),
                claim_ref: Some(ObjectReference {
                    namespace: Some("database".into()),
                    name: Some(format!("capi-postgres-{ordinal}")),
                    ..Default::default()
                }),
                host_path: Some(HostPathVolumeSource {
                    path: format!(
                        "{}/volumes/cnpg/{ordinal}",
                        context.inputs.storage_container_path
                    ),
                    type_: Some("DirectoryOrCreate".into()),
                }),
                ..Default::default()
            }),
            ..Default::default()
        })
        .collect()
}

pub fn cnpg_cluster(
    context: &Context<'_>,
    storage_class: &str,
    postgres_image: &str,
) -> DynamicObject {
    let affinity = if context.spec.databases > context.spec.workers {
        "preferred"
    } else {
        "required"
    };
    context.object("postgresql.cnpg.io/v1", "Cluster", "capi-postgres", "database", "cnpg", json!({
        "instances":context.spec.databases, "imageName":postgres_image,
        "affinity":{"enablePodAntiAffinity":true,"podAntiAffinityType":affinity,"topologyKey":"kubernetes.io/hostname"},
        "bootstrap":{"initdb":{"database":"app","owner":"app"}},
        "storage":{"size":"1Gi","storageClass":storage_class},
        "resources":{"requests":{"cpu":"100m","memory":"256Mi"},"limits":{"cpu":"1","memory":"1Gi"}}
    }))
}

pub fn cnpg_objects(
    context: &Context<'_>,
    storage_class: &str,
    postgres_image: &str,
) -> Result<Vec<DynamicObject>, BuildError> {
    let mut objects = vec![to_dynamic(&database_namespace(context))?];
    for volume in persistent_volumes(context, storage_class) {
        objects.push(to_dynamic(&volume)?);
    }
    objects.push(cnpg_cluster(context, storage_class, postgres_image));
    Ok(objects)
}
