use k8s_openapi::{
    api::{
        apps::v1::Deployment,
        core::v1::{Namespace, Pod, Secret},
        rbac::v1::{PolicyRule, Role, RoleBinding, RoleRef, Subject},
    },
    apimachinery::pkg::apis::meta::v1::OwnerReference,
};
use kube::core::{ApiResource, DynamicObject, GroupVersionKind};
use kube::{Api, Client, ResourceExt, api::PostParams};
use sha2::{Digest, Sha256};
use tenant_admin_shared::{
    catalog::{
        CatalogQueryRequest, CatalogQueryResponse, CatalogView, DatabaseAddRequest,
        DatabaseBlocker, DatabaseConditionView, DatabaseDeleteRequest, DatabaseView,
        FinalizationView, InstanceView, QueryIdentityView, StorageView,
    },
    query::{
        DatabaseInstanceRole, DisplayAttribute, ProviderMode, TenantProvider, TopologyEdge,
        TopologyEdgeKind, TopologyGraph, TopologyHealth, TopologyNode, TopologyNodeKind,
        TopologyNodeProvenance, TopologyOwnership, TopologySemanticKind,
    },
};
use tenant_controller::{
    api::{
        AzureProviderStatus, FINALIZER as TENANT_FINALIZER, Tenant, TenantPhase, TenantProviderSpec,
    },
    management::{AZURE_MANAGEMENT_RESOURCES, MANAGEMENT_RESOURCES},
    tenant_client::load_tenant_client_with_owner,
};
use tenant_database_controller::{
    api::{
        CatalogEntry, DATABASE_LIMIT, DatabasePhase, EntryStatus, FINALIZER as CATALOG_FINALIZER,
        TenantDatabaseCatalog, valid_logical_uid, valid_name,
    },
    ownership::{self, CATALOG_LABEL, ENTRY_LABEL, TENANT_LABEL},
};
use tenant_database_runtime::database_namespace;
use uuid::Uuid;

use super::{
    CONTROLLER_NAMESPACE, DataSource, DatabaseCredentials, KubeDataSource, QueryClusterBinding,
    QueryConnection, SourceError, database_unavailable, mutation_error, observed_at,
    pod_binding_error, query_execution_error, validate_pod_binding, validate_query_request,
};
use crate::projection::{
    is_accepted_azure_management_resource, is_accepted_local_management_resource,
};

fn unavailable() -> SourceError {
    database_unavailable("Catalog identity or readiness is unavailable", true)
}

fn not_ready() -> SourceError {
    database_unavailable("Database platform or Tenant readiness is unavailable", true)
}

fn provider_mode(tenant: &Tenant) -> ProviderMode {
    match &tenant.spec.provider {
        TenantProviderSpec::Local => ProviderMode::Local,
        TenantProviderSpec::Azure => ProviderMode::Azure,
    }
}

fn uid(value: Option<&str>) -> Result<&str, SourceError> {
    value
        .filter(|value| !value.is_empty())
        .ok_or_else(unavailable)
}

pub(super) async fn read(
    client: &Client,
    tenant: &Tenant,
) -> Result<TenantDatabaseCatalog, SourceError> {
    let name = tenant
        .metadata
        .name
        .as_deref()
        .filter(|name| valid_name(name))
        .ok_or_else(unavailable)?;
    let tenant_uid = uid(tenant.metadata.uid.as_deref())?;
    let capability = tenant
        .status
        .as_ref()
        .and_then(|status| status.database_capability.as_ref())
        .ok_or_else(unavailable)?;
    let namespace = database_namespace(name);
    let ns = Api::<Namespace>::all(client.clone())
        .get_opt(&namespace)
        .await
        .map_err(read_error)?
        .ok_or_else(unavailable)?;
    if ns.metadata.uid.as_deref() != Some(capability.namespace_uid.as_str())
        || ns.metadata.deletion_timestamp.is_some()
        || ns
            .metadata
            .owner_references
            .as_ref()
            .is_some_and(|owners| !owners.is_empty())
        || !tenant
            .finalizers()
            .iter()
            .any(|value| value == TENANT_FINALIZER)
        || ns
            .metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(TENANT_LABEL))
            .map(String::as_str)
            != Some(tenant_uid)
        || capability.namespace != namespace
        || !valid_logical_uid(&capability.catalog_uid)
    {
        return Err(SourceError::StaleIdentity);
    }
    let catalog = Api::<TenantDatabaseCatalog>::namespaced(client.clone(), &namespace)
        .get_opt(name)
        .await
        .map_err(read_error)?
        .ok_or_else(unavailable)?;
    if catalog.metadata.uid.as_deref() != Some(&capability.catalog_uid)
        || catalog.metadata.namespace.as_deref() != Some(namespace.as_str())
        || catalog.metadata.deletion_timestamp.is_some()
        || catalog
            .metadata
            .finalizers
            .as_ref()
            .is_none_or(|finalizers| !finalizers.iter().any(|value| value == CATALOG_FINALIZER))
        || catalog.spec.tenant_name != name
        || catalog.spec.tenant_uid != tenant_uid
        || catalog.metadata.owner_references.as_deref()
            != Some(
                &[OwnerReference {
                    api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
                    kind: "Tenant".into(),
                    name: name.into(),
                    uid: tenant_uid.into(),
                    ..Default::default()
                }][..],
            )
        || catalog
            .metadata
            .resource_version
            .as_deref()
            .is_none_or(str::is_empty)
        || tenant_database_controller::api::validate_spec(&catalog.spec).is_err()
    {
        return Err(SourceError::StaleIdentity);
    }
    Ok(catalog)
}

fn read_error(error: kube::Error) -> SourceError {
    tracing::warn!(
        status = super::database_error_status(&error),
        "catalog or prerequisite read failed"
    );
    SourceError::KubernetesUnavailable
}

fn ready(tenant: &Tenant) -> Result<(), SourceError> {
    let status = tenant.status.as_ref().ok_or_else(not_ready)?;
    if tenant.metadata.deletion_timestamp.is_some()
        || !tenant
            .finalizers()
            .iter()
            .any(|value| value == TENANT_FINALIZER)
        || status.observed_generation != tenant.metadata.generation
        || status.phase != Some(TenantPhase::Ready)
        || !status
            .database_capability
            .as_ref()
            .is_some_and(|capability| capability.available)
        || !status.conditions.iter().any(|condition| {
            condition.type_ == "Ready"
                && condition.status == "True"
                && condition.observed_generation == tenant.metadata.generation
        })
    {
        return Err(not_ready());
    }
    Ok(())
}

pub(super) fn capability_available(tenant: &Tenant) -> bool {
    tenant
        .status
        .as_ref()
        .and_then(|status| status.database_capability.as_ref())
        .is_some_and(|capability| capability.available)
}

async fn live_tenant(source: &KubeDataSource, expected: &Tenant) -> Result<Tenant, SourceError> {
    let name = expected.metadata.name.as_deref().ok_or_else(unavailable)?;
    let tenant = Api::<Tenant>::all(source.client.clone())
        .get_opt(name)
        .await
        .map_err(read_error)?
        .ok_or(SourceError::StaleIdentity)?;
    if tenant.metadata.uid != expected.metadata.uid {
        return Err(SourceError::StaleIdentity);
    }
    Ok(tenant)
}

fn exact_catalog(catalog: &TenantDatabaseCatalog, expected: &str) -> Result<(), SourceError> {
    if !valid_logical_uid(expected) || catalog.metadata.uid.as_deref() != Some(expected) {
        return Err(SourceError::StaleIdentity);
    }
    if catalog.spec.closed {
        return Err(SourceError::Conflict);
    }
    Ok(())
}

async fn rollout(source: &KubeDataSource, tenant: &Tenant) -> Result<(), SourceError> {
    let mode = if matches!(tenant.spec.provider, TenantProviderSpec::Azure) {
        tenant_admin_shared::query::ProviderMode::Azure
    } else {
        tenant_admin_shared::query::ProviderMode::Local
    };
    if !source.load_creation_capability(mode).await.available {
        return Err(not_ready());
    }
    let deployment = Api::<Deployment>::namespaced(source.client.clone(), CONTROLLER_NAMESPACE)
        .get_opt("database-controller")
        .await
        .map_err(read_error)?
        .ok_or_else(not_ready)?;
    let replicas = deployment
        .spec
        .as_ref()
        .and_then(|spec| spec.replicas)
        .unwrap_or(1);
    let status = deployment.status.as_ref();
    if replicas < 1
        || deployment.metadata.deletion_timestamp.is_some()
        || deployment.metadata.generation.is_none()
        || status.and_then(|s| s.observed_generation) != deployment.metadata.generation
        || status.and_then(|s| s.updated_replicas) != Some(replicas)
        || status.and_then(|s| s.ready_replicas) != Some(replicas)
        || status.and_then(|s| s.available_replicas) != Some(replicas)
    {
        return Err(not_ready());
    }
    for kind in [
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    ] {
        let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
            "admissionregistration.k8s.io",
            "v1",
            kind,
        ));
        resource.plural = match kind {
            "ValidatingAdmissionPolicy" => "validatingadmissionpolicies",
            _ => "validatingadmissionpolicybindings",
        }
        .into();
        let cutover = Api::<DynamicObject>::all_with(source.client.clone(), &resource)
            .get_opt("tenant-database-catalog-cutover-create-lock")
            .await
            .map_err(read_error)?;
        if cutover.is_some() {
            return Err(not_ready());
        }
    }
    let name = tenant.metadata.name.as_deref().ok_or_else(unavailable)?;
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
        "authorization.k8s.io",
        "v1",
        "SelfSubjectAccessReview",
    ));
    resource.plural = "selfsubjectaccessreviews".into();
    let api = Api::<DynamicObject>::all_with(source.client.clone(), &resource);
    for verb in ["get", "update"] {
        let mut review = DynamicObject::new("", &resource);
        review.data = serde_json::json!({"spec": {"resourceAttributes": {
            "namespace": database_namespace(name), "group": "tenancy.cnpg-vcluster.io",
            "resource": "tenantdatabasecatalogs", "name": name, "verb": verb
        }}});
        let result = api
            .create(&PostParams::default(), &review)
            .await
            .map_err(read_error)?;
        if result.data.pointer("/status/allowed") != Some(&serde_json::Value::Bool(true))
            || result.data.pointer("/status/denied") == Some(&serde_json::Value::Bool(true))
            || result
                .data
                .pointer("/status/evaluationError")
                .and_then(|v| v.as_str())
                .is_some_and(|v| !v.is_empty())
        {
            return Err(not_ready());
        }
    }
    Ok(())
}

