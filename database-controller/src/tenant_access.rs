use std::{collections::BTreeMap, path::PathBuf};

use k8s_openapi::api::core::v1::{ConfigMap, Namespace};
use kube::{
    Api, Client, ResourceExt,
    api::ApiResource,
    core::{DynamicObject, GroupVersionKind},
};
use tenant_controller::{
    api::{SUPPORTED_KUBERNETES_VERSION, Tenant, TenantPhase, TenantProviderSpec, spec_hash},
    docker::{BollardDockerClient, DockerClient, validate_volume},
    foundation,
    ownership::{
        FOUNDATION_ANNOTATION, Identity, SPEC_HASH_ANNOTATION, TENANT_UID_ANNOTATION,
        validate_root_ownership,
    },
    resources::canonical_exact_reference,
    tenant_client::load_tenant_client,
};

use crate::{api::TenantDatabaseCatalog, reconcile::ObserveError};

pub struct LocalAccess {
    pub client: Client,
    pub root: PathBuf,
    pub worker_root: PathBuf,
    pub image: String,
}

fn dynamic(
    client: Client,
    namespace: &str,
    group: &str,
    version: &str,
    kind: &str,
    plural: &str,
) -> Api<DynamicObject> {
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(group, version, kind));
    resource.plural = plural.into();
    Api::namespaced_with(client, namespace, &resource)
}

fn required(value: Option<&str>) -> Result<&str, ObserveError> {
    value
        .filter(|value| !value.is_empty())
        .ok_or(ObserveError::Identity)
}

