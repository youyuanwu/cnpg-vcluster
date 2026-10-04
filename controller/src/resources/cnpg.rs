use std::collections::BTreeMap;

use k8s_openapi::api::storage::v1::StorageClass;
use kube::core::DynamicObject;

use super::{
    BuildError, Context, decode_manifest, manifest::replace_object_strings, mark_tenant_object,
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