fn insert(
    catalog: &mut TenantDatabaseCatalog,
    request: &DatabaseAddRequest,
    logical_uid: &str,
) -> Result<(), SourceError> {
    exact_catalog(catalog, &request.catalog_uid)?;
    if !valid_name(&request.name) || !(1..=3).contains(&request.instances) {
        return Err(SourceError::Rejected);
    }
    if catalog
        .spec
        .entries
        .values()
        .any(|entry| entry.name == request.name)
        || catalog.spec.entries.contains_key(logical_uid)
        || catalog.spec.entries.len() >= DATABASE_LIMIT
    {
        return Err(SourceError::Conflict);
    }
    catalog.spec.entries.insert(
        logical_uid.into(),
        CatalogEntry {
            name: request.name.clone(),
            instances: request.instances as i32,
            deleting: false,
        },
    );
    Ok(())
}

fn mark_delete(
    catalog: &mut TenantDatabaseCatalog,
    request: &DatabaseDeleteRequest,
) -> Result<(), SourceError> {
    exact_catalog(catalog, &request.catalog_uid)?;
    if !valid_logical_uid(&request.logical_uid) || !valid_name(&request.confirmation) {
        return Err(SourceError::Rejected);
    }
    let entry = catalog
        .spec
        .entries
        .get_mut(&request.logical_uid)
        .ok_or(SourceError::StaleIdentity)?;
    if entry.name != request.confirmation {
        return Err(SourceError::StaleIdentity);
    }
    entry.deleting = true;
    Ok(())
}

async fn recover(
    source: &KubeDataSource,
    tenant: &Tenant,
    catalog_uid: &str,
    logical_uid: &str,
    name: &str,
    deleting: bool,
) -> Result<CatalogView, SourceError> {
    let current = read(&source.client, tenant)
        .await
        .map_err(|_| SourceError::MutationOutcomeUnknown)?;
    if current.metadata.uid.as_deref() != Some(catalog_uid) {
        return Err(SourceError::StaleIdentity);
    }
    let entry = current.spec.entries.get(logical_uid);
    if entry.is_some_and(|entry| entry.name == name && entry.deleting == deleting)
        || (deleting
            && entry.is_none()
            && !current
                .spec
                .entries
                .values()
                .any(|entry| entry.name == name))
    {
        return project(
            &current,
            provider_mode(tenant),
            capability_available(tenant),
        );
    }
    Err(SourceError::MutationOutcomeUnknown)
}

pub(super) async fn add(
    source: &KubeDataSource,
    tenant: &Tenant,
    request: &DatabaseAddRequest,
) -> Result<CatalogView, SourceError> {
    let logical_uid = Uuid::new_v4().to_string();
    for attempt in 0..3 {
        let live = live_tenant(source, tenant).await?;
        ready(&live)?;
        rollout(source, &live).await?;
        let mut catalog = read(&source.client, &live).await?;
        insert(&mut catalog, request, &logical_uid)?;
        let name = catalog.name_any();
        let api = Api::<TenantDatabaseCatalog>::namespaced(
            source.client.clone(),
            &database_namespace(&name),
        );
        catalog.status = None;
        match api.replace(&name, &PostParams::default(), &catalog).await {
            Ok(updated) => {
                return project(&updated, provider_mode(&live), capability_available(&live));
            }
            Err(kube::Error::Api(status)) if status.code == 409 && attempt < 2 => continue,
            Err(kube::Error::Api(status)) if status.code == 409 => {
                return Err(SourceError::Conflict);
            }
            Err(error @ kube::Error::Api(_))
                if matches!(super::database_error_status(&error), 400 | 403 | 404 | 422) =>
            {
                return Err(mutation_error(error));
            }
            Err(error) => {
                tracing::warn!(
                    status = super::database_error_status(&error),
                    "catalog add outcome uncertain"
                );
                return recover(
                    source,
                    &live,
                    &request.catalog_uid,
                    &logical_uid,
                    &request.name,
                    false,
                )
                .await;
            }
        }
    }
    Err(SourceError::Conflict)
}

pub(super) async fn delete(
    source: &KubeDataSource,
    tenant: &Tenant,
    request: &DatabaseDeleteRequest,
) -> Result<CatalogView, SourceError> {
    for attempt in 0..3 {
        let live = live_tenant(source, tenant).await?;
        let mut catalog = read(&source.client, &live).await?;
        mark_delete(&mut catalog, request)?;
        let name = catalog.name_any();
        let api = Api::<TenantDatabaseCatalog>::namespaced(
            source.client.clone(),
            &database_namespace(&name),
        );
        catalog.status = None;
        match api.replace(&name, &PostParams::default(), &catalog).await {
            Ok(updated) => {
                return project(&updated, provider_mode(&live), capability_available(&live));
            }
            Err(kube::Error::Api(status)) if status.code == 409 && attempt < 2 => continue,
            Err(kube::Error::Api(status)) if status.code == 409 => {
                return Err(SourceError::Conflict);
            }
            Err(error @ kube::Error::Api(_))
                if matches!(super::database_error_status(&error), 400 | 403 | 404 | 422) =>
            {
                return Err(mutation_error(error));
            }
            Err(error) => {
                tracing::warn!(
                    status = super::database_error_status(&error),
                    "catalog deletion intent outcome uncertain"
                );
                return recover(
                    source,
                    &live,
                    &request.catalog_uid,
                    &request.logical_uid,
                    &request.confirmation,
                    true,
                )
                .await;
            }
        }
    }
    Err(SourceError::Conflict)
}

fn credential_rules(tenant: &Tenant) -> Result<(PolicyRule, Vec<Subject>), SourceError> {
    let name = tenant.metadata.name.as_deref().ok_or_else(unavailable)?;
    Ok((
        PolicyRule {
            api_groups: Some(vec![String::new()]),
            resources: Some(vec!["secrets".into()]),
            resource_names: Some(vec![format!("{name}-kubeconfig")]),
            verbs: vec!["get".into()],
            ..Default::default()
        },
        ["tenant-admin", "database-controller"]
            .map(|subject| Subject {
                kind: "ServiceAccount".into(),
                name: subject.into(),
                namespace: Some("tenant-system".into()),
                ..Default::default()
            })
            .to_vec(),
    ))
}

fn validated_credential_rbac(
    role: &Role,
    binding: &RoleBinding,
    tenant: &Tenant,
) -> Result<(), SourceError> {
    let name = tenant.metadata.name.as_deref().ok_or_else(unavailable)?;
    let tenant_uid = tenant.metadata.uid.as_deref().ok_or_else(unavailable)?;
    let role_name = "tenant-database-credentials";
    let (rule, subjects) = credential_rules(tenant)?;
    let valid_meta = |meta: &kube::core::ObjectMeta| {
        meta.name.as_deref() == Some(role_name)
            && meta.namespace.as_deref() == Some(name)
            && meta.uid.as_deref().is_some_and(|uid| !uid.is_empty())
            && meta.deletion_timestamp.is_none()
            && meta
                .labels
                .as_ref()
                .and_then(|labels| labels.get(TENANT_LABEL))
                .map(String::as_str)
                == Some(tenant_uid)
            && meta.owner_references.as_ref().is_none_or(Vec::is_empty)
    };
    if !valid_meta(&role.metadata)
        || !valid_meta(&binding.metadata)
        || role.rules.as_deref() != Some(&[rule][..])
        || binding.role_ref
            != (RoleRef {
                api_group: Some("rbac.authorization.k8s.io".into()),
                kind: "Role".into(),
                name: role_name.into(),
            })
        || binding.subjects.as_deref() != Some(subjects.as_slice())
    {
        return Err(database_unavailable(
            "Tenant credential permissions are invalid",
            false,
        ));
    }
    Ok(())
}

fn management_object<'a>(
    resources: &'a [DynamicObject],
    name: &str,
    kind: &str,
    azure: bool,
) -> Result<&'a DynamicObject, SourceError> {
    let definitions = if azure {
        AZURE_MANAGEMENT_RESOURCES
    } else {
        MANAGEMENT_RESOURCES
    };
    let definition = definitions
        .iter()
        .find(|definition| definition.kind == kind)
        .ok_or_else(unavailable)?;
    let expected_name = definition.expected_name(name).ok_or_else(unavailable)?;
    let found: Vec<_> = resources
        .iter()
        .filter(|object| {
            object.types.as_ref().is_some_and(|types| {
                types.kind == definition.kind && types.api_version == definition.api_version
            }) && object.metadata.name.as_deref() == Some(expected_name.as_str())
                && object.metadata.namespace.as_deref() == Some(name)
        })
        .collect();
    match found.as_slice() {
        [object] if object.metadata.deletion_timestamp.is_none() => Ok(object),
        _ => Err(database_unavailable(
            "Tenant management identity is unavailable",
            false,
        )),
    }
}