pub async fn load(
    client: Client,
    catalog: &TenantDatabaseCatalog,
) -> Result<LocalAccess, ObserveError> {
    let tenant = Api::<Tenant>::all(client.clone())
        .get(&catalog.spec.tenant_name)
        .await?;
    let tenant_uid = required(tenant.metadata.uid.as_deref())?;
    let status = tenant.status.as_ref().ok_or(ObserveError::Identity)?;
    let capability = status
        .database_capability
        .as_ref()
        .ok_or(ObserveError::Identity)?;
    let catalog_ns = catalog.namespace().ok_or(ObserveError::Identity)?;
    let live_ns = Api::<Namespace>::all(client.clone())
        .get(&catalog_ns)
        .await?;
    if tenant_uid != catalog.spec.tenant_uid
        || !matches!(tenant.spec.provider, TenantProviderSpec::Local)
        || tenant
            .metadata
            .finalizers
            .as_ref()
            .is_none_or(|f| !f.contains(&"tenancy.cnpg-vcluster.io/finalizer".into()))
        || status.observed_generation != tenant.metadata.generation
        || (!catalog.spec.closed
            && (tenant.metadata.deletion_timestamp.is_some()
                || status.phase != Some(TenantPhase::Ready)
                || !capability.available))
        || capability.namespace != catalog_ns
        || capability.namespace_uid != required(live_ns.metadata.uid.as_deref())?
        || capability.catalog_uid != required(catalog.metadata.uid.as_deref())?
    {
        return Err(ObserveError::Identity);
    }
    let foundation_map = Api::<ConfigMap>::namespaced(client.clone(), "tenant-system")
        .get("tenant-foundation")
        .await?;
    let data = foundation_map.data.as_ref().ok_or(ObserveError::Identity)?;
    let raw = required(data.get("foundation.json").map(String::as_str))?;
    let hash = required(data.get("foundation.sha256").map(String::as_str))?;
    let image = serde_json::from_str::<serde_json::Value>(raw)
        .map_err(|_| ObserveError::Identity)?
        .pointer("/controllerImage")
        .and_then(serde_json::Value::as_str)
        .ok_or(ObserveError::Identity)?
        .to_owned();
    let foundation = foundation::parse_runtime(raw, hash, SUPPORTED_KUBERNETES_VERSION, &image)
        .map_err(|_| ObserveError::Identity)?;
    if status.foundation_hash() != Some(hash) {
        return Err(ObserveError::Identity);
    }
    let foundation = foundation
        .creation(status.foundation_hash())
        .map_err(|_| ObserveError::Identity)?;
    let allocation = status.allocation().ok_or(ObserveError::Identity)?;
    let slot = foundation
        .slots
        .iter()
        .find(|slot| {
            slot.slot_id == allocation.slot_id
                && slot.endpoint == allocation.endpoint
                && slot.pod_cidr == allocation.pod_cidr
                && slot.service_cidr == allocation.service_cidr
        })
        .ok_or(ObserveError::Identity)?;
    let spec_hash = spec_hash(&tenant.spec);
    let identity = Identity {
        tenant_name: &catalog.spec.tenant_name,
        tenant_uid,
        spec_hash: &spec_hash,
        foundation_hash: hash,
        ownership_label: &foundation.inputs.ownership_label,
        lab_prefix: &foundation.inputs.lab_prefix,
    };
    let namespace = catalog.spec.tenant_name.as_str();
    let cluster = dynamic(
        client.clone(),
        namespace,
        "cluster.x-k8s.io",
        "v1beta2",
        "Cluster",
        "clusters",
    )
    .get(namespace)
    .await?;
    validate_root_ownership(&cluster.metadata, identity, "cluster")
        .map_err(|_| ObserveError::Identity)?;
    if status.cluster_uid() != cluster.metadata.uid.as_deref() {
        return Err(ObserveError::Identity);
    }
    let plane = dynamic(
        client.clone(),
        namespace,
        "controlplane.cluster.x-k8s.io",
        "v1alpha2",
        "KamajiControlPlane",
        "kamajicontrolplanes",
    )
    .get(namespace)
    .await?;
    validate_root_ownership(&plane.metadata, identity, "kamaji-control-plane")
        .map_err(|_| ObserveError::Identity)?;
    if plane
        .metadata
        .owner_references
        .as_deref()
        .is_none_or(|owners| {
            owners.len() != 1
                || owners[0].uid != cluster.uid().unwrap_or_default()
                || owners[0].name != namespace
                || owners[0].kind != "Cluster"
                || owners[0].api_version != "cluster.x-k8s.io/v1beta2"
        })
    {
        return Err(ObserveError::Identity);
    }
    let (tenant_client, _) = load_tenant_client(
        client,
        &plane,
        namespace,
        namespace,
        &format!("{}:{}", slot.endpoint, foundation.inputs.api_port),
    )
    .await
    .map_err(|_| ObserveError::Identity)?;
    let volume_name = format!("{}-{}-storage", foundation.inputs.lab_prefix, namespace);
    let labels: BTreeMap<String, String> = identity
        .labels()
        .into_iter()
        .chain([
            ("cnpg-vcluster.capi/role".into(), "tenant-storage".into()),
            ("cnpg-vcluster.capi/tenant".into(), namespace.into()),
            (TENANT_UID_ANNOTATION.into(), tenant_uid.into()),
            (SPEC_HASH_ANNOTATION.into(), spec_hash.clone()),
            (FOUNDATION_ANNOTATION.into(), hash.into()),
        ])
        .collect();
    let docker =
        BollardDockerClient::connect("/var/run/docker.sock").map_err(|_| ObserveError::Identity)?;
    let volume = docker
        .inspect_volume(&volume_name)
        .await
        .map_err(|_| ObserveError::Identity)?
        .ok_or(ObserveError::Identity)?;
    validate_volume(&volume, &volume_name, &labels).map_err(|_| ObserveError::Identity)?;
    let root = PathBuf::from(&volume.mountpoint);
    if root != std::path::Path::new(&format!("/var/lib/docker/volumes/{volume_name}/_data"))
        || root.components().any(|component| {
            matches!(
                component,
                std::path::Component::ParentDir | std::path::Component::CurDir
            )
        })
    {
        return Err(ObserveError::Identity);
    }
    let worker_root = PathBuf::from(&foundation.inputs.storage_container_path);
    if !worker_root.is_absolute()
        || worker_root == std::path::Path::new("/")
        || worker_root.components().any(|component| {
            matches!(
                component,
                std::path::Component::ParentDir | std::path::Component::CurDir
            )
        })
    {
        return Err(ObserveError::Identity);
    }
    let archive = foundation
        .cache
        .image_archives
        .iter()
        .find(|archive| archive.key == "POSTGRES_IMAGE" && archive.worker)
        .ok_or(ObserveError::Identity)?;
    let image = canonical_exact_reference(archive).map_err(|_| ObserveError::Identity)?;
    Ok(LocalAccess {
        client: tenant_client,
        root,
        worker_root,
        image,
    })
}