fn validate_azure_credential(
    secret: &Secret,
    azure: &AzureProviderStatus,
) -> Result<(), SourceError> {
    let recorded = azure.kubeconfig.as_ref().ok_or_else(unavailable)?;
    let data = secret
        .data
        .as_ref()
        .and_then(|data| data.get("value"))
        .ok_or_else(unavailable)?;
    let digest = format!("{:x}", Sha256::digest(&data.0));
    if secret.metadata.uid.as_deref() != Some(recorded.secret_uid.as_str())
        || digest != recorded.content_sha256
    {
        return Err(SourceError::StaleIdentity);
    }
    Ok(())
}

async fn tenant_client(source: &KubeDataSource, tenant: &Tenant) -> Result<Client, SourceError> {
    let name = tenant.metadata.name.as_deref().ok_or_else(unavailable)?;
    let azure = matches!(tenant.spec.provider, TenantProviderSpec::Azure);
    let mode = if azure {
        tenant_admin_shared::query::ProviderMode::Azure
    } else {
        tenant_admin_shared::query::ProviderMode::Local
    };
    let resources = source.list_management_resources(mode, name).await?;
    let cluster = management_object(&resources, name, "Cluster", azure)?;
    let plane = management_object(&resources, name, "KamajiControlPlane", azure)?;
    if azure {
        if !is_accepted_azure_management_resource(tenant, &resources, cluster)
            || !is_accepted_azure_management_resource(tenant, &resources, plane)
        {
            return Err(SourceError::StaleIdentity);
        }
    } else if !is_accepted_local_management_resource(tenant, &resources, cluster)
        || !is_accepted_local_management_resource(tenant, &resources, plane)
        || tenant_controller::ownership::validate_provider_owner(plane, name, true, &resources)
            .is_err()
    {
        return Err(SourceError::StaleIdentity);
    }
    let status = tenant.status.as_ref().ok_or_else(unavailable)?;
    let endpoint = if azure {
        let azure = status.azure().ok_or_else(unavailable)?;
        let management = azure.management.as_ref().ok_or_else(unavailable)?;
        if cluster.metadata.uid.as_deref() != management.cluster_uid.as_deref()
            || plane.metadata.uid.as_deref() != management.kamaji_control_plane_uid.as_deref()
            || azure.binding.as_ref().is_none_or(|binding| {
                tenant.metadata.uid.as_deref() != Some(binding.tenant_uid.as_str())
            })
        {
            return Err(SourceError::StaleIdentity);
        }
        azure.endpoint.clone().ok_or_else(unavailable)?
    } else {
        let local = status.local().ok_or_else(unavailable)?;
        let host = local
            .allocation
            .as_ref()
            .map(|allocation| allocation.endpoint.as_str())
            .ok_or_else(unavailable)?;
        super::trusted_endpoint_authority(cluster, uid(local.cluster_uid.as_deref())?, host)
            .map_err(|_| SourceError::StaleIdentity)?
    };
    let roles = Api::<Role>::namespaced(source.client.clone(), name);
    let bindings = Api::<RoleBinding>::namespaced(source.client.clone(), name);
    let role = roles
        .get_opt("tenant-database-credentials")
        .await
        .map_err(read_error)?
        .ok_or_else(unavailable)?;
    let binding = bindings
        .get_opt("tenant-database-credentials")
        .await
        .map_err(read_error)?
        .ok_or_else(unavailable)?;
    validated_credential_rbac(&role, &binding, tenant)?;
    let (client, secret) = load_tenant_client_with_owner(
        source.client.clone(),
        plane,
        if azure { Some(cluster) } else { None },
        name,
        name,
        &endpoint,
    )
    .await
    .map_err(|error| {
        tracing::warn!(reason = ?error.class(), "Tenant administrative credentials unavailable");
        database_unavailable(
            "Tenant administrative credentials are invalid or unavailable",
            true,
        )
    })?;
    if azure {
        validate_azure_credential(&secret, status.azure().ok_or_else(unavailable)?)?;
    }
    Ok(client)
}

fn query_state<'a>(
    catalog: &'a TenantDatabaseCatalog,
    request: &CatalogQueryRequest,
    provider: ProviderMode,
) -> Result<(&'a str, &'a str, &'a str, &'a str), SourceError> {
    if catalog.metadata.uid.as_deref() != Some(request.catalog_uid.as_str())
        || !valid_logical_uid(&request.logical_uid)
        || catalog.spec.closed
        || request.instance_uid.is_empty()
        || request.instance_uid.len() > 128
    {
        return Err(SourceError::StaleIdentity);
    }
    let entry = catalog
        .spec
        .entries
        .get(&request.logical_uid)
        .ok_or(SourceError::StaleIdentity)?;
    let state = catalog
        .status
        .as_ref()
        .and_then(|s| s.entries.get(&request.logical_uid))
        .ok_or_else(unavailable)?;
    if entry.deleting
        || catalog.metadata.deletion_timestamp.is_some()
        || state.logical_uid != request.logical_uid
        || state
            .provider
            .as_ref()
            .map(|provider| provider.kind.as_str())
            != Some(match provider {
                ProviderMode::Local => "local",
                ProviderMode::Azure => "azure",
            })
        || !entry_ready(state, catalog.metadata.generation)
    {
        return Err(database_unavailable(
            "Database is not ready for queries",
            false,
        ));
    }
    let cluster = state.cnpg_cluster.as_ref().ok_or_else(unavailable)?;
    let namespace = state.namespace.as_ref().ok_or_else(unavailable)?;
    let credentials = state.credentials.as_ref().ok_or_else(unavailable)?;
    let query = state.query.as_ref().ok_or_else(unavailable)?;
    if !valid_logical_uid(&request.catalog_uid)
        || ownership::names(&request.catalog_uid, &request.logical_uid).ok()
            != Some((namespace.name.clone(), cluster.name.clone()))
        || query.cluster_uid != cluster.uid
        || query.credential_uid != credentials.uid
        || credentials.name != format!("{}-superuser", cluster.name)
        || !state.instances.iter().any(|instance| {
            instance.name == request.instance
                && instance.uid == request.instance_uid
                && instance.ready
        })
    {
        return Err(SourceError::StaleIdentity);
    }
    Ok((
        &namespace.name,
        &cluster.name,
        &cluster.uid,
        &credentials.uid,
    ))
}

fn entry_ready(state: &EntryStatus, generation: Option<i64>) -> bool {
    state.phase == DatabasePhase::Ready
        && Some(state.observed_generation) == generation
        && state.conditions.iter().any(|condition| {
            condition.type_ == "Ready"
                && condition.status == "True"
                && condition.observed_generation == generation
        })
}

fn safe_uid(value: &str) -> Option<String> {
    if (1..=128).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    {
        Some(value.into())
    } else {
        None
    }
}

fn condition_reason(reason: &str) -> &'static str {
    match reason {
        "AllInstancesReady" => "AllInstancesReady",
        "CredentialsPending" => "CredentialsPending",
        "StoragePending" => "StoragePending",
        "InstancesPending" => "InstancesPending",
        "OwnershipInvalid" => "OwnershipInvalid",
        "UnknownCreateOutcome" => "UnknownCreateOutcome",
        "ClaimPending" => "ClaimPending",
        "RuntimeNotReady" => "RuntimeNotReady",
        "DiskPending" => "DiskPending",
        "DiskIdentityUnavailable" => "DiskIdentityUnavailable",
        "CapabilityNotReady" => "CapabilityNotReady",
        "Deleting" => "Deleting",
        _ => "ConditionUnavailable",
    }
}

fn validate_cluster_object(
    cluster: &DynamicObject,
    catalog: &TenantDatabaseCatalog,
    request: &CatalogQueryRequest,
    namespace: &str,
    name: &str,
    cluster_uid: &str,
) -> Result<(), SourceError> {
    let labels = cluster
        .metadata
        .labels
        .as_ref()
        .ok_or(SourceError::StaleIdentity)?;
    if cluster
        .types
        .as_ref()
        .is_none_or(|types| types.api_version != "postgresql.cnpg.io/v1" || types.kind != "Cluster")
        || cluster.metadata.namespace.as_deref() != Some(namespace)
        || cluster.metadata.name.as_deref() != Some(name)
        || cluster.metadata.uid.as_deref() != Some(cluster_uid)
        || cluster.metadata.deletion_timestamp.is_some()
        || labels.get(CATALOG_LABEL).map(String::as_str) != catalog.metadata.uid.as_deref()
        || labels.get(ENTRY_LABEL).map(String::as_str) != Some(&request.logical_uid)
        || labels.get(TENANT_LABEL).map(String::as_str) != Some(&catalog.spec.tenant_uid)
    {
        return Err(SourceError::StaleIdentity);
    }
    Ok(())
}

fn validate_secret(
    secret: &Secret,
    namespace: &str,
    name: &str,
    uid: &str,
    cluster: &QueryClusterBinding,
) -> Result<DatabaseCredentials, SourceError> {
    if secret.metadata.namespace.as_deref() != Some(namespace)
        || secret.metadata.name.as_deref() != Some(name)
        || secret.metadata.uid.as_deref() != Some(uid)
        || secret.metadata.deletion_timestamp.is_some()
        || secret.type_.as_deref() != Some("kubernetes.io/basic-auth")
        || secret
            .metadata
            .owner_references
            .as_ref()
            .is_none_or(|owners| {
                owners.len() != 1
                    || owners[0].api_version != cluster.api_version
                    || owners[0].kind != cluster.kind
                    || owners[0].name != cluster.name
                    || owners[0].uid != cluster.uid
                    || owners[0].controller != Some(true)
            })
    {
        return Err(SourceError::StaleIdentity);
    }
    let data = secret.data.as_ref().ok_or_else(unavailable)?;
    Ok(DatabaseCredentials {
        username: super::credential_text(data.get("username").map(|v| v.0.as_slice()), 1024)?,
        password: super::credential_text(data.get("password").map(|v| v.0.as_slice()), 16 * 1024)?,
    })
}

pub(super) async fn query(
    source: &KubeDataSource,
    tenant: &Tenant,
    request: &CatalogQueryRequest,
) -> Result<CatalogQueryResponse, SourceError> {
    validate_query_request(&tenant_admin_shared::query::DatabaseQueryRequest {
        instance: request.instance.clone(),
        database: request.database.clone(),
        sql: request.sql.clone(),
    })?;
    let live = live_tenant(source, tenant).await?;
    ready(&live)?;
    let catalog = read(&source.client, &live).await?;
    let (namespace, cluster_name, cluster_uid, credential_uid) =
        query_state(&catalog, request, provider_mode(&live))?;
    let client = tenant_client(source, &live).await?;
    let mut resource = ApiResource::from_gvk(&GroupVersionKind::gvk(
        "postgresql.cnpg.io",
        "v1",
        "Cluster",
    ));
    resource.plural = "clusters".into();
    let cluster = Api::<DynamicObject>::namespaced_with(client.clone(), namespace, &resource)
        .get_opt(cluster_name)
        .await
        .map_err(read_error)?
        .ok_or_else(unavailable)?;
    validate_cluster_object(
        &cluster,
        &catalog,
        request,
        namespace,
        cluster_name,
        cluster_uid,
    )?;
    let binding = QueryClusterBinding {
        api_version: "postgresql.cnpg.io/v1".into(),
        kind: "Cluster".into(),
        name: cluster_name.into(),
        uid: cluster_uid.into(),
        namespace: namespace.into(),
    };
    let pod = Api::<Pod>::namespaced(client.clone(), namespace)
        .get_opt(&request.instance)
        .await
        .map_err(read_error)?
        .ok_or_else(unavailable)?;
    if pod.metadata.uid.as_deref() != Some(request.instance_uid.as_str()) {
        return Err(SourceError::StaleIdentity);
    }
    let pod = validate_pod_binding(&pod, &request.instance, &binding).map_err(pod_binding_error)?;
    let secret = Api::<Secret>::namespaced(client.clone(), namespace)
        .get_opt(&format!("{cluster_name}-superuser"))
        .await
        .map_err(read_error)?
        .ok_or_else(unavailable)?;
    let credentials = validate_secret(
        &secret,
        namespace,
        &format!("{cluster_name}-superuser"),
        credential_uid,
        &binding,
    )?;
    ready(&live_tenant(source, &live).await?)?;
    let current = read(&source.client, &live).await?;
    query_state(&current, request, provider_mode(&live))?;
    let execution = source
        .query_executor
        .execute(QueryConnection {
            client,
            pod: &pod,
            database: &request.database,
            username: &credentials.username,
            password: &credentials.password,
            sql: &request.sql,
        })
        .await
        .map_err(query_execution_error)?;
    Ok(CatalogQueryResponse {
        catalog_uid: request.catalog_uid.clone(),
        logical_uid: request.logical_uid.clone(),
        instance: request.instance.clone(),
        instance_uid: request.instance_uid.clone(),
        executed_at: observed_at(),
        duration_ms: execution.duration_ms,
        truncated: execution.truncated,
        results: execution.results,
    })
}

pub(super) fn project(
    catalog: &TenantDatabaseCatalog,
    provider: ProviderMode,
    capability_available: bool,
) -> Result<CatalogView, SourceError> {
    let catalog_uid = uid(catalog.metadata.uid.as_deref())?;
    let rv = uid(catalog.metadata.resource_version.as_deref())?;
    let mut databases = Vec::new();
    for (logical_uid, entry) in &catalog.spec.entries {
        let raw_state = catalog
            .status
            .as_ref()
            .and_then(|status| status.entries.get(logical_uid))
            .filter(|state| state.logical_uid == *logical_uid);
        let expected_provider = match provider {
            ProviderMode::Local => "local",
            ProviderMode::Azure => "azure",
        };
        let state = raw_state.filter(|state| {
            state
                .provider
                .as_ref()
                .is_some_and(|identity| identity.kind == expected_provider)
        });
        let mut blockers = Vec::new();
        if raw_state.is_some() && state.is_none() {
            blockers.push(DatabaseBlocker {
                code: "ProviderMismatch".into(),
                message: "Database provider identity does not match the Tenant".into(),
            });
        }
        if let Some(state) = state {
            for condition in state
                .conditions
                .iter()
                .take(16)
                .filter(|condition| condition.status != "True")
            {
                let code = condition_reason(&condition.reason);
                blockers.push(DatabaseBlocker {
                    code: code.into(),
                    message: "Database condition is not ready".into(),
                });
            }
        }
        if raw_state.is_none() {
            blockers.push(DatabaseBlocker {
                code: "ObservationPending".into(),
                message: "Database observation is pending".into(),
            });
        }
        let instances: Vec<_> = state
            .into_iter()
            .flat_map(|state| state.instances.iter())
            .take(3)
            .filter(|instance| {
                super::is_dns_label(&instance.name, 63)
                    && !instance.uid.is_empty()
                    && instance.uid.len() <= 128
                    && instance
                        .uid
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'-')
            })
            .map(|instance| InstanceView {
                name: instance.name.clone(),
                uid: instance.uid.clone(),
                role: if matches!(instance.role.as_str(), "primary" | "standby") {
                    instance.role.clone()
                } else {
                    "unknown".into()
                },
                ready: instance.ready,
            })
            .collect();
        let storage = state.map(|s| s.storage.as_slice()).unwrap_or_default();
        let per_instance_bytes: u64 = match provider {
            ProviderMode::Local => 1024 * 1024 * 1024,
            ProviderMode::Azure => 4 * 1024 * 1024 * 1024,
        };
        let requested_bytes = per_instance_bytes * u64::try_from(entry.instances).unwrap_or(0);
        let storage_views: Vec<_> = storage
            .iter()
            .take(3)
            .filter(|s| (1..=entry.instances).contains(&s.ordinal))
            .map(|s| StorageView {
                ordinal: s.ordinal as u32,
                requested_bytes: per_instance_bytes,
                healthy: s.healthy
                    && u64::try_from(s.requested_bytes).ok() == Some(per_instance_bytes),
                pv_uid: s.pv.as_ref().and_then(|id| safe_uid(&id.uid)),
                pvc_uid: s.pvc.as_ref().and_then(|id| safe_uid(&id.uid)),
                disk_uid: s.disk.as_ref().and_then(|id| safe_uid(&id.uid)),
            })
            .collect();
        let conditions = state
            .into_iter()
            .flat_map(|s| s.conditions.iter())
            .take(16)
            .map(|condition| {
                let reason = condition_reason(&condition.reason);
                DatabaseConditionView {
                    condition_type: if condition.type_ == "Ready" {
                        "Ready"
                    } else {
                        "Unknown"
                    }
                    .into(),
                    status: match condition.status.as_str() {
                        "True" => "True",
                        "False" => "False",
                        _ => "Unknown",
                    }
                    .into(),
                    reason: reason.into(),
                    message: format!("Database condition: {reason}"),
                    observed_generation: condition.observed_generation,
                }
            })
            .collect();
        let query_identity = state.and_then(|s| s.query.as_ref()).and_then(|id| {
            Some(QueryIdentityView {
                cluster_uid: safe_uid(&id.cluster_uid)?,
                credential_uid: safe_uid(&id.credential_uid)?,
            })
        });
        let finalization = state
            .and_then(|s| s.finalization.as_ref())
            .map(|finalization| FinalizationView {
                terminal_verified: finalization.terminal_verified,
                verified_absent_count: u32::try_from(finalization.verified_absent.len())
                    .unwrap_or(u32::MAX),
                pending_count: u32::try_from(finalization.pending.len()).unwrap_or(u32::MAX),
            });
        let phase = if raw_state.is_some() && state.is_none() {
            "ownership-invalid"
        } else if entry.deleting {
            "deleting"
        } else {
            match state.map(|state| state.phase) {
                Some(DatabasePhase::Ready)
                    if state.is_some_and(|s| entry_ready(s, catalog.metadata.generation)) =>
                {
                    "ready"
                }
                Some(DatabasePhase::Degraded) => "degraded",
                Some(DatabasePhase::OwnershipInvalid) => "ownership-invalid",
                _ => "progressing",
            }
        };
        let health = match phase {
            "ready" => TopologyHealth::Ready,
            "deleting" => TopologyHealth::Deleting,
            "degraded" | "ownership-invalid" => TopologyHealth::Degraded,
            _ => TopologyHealth::Progressing,
        };
        let root_id = format!("database:{logical_uid}");
        let mut nodes = vec![TopologyNode {
            id: root_id.clone(),
            kind: TopologyNodeKind::Database,
            semantic_kind: TopologySemanticKind::DatabaseCluster,
            ownership: TopologyOwnership::TenantOwned,
            provenance: TopologyNodeProvenance::DatabaseLogicalRepresentation,
            database_role: None,
            placement: None,
            label: entry.name.clone(),
            health,
            resource: None,
            attributes: vec![
                DisplayAttribute {
                    label: "Requested instances".into(),
                    value: entry.instances.to_string(),
                },
                DisplayAttribute {
                    label: "Storage bytes".into(),
                    value: requested_bytes.to_string(),
                },
            ],
        }];
        let mut edges = Vec::new();
        for instance in &instances {
            let id = format!("{root_id}:{}", instance.uid);
            nodes.push(TopologyNode {
                id: id.clone(),
                kind: TopologyNodeKind::Database,
                semantic_kind: TopologySemanticKind::DatabaseInstance,
                ownership: TopologyOwnership::TenantOwned,
                provenance: TopologyNodeProvenance::DatabaseLogicalRepresentation,
                database_role: Some(catalog_database_role(&instance.role)),
                placement: None,
                label: instance.name.clone(),
                health: if instance.ready {
                    TopologyHealth::Ready
                } else {
                    TopologyHealth::Progressing
                },
                resource: None,
                attributes: vec![DisplayAttribute {
                    label: "Role".into(),
                    value: instance.role.clone(),
                }],
            });
            edges.push(TopologyEdge {
                id: format!("{root_id}->{id}"),
                source: root_id.clone(),
                target: id,
                kind: TopologyEdgeKind::Represents,
                label: None,
            });
        }

        databases.push(DatabaseView {
            logical_uid: logical_uid.clone(),
            name: entry.name.clone(),
            instances: entry.instances as u32,
            deleting: entry.deleting,
            phase: phase.into(),
            observed_generation: state.map(|s| s.observed_generation),
            provider: Some(expected_provider.into()),
            namespace: state
                .and_then(|s| s.namespace.as_ref().map(|n| n.name.clone()))
                .filter(|n| n.len() <= 63),
            namespace_uid: state.and_then(|s| s.namespace.as_ref().and_then(|n| safe_uid(&n.uid))),
            cluster: state
                .and_then(|s| s.cnpg_cluster.as_ref().map(|n| n.name.clone()))
                .filter(|n| n.len() <= 63),
            cluster_uid: state.and_then(|s| s.cnpg_cluster.as_ref().and_then(|n| safe_uid(&n.uid))),
            credential_uid: state
                .and_then(|s| s.credentials.as_ref().and_then(|n| safe_uid(&n.uid))),
            query_identity,
            ready_instances: instances.iter().filter(|i| i.ready).count() as u32,
            storage_requested_bytes: requested_bytes,
            storage_healthy: storage_views.iter().filter(|s| s.healthy).count() as u32,
            storage: storage_views,
            conditions,
            finalization,
            instance_topology: instances,
            blockers,
            topology: TopologyGraph {
                tenant_name: catalog.spec.tenant_name.clone(),
                provider: match provider {
                    ProviderMode::Local => TenantProvider::Local,
                    ProviderMode::Azure => TenantProvider::Azure,
                },
                nodes,
                edges,
            },
        });
    }

    Ok(CatalogView {
        tenant: catalog.spec.tenant_name.clone(),
        tenant_uid: catalog.spec.tenant_uid.clone(),
        catalog_uid: catalog_uid.into(),
        resource_version: rv.into(),
        closed: catalog.spec.closed,
        capability_available,
        databases,
    })
}

fn catalog_database_role(role: &str) -> DatabaseInstanceRole {
    match role {
        "primary" => DatabaseInstanceRole::Primary,
        "standby" => DatabaseInstanceRole::Standby,
        _ => DatabaseInstanceRole::Unknown,
    }
}

#[cfg(test)]
mod tests {
    use std::{
        collections::{BTreeMap, VecDeque},
        convert::Infallible,
        sync::{Arc, Mutex},
    };

    use axum::http::{Request, Response};
    use http_body_util::BodyExt;
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::{Condition, Time};
    use kube::client::Body;
    use serde_json::{Value, json};
    use tenant_controller::api::{
        AzureKubeconfigStatus, DatabaseCapability, TenantSpec, TenantStatus,
    };
    use tenant_database_controller::api::{CatalogStatus, TenantDatabaseCatalogSpec};
    use tower::service_fn;

    use super::*;

    const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const FIRST: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const SECOND: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    const THIRD: &str = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    const FOURTH: &str = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";

    fn tenant() -> Tenant {
        let mut tenant = Tenant::new("tenant-a", TenantSpec::local("1.36.4", 1));
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.finalizers = Some(vec![TENANT_FINALIZER.into()]);
        tenant.metadata.generation = Some(2);
        tenant.status = Some(TenantStatus {
            observed_generation: Some(2),
            phase: Some(TenantPhase::Ready),
            database_capability: Some(DatabaseCapability {
                available: true,
                reason: "Ready".into(),
                namespace: "tenant-db-tenant-a".into(),
                namespace_uid: "namespace-uid".into(),
                catalog_uid: CATALOG.into(),
                storage_namespace_uid: None,
            }),
            conditions: vec![Condition {
                type_: "Ready".into(),
                status: "True".into(),
                reason: "Ready".into(),
                message: "ready".into(),
                observed_generation: Some(2),
                last_transition_time: serde_json::from_str::<Time>(r#""2026-01-01T00:00:00Z""#)
                    .unwrap(),
            }],
            ..TenantStatus::default()
        });
        tenant
    }

    fn catalog() -> TenantDatabaseCatalog {
        let mut catalog = TenantDatabaseCatalog::new(
            "tenant-a",
            TenantDatabaseCatalogSpec {
                tenant_name: "tenant-a".into(),
                tenant_uid: "tenant-uid".into(),
                closed: false,
                entries: BTreeMap::new(),
            },
        );
        catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
        catalog.metadata.uid = Some(CATALOG.into());
        catalog.metadata.finalizers = Some(vec![CATALOG_FINALIZER.into()]);
        catalog.metadata.resource_version = Some("10".into());
        catalog.metadata.generation = Some(2);
        catalog.metadata.owner_references = Some(vec![OwnerReference {
            api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
            kind: "Tenant".into(),
            name: "tenant-a".into(),
            uid: "tenant-uid".into(),
            ..Default::default()
        }]);
        catalog
    }

    fn entry(name: &str) -> CatalogEntry {
        CatalogEntry {
            name: name.into(),
            instances: 3,
            deleting: false,
        }
    }

    fn query_request(uid: &str) -> CatalogQueryRequest {
        CatalogQueryRequest {
            catalog_uid: CATALOG.into(),
            logical_uid: uid.into(),
            instance: "pg-instance-1".into(),
            instance_uid: format!("instance-{uid}"),
            database: "postgres".into(),
            sql: "select 1".into(),
        }
    }

    fn observed(catalog: &mut TenantDatabaseCatalog, uid: &str, name: &str) {
        let (namespace, cluster) = ownership::names(CATALOG, uid).unwrap();
        catalog.spec.entries.insert(uid.into(), entry(name));
        let state = serde_json::from_value(json!({
            "logicalUID": uid, "observedGeneration": 2, "phase": "Ready",
            "conditions": [{"type":"Ready","status":"True","reason":"AllInstancesReady",
                "message":"ready","observedGeneration":2,"lastTransitionTime":"2026-01-01T00:00:00Z"}],
            "provider": {"kind":"local"},
            "namespace": {"name":namespace,"uid":"db-namespace-uid"},
            "cnpgCluster": {"name":cluster,"uid":format!("{uid}-cluster")},
            "credentials": {"name":format!("{cluster}-superuser"),"uid":format!("{uid}-secret")},
            "query": {"clusterUid":format!("{uid}-cluster"),"credentialUid":format!("{uid}-secret")},
            "storage": [{"ordinal":1,"requestedBytes":1073741824,"healthy":true,
                "path":"/private/secret"}],
            "instances": [{"name":"pg-instance-1","uid":format!("instance-{uid}"),"role":"primary","ready":true}],
        })).unwrap();
        catalog
            .status
            .get_or_insert_with(CatalogStatus::default)
            .entries
            .insert(uid.into(), state);
    }

    #[test]
    fn conditional_intents_reject_duplicate_capacity_closed_and_replaced_identity() {
        let mut catalog = catalog();
        let request = DatabaseAddRequest {
            catalog_uid: CATALOG.into(),
            name: "alpha".into(),
            instances: 2,
        };
        insert(&mut catalog, &request, FIRST).unwrap();
        assert_eq!(catalog.spec.entries[FIRST].instances, 2);
        assert_eq!(
            insert(&mut catalog, &request, SECOND),
            Err(SourceError::Conflict)
        );
        catalog.spec.entries.insert(SECOND.into(), entry("beta"));
        catalog.spec.entries.insert(THIRD.into(), entry("gamma"));
        assert_eq!(
            insert(
                &mut catalog,
                &DatabaseAddRequest {
                    name: "delta".into(),
                    ..request.clone()
                },
                FOURTH
            ),
            Err(SourceError::Conflict)
        );
        assert_eq!(
            mark_delete(
                &mut catalog,
                &DatabaseDeleteRequest {
                    catalog_uid: CATALOG.into(),
                    logical_uid: FIRST.into(),
                    confirmation: "beta".into(),
                }
            ),
            Err(SourceError::StaleIdentity)
        );
        assert!(!catalog.spec.entries[FIRST].deleting);
        mark_delete(
            &mut catalog,
            &DatabaseDeleteRequest {
                catalog_uid: CATALOG.into(),
                logical_uid: FIRST.into(),
                confirmation: "alpha".into(),
            },
        )
        .unwrap();
        assert!(catalog.spec.entries[FIRST].deleting);
        catalog.spec.closed = true;
        assert_eq!(
            insert(&mut catalog, &request, FOURTH),
            Err(SourceError::Conflict)
        );
        assert_eq!(
            mark_delete(
                &mut catalog,
                &DatabaseDeleteRequest {
                    catalog_uid: CATALOG.into(),
                    logical_uid: SECOND.into(),
                    confirmation: "beta".into(),
                }
            ),
            Err(SourceError::Conflict)
        );
    }

    #[test]
    fn query_selects_exact_non_deleting_ready_entry_and_instance() {
        let mut catalog = catalog();
        observed(&mut catalog, FIRST, "alpha");
        observed(&mut catalog, SECOND, "beta");
        assert!(query_state(&catalog, &query_request(FIRST), ProviderMode::Local).is_ok());
        let mut wrong = query_request(FIRST);
        wrong.instance_uid = format!("instance-{SECOND}");
        assert!(query_state(&catalog, &wrong, ProviderMode::Local).is_err());
        wrong = query_request(FIRST);
        wrong.catalog_uid = SECOND.into();
        assert_eq!(
            query_state(&catalog, &wrong, ProviderMode::Local).unwrap_err(),
            SourceError::StaleIdentity
        );
        catalog.spec.entries.get_mut(FIRST).unwrap().deleting = true;
        assert!(query_state(&catalog, &query_request(FIRST), ProviderMode::Local).is_err());
        assert!(query_state(&catalog, &query_request(SECOND), ProviderMode::Local).is_ok());
        catalog
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut(SECOND)
            .unwrap()
            .observed_generation = 1;
        assert!(query_state(&catalog, &query_request(SECOND), ProviderMode::Local).is_err());
    }

    #[test]
    fn projection_bounds_per_entry_and_excludes_private_storage_and_messages() {
        let mut catalog = catalog();
        observed(&mut catalog, FIRST, "alpha");
        observed(&mut catalog, SECOND, "beta");
        let state = catalog
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut(FIRST)
            .unwrap();
        state.conditions[0].status = "False".into();
        state.conditions[0].reason = "password=hidden".into();
        state.conditions[0].message = "token=private".into();
        state.instances[0].role = "password=hidden".into();
        catalog
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut(SECOND)
            .unwrap()
            .instances[0]
            .role = "standby".into();
        let view = project(&catalog, ProviderMode::Local, true).unwrap();
        assert!(view.capability_available);
        assert!(
            !project(&catalog, ProviderMode::Local, false)
                .unwrap()
                .capability_available
        );
        assert_eq!(view.databases.len(), 2);
        assert_eq!(view.databases[0].instance_topology[0].role, "unknown");
        let database_instance = view.databases[0]
            .topology
            .nodes
            .iter()
            .find(|node| node.semantic_kind == TopologySemanticKind::DatabaseInstance)
            .expect("catalog instance topology node");
        assert_eq!(
            database_instance.database_role,
            Some(DatabaseInstanceRole::Unknown)
        );
        assert_eq!(database_instance.ownership, TopologyOwnership::TenantOwned);
        assert!(database_instance.placement.is_none());
        assert_eq!(
            view.databases[1]
                .topology
                .nodes
                .iter()
                .find(|node| node.semantic_kind == TopologySemanticKind::DatabaseInstance)
                .and_then(|node| node.database_role),
            Some(DatabaseInstanceRole::Standby)
        );
        assert_eq!(view.databases[0].phase, "progressing");
        assert_eq!(
            view.databases[0].topology.nodes[0].health,
            TopologyHealth::Progressing
        );
        assert_eq!(view.databases[1].phase, "ready");
        assert_eq!(
            view.databases[0].storage_requested_bytes,
            3 * 1024 * 1024 * 1024
        );
        assert_eq!(
            view.databases[0].conditions[0].reason,
            "ConditionUnavailable"
        );
        assert_eq!(
            view.databases[0]
                .query_identity
                .as_ref()
                .unwrap()
                .cluster_uid,
            format!("{FIRST}-cluster")
        );
        assert_eq!(view.databases[0].storage[0].ordinal, 1);
        let text = serde_json::to_string(&view).unwrap();
        assert!(!text.contains("/private"));
        assert!(!text.contains("hidden"));
        assert!(!text.contains("private"));
        assert!(view.databases.iter().all(|entry| {
            entry
                .topology
                .nodes
                .iter()
                .all(|node| node.id.contains(&entry.logical_uid))
        }));
        assert!(view.databases.iter().all(|entry| {
            entry
                .blockers
                .iter()
                .all(|blocker| blocker.message.len() <= 512)
        }));
    }

    #[test]
    fn tenant_topology_contains_only_each_catalog_entry_own_instances() {
        let mut catalog = catalog();
        observed(&mut catalog, FIRST, "alpha");
        observed(&mut catalog, SECOND, "beta");
        let view = project(&catalog, ProviderMode::Local, true).unwrap();
        let mut graph = TopologyGraph {
            tenant_name: "tenant-a".into(),
            provider: TenantProvider::Local,
            nodes: vec![
                TopologyNode {
                    id: "tenant".into(),
                    kind: TopologyNodeKind::Tenant,
                    semantic_kind: TopologySemanticKind::Tenant,
                    ownership: TopologyOwnership::TenantOwned,
                    provenance: TopologyNodeProvenance::ExactKubernetesResource,
                    database_role: None,
                    placement: None,
                    label: "tenant-a".into(),
                    health: TopologyHealth::Ready,
                    resource: None,
                    attributes: vec![],
                },
                TopologyNode {
                    id: "database:cluster".into(),
                    kind: TopologyNodeKind::Database,
                    semantic_kind: TopologySemanticKind::DatabaseCluster,
                    ownership: TopologyOwnership::TenantOwned,
                    provenance: TopologyNodeProvenance::SyntheticSummary,
                    database_role: None,
                    placement: None,
                    label: "legacy".into(),
                    health: TopologyHealth::Unknown,
                    resource: None,
                    attributes: vec![],
                },
            ],
            edges: vec![],
        };
        crate::projection::merge_catalog_topology(&mut graph, &view);
        assert_eq!(
            graph
                .nodes
                .iter()
                .filter(|node| node.id.starts_with("database:"))
                .count(),
            4
        );
        assert!(!graph.nodes.iter().any(|node| node.id == "database:cluster"));
        assert_eq!(graph.edges.len(), 4);
        assert_eq!(
            graph
                .nodes
                .iter()
                .filter(|node| node.semantic_kind == TopologySemanticKind::DatabaseInstance)
                .map(|node| node.database_role)
                .collect::<Vec<_>>(),
            [
                Some(DatabaseInstanceRole::Primary),
                Some(DatabaseInstanceRole::Primary)
            ]
        );
        for edge in &graph.edges {
            if edge.kind == TopologyEdgeKind::Represents {
                assert!(edge.target.starts_with(&edge.source));
            }
        }
    }

    #[test]
    fn azure_projection_keeps_provider_and_rejects_cross_provider_status() {
        let mut catalog = catalog();
        observed(&mut catalog, FIRST, "alpha");
        let ready = project(&catalog, ProviderMode::Local, true).unwrap();
        assert_eq!(ready.databases[0].topology.provider, TenantProvider::Local);
        let rejected = project(&catalog, ProviderMode::Azure, true).unwrap();
        assert_eq!(
            rejected.databases[0].topology.provider,
            TenantProvider::Azure
        );
        assert_eq!(rejected.databases[0].phase, "ownership-invalid");
        assert_eq!(
            rejected.databases[0].storage_requested_bytes,
            3 * 4 * 1024 * 1024 * 1024
        );
        assert!(rejected.databases[0].instance_topology.is_empty());
        assert!(query_state(&catalog, &query_request(FIRST), ProviderMode::Azure).is_err());
        catalog
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut(FIRST)
            .unwrap()
            .provider
            .as_mut()
            .unwrap()
            .kind = "azure".into();
        let accepted = project(&catalog, ProviderMode::Azure, true).unwrap();
        assert_eq!(accepted.databases[0].phase, "ready");
        assert_eq!(accepted.databases[0].instance_topology.len(), 1);
        assert!(query_state(&catalog, &query_request(FIRST), ProviderMode::Azure).is_ok());
    }

    #[test]
    fn pending_projection_retains_requested_capacity_without_storage_observations() {
        let mut catalog = catalog();
        catalog.spec.entries.insert(FIRST.into(), entry("alpha"));
        let local = project(&catalog, ProviderMode::Local, true).unwrap();
        let azure = project(&catalog, ProviderMode::Azure, true).unwrap();
        for view in [&local, &azure] {
            assert_eq!(view.databases[0].phase, "progressing");
            assert!(view.databases[0].storage.is_empty());
            assert_eq!(view.databases[0].storage_healthy, 0);
            assert_eq!(view.databases[0].blockers[0].code, "ObservationPending");
        }
        assert_eq!(
            local.databases[0].storage_requested_bytes,
            3 * 1024 * 1024 * 1024
        );
        assert_eq!(
            azure.databases[0].storage_requested_bytes,
            12 * 1024 * 1024 * 1024
        );
    }

    #[test]
    fn finalization_projection_reports_bounded_progress_without_private_paths() {
        let mut catalog = catalog();
        observed(&mut catalog, FIRST, "alpha");
        catalog.spec.entries.get_mut(FIRST).unwrap().deleting = true;
        catalog
            .status
            .as_mut()
            .unwrap()
            .entries
            .get_mut(FIRST)
            .unwrap()
            .finalization = Some(tenant_database_controller::api::FinalizationStatus {
            terminal_verified: false,
            verified_absent: vec!["/private/one".into()],
            pending: vec!["secret=private".into(), "/private/two".into()],
        });
        let view = project(&catalog, ProviderMode::Local, true).unwrap();
        assert_eq!(view.databases[0].phase, "deleting");
        assert_eq!(
            view.databases[0]
                .finalization
                .as_ref()
                .unwrap()
                .pending_count,
            2
        );
        assert_eq!(
            view.databases[0]
                .finalization
                .as_ref()
                .unwrap()
                .verified_absent_count,
            1
        );
        let response = serde_json::to_string(&view).unwrap();
        assert!(!response.contains("/private"));
        assert!(!response.contains("secret=private"));
    }

    #[test]
    fn azure_kubeconfig_requires_recorded_uid_and_exact_content_digest() {
        let mut secret = Secret {
            metadata: kube::core::ObjectMeta {
                uid: Some("owned-secret".into()),
                ..Default::default()
            },
            data: Some(BTreeMap::from([(
                "value".into(),
                k8s_openapi::ByteString(b"credential-material".to_vec()),
            )])),
            ..Default::default()
        };
        let mut status = AzureProviderStatus {
            kubeconfig: Some(AzureKubeconfigStatus {
                secret_uid: "owned-secret".into(),
                content_sha256: format!("{:x}", Sha256::digest(b"credential-material")),
            }),
            ..AzureProviderStatus::default()
        };
        assert!(validate_azure_credential(&secret, &status).is_ok());
        secret.metadata.uid = Some("replacement-secret".into());
        assert_eq!(
            validate_azure_credential(&secret, &status),
            Err(SourceError::StaleIdentity)
        );
        secret.metadata.uid = Some("owned-secret".into());
        status.kubeconfig.as_mut().unwrap().content_sha256 = "bad-digest".into();
        assert_eq!(
            validate_azure_credential(&secret, &status),
            Err(SourceError::StaleIdentity)
        );
        assert!(
            !format!("{:?}", validate_azure_credential(&secret, &status))
                .contains("credential-material")
        );
    }

    #[test]
    fn credential_rbac_requires_exact_tenant_bound_role_and_binding() {
        let tenant = tenant();
        let (rule, subjects) = credential_rules(&tenant).unwrap();
        let metadata = kube::core::ObjectMeta {
            name: Some("tenant-database-credentials".into()),
            namespace: Some("tenant-a".into()),
            uid: Some("role-uid".into()),
            labels: Some(BTreeMap::from([(TENANT_LABEL.into(), "tenant-uid".into())])),
            ..Default::default()
        };
        let role = Role {
            metadata: metadata.clone(),
            rules: Some(vec![rule]),
        };
        let binding = RoleBinding {
            metadata,
            role_ref: RoleRef {
                api_group: Some("rbac.authorization.k8s.io".into()),
                kind: "Role".into(),
                name: "tenant-database-credentials".into(),
            },
            subjects: Some(subjects),
        };
        assert!(validated_credential_rbac(&role, &binding, &tenant).is_ok());
        let mut foreign = role.clone();
        foreign.rules.as_mut().unwrap()[0].resource_names = Some(vec!["other-kubeconfig".into()]);
        assert!(validated_credential_rbac(&foreign, &binding, &tenant).is_err());
        let mut foreign = binding.clone();
        foreign.subjects.as_mut().unwrap()[0].name = "other-admin".into();
        assert!(validated_credential_rbac(&role, &foreign, &tenant).is_err());
        let mut foreign = role.clone();
        foreign
            .metadata
            .labels
            .as_mut()
            .unwrap()
            .insert(TENANT_LABEL.into(), "other-uid".into());
        assert!(validated_credential_rbac(&foreign, &binding, &tenant).is_err());
    }

    fn fixture_client(responses: Vec<Value>) -> (Client, Arc<Mutex<Vec<String>>>) {
        let responses = Arc::new(Mutex::new(VecDeque::from(responses)));
        let calls = Arc::new(Mutex::new(Vec::new()));
        let seen = calls.clone();
        let client = Client::new(
            service_fn(move |request: Request<Body>| {
                let responses = responses.clone();
                let seen = seen.clone();
                async move {
                    seen.lock().unwrap().push(format!(
                        "{} {}",
                        request.method(),
                        request.uri().path()
                    ));
                    let body = responses.lock().unwrap().pop_front().unwrap();
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(200)
                            .header("content-type", "application/json")
                            .body(Body::from(body.to_string().into_bytes()))
                            .unwrap(),
                    )
                }
            }),
            "default",
        );
        (client, calls)
    }

    #[tokio::test]
    async fn ambiguous_outcome_only_rereads_authoritative_exact_catalog() {
        let mut catalog = catalog();
        catalog.spec.entries.insert(FIRST.into(), entry("alpha"));
        let namespace = json!({"apiVersion":"v1","kind":"Namespace",
            "metadata":{"name":"tenant-db-tenant-a","uid":"namespace-uid",
                "labels":{"tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid"}}});
        let (client, calls) = fixture_client(vec![
            namespace.clone(),
            serde_json::to_value(&catalog).unwrap(),
        ]);
        let source = KubeDataSource::new(client);
        let result = recover(&source, &tenant(), CATALOG, FIRST, "alpha", false)
            .await
            .unwrap();
        assert_eq!(result.databases[0].logical_uid, FIRST);
        assert_eq!(
            calls.lock().unwrap().as_slice(),
            [
                "GET /api/v1/namespaces/tenant-db-tenant-a",
                "GET /apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabasecatalogs/tenant-a",
            ]
        );
        catalog.metadata.uid = Some(SECOND.into());
        let (client, _) = fixture_client(vec![namespace, serde_json::to_value(catalog).unwrap()]);
        let source = KubeDataSource::new(client);
        assert_eq!(
            recover(&source, &tenant(), CATALOG, FIRST, "alpha", false)
                .await
                .unwrap_err(),
            SourceError::MutationOutcomeUnknown
        );
    }

    type RecordedCalls = Arc<Mutex<Vec<(String, Value)>>>;

    fn scripted_client(responses: Vec<(u16, Value)>) -> (Client, RecordedCalls) {
        let responses = Arc::new(Mutex::new(VecDeque::from(responses)));
        let calls = Arc::new(Mutex::new(Vec::new()));
        let recorded = calls.clone();
        let client = Client::new(
            service_fn(move |request: Request<Body>| {
                let responses = responses.clone();
                let recorded = recorded.clone();
                async move {
                    let method = request.method().to_string();
                    let path = request.uri().path().to_owned();
                    let body = request.into_body().collect().await.unwrap().to_bytes();
                    let input: Value = if body.is_empty() {
                        Value::Null
                    } else {
                        serde_json::from_slice(&body).unwrap()
                    };
                    recorded
                        .lock()
                        .unwrap()
                        .push((format!("{method} {path}"), input.clone()));
                    let (status, mut output) = responses.lock().unwrap().pop_front().unwrap();
                    if output.is_null() {
                        output = input;
                    }
                    if output == "__last_update__" {
                        output = recorded
                            .lock()
                            .unwrap()
                            .iter()
                            .rev()
                            .find(|(path, _)| path.starts_with("PUT "))
                            .unwrap()
                            .1
                            .clone();
                    }
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "application/json")
                            .body(Body::from(output.to_string().into_bytes()))
                            .unwrap(),
                    )
                }
            }),
            "default",
        );
        (client, calls)
    }

    fn deployment(name: &str) -> Value {
        json!({
            "apiVersion":"apps/v1","kind":"Deployment",
            "metadata":{"name":name,"namespace":"tenant-system","generation":3},
            "spec":{"replicas":1,"template":{"spec":{"containers":[{
                "name":"manager","args":["--provider=local","--supported-kubernetes-version=1.36.4"]
            }]}}},
            "status":{"observedGeneration":3,"updatedReplicas":1,"readyReplicas":1,"availableReplicas":1}
        })
    }

    fn namespace() -> Value {
        json!({"apiVersion":"v1","kind":"Namespace",
            "metadata":{"name":"tenant-db-tenant-a","uid":"namespace-uid",
                "labels":{"tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid"}}})
    }

    fn allowed() -> Value {
        json!({"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectAccessReview",
            "status":{"allowed":true,"denied":false}})
    }

    fn absent() -> (u16, Value) {
        (
            404,
            json!({"apiVersion":"v1","kind":"Status","code":404,"reason":"NotFound"}),
        )
    }

    fn approved_rollout() -> Vec<(u16, Value)> {
        vec![
            (200, deployment("tenant-controller")),
            (200, deployment("database-controller")),
            absent(),
            absent(),
            (201, allowed()),
            (201, allowed()),
        ]
    }

    #[tokio::test]
    async fn add_rejects_unprotected_live_catalog_and_namespace() {
        for failure in ["tenant-finalizer", "catalog-finalizer", "namespace-owner"] {
            let mut live = tenant();
            let mut current_namespace = namespace();
            let mut current_catalog = catalog();
            match failure {
                "tenant-finalizer" => live.metadata.finalizers = None,
                "catalog-finalizer" => current_catalog.metadata.finalizers = None,
                "namespace-owner" => {
                    current_namespace["metadata"]["ownerReferences"] = json!([{
                        "apiVersion":"v1", "kind":"Namespace", "name":"foreign", "uid":"foreign"
                    }]);
                }
                _ => unreachable!(),
            }
            let mut responses = vec![(200, serde_json::to_value(&live).unwrap())];
            if failure != "tenant-finalizer" {
                responses.extend(approved_rollout());
                responses.push((200, current_namespace));
                if failure == "catalog-finalizer" {
                    responses.push((200, serde_json::to_value(current_catalog).unwrap()));
                }
            }
            let (client, calls) = scripted_client(responses);
            let error = add(
                &KubeDataSource::new(client),
                &tenant(),
                &DatabaseAddRequest {
                    catalog_uid: CATALOG.into(),
                    name: "alpha".into(),
                    instances: 2,
                },
            )
            .await
            .unwrap_err();
            assert!(
                matches!(
                    error,
                    SourceError::StaleIdentity | SourceError::DatabaseUnavailable { .. }
                ),
                "{failure}: {error:?}"
            );
            assert!(
                calls
                    .lock()
                    .unwrap()
                    .iter()
                    .all(|(path, _)| !path.starts_with("PUT "))
            );
        }
    }

    #[tokio::test]
    async fn status_churn_409_rereads_and_retries_same_logical_uid() {
        let mut next = catalog();
        next.metadata.resource_version = Some("11".into());
        let denied = json!({"apiVersion":"v1","kind":"Status","code":409,
            "reason":"Conflict","message":"changed"});
        let mut responses = vec![(200, serde_json::to_value(tenant()).unwrap())];
        responses.extend(approved_rollout());
        responses.extend([
            (200, namespace()),
            (200, serde_json::to_value(catalog()).unwrap()),
            (409, denied),
            (200, serde_json::to_value(tenant()).unwrap()),
        ]);
        responses.extend(approved_rollout());
        responses.extend([
            (200, namespace()),
            (200, serde_json::to_value(next).unwrap()),
            (200, Value::Null),
        ]);
        let (client, calls) = scripted_client(responses);
        let source = KubeDataSource::new(client);
        let result = add(
            &source,
            &tenant(),
            &DatabaseAddRequest {
                catalog_uid: CATALOG.into(),
                name: "alpha".into(),
                instances: 2,
            },
        )
        .await
        .unwrap();
        assert_eq!(result.databases.len(), 1);
        let puts: Vec<_> = calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(path, _)| path.starts_with("PUT "))
            .map(|(_, body)| body.clone())
            .collect();
        assert_eq!(puts.len(), 2);
        assert_eq!(puts[0]["metadata"]["resourceVersion"], "10");
        assert_eq!(puts[1]["metadata"]["resourceVersion"], "11");
        assert_eq!(puts[0]["spec"]["entries"], puts[1]["spec"]["entries"]);
        assert!(puts.iter().all(|body| body.get("status").is_none()));
    }

    #[tokio::test]
    async fn conflicting_add_reread_rejects_duplicate_fourth_and_replaced_catalog() {
        let mut original = catalog();
        original.spec.entries.insert(FIRST.into(), entry("alpha"));
        original.spec.entries.insert(SECOND.into(), entry("beta"));
        let mut duplicate = original.clone();
        duplicate.metadata.resource_version = Some("11".into());
        duplicate.spec.entries.insert(THIRD.into(), entry("delta"));
        let mut full = duplicate.clone();
        full.spec.entries.get_mut(THIRD).unwrap().name = "gamma".into();
        let mut replaced = original.clone();
        replaced.metadata.uid = Some(FOURTH.into());
        for (name, current, expected) in [
            ("duplicate", duplicate, SourceError::Conflict),
            ("fourth", full, SourceError::Conflict),
            ("replaced", replaced, SourceError::StaleIdentity),
        ] {
            let mut replies = vec![(200, serde_json::to_value(tenant()).unwrap())];
            replies.extend(approved_rollout());
            replies.extend([
                (200, namespace()),
                (200, serde_json::to_value(&original).unwrap()),
                (
                    409,
                    json!({"apiVersion":"v1","kind":"Status","code":409,"reason":"Conflict"}),
                ),
                (200, serde_json::to_value(tenant()).unwrap()),
            ]);
            replies.extend(approved_rollout());
            replies.extend([
                (200, namespace()),
                (200, serde_json::to_value(current).unwrap()),
            ]);
            let (client, calls) = scripted_client(replies);
            let source = KubeDataSource::new(client);
            let error = add(
                &source,
                &tenant(),
                &DatabaseAddRequest {
                    catalog_uid: CATALOG.into(),
                    name: "delta".into(),
                    instances: 2,
                },
            )
            .await
            .unwrap_err();
            assert_eq!(error, expected, "{name}");
            assert_eq!(
                calls
                    .lock()
                    .unwrap()
                    .iter()
                    .filter(|(path, _)| path.starts_with("PUT "))
                    .count(),
                1,
                "{name}"
            );
        }
    }

    #[tokio::test]
    async fn conflicting_delete_never_marks_a_same_name_replacement() {
        let mut original = catalog();
        original.spec.entries.insert(FIRST.into(), entry("alpha"));
        let mut replacement = catalog();
        replacement.metadata.resource_version = Some("11".into());
        replacement
            .spec
            .entries
            .insert(SECOND.into(), entry("alpha"));
        let (client, calls) = scripted_client(vec![
            (200, serde_json::to_value(tenant()).unwrap()),
            (200, namespace()),
            (200, serde_json::to_value(original).unwrap()),
            (
                409,
                json!({"apiVersion":"v1","kind":"Status","code":409,"reason":"Conflict"}),
            ),
            (200, serde_json::to_value(tenant()).unwrap()),
            (200, namespace()),
            (200, serde_json::to_value(replacement).unwrap()),
        ]);
        let source = KubeDataSource::new(client);
        assert_eq!(
            delete(
                &source,
                &tenant(),
                &DatabaseDeleteRequest {
                    catalog_uid: CATALOG.into(),
                    logical_uid: FIRST.into(),
                    confirmation: "alpha".into(),
                }
            )
            .await
            .unwrap_err(),
            SourceError::StaleIdentity
        );
        assert_eq!(
            calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(path, _)| path.starts_with("PUT "))
                .count(),
            1
        );
    }

    #[tokio::test]
    async fn denied_effective_update_permission_prevents_all_catalog_writes() {
        let mut denied = allowed();
        denied["status"]["allowed"] = json!(false);
        let (client, calls) = scripted_client(vec![
            (200, serde_json::to_value(tenant()).unwrap()),
            (200, deployment("tenant-controller")),
            (200, deployment("database-controller")),
            absent(),
            absent(),
            (201, allowed()),
            (201, denied),
        ]);
        let source = KubeDataSource::new(client);
        let result = add(
            &source,
            &tenant(),
            &DatabaseAddRequest {
                catalog_uid: CATALOG.into(),
                name: "alpha".into(),
                instances: 2,
            },
        )
        .await;
        assert_eq!(result.unwrap_err(), not_ready());
        assert!(
            calls
                .lock()
                .unwrap()
                .iter()
                .all(|(path, _)| !path.starts_with("PUT "))
        );
    }

    #[tokio::test]
    async fn cutover_policy_or_binding_refences_addition_before_catalog_update() {
        for kind in [
            "ValidatingAdmissionPolicy",
            "ValidatingAdmissionPolicyBinding",
        ] {
            let mut responses = vec![
                (200, serde_json::to_value(tenant()).unwrap()),
                (200, deployment("tenant-controller")),
                (200, deployment("database-controller")),
            ];
            if kind == "ValidatingAdmissionPolicyBinding" {
                responses.push(absent());
            }
            responses.push((200, json!({"apiVersion":"admissionregistration.k8s.io/v1",
                "kind":kind,"metadata":{"name":"tenant-database-catalog-cutover-create-lock","uid":"lock-uid"}})));
            let (client, calls) = scripted_client(responses);
            let source = KubeDataSource::new(client);
            let result = add(
                &source,
                &tenant(),
                &DatabaseAddRequest {
                    catalog_uid: CATALOG.into(),
                    name: "alpha".into(),
                    instances: 2,
                },
            )
            .await;
            assert_eq!(result.unwrap_err(), not_ready());
            assert!(
                calls
                    .lock()
                    .unwrap()
                    .iter()
                    .all(|(path, _)| !path.starts_with("PUT "))
            );
        }
    }

    #[tokio::test]
    async fn failed_controller_rollout_after_conflict_prevents_second_put() {
        let mut responses = vec![(200, serde_json::to_value(tenant()).unwrap())];
        responses.extend(approved_rollout());
        responses.extend([
            (200, namespace()),
            (200, serde_json::to_value(catalog()).unwrap()),
            (
                409,
                json!({"apiVersion":"v1","kind":"Status","code":409,"reason":"Conflict"}),
            ),
            (200, serde_json::to_value(tenant()).unwrap()),
            (200, deployment("tenant-controller")),
        ]);
        let mut rolling = deployment("database-controller");
        rolling["status"]["availableReplicas"] = json!(0);
        responses.push((200, rolling));
        let (client, calls) = scripted_client(responses);
        let source = KubeDataSource::new(client);
        assert_eq!(
            add(
                &source,
                &tenant(),
                &DatabaseAddRequest {
                    catalog_uid: CATALOG.into(),
                    name: "alpha".into(),
                    instances: 2,
                }
            )
            .await
            .unwrap_err(),
            not_ready()
        );
        assert_eq!(
            calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(path, _)| path.starts_with("PUT "))
                .count(),
            1
        );
    }

    #[tokio::test]
    async fn lost_add_reply_recovers_by_get_without_second_put() {
        let failure = json!({"apiVersion":"v1","kind":"Status","code":500,
            "reason":"InternalError","message":"reply was lost"});
        let mut responses = vec![(200, serde_json::to_value(tenant()).unwrap())];
        responses.extend(approved_rollout());
        responses.extend([
            (200, namespace()),
            (200, serde_json::to_value(catalog()).unwrap()),
            (500, failure),
            (200, namespace()),
            (200, json!("__last_update__")),
        ]);
        let (client, calls) = scripted_client(responses);
        let source = KubeDataSource::new(client);
        let view = add(
            &source,
            &tenant(),
            &DatabaseAddRequest {
                catalog_uid: CATALOG.into(),
                name: "alpha".into(),
                instances: 2,
            },
        )
        .await
        .unwrap();
        assert_eq!(view.databases[0].name, "alpha");
        assert_eq!(
            calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(path, _)| path.starts_with("PUT "))
                .count(),
            1
        );
    }
}
