use std::time::Duration;

use k8s_openapi::api::core::v1::{ConfigMap, Namespace};
use kube::{Api, Client, ResourceExt, core::DynamicObject, runtime::controller::Action};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tenant_controller::{
    api::{Tenant, TenantPhase, TenantProviderSpec},
    azure::{AzureConfiguration, CONFIG_NAME},
    tenant_client::load_tenant_client_with_owner,
};
use tenant_database_runtime::{azure_runtime, catalog_runtime};

use super::{
    ObserveError, Progress, creation_order, local, next_action, stop_after_progress, verify_current,
};
use crate::{
    api::{
        CreateState, DatabasePhase, EntryStatus, FinalizationStatus, InstanceObservation,
        ProviderIdentity, QueryIdentity, ResourceIdentity, StorageIdentity, TenantDatabaseCatalog,
    },
    ownership, status,
};

const SIZE: i64 = 4 * 1024 * 1024 * 1024;
const RETRY: Duration = Duration::from_secs(10);
const RESYNC: Duration = Duration::from_secs(60);
const POSTGRES_IMAGE: &str = "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185";
const DISK_VERSION: &str = "v1api20240302";
const ARM_VERSION: &str = "2024-03-02";

pub(crate) struct Access {
    client: Client,
    capability_ready: bool,
    replay_issued: bool,
    pub(crate) storage_namespace: String,
    storage_uid: String,
    pub(crate) group_id: String,
    pub(crate) location: String,
    arm: Option<Arm>,
}

impl Access {
    pub(crate) fn arm(&self) -> Result<&Arm, ObserveError> {
        self.arm
            .as_ref()
            .ok_or(ObserveError::Azure("dedicated disk identity unavailable"))
    }

    fn prerequisite_reason(&self) -> Option<&'static str> {
        if !self.capability_ready {
            Some("CapabilityNotReady")
        } else if self.arm.is_none() {
            Some("DiskIdentityUnavailable")
        } else {
            None
        }
    }
}

pub(crate) struct Arm {
    client: reqwest::Client,
    client_id: String,
    token_file: String,
    token_endpoint: String,
    endpoint: String,
}

impl Arm {
    fn new(tenant_id: &str) -> Result<Self, ObserveError> {
        let client_id = std::env::var("AZURE_CLIENT_ID")
            .map_err(|_| ObserveError::Azure("workload identity client ID missing"))?;
        let pinned = std::env::var("DATABASE_DISK_CLIENT_ID")
            .map_err(|_| ObserveError::Azure("dedicated disk identity missing"))?;
        let actual_tenant = std::env::var("AZURE_TENANT_ID")
            .map_err(|_| ObserveError::Azure("workload identity tenant missing"))?;
        let token_file = std::env::var("AZURE_FEDERATED_TOKEN_FILE")
            .map_err(|_| ObserveError::Azure("federated token path missing"))?;
        if !crate::api::valid_logical_uid(&client_id.to_ascii_lowercase())
            || !crate::api::valid_logical_uid(&actual_tenant.to_ascii_lowercase())
            || !client_id.eq_ignore_ascii_case(&pinned)
            || !actual_tenant.eq_ignore_ascii_case(tenant_id)
            || !token_file.starts_with('/')
        {
            return Err(ObserveError::Azure("workload identity binding differs"));
        }
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(20))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| ObserveError::Azure("ARM HTTP client unavailable"))?;
        Ok(Self {
            client,
            client_id,
            token_file,
            token_endpoint: format!(
                "https://login.microsoftonline.com/{actual_tenant}/oauth2/v2.0/token"
            ),
            endpoint: "https://management.azure.com".into(),
        })
    }

    async fn token(&self) -> Result<String, ObserveError> {
        let assertion = tokio::fs::read_to_string(&self.token_file)
            .await
            .map_err(|_| ObserveError::Azure("federated token unavailable"))?;
        if assertion.is_empty() || assertion.len() > 65536 {
            return Err(ObserveError::Azure("federated token invalid"));
        }
        let response = self
            .client
            .post(&self.token_endpoint)
            .form(&[
                ("client_id", self.client_id.as_str()),
                ("scope", "https://management.azure.com/.default"),
                ("grant_type", "client_credentials"),
                (
                    "client_assertion_type",
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                ),
                ("client_assertion", assertion.trim()),
            ])
            .send()
            .await
            .map_err(|_| ObserveError::Azure("token exchange failed"))?;
        if !response.status().is_success() {
            return Err(ObserveError::Azure("token exchange denied"));
        }
        let value: Value = response
            .json()
            .await
            .map_err(|_| ObserveError::Azure("token exchange response invalid"))?;
        value
            .get("access_token")
            .and_then(Value::as_str)
            .filter(|token| !token.is_empty())
            .map(str::to_owned)
            .ok_or(ObserveError::Azure("access token missing"))
    }

    pub(crate) async fn request(
        &self,
        arm_id: &str,
        delete: bool,
    ) -> Result<Option<Value>, ObserveError> {
        let token = self.token().await?;
        self.request_with_token(arm_id, delete, &token).await
    }

    async fn request_with_token(
        &self,
        arm_id: &str,
        delete: bool,
        token: &str,
    ) -> Result<Option<Value>, ObserveError> {
        let url = format!("{}{arm_id}?api-version={ARM_VERSION}", self.endpoint);
        let request = if delete {
            self.client.delete(url)
        } else {
            self.client.get(url)
        };
        let response = request
            .bearer_auth(token)
            .send()
            .await
            .map_err(|_| ObserveError::Azure("ARM transport failed"))?;
        match response.status().as_u16() {
            404 if !delete => Ok(None),
            200 if !delete => response
                .json()
                .await
                .map(Some)
                .map_err(|_| ObserveError::Azure("ARM disk response invalid")),
            200 | 202 | 204 if delete => Ok(Some(Value::Null)),
            _ => Err(ObserveError::Azure("ARM disk request was not conclusive")),
        }
    }
}

pub(crate) fn disk_name(cluster: &str, ordinal: i32) -> String {
    format!("{cluster}-{ordinal}")
}

pub(crate) fn arm_id(group_id: &str, name: &str) -> String {
    format!("{group_id}/providers/Microsoft.Compute/disks/{name}")
}

pub(crate) fn tags(catalog: &TenantDatabaseCatalog, uid: &str) -> Result<Value, ObserveError> {
    Ok(json!({
        "cnpg-vcluster-catalog-uid": catalog.metadata.uid.as_deref().ok_or(ObserveError::Identity)?,
        "cnpg-vcluster-entry-uid": uid,
        "cnpg-vcluster-tenant-uid": catalog.spec.tenant_uid,
    }))
}

pub(crate) fn disk(
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    access: &Access,
    name: &str,
) -> Result<DynamicObject, ObserveError> {
    serde_json::from_value(json!({
        "apiVersion":format!("compute.azure.com/{DISK_VERSION}"), "kind":"Disk",
        "metadata":{"name":name,"namespace":access.storage_namespace,
            "labels":ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?},
        "spec":{"azureName":name,"owner":{"armId":access.group_id},
            "location":access.location,"creationData":{"createOption":"Empty"},
            "diskSizeGB":4,"sku":{"name":"StandardSSD_LRS"},"tags":tags(catalog, uid)?}
    }))
    .map_err(|_| ObserveError::Identity)
}

fn volume(
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster: &str,
    ordinal: i32,
    id: &str,
) -> Result<DynamicObject, ObserveError> {
    serde_json::from_value(json!({
        "apiVersion":"v1","kind":"PersistentVolume",
        "metadata":{"name":format!("pv-{cluster}-{ordinal}"),
            "labels":ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?},
        "spec":{"capacity":{"storage":"4Gi"},"accessModes":["ReadWriteOnce"],
            "persistentVolumeReclaimPolicy":"Retain","volumeMode":"Filesystem",
            "storageClassName":azure_runtime::STORAGE_CLASS,
            "csi":{"driver":"disk.csi.azure.com","volumeHandle":id,"fsType":"ext4"},
            "claimRef":{"namespace":namespace,"name":disk_name(cluster, ordinal)}}
    }))
    .map_err(|_| ObserveError::Identity)
}

fn claim(
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster: &str,
    ordinal: i32,
) -> Result<DynamicObject, ObserveError> {
    let mut labels = ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?;
    labels.extend([
        ("cnpg.io/cluster".into(), cluster.into()),
        ("cnpg.io/instanceName".into(), disk_name(cluster, ordinal)),
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
        "metadata":{"name":disk_name(cluster, ordinal),"namespace":namespace,"labels":labels},
        "spec":{"accessModes":["ReadWriteOnce"],
            "storageClassName":azure_runtime::STORAGE_CLASS,"volumeMode":"Filesystem",
            "volumeName":format!("pv-{cluster}-{ordinal}"),
            "resources":{"requests":{"storage":"4Gi"}}}
    }))
    .map_err(|_| ObserveError::Identity)
}

fn cluster(
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    name: &str,
    instances: i32,
) -> Result<DynamicObject, ObserveError> {
    serde_json::from_value(json!({
        "apiVersion":"postgresql.cnpg.io/v1","kind":"Cluster",
        "metadata":{"name":name,"namespace":namespace,
            "labels":ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?},
        "spec":{"instances":instances,"imageName":POSTGRES_IMAGE,"enableSuperuserAccess":true,
            "postgresUID":26,"postgresGID":26,
            "inheritedMetadata":{"labels":ownership::labels(catalog, uid).map_err(|_| ObserveError::Foreign)?},
            "storage":{"size":"4Gi","storageClass":azure_runtime::STORAGE_CLASS,
                "pvcTemplate":{"storageClassName":azure_runtime::STORAGE_CLASS,
                    "accessModes":["ReadWriteOnce"],"volumeMode":"Filesystem"}}}
    })).map_err(|_| ObserveError::Identity)
}

pub(crate) fn storage(state: &mut EntryStatus, ordinal: i32) -> &mut StorageIdentity {
    if !state.storage.iter().any(|item| item.ordinal == ordinal) {
        state.storage.push(StorageIdentity {
            ordinal,
            requested_bytes: SIZE,
            path: None,
            pv: None,
            pvc: None,
            disk: None,
            arm_id: None,
            healthy: false,
        });
    }
    state
        .storage
        .iter_mut()
        .find(|item| item.ordinal == ordinal)
        .expect("inserted")
}

fn validate_state(
    state: &EntryStatus,
    uid: &str,
    namespace: &str,
    cluster: &str,
    instances: i32,
    access: &Access,
) -> Result<(), ObserveError> {
    if state.logical_uid != uid
        || state.provider.as_ref().is_none_or(|provider| {
            provider.kind != "azure"
                || provider.storage_namespace.as_ref().is_none_or(|id| {
                    id.name != access.storage_namespace || id.uid != access.storage_uid
                })
        })
        || state
            .namespace
            .as_ref()
            .is_some_and(|id| id.name != namespace || id.uid.is_empty())
        || state
            .cnpg_cluster
            .as_ref()
            .is_some_and(|id| id.name != cluster || id.uid.is_empty())
        || state
            .credentials
            .as_ref()
            .is_some_and(|id| id.name != format!("{cluster}-superuser") || id.uid.is_empty())
        || state.storage.iter().any(|item| {
            !(1..=instances).contains(&item.ordinal)
                || item.requested_bytes != SIZE
                || item.path.is_some()
                || item.arm_id.as_deref()
                    != Some(arm_id(&access.group_id, &disk_name(cluster, item.ordinal)).as_str())
                || item.disk.as_ref().is_some_and(|id| {
                    id.name != disk_name(cluster, item.ordinal) || id.uid.is_empty()
                })
                || item.pv.as_ref().is_some_and(|id| {
                    id.name != format!("pv-{cluster}-{}", item.ordinal) || id.uid.is_empty()
                })
                || item.pvc.as_ref().is_some_and(|id| {
                    id.name != disk_name(cluster, item.ordinal) || id.uid.is_empty()
                })
        })
        || state.storage.iter().enumerate().any(|(index, item)| {
            state.storage[index + 1..]
                .iter()
                .any(|other| other.ordinal == item.ordinal)
        })
        || state.create_intents.len() > 11
        || state
            .create_intents
            .iter()
            .enumerate()
            .any(|(index, intent)| {
                state.create_intents[index + 1..]
                    .iter()
                    .any(|other| other.kind == intent.kind && other.ordinal == intent.ordinal)
                    || match intent.kind.as_str() {
                        "Disk" | "PersistentVolume" | "PersistentVolumeClaim" => {
                            !(1..=instances).contains(&intent.ordinal)
                                || (intent.kind == "Disk"
                                    && intent.name != disk_name(cluster, intent.ordinal))
                        }
                        "Namespace" | "Cluster" => intent.ordinal != 0,
                        _ => true,
                    }
            })
    {
        return Err(ObserveError::Foreign);
    }
    Ok(())
}

async fn load(
    management: Client,
    catalog: &TenantDatabaseCatalog,
    replay_issued: bool,
) -> Result<Access, ObserveError> {
    let tenant = Api::<Tenant>::all(management.clone())
        .get(&catalog.spec.tenant_name)
        .await?;
    let status = tenant.status.as_ref().ok_or(ObserveError::Identity)?;
    let capability = status
        .database_capability
        .as_ref()
        .ok_or(ObserveError::Identity)?;
    let azure = status.azure().ok_or(ObserveError::Identity)?;
    let name = &catalog.spec.tenant_name;
    let storage_namespace = catalog_runtime::storage_namespace(name);
    let storage_ns = Api::<Namespace>::all(management.clone())
        .get(&storage_namespace)
        .await?;
    let storage_uid = storage_ns
        .uid()
        .filter(|uid| !uid.is_empty())
        .ok_or(ObserveError::Identity)?;
    let provider_map = Api::<ConfigMap>::namespaced(management.clone(), "tenant-system")
        .get(CONFIG_NAME)
        .await?;
    let config =
        AzureConfiguration::from_config_map(&provider_map).map_err(|_| ObserveError::Identity)?;
    let canonical = tenant_controller::api::canonical_spec(
        name,
        &tenant.spec,
        &config.values.supported_kubernetes_version,
    )
    .map_err(|_| ObserveError::Identity)?;
    let binding = azure.binding.as_ref().ok_or(ObserveError::Identity)?;
    let namespace = Api::<Namespace>::all(management.clone()).get(name).await?;
    let plane = local::api(
        management.clone(),
        Some(name),
        "controlplane.cluster.x-k8s.io",
        "v1alpha1",
        "KamajiControlPlane",
        "kamajicontrolplanes",
    )
    .get(name)
    .await?;
    let cluster = local::api(
        management.clone(),
        Some(name),
        "cluster.x-k8s.io",
        "v1beta1",
        "Cluster",
        "clusters",
    )
    .get(name)
    .await?;
    let endpoint = azure.endpoint.as_deref().ok_or(ObserveError::Identity)?;
    let (client, secret) =
        load_tenant_client_with_owner(management, &plane, Some(&cluster), name, name, endpoint)
            .await
            .map_err(|_| ObserveError::Identity)?;
    let kubeconfig = azure.kubeconfig.as_ref().ok_or(ObserveError::Identity)?;
    let digest = secret
        .data
        .as_ref()
        .and_then(|data| data.get("value"))
        .map(|data| format!("{:x}", Sha256::digest(&data.0)))
        .ok_or(ObserveError::Identity)?;
    if tenant.uid().as_deref() != Some(catalog.spec.tenant_uid.as_str())
        || !matches!(tenant.spec.provider, TenantProviderSpec::Azure)
        || capability.namespace != catalog.namespace().ok_or(ObserveError::Identity)?
        || capability.catalog_uid != catalog.uid().ok_or(ObserveError::Identity)?
        || capability.storage_namespace_uid.as_deref() != Some(storage_uid.as_str())
        || storage_ns
            .metadata
            .labels
            .as_ref()
            .and_then(|labels| labels.get(ownership::TENANT_LABEL))
            != Some(&catalog.spec.tenant_uid)
        || storage_ns.metadata.deletion_timestamp.is_some()
        || namespace.uid().as_deref()
            != azure
                .management
                .as_ref()
                .and_then(|id| id.namespace_uid.as_deref())
        || plane.uid().as_deref()
            != azure
                .management
                .as_ref()
                .and_then(|id| id.kamaji_control_plane_uid.as_deref())
        || cluster.uid().as_deref()
            != azure
                .management
                .as_ref()
                .and_then(|id| id.cluster_uid.as_deref())
        || secret.uid().as_deref() != Some(kubeconfig.secret_uid.as_str())
        || digest != kubeconfig.content_sha256
        || binding.tenant_uid != catalog.spec.tenant_uid
        || binding.specification_sha256 != tenant_controller::api::spec_hash(&canonical)
        || binding.provider_config_uid != config.config_map_uid
        || binding.provider_config_sha256 != config.sha256
        || binding.resource_group_id != config.values.resource_group_id
        || binding.foundation_sha256 != config.values.foundation_sha256
        || binding.foundation_defaults_sha256 != config.values.foundation_defaults_sha256
        || !config
            .values
            .resource_group_id
            .eq_ignore_ascii_case(&format!(
                "/subscriptions/{}/resourceGroups/{}",
                config.values.subscription_id, config.values.resource_group_name
            ))
    {
        return Err(ObserveError::Identity);
    }
    let arm = match Arm::new(&config.values.tenant_id) {
        Ok(arm) => Some(arm),
        Err(error) => {
            tracing::warn!(%error, "dedicated Azure disk identity unavailable");
            None
        }
    };
    Ok(Access {
        client,
        capability_ready: tenant.metadata.deletion_timestamp.is_none()
            && status.observed_generation == tenant.metadata.generation
            && status.phase == Some(TenantPhase::Ready)
            && capability.available,
        replay_issued,
        storage_namespace,
        storage_uid,
        group_id: config.values.resource_group_id,
        location: config.values.location,
        arm,
    })
}

fn entry(uid: &str, generation: i64, access: &Access) -> EntryStatus {
    EntryStatus {
        logical_uid: uid.into(),
        observed_generation: generation,
        phase: DatabasePhase::Pending,
        conditions: vec![],
        provider: Some(ProviderIdentity {
            kind: "azure".into(),
            storage_namespace: Some(ResourceIdentity {
                name: access.storage_namespace.clone(),
                uid: access.storage_uid.clone(),
            }),
        }),
        namespace: None,
        cnpg_cluster: None,
        credentials: None,
        storage: vec![],
        instances: vec![],
        query: None,
        finalization: None,
        create_intents: vec![],
    }
}

pub(crate) fn disk_api(management: Client, namespace: &str) -> Api<DynamicObject> {
    local::api(
        management,
        Some(namespace),
        "compute.azure.com",
        DISK_VERSION,
        "Disk",
        "disks",
    )
}

pub(crate) fn disk_identity(
    live: &DynamicObject,
    desired: &DynamicObject,
    catalog: &TenantDatabaseCatalog,
    uid: &str,
    expected: Option<&ResourceIdentity>,
    expected_arm: &str,
) -> Result<ResourceIdentity, ObserveError> {
    let id = local::check(live, catalog, uid, &desired.name_any(), expected)?;
    if !local::matches_desired(live, desired)
        || live
            .data
            .pointer("/status/id")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.eq_ignore_ascii_case(expected_arm))
    {
        return Err(ObserveError::Foreign);
    }
    Ok(id)
}

pub(crate) fn arm_disk(
    actual: &Value,
    expected: &str,
    labels: &Value,
    location: &str,
) -> Result<(), ObserveError> {
    if actual
        .get("id")
        .and_then(Value::as_str)
        .is_none_or(|id| !id.eq_ignore_ascii_case(expected))
        || actual.pointer("/properties/diskSizeGB") != Some(&json!(4))
        || actual.pointer("/sku/name") != Some(&json!("StandardSSD_LRS"))
        || actual
            .get("location")
            .and_then(Value::as_str)
            .is_none_or(|value| !value.eq_ignore_ascii_case(location))
        || actual.get("tags") != Some(labels)
    {
        return Err(ObserveError::Foreign);
    }
    if actual.pointer("/properties/provisioningState") != Some(&json!("Succeeded")) {
        return Err(ObserveError::Azure("ARM disk creation is not terminal"));
    }
    Ok(())
}

#[expect(
    clippy::too_many_arguments,
    reason = "each disk is bound to an exact entry, ordinal and ARM ID"
)]
async fn disk_ready(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    state: &mut EntryStatus,
    access: &Access,
    name: &str,
    ordinal: i32,
    expected_arm: &str,
) -> Result<bool, ObserveError> {
    let api = disk_api(management.clone(), &access.storage_namespace);
    let desired = disk(catalog, uid, access, name)?;
    let previous = local::intent(state, "Disk", name, ordinal)?;
    let live = api.get_opt(name).await?;
    let id = if let Some(live) = live {
        if !matches!(previous, Some(CreateState::Issued | CreateState::Observed)) {
            return Err(ObserveError::Foreign);
        }
        let expected = state
            .storage
            .iter()
            .find(|s| s.ordinal == ordinal)
            .and_then(|s| s.disk.as_ref());
        let id = disk_identity(&live, &desired, catalog, uid, expected, expected_arm)?;
        Some((id, live))
    } else {
        let replay = access.replay_issued && previous == Some(CreateState::Issued);
        if !local::may_issue(previous) && !replay {
            return Ok(false);
        }
        if replay {
            tracing::warn!(
                kind = "Disk",
                name,
                ordinal,
                "replaying issued create after controller restart"
            );
        }
        if !replay && previous != Some(CreateState::Planned) {
            local::record(
                management.clone(),
                catalog,
                uid,
                state,
                "Disk",
                name,
                ordinal,
                CreateState::Planned,
            )
            .await?;
        }
        if !replay {
            local::record(
                management.clone(),
                catalog,
                uid,
                state,
                "Disk",
                name,
                ordinal,
                CreateState::Issued,
            )
            .await?;
        }
        verify_current(management.clone(), catalog).await?;
        match api
            .create(&kube::api::PostParams::default(), &desired)
            .await
        {
            Ok(live) => {
                let id = disk_identity(&live, &desired, catalog, uid, None, expected_arm)?;
                Some((id, live))
            }
            Err(error) if local::definite_rejection(&error) => {
                local::record(
                    management,
                    catalog,
                    uid,
                    state,
                    "Disk",
                    name,
                    ordinal,
                    CreateState::Rejected,
                )
                .await?;
                return Err(ObserveError::Api(error));
            }
            Err(error) => return Err(ObserveError::Api(error)),
        }
    };
    let (id, live) = id.expect("disk is observed");
    if storage(state, ordinal).disk.as_ref() != Some(&id) {
        if storage(state, ordinal).disk.is_some() {
            return Err(ObserveError::Foreign);
        }
        storage(state, ordinal).disk = Some(id);
        local::save(management, catalog, uid, state).await?;
        return Ok(false);
    }
    let ready = live
        .data
        .pointer("/status/id")
        .and_then(Value::as_str)
        .is_some_and(|id| id.eq_ignore_ascii_case(expected_arm))
        && live
            .data
            .pointer("/status/conditions")
            .and_then(Value::as_array)
            .is_some_and(|conditions| {
                conditions.iter().any(|condition| {
                    condition["type"] == "Ready"
                        && condition["status"] == "True"
                        && condition["observedGeneration"]
                            .as_i64()
                            .is_some_and(|generation| {
                                generation >= live.metadata.generation.unwrap_or(i64::MAX)
                            })
                })
            });
    let Some(actual) = access.arm()?.request(expected_arm, false).await? else {
        return Ok(false);
    };
    arm_disk(
        &actual,
        expected_arm,
        &tags(catalog, uid)?,
        &access.location,
    )?;
    if previous != Some(CreateState::Observed) {
        local::record(
            management,
            catalog,
            uid,
            state,
            "Disk",
            name,
            ordinal,
            CreateState::Observed,
        )
        .await?;
        return Ok(false);
    }
    Ok(ready)
}

fn set_id(
    state: &mut EntryStatus,
    kind: &str,
    ordinal: i32,
    id: ResourceIdentity,
) -> Result<bool, ObserveError> {
    let slot = match kind {
        "Namespace" => &mut state.namespace,
        "Cluster" => &mut state.cnpg_cluster,
        "PersistentVolume" => &mut storage(state, ordinal).pv,
        _ => return Err(ObserveError::Foreign),
    };
    if slot.as_ref() == Some(&id) {
        Ok(false)
    } else if slot.is_some() {
        Err(ObserveError::Foreign)
    } else {
        *slot = Some(id);
        Ok(true)
    }
}

fn initialize_arm_ids(
    state: &mut EntryStatus,
    group_id: &str,
    cluster_name: &str,
    instances: i32,
) -> Result<bool, ObserveError> {
    let mut changed = false;
    for ordinal in 1..=instances {
        let expected = arm_id(group_id, &disk_name(cluster_name, ordinal));
        let item = storage(state, ordinal);
        if item
            .arm_id
            .as_ref()
            .is_some_and(|actual| !actual.eq_ignore_ascii_case(&expected))
        {
            return Err(ObserveError::Foreign);
        }
        if item.arm_id.is_none() {
            item.arm_id = Some(expected);
            changed = true;
        }
    }
    Ok(changed)
}

pub async fn reconcile(
    management: Client,
    observed: &TenantDatabaseCatalog,
    replay_issued: bool,
) -> Result<(Action, TenantDatabaseCatalog), ObserveError> {
    let mut catalog = verify_current(management.clone(), observed).await?;
    let mut waiting = false;
    if let Some(uid) = catalog
        .status
        .as_ref()
        .and_then(|s| {
            s.entries
                .keys()
                .find(|uid| !catalog.spec.entries.contains_key(*uid))
        })
        .cloned()
    {
        catalog = status::update(management, &catalog, &uid, None).await?;
        return Ok((Action::requeue(RETRY), catalog));
    }
    let access = load(management.clone(), &catalog, replay_issued).await?;
    let generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    let mut entries: Vec<_> = catalog.spec.entries.clone().into_iter().collect();
    entries.sort_by_key(|(_, spec)| creation_order(spec.deleting, catalog.spec.closed));
    for (uid, spec) in entries {
        let (namespace, cluster) = ownership::names(
            catalog.uid().as_deref().ok_or(ObserveError::Identity)?,
            &uid,
        )
        .map_err(|_| ObserveError::Foreign)?;
        let mut state = catalog
            .status
            .as_ref()
            .and_then(|s| s.entries.get(&uid))
            .cloned()
            .unwrap_or_else(|| entry(&uid, generation, &access));
        validate_state(&state, &uid, &namespace, &cluster, spec.instances, &access)?;
        if catalog
            .status
            .as_ref()
            .is_none_or(|s| !s.entries.contains_key(&uid))
        {
            local::save(management.clone(), &mut catalog, &uid, &state).await?;
            waiting = true;
            continue;
        }
        let creating = !spec.deleting && !catalog.spec.closed;
        let result = if !creating {
            finalize(
                management.clone(),
                &mut catalog,
                &uid,
                &namespace,
                &cluster,
                &mut state,
                spec.instances,
                &access,
            )
            .await
        } else {
            ensure(
                management.clone(),
                &mut catalog,
                &uid,
                &namespace,
                &cluster,
                &mut state,
                spec.instances,
                &access,
            )
            .await
        };
        match result {
            Ok(progress) => {
                if stop_after_progress(progress, creating, &mut waiting) {
                    return Ok((Action::requeue(RETRY), catalog));
                }
            }
            Err(ObserveError::Foreign) => {
                state.query = None;
                state.observed_generation = generation;
                local::set_health(&mut state, false, generation, Some("OwnershipInvalid"));
                state.phase = DatabasePhase::OwnershipInvalid;
                if catalog.status.as_ref().and_then(|s| s.entries.get(&uid)) != Some(&state) {
                    local::save(management.clone(), &mut catalog, &uid, &state).await?;
                    waiting = true;
                }
            }
            Err(error) => return Err(error),
        }
    }
    Ok((next_action(waiting, RETRY, RESYNC), catalog))
}

#[expect(
    clippy::too_many_arguments,
    reason = "exact Azure entry and storage identities"
)]
async fn ensure(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster_name: &str,
    state: &mut EntryStatus,
    instances: i32,
    access: &Access,
) -> Result<Progress, ObserveError> {
    if let Some(reason) = access.prerequisite_reason() {
        return local::blocked_progress(management, catalog, uid, state, reason).await;
    }
    let ns = local::object(
        "Namespace",
        namespace,
        None,
        local::identity(catalog, uid)?,
        None,
    )?;
    let id = local::create_with_replay(
        management.clone(),
        catalog,
        uid,
        state,
        local::core(access.client.clone(), None, "Namespace", "namespaces"),
        ns,
        "Namespace",
        0,
        access.replay_issued,
    )
    .await?;
    let Some(id) = id else {
        return local::blocked_progress(management, catalog, uid, state, "UnknownCreateOutcome")
            .await;
    };
    if set_id(state, "Namespace", 0, id)? {
        local::save(management, catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let runtime = azure_runtime::observe(access.client.clone()).await;
    if let Err(error) = &runtime {
        tracing::warn!(%error, "Azure database runtime observation unavailable");
    }
    if !matches!(runtime, Ok("Ready")) {
        return local::blocked_progress(management, catalog, uid, state, "RuntimeNotReady").await;
    }
    if initialize_arm_ids(state, &access.group_id, cluster_name, instances)? {
        local::save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let mut disks_waiting = false;
    for ordinal in 1..=instances {
        let name = disk_name(cluster_name, ordinal);
        let expected_arm = arm_id(&access.group_id, &name);
        if !disk_ready(
            management.clone(),
            catalog,
            uid,
            state,
            access,
            &name,
            ordinal,
            &expected_arm,
        )
        .await?
        {
            disks_waiting = true;
        }
    }
    if disks_waiting {
        return local::blocked_progress(management, catalog, uid, state, "DiskPending").await;
    }
    let mut volumes_changed = false;
    for ordinal in 1..=instances {
        let expected_arm = storage(state, ordinal)
            .arm_id
            .clone()
            .ok_or(ObserveError::Foreign)?;
        let pv = volume(
            catalog,
            uid,
            namespace,
            cluster_name,
            ordinal,
            &expected_arm,
        )?;
        let id = local::create_with_replay(
            management.clone(),
            catalog,
            uid,
            state,
            local::core(
                access.client.clone(),
                None,
                "PersistentVolume",
                "persistentvolumes",
            ),
            pv,
            "PersistentVolume",
            ordinal,
            access.replay_issued,
        )
        .await?;
        let Some(id) = id else {
            return local::blocked_progress(
                management,
                catalog,
                uid,
                state,
                "UnknownCreateOutcome",
            )
            .await;
        };
        if set_id(state, "PersistentVolume", ordinal, id)? {
            volumes_changed = true;
        }
    }
    if volumes_changed {
        local::save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let desired = cluster(catalog, uid, namespace, cluster_name, instances)?;
    let cluster_api = local::api(
        access.client.clone(),
        Some(namespace),
        "postgresql.cnpg.io",
        "v1",
        "Cluster",
        "clusters",
    );
    let id = local::create_with_replay(
        management.clone(),
        catalog,
        uid,
        state,
        cluster_api.clone(),
        desired,
        "Cluster",
        0,
        access.replay_issued,
    )
    .await?;
    let Some(id) = id else {
        return local::blocked_progress(management, catalog, uid, state, "UnknownCreateOutcome")
            .await;
    };
    if set_id(state, "Cluster", 0, id.clone())? {
        local::save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let mut claims_waiting = false;
    let mut claims_changed = false;
    for ordinal in 1..=instances {
        let desired = claim(catalog, uid, namespace, cluster_name, ordinal)?;
        let claims = local::core(
            access.client.clone(),
            Some(namespace),
            "PersistentVolumeClaim",
            "persistentvolumeclaims",
        );
        let Some(actual) = claims.get_opt(&desired.name_any()).await? else {
            claims_waiting = true;
            continue;
        };
        let bound = state
            .storage
            .iter()
            .find(|s| s.ordinal == ordinal)
            .and_then(|s| s.pvc.as_ref());
        let claimed = local::check(&actual, catalog, uid, &desired.name_any(), bound)?;
        if !local::owned_by_cluster(actual.metadata.owner_references.as_deref(), &id)
            || !local::matches_desired(&actual, &desired)
        {
            return Err(ObserveError::Foreign);
        }
        if storage(state, ordinal).pvc.as_ref() != Some(&claimed) {
            storage(state, ordinal).pvc = Some(claimed);
            claims_changed = true;
        }
    }
    if claims_waiting {
        return local::blocked_progress(management, catalog, uid, state, "ClaimPending").await;
    }
    if claims_changed {
        local::save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let live = cluster_api.get(cluster_name).await?;
    local::check(
        &live,
        catalog,
        uid,
        cluster_name,
        state.cnpg_cluster.as_ref(),
    )?;
    let secret_name = format!("{cluster_name}-superuser");
    let secrets = local::core(access.client.clone(), Some(namespace), "Secret", "secrets");
    if let Some(secret) = secrets.get_opt(&secret_name).await? {
        if !local::owned_by_cluster(secret.metadata.owner_references.as_deref(), &id)
            || secret.data.pointer("/type").and_then(Value::as_str)
                != Some("kubernetes.io/basic-auth")
        {
            return Err(ObserveError::Foreign);
        }
        let secret_id = ResourceIdentity {
            name: secret_name,
            uid: secret.uid().ok_or(ObserveError::Foreign)?,
        };
        if state
            .credentials
            .as_ref()
            .is_some_and(|bound| bound != &secret_id)
        {
            return Err(ObserveError::Foreign);
        }
        state.credentials = Some(secret_id);
    } else if state.credentials.is_some() {
        return Err(ObserveError::Foreign);
    }
    let pods = local::core(access.client.clone(), Some(namespace), "Pod", "pods")
        .list(
            &kube::api::ListParams::default()
                .labels(&format!("cnpg.io/cluster={cluster_name}"))
                .limit(4),
        )
        .await?;
    if pods.items.len() > instances as usize || pods.metadata.continue_.is_some() {
        return Err(ObserveError::Foreign);
    }
    state.instances = pods
        .items
        .iter()
        .map(|pod| {
            if !local::owned_by_cluster(pod.metadata.owner_references.as_deref(), &id) {
                return Err(ObserveError::Foreign);
            }
            Ok(InstanceObservation {
                name: pod.name_any(),
                uid: pod.uid().ok_or(ObserveError::Foreign)?,
                role: pod
                    .metadata
                    .labels
                    .as_ref()
                    .and_then(|labels| labels.get("cnpg.io/instanceRole"))
                    .cloned()
                    .unwrap_or_else(|| "unknown".into()),
                ready: pod
                    .data
                    .pointer("/status/conditions")
                    .and_then(Value::as_array)
                    .is_some_and(|conditions| {
                        conditions.iter().any(|condition| {
                            condition["type"] == "Ready" && condition["status"] == "True"
                        })
                    }),
            })
        })
        .collect::<Result<_, _>>()?;
    for item in &mut state.storage {
        let pv = local::core(
            access.client.clone(),
            None,
            "PersistentVolume",
            "persistentvolumes",
        )
        .get(&format!("pv-{cluster_name}-{}", item.ordinal))
        .await?;
        let pvc = local::core(
            access.client.clone(),
            Some(namespace),
            "PersistentVolumeClaim",
            "persistentvolumeclaims",
        )
        .get(&disk_name(cluster_name, item.ordinal))
        .await?;
        local::check(&pv, catalog, uid, &pv.name_any(), item.pv.as_ref())?;
        local::check(&pvc, catalog, uid, &pvc.name_any(), item.pvc.as_ref())?;
        if !local::matches_desired(
            &pv,
            &volume(
                catalog,
                uid,
                namespace,
                cluster_name,
                item.ordinal,
                item.arm_id.as_deref().ok_or(ObserveError::Foreign)?,
            )?,
        ) || !local::matches_desired(
            &pvc,
            &claim(catalog, uid, namespace, cluster_name, item.ordinal)?,
        ) {
            return Err(ObserveError::Foreign);
        }
        item.healthy = pv.data.pointer("/status/phase") == Some(&json!("Bound"))
            && pvc.data.pointer("/status/phase") == Some(&json!("Bound"));
    }
    let ready = live
        .data
        .pointer("/status/readyInstances")
        .and_then(Value::as_i64)
        == Some(i64::from(instances))
        && state.credentials.is_some()
        && state.storage.len() == instances as usize
        && state.storage.iter().all(|item| item.healthy)
        && state.instances.len() == instances as usize
        && state.instances.iter().all(|pod| pod.ready);
    let generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    local::set_health(state, ready, generation, None);
    state.query = state
        .credentials
        .as_ref()
        .filter(|_| ready)
        .map(|secret| QueryIdentity {
            cluster_uid: id.uid,
            credential_uid: secret.uid.clone(),
        });
    state.observed_generation = generation;
    if catalog.status.as_ref().and_then(|s| s.entries.get(uid)) != Some(state) {
        local::save(management, catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    Ok(if ready {
        Progress::Stable
    } else {
        Progress::Waiting
    })
}

#[expect(
    clippy::too_many_arguments,
    reason = "exact Azure entry and storage identities"
)]
async fn finalize(
    management: Client,
    catalog: &mut TenantDatabaseCatalog,
    uid: &str,
    namespace: &str,
    cluster_name: &str,
    state: &mut EntryStatus,
    instances: i32,
    access: &Access,
) -> Result<Progress, ObserveError> {
    let generation = catalog.metadata.generation.ok_or(ObserveError::Identity)?;
    local::set_health(state, false, generation, Some("Deleting"));
    state.phase = DatabasePhase::Deleting;
    state.query = None;
    state.observed_generation = generation;
    if state.finalization.is_none() {
        state.finalization = Some(FinalizationStatus {
            terminal_verified: false,
            verified_absent: vec![],
            pending: (1..=instances)
                .map(|ordinal| arm_id(&access.group_id, &disk_name(cluster_name, ordinal)))
                .collect(),
        });
    }
    if catalog.status.as_ref().and_then(|s| s.entries.get(uid)) != Some(state) {
        local::save(management.clone(), catalog, uid, state).await?;
        return Ok(Progress::Changed);
    }
    let clusters = local::api(
        access.client.clone(),
        Some(namespace),
        "postgresql.cnpg.io",
        "v1",
        "Cluster",
        "clusters",
    );
    let desired = cluster(catalog, uid, namespace, cluster_name, instances)?;
    if local::adopt(
        management.clone(),
        catalog,
        uid,
        state,
        clusters.clone(),
        &desired,
        "Cluster",
        0,
    )
    .await?
    {
        return Ok(Progress::Changed);
    }
    if local::delete_exact(
        management.clone(),
        catalog,
        uid,
        clusters,
        &desired,
        state.cnpg_cluster.as_ref(),
    )
    .await?
        == Progress::Waiting
    {
        return Ok(Progress::Waiting);
    }
    let mut dependent_waiting = false;
    let secret_name = format!("{cluster_name}-superuser");
    let secrets = local::core(access.client.clone(), Some(namespace), "Secret", "secrets");
    if let Some(secret) = secrets.get_opt(&secret_name).await? {
        let cluster = state.cnpg_cluster.as_ref().ok_or(ObserveError::Foreign)?;
        if !local::owned_by_cluster(secret.metadata.owner_references.as_deref(), cluster)
            || secret.data.pointer("/type") != Some(&json!("kubernetes.io/basic-auth"))
        {
            return Err(ObserveError::Foreign);
        }
        let id = ResourceIdentity {
            name: secret_name.clone(),
            uid: secret.uid().ok_or(ObserveError::Foreign)?,
        };
        if state.credentials.as_ref().is_some_and(|bound| bound != &id) {
            return Err(ObserveError::Foreign);
        }
        if state.credentials.is_none() {
            state.credentials = Some(id.clone());
            local::save(management.clone(), catalog, uid, state).await?;
            return Ok(Progress::Changed);
        }
        verify_current(management.clone(), catalog).await?;
        secrets
            .delete(
                &secret_name,
                &kube::api::DeleteParams {
                    preconditions: Some(kube::api::Preconditions {
                        uid: Some(id.uid),
                        resource_version: None,
                    }),
                    ..Default::default()
                },
            )
            .await?;
        dependent_waiting = true;
    }
    for ordinal in 1..=instances {
        let desired_claim = claim(catalog, uid, namespace, cluster_name, ordinal)?;
        let claims = local::core(
            access.client.clone(),
            Some(namespace),
            "PersistentVolumeClaim",
            "persistentvolumeclaims",
        );
        if local::adopt(
            management.clone(),
            catalog,
            uid,
            state,
            claims.clone(),
            &desired_claim,
            "PersistentVolumeClaim",
            ordinal,
        )
        .await?
        {
            return Ok(Progress::Changed);
        }
        if local::delete_exact(
            management.clone(),
            catalog,
            uid,
            claims,
            &desired_claim,
            state
                .storage
                .iter()
                .find(|s| s.ordinal == ordinal)
                .and_then(|s| s.pvc.as_ref()),
        )
        .await?
            == Progress::Waiting
        {
            dependent_waiting = true;
        }
    }
    if dependent_waiting {
        return Ok(Progress::Waiting);
    }
    let mut volume_waiting = false;
    for ordinal in 1..=instances {
        let id = arm_id(&access.group_id, &disk_name(cluster_name, ordinal));
        let desired_volume = volume(catalog, uid, namespace, cluster_name, ordinal, &id)?;
        let volumes = local::core(
            access.client.clone(),
            None,
            "PersistentVolume",
            "persistentvolumes",
        );
        if local::adopt(
            management.clone(),
            catalog,
            uid,
            state,
            volumes.clone(),
            &desired_volume,
            "PersistentVolume",
            ordinal,
        )
        .await?
        {
            return Ok(Progress::Changed);
        }
        if local::delete_exact(
            management.clone(),
            catalog,
            uid,
            volumes,
            &desired_volume,
            state
                .storage
                .iter()
                .find(|s| s.ordinal == ordinal)
                .and_then(|s| s.pv.as_ref()),
        )
        .await?
            == Progress::Waiting
        {
            volume_waiting = true;
        }
    }
    if volume_waiting {
        return Ok(Progress::Waiting);
    }
    let namespaces = local::core(access.client.clone(), None, "Namespace", "namespaces");
    let desired_namespace = local::object(
        "Namespace",
        namespace,
        None,
        local::identity(catalog, uid)?,
        None,
    )?;
    if local::adopt(
        management.clone(),
        catalog,
        uid,
        state,
        namespaces.clone(),
        &desired_namespace,
        "Namespace",
        0,
    )
    .await?
    {
        return Ok(Progress::Changed);
    }
    if local::delete_exact(
        management.clone(),
        catalog,
        uid,
        namespaces,
        &desired_namespace,
        state.namespace.as_ref(),
    )
    .await?
        == Progress::Waiting
    {
        return Ok(Progress::Waiting);
    }

    crate::finalize::azure::cleanup(
        management,
        catalog,
        uid,
        cluster_name,
        state,
        instances,
        access,
    )
    .await
}

#[cfg(test)]
#[path = "azure_live_tests.rs"]
mod live_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::{CatalogEntry, CreateIntent, TenantDatabaseCatalogSpec};
    use crate::finalize::local::all_creates_resolved;
    use axum::{
        Json, Router,
        http::{Method, Request, StatusCode},
        routing::any,
    };
    use std::{
        collections::BTreeSet,
        convert::Infallible,
        sync::{Arc, Mutex},
    };
    use tower::service_fn;

    const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const ENTRIES: [&str; 3] = [
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    ];
    const GROUP: &str = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/pg-rg";

    fn fixtures() -> (TenantDatabaseCatalog, Access) {
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
                            (*uid).into(),
                            CatalogEntry {
                                name: format!("database-{index}"),
                                instances: 3,
                                deleting: false,
                            },
                        )
                    })
                    .collect(),
            },
        );
        catalog.metadata.uid = Some(CATALOG.into());
        catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
        let client = Client::new(
            service_fn(|_| async {
                Ok::<_, Infallible>(axum::http::Response::new(axum::body::Body::empty()))
            }),
            "default",
        );
        let access = Access {
            client,
            capability_ready: true,
            replay_issued: false,
            storage_namespace: "tenant-db-storage-tenant-a".into(),
            storage_uid: "storage-uid".into(),
            group_id: GROUP.into(),
            location: "eastus".into(),
            arm: Some(Arm {
                client: reqwest::Client::new(),
                client_id: String::new(),
                token_file: String::new(),
                token_endpoint: String::new(),
                endpoint: "https://management.azure.com".into(),
            }),
        };
        (catalog, access)
    }

    #[tokio::test]
    async fn arm_identity_preparation_batches_all_instance_ordinals() {
        let (_, access) = fixtures();
        let uid = ENTRIES[0];
        let (_, cluster_name) = ownership::names(CATALOG, uid).unwrap();
        let mut state = entry(uid, 1, &access);
        assert!(initialize_arm_ids(&mut state, GROUP, &cluster_name, 3).unwrap());
        assert_eq!(state.storage.len(), 3);
        for ordinal in 1..=3 {
            assert_eq!(
                storage(&mut state, ordinal).arm_id.as_deref(),
                Some(arm_id(GROUP, &disk_name(&cluster_name, ordinal)).as_str()),
            );
        }
        assert!(!initialize_arm_ids(&mut state, GROUP, &cluster_name, 3).unwrap());
        storage(&mut state, 2).arm_id = Some("foreign".into());
        assert!(matches!(
            initialize_arm_ids(&mut state, GROUP, &cluster_name, 3),
            Err(ObserveError::Foreign)
        ));
    }

    #[tokio::test]
    async fn nine_disks_are_uid_scoped_pinned_and_statically_bound() {
        let (catalog, access) = fixtures();
        let mut names = BTreeSet::new();
        let mut ids = BTreeSet::new();
        for uid in ENTRIES {
            let (namespace, cluster_name) = ownership::names(CATALOG, uid).unwrap();
            let cnpg = cluster(&catalog, uid, &namespace, &cluster_name, 3).unwrap();
            assert_eq!(cnpg.data["spec"]["storage"]["size"], "4Gi");
            assert_eq!(cnpg.data["spec"]["imageName"], POSTGRES_IMAGE);
            for ordinal in 1..=3 {
                let name = disk_name(&cluster_name, ordinal);
                let id = arm_id(GROUP, &name);
                assert!(names.insert(name.clone()));
                assert!(ids.insert(id.clone()));
                let disk = disk(&catalog, uid, &access, &name).unwrap();
                assert_eq!(
                    disk.namespace().as_deref(),
                    Some("tenant-db-storage-tenant-a")
                );
                assert_eq!(
                    disk.types.unwrap().api_version,
                    "compute.azure.com/v1api20240302"
                );
                assert_eq!(disk.data["spec"]["owner"]["armId"], GROUP);
                assert_eq!(disk.data["spec"]["sku"]["name"], "StandardSSD_LRS");
                assert_eq!(disk.data["spec"]["diskSizeGB"], 4);
                assert_eq!(disk.data["spec"]["creationData"]["createOption"], "Empty");
                assert_eq!(disk.data["spec"]["tags"]["cnpg-vcluster-entry-uid"], uid);
                let pv = volume(&catalog, uid, &namespace, &cluster_name, ordinal, &id).unwrap();
                let pvc = claim(&catalog, uid, &namespace, &cluster_name, ordinal).unwrap();
                assert_eq!(pv.data["spec"]["csi"]["volumeHandle"], id);
                assert_eq!(pv.data["spec"]["csi"]["driver"], "disk.csi.azure.com");
                assert_eq!(pv.data["spec"]["persistentVolumeReclaimPolicy"], "Retain");
                assert_eq!(pvc.data["spec"]["volumeName"], pv.name_any());
                assert_eq!(pvc.data["spec"]["resources"]["requests"]["storage"], "4Gi");
                assert_eq!(pvc.data["spec"]["storageClassName"], "cnpg-azure-disk");
            }
        }
        assert_eq!((names.len(), ids.len()), (9, 9));
        let successor = ownership::names(CATALOG, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee").unwrap();
        assert!(!names.contains(&disk_name(&successor.1, 1)));
    }

    #[tokio::test]
    async fn restart_timeout_and_partial_provisioning_keep_exact_disk_intent() {
        let (catalog, access) = fixtures();
        let uid = ENTRIES[0];
        let (namespace, cluster_name) = ownership::names(CATALOG, uid).unwrap();
        let mut state = entry(uid, 1, &access);
        storage(&mut state, 1).arm_id = Some(arm_id(GROUP, &disk_name(&cluster_name, 1)));
        state.create_intents.push(CreateIntent {
            kind: "Disk".into(),
            name: disk_name(&cluster_name, 1),
            ordinal: 1,
            state: CreateState::Issued,
        });
        assert!(validate_state(&state, uid, &namespace, &cluster_name, 3, &access).is_ok());
        assert!(!local::may_issue(
            local::intent(&state, "Disk", &disk_name(&cluster_name, 1), 1).unwrap()
        ));
        assert!(!all_creates_resolved(&state));
        state.finalization = Some(FinalizationStatus {
            terminal_verified: false,
            pending: vec![state.storage[0].arm_id.clone().unwrap()],
            verified_absent: vec![],
        });
        assert!(!state.finalization.as_ref().unwrap().terminal_verified);
        state.create_intents[0].state = CreateState::Observed;
        assert!(!all_creates_resolved(&state));
        state.storage[0].disk = Some(ResourceIdentity {
            name: disk_name(&cluster_name, 1),
            uid: "observed-disk-uid".into(),
        });
        assert!(all_creates_resolved(&state));
        state.storage[0].disk.as_mut().unwrap().name = "foreign".into();
        assert!(validate_state(&state, uid, &namespace, &cluster_name, 3, &access).is_err());
        state.storage[0].disk.as_mut().unwrap().name = disk_name(&cluster_name, 1);
        state.storage[0].arm_id = Some("/subscriptions/foreign/resourceGroups/foreign".into());
        assert!(validate_state(&state, uid, &namespace, &cluster_name, 3, &access).is_err());
        assert_eq!(catalog.spec.entries.len(), 3);
    }

    #[tokio::test]
    async fn delayed_aso_disk_after_timeout_is_adopted_without_a_second_create() {
        use axum::{body::Body as AxumBody, http::Response};
        use http_body_util::BodyExt;
        use kube::client::Body;

        let (mut catalog, access) = fixtures();
        let uid = ENTRIES[0];
        let (_, cluster_name) = ownership::names(CATALOG, uid).unwrap();
        let name = disk_name(&cluster_name, 1);
        let expected_arm = arm_id(GROUP, &name);
        let mut state = entry(uid, 1, &access);
        storage(&mut state, 1).arm_id = Some(expected_arm.clone());
        state.create_intents.push(CreateIntent {
            kind: "Disk".into(),
            name: name.clone(),
            ordinal: 1,
            state: CreateState::Issued,
        });
        catalog.spec.entries.retain(|key, _| key == uid);
        catalog.metadata.resource_version = Some("1".into());
        catalog.metadata.generation = Some(1);
        catalog.metadata.finalizers = Some(vec![crate::api::FINALIZER.into()]);
        catalog.metadata.owner_references = Some(vec![
            k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference {
                api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
                kind: "Tenant".into(),
                name: "tenant-a".into(),
                uid: "tenant-uid".into(),
                ..Default::default()
            },
        ]);
        catalog.status = Some(crate::api::CatalogStatus {
            entries: [(uid.into(), state.clone())].into(),
            observer: None,
        });
        struct Mock {
            catalog: TenantDatabaseCatalog,
            disk: DynamicObject,
            appeared: bool,
            writes: usize,
            creates: usize,
        }
        let mut disk = disk(&catalog, uid, &access, &name).unwrap();
        disk.metadata.uid = Some("aso-disk-uid".into());
        disk.metadata.generation = Some(1);
        let mock = Arc::new(Mutex::new(Mock {
            catalog: catalog.clone(),
            disk,
            appeared: false,
            writes: 0,
            creates: 0,
        }));
        let disk_path = format!(
            "/apis/compute.azure.com/v1api20240302/namespaces/{}/disks/{name}",
            access.storage_namespace,
        );
        let catalog_path = "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabasecatalogs/tenant-a";
        let server = mock.clone();
        let management = Client::new(
            service_fn(move |request: Request<Body>| {
                let server = server.clone();
                let disk_path = disk_path.clone();
                async move {
                    let method = request.method().clone();
                    let path = request.uri().path().to_owned();
                    let body = request.into_body().collect().await.unwrap().to_bytes();
                    let mut server = server.lock().unwrap();
                    let (code, value) = match (method.clone(), path.as_str()) {
                        (Method::GET, p) if p == catalog_path => {
                            (StatusCode::OK, json!(server.catalog))
                        }
                        (
                            Method::GET,
                            "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a",
                        ) => (
                            StatusCode::OK,
                            json!({
                                "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha4","kind":"Tenant",
                                "metadata":{"name":"tenant-a","uid":"tenant-uid",
                                    "finalizers":["tenancy.cnpg-vcluster.io/finalizer"]},
                                "spec":{"provider":{"type":"azure"}},
                                "status":{"catalogCreateIntent":{"namespace":"tenant-db-tenant-a",
                                    "name":"tenant-a","tenantUID":"tenant-uid"},
                                    "databaseCapability":{"namespace":"tenant-db-tenant-a",
                                        "namespaceUID":"db-ns-uid","catalogUID":CATALOG}}
                            }),
                        ),
                        (Method::GET, "/api/v1/namespaces/tenant-db-tenant-a") => (
                            StatusCode::OK,
                            json!({"apiVersion":"v1","kind":"Namespace",
                            "metadata":{"name":"tenant-db-tenant-a","uid":"db-ns-uid",
                                "labels":{"tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid"}}}),
                        ),
                        (Method::GET, p) if p == disk_path => {
                            if server.appeared {
                                (StatusCode::OK, json!(server.disk))
                            } else {
                                (
                                    StatusCode::NOT_FOUND,
                                    json!({"reason":"NotFound","code":404}),
                                )
                            }
                        }
                        (Method::PUT, p) if p == format!("{catalog_path}/status") => {
                            let mut next: TenantDatabaseCatalog =
                                serde_json::from_slice(&body).unwrap();
                            assert_eq!(
                                next.metadata.resource_version,
                                server.catalog.metadata.resource_version
                            );
                            server.writes += 1;
                            next.metadata.resource_version = Some((server.writes + 1).to_string());
                            server.catalog = next.clone();
                            (StatusCode::OK, json!(next))
                        }
                        (Method::POST, _) => {
                            server.creates += 1;
                            (
                                StatusCode::BAD_REQUEST,
                                json!({"reason":"UnexpectedCreate","code":400}),
                            )
                        }
                        _ => panic!("unexpected Kubernetes API call: {path}"),
                    };
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(code)
                            .header("content-type", "application/json")
                            .body(AxumBody::from(value.to_string()))
                            .unwrap(),
                    )
                }
            }),
            "default",
        );
        assert!(
            !disk_ready(
                management.clone(),
                &mut catalog,
                uid,
                &mut state,
                &access,
                &name,
                1,
                &expected_arm
            )
            .await
            .unwrap()
        );
        assert_eq!(mock.lock().unwrap().creates, 0);
        mock.lock().unwrap().appeared = true;
        assert!(
            !disk_ready(
                management.clone(),
                &mut catalog,
                uid,
                &mut state,
                &access,
                &name,
                1,
                &expected_arm
            )
            .await
            .unwrap()
        );
        assert_eq!(state.create_intents[0].state, CreateState::Issued);
        assert_eq!(state.storage[0].disk.as_ref().unwrap().uid, "aso-disk-uid");
        assert!(!all_creates_resolved(&state));
        assert!(matches!(
            disk_ready(
                management,
                &mut catalog,
                uid,
                &mut state,
                &access,
                &name,
                1,
                &expected_arm
            )
            .await,
            Err(ObserveError::Azure(_))
        ));
        assert_eq!(state.create_intents[0].state, CreateState::Issued);
        assert_eq!(mock.lock().unwrap().creates, 0);
        assert_eq!(mock.lock().unwrap().writes, 1);
    }

    #[tokio::test]
    async fn cloud_absence_and_foreign_disk_identity_fail_closed() {
        let (catalog, access) = fixtures();
        let uid = ENTRIES[0];
        let name = "pg-a-1";
        let expected = arm_id(GROUP, name);
        let expected_tags = tags(&catalog, uid).unwrap();
        let mut actual = json!({
            "id":expected,"location":"eastus",
            "properties":{"diskSizeGB":4,"provisioningState":"Succeeded"},
            "sku":{"name":"StandardSSD_LRS"},"tags":expected_tags
        });
        assert!(arm_disk(&actual, &expected, &expected_tags, &access.location).is_ok());
        actual["properties"]["provisioningState"] = json!("Creating");
        assert!(matches!(
            arm_disk(&actual, &expected, &expected_tags, &access.location),
            Err(ObserveError::Azure(_))
        ));
        actual["properties"]["provisioningState"] = json!("Succeeded");
        actual["tags"]["cnpg-vcluster-entry-uid"] = json!(ENTRIES[1]);
        assert!(arm_disk(&actual, &expected, &expected_tags, &access.location).is_err());
        actual["tags"] = expected_tags.clone();
        actual["sku"]["name"] = json!("Premium_LRS");
        assert!(arm_disk(&actual, &expected, &expected_tags, &access.location).is_err());
        let desired = disk(&catalog, uid, &access, name).unwrap();
        let mut live = desired.clone();
        live.metadata.uid = Some("disk-uid".into());
        assert!(disk_identity(&live, &desired, &catalog, uid, None, &expected).is_ok());
        live.data["spec"]["sku"]["name"] = json!("Premium_LRS");
        assert!(disk_identity(&live, &desired, &catalog, uid, None, &expected).is_err());
        live.data = desired.data.clone();
        live.metadata.uid = Some("replacement-uid".into());
        assert!(
            disk_identity(
                &live,
                &desired,
                &catalog,
                uid,
                Some(&ResourceIdentity {
                    name: name.into(),
                    uid: "disk-uid".into(),
                }),
                &expected
            )
            .is_err()
        );
        let pv = volume(&catalog, uid, "db-a", "pg-a", 1, &expected).unwrap();
        let mut drifted = pv.clone();
        drifted.data["spec"]["persistentVolumeReclaimPolicy"] = json!("Delete");
        assert!(!local::matches_desired(&drifted, &pv));
        drifted.data = pv.data.clone();
        drifted.data["spec"]["csi"]["volumeHandle"] = json!("/subscriptions/foreign/disk");
        assert!(!local::matches_desired(&drifted, &pv));
    }

    #[tokio::test]
    async fn missing_disk_identity_and_capability_do_not_preserve_ready_projection() {
        let (_, mut access) = fixtures();
        let mut state = entry(ENTRIES[0], 1, &access);
        state.phase = DatabasePhase::Ready;
        state.query = Some(QueryIdentity {
            cluster_uid: "cluster-uid".into(),
            credential_uid: "credential-uid".into(),
        });
        access.capability_ready = false;
        access.arm = None;
        assert_eq!(access.prerequisite_reason(), Some("CapabilityNotReady"));
        assert!(matches!(access.arm(), Err(ObserveError::Azure(_))));
        access.capability_ready = true;
        assert_eq!(
            access.prerequisite_reason(),
            Some("DiskIdentityUnavailable")
        );
        state.query = None;
        local::set_health(&mut state, false, 2, access.prerequisite_reason());
        assert_eq!(state.phase, DatabasePhase::Degraded);
        assert!(state.query.is_none());
        assert_eq!(state.conditions[0].reason, "DiskIdentityUnavailable");
    }

    #[tokio::test]
    async fn arm_fallback_requires_token_exact_id_and_direct_not_found() {
        let (catalog, mut access) = fixtures();
        let id = arm_id(GROUP, "pg-test-1");
        let calls = Arc::new(Mutex::new(Vec::new()));
        let deleted = Arc::new(Mutex::new(false));
        let server_calls = calls.clone();
        let server_deleted = deleted.clone();
        let expected = id.clone();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let handler = move |request: Request<axum::body::Body>| {
            let calls = server_calls.clone();
            let deleted = server_deleted.clone();
            let expected = expected.clone();
            async move {
                let path = request.uri().path().to_string();
                let method = request.method().clone();
                let token = request
                    .headers()
                    .get("authorization")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("")
                    .to_string();
                let query = request.uri().query().unwrap_or("").to_string();
                calls
                    .lock()
                    .unwrap()
                    .push((method.clone(), path.clone(), query, token.clone()));
                if token != "Bearer test-token" || path != expected {
                    return (StatusCode::FORBIDDEN, Json(json!({"error":"denied"})));
                }
                if method == Method::DELETE {
                    *deleted.lock().unwrap() = true;
                    return (StatusCode::ACCEPTED, Json(Value::Null));
                }
                if *deleted.lock().unwrap() {
                    return (StatusCode::NOT_FOUND, Json(json!({"error":"not found"})));
                }
                (
                    StatusCode::OK,
                    Json(json!({
                        "id":expected, "location":"eastus",
                        "sku":{"name":"StandardSSD_LRS"},
                        "properties":{"diskSizeGB":4,"provisioningState":"Succeeded"},
                        "tags":tags(&catalog, ENTRIES[0]).unwrap(),
                    })),
                )
            }
        };
        let server = tokio::spawn(async move {
            axum::serve(listener, Router::new().route("/{*path}", any(handler)))
                .await
                .unwrap();
        });
        access.arm.as_mut().unwrap().endpoint = format!("http://{address}");
        let actual = access
            .arm()
            .unwrap()
            .request_with_token(&id, false, "test-token")
            .await
            .unwrap()
            .unwrap();
        arm_disk(
            &actual,
            &id,
            &tags(&fixtures().0, ENTRIES[0]).unwrap(),
            "eastus",
        )
        .unwrap();
        assert!(
            access
                .arm()
                .unwrap()
                .request_with_token(&id, true, "test-token")
                .await
                .unwrap()
                .is_some()
        );
        assert!(
            access
                .arm()
                .unwrap()
                .request_with_token(&id, false, "test-token")
                .await
                .unwrap()
                .is_none()
        );
        assert!(
            access
                .arm()
                .unwrap()
                .request_with_token(&id, false, "wrong-token")
                .await
                .is_err()
        );
        let calls = calls.lock().unwrap();
        assert_eq!(
            calls
                .iter()
                .map(|(method, _, _, _)| method)
                .collect::<Vec<_>>(),
            vec![&Method::GET, &Method::DELETE, &Method::GET, &Method::GET]
        );
        assert!(
            calls
                .iter()
                .all(|(_, path, query, _)| path == &id && query == "api-version=2024-03-02")
        );
        server.abort();
    }

    #[tokio::test]
    async fn nine_cloud_disks_require_independent_direct_absence_and_preserve_siblings() {
        use std::collections::BTreeMap;

        let (catalog, mut access) = fixtures();
        let mut expected = BTreeMap::new();
        for uid in ENTRIES {
            let (_, cluster) = ownership::names(CATALOG, uid).unwrap();
            for ordinal in 1..=3 {
                let id = arm_id(GROUP, &disk_name(&cluster, ordinal));
                expected.insert(
                    id.clone(),
                    json!({
                        "id":id,"location":"eastus","sku":{"name":"StandardSSD_LRS"},
                        "properties":{"diskSizeGB":4,"provisioningState":"Succeeded"},
                        "tags":tags(&catalog, uid).unwrap(),
                    }),
                );
            }
        }
        let cloud = Arc::new(Mutex::new(expected));
        let calls = Arc::new(Mutex::new(Vec::<(Method, String)>::new()));
        let live = cloud.clone();
        let recorded = calls.clone();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let router = Router::new().route(
                "/{*path}",
                any(move |request: Request<axum::body::Body>| {
                    let cloud = live.clone();
                    let calls = recorded.clone();
                    async move {
                        let path = request.uri().path().to_owned();
                        if request.uri().query() != Some("api-version=2024-03-02")
                            || request
                                .headers()
                                .get("authorization")
                                .and_then(|h| h.to_str().ok())
                                != Some("Bearer test-token")
                        {
                            return (StatusCode::FORBIDDEN, Json(json!({"error":"denied"})));
                        }
                        calls
                            .lock()
                            .unwrap()
                            .push((request.method().clone(), path.clone()));
                        match request.method().as_str() {
                            "GET" => match cloud.lock().unwrap().get(&path).cloned() {
                                Some(disk) => (StatusCode::OK, Json(disk)),
                                None => (StatusCode::NOT_FOUND, Json(json!({"error":"NotFound"}))),
                            },
                            "DELETE" => {
                                if cloud.lock().unwrap().remove(&path).is_none() {
                                    (StatusCode::NOT_FOUND, Json(Value::Null))
                                } else {
                                    (StatusCode::ACCEPTED, Json(Value::Null))
                                }
                            }
                            _ => (StatusCode::METHOD_NOT_ALLOWED, Json(Value::Null)),
                        }
                    }
                }),
            );
            axum::serve(listener, router).await.unwrap();
        });
        access.arm.as_mut().unwrap().endpoint = format!("http://{endpoint}");
        for (index, uid) in ENTRIES.iter().enumerate() {
            let (_, cluster) = ownership::names(CATALOG, uid).unwrap();
            for ordinal in 1..=3 {
                let id = arm_id(GROUP, &disk_name(&cluster, ordinal));
                let arm = access.arm().unwrap();
                let actual = arm
                    .request_with_token(&id, false, "test-token")
                    .await
                    .unwrap()
                    .unwrap();
                arm_disk(&actual, &id, &tags(&catalog, uid).unwrap(), "eastus").unwrap();
                assert!(
                    arm.request_with_token(&id, true, "test-token")
                        .await
                        .unwrap()
                        .is_some()
                );
                assert!(
                    arm.request_with_token(&id, false, "test-token")
                        .await
                        .unwrap()
                        .is_none()
                );
            }
            assert_eq!(cloud.lock().unwrap().len(), 6 - index * 3);
            for sibling in ENTRIES.iter().skip(index + 1) {
                let (_, cluster) = ownership::names(CATALOG, sibling).unwrap();
                assert!(
                    cloud
                        .lock()
                        .unwrap()
                        .contains_key(&arm_id(GROUP, &disk_name(&cluster, 1)))
                );
            }
        }
        assert!(cloud.lock().unwrap().is_empty());
        assert_eq!(calls.lock().unwrap().len(), 27);
        server.abort();
    }

    #[tokio::test]
    async fn late_arm_create_after_aso_disappearance_blocks_then_recovers_exact_deletion() {
        use axum::{body::Body as AxumBody, http::Response};
        use http_body_util::BodyExt;
        use kube::client::Body;
        use std::sync::atomic::{AtomicBool, Ordering};

        let (mut catalog, mut access) = fixtures();
        let uid = ENTRIES[0];
        let (_, cluster_name) = ownership::names(CATALOG, uid).unwrap();
        let disk_name = disk_name(&cluster_name, 1);
        let id = arm_id(GROUP, &disk_name);
        let first = catalog.spec.entries.get_mut(uid).unwrap();
        first.instances = 1;
        first.deleting = true;
        catalog.metadata.resource_version = Some("1".into());
        catalog.metadata.generation = Some(1);
        catalog.metadata.finalizers = Some(vec![crate::api::FINALIZER.into()]);
        catalog.metadata.owner_references = Some(vec![
            k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference {
                api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
                kind: "Tenant".into(),
                name: "tenant-a".into(),
                uid: "tenant-uid".into(),
                ..Default::default()
            },
        ]);
        let mut state = entry(uid, 1, &access);
        let item = storage(&mut state, 1);
        item.arm_id = Some(id.clone());
        item.disk = Some(ResourceIdentity {
            name: disk_name.clone(),
            uid: "aso-original".into(),
        });
        state.create_intents.push(CreateIntent {
            kind: "Disk".into(),
            name: disk_name.clone(),
            ordinal: 1,
            state: CreateState::Issued,
        });
        state.finalization = Some(FinalizationStatus {
            terminal_verified: false,
            verified_absent: vec![],
            pending: vec![id.clone()],
        });
        catalog.status = Some(crate::api::CatalogStatus {
            entries: [(uid.into(), state.clone())].into(),
            observer: None,
        });
        let current = Arc::new(Mutex::new(catalog.clone()));
        let writes = Arc::new(Mutex::new((0usize, 0usize)));
        let storage = access.storage_namespace.clone();
        let disk_path = format!(
            "/apis/compute.azure.com/v1api20240302/namespaces/{storage}/disks/{disk_name}",
        );
        let catalog_path = "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabasecatalogs/tenant-a";
        let api_state = current.clone();
        let api_writes = writes.clone();
        let management = Client::new(
            service_fn(move |request: Request<Body>| {
                let state = api_state.clone();
                let writes = api_writes.clone();
                let disk_path = disk_path.clone();
                async move {
                    let method = request.method().clone();
                    let path = request.uri().path().to_owned();
                    let body = request.into_body().collect().await.unwrap().to_bytes();
                    let mut current = state.lock().unwrap();
                    let (code, value) = match (method.clone(), path.as_str()) {
                        (Method::GET, p) if p == catalog_path => (StatusCode::OK, json!(*current)),
                        (
                            Method::GET,
                            "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a",
                        ) => (
                            StatusCode::OK,
                            json!({
                                "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha4","kind":"Tenant",
                                "metadata":{"name":"tenant-a","uid":"tenant-uid",
                                    "finalizers":["tenancy.cnpg-vcluster.io/finalizer"]},
                                "spec":{"provider":{"type":"azure"}},
                                "status":{"catalogCreateIntent":{"namespace":"tenant-db-tenant-a",
                                    "name":"tenant-a","tenantUID":"tenant-uid"},
                                    "databaseCapability":{"namespace":"tenant-db-tenant-a",
                                        "namespaceUID":"db-ns-uid","catalogUID":CATALOG}}
                            }),
                        ),
                        (Method::GET, "/api/v1/namespaces/tenant-db-tenant-a") => (
                            StatusCode::OK,
                            json!({"apiVersion":"v1","kind":"Namespace",
                            "metadata":{"name":"tenant-db-tenant-a","uid":"db-ns-uid",
                                "labels":{"tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid"}}}),
                        ),
                        (Method::GET, p) if p == disk_path => (
                            StatusCode::NOT_FOUND,
                            json!({"reason":"NotFound","code":404}),
                        ),
                        (Method::PUT, p)
                            if p == format!("{catalog_path}/status") || p == catalog_path =>
                        {
                            let mut next: TenantDatabaseCatalog =
                                serde_json::from_slice(&body).unwrap();
                            assert_eq!(
                                next.metadata.resource_version,
                                current.metadata.resource_version
                            );
                            let mut counts = writes.lock().unwrap();
                            if p == catalog_path {
                                counts.1 += 1
                            } else {
                                counts.0 += 1
                            };
                            next.metadata.resource_version =
                                Some((counts.0 + counts.1 + 1).to_string());
                            *current = next.clone();
                            (StatusCode::OK, json!(next))
                        }
                        _ => panic!("unexpected Kubernetes API call: {method} {path}"),
                    };
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(code)
                            .header("content-type", "application/json")
                            .body(AxumBody::from(value.to_string()))
                            .unwrap(),
                    )
                }
            }),
            "default",
        );

        let present = Arc::new(AtomicBool::new(false));
        let fail_delete = Arc::new(AtomicBool::new(false));
        let arm_calls = Arc::new(Mutex::new(Vec::<Method>::new()));
        let arm_present = present.clone();
        let delete_failure = fail_delete.clone();
        let calls = arm_calls.clone();
        let expected_id = id.clone();
        let cloud_tags = tags(&catalog, uid).unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let router = Router::new().route(
                "/{*path}",
                any(move |request: Request<AxumBody>| {
                    let present = arm_present.clone();
                    let fail_delete = delete_failure.clone();
                    let calls = calls.clone();
                    let expected_id = expected_id.clone();
                    let tags = cloud_tags.clone();
                    async move {
                        if request.uri().path() == "/token" && request.method() == Method::POST {
                            return (StatusCode::OK, Json(json!({"access_token":"test-token"})));
                        }
                        if request.uri().path() != expected_id
                            || request.uri().query() != Some("api-version=2024-03-02")
                            || request
                                .headers()
                                .get("authorization")
                                .and_then(|v| v.to_str().ok())
                                != Some("Bearer test-token")
                        {
                            return (StatusCode::FORBIDDEN, Json(json!({"error":"foreign"})));
                        }
                        calls.lock().unwrap().push(request.method().clone());
                        if request.method() == Method::DELETE {
                            if fail_delete.swap(false, Ordering::SeqCst) {
                                return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error":"retry"})));
                            }
                            present.store(false, Ordering::SeqCst);
                            return (StatusCode::ACCEPTED, Json(Value::Null));
                        }
                        if present.load(Ordering::SeqCst) {
                            (
                                StatusCode::OK,
                                Json(json!({
                                    "id":expected_id,"location":"eastus",
                                    "sku":{"name":"StandardSSD_LRS"},
                                    "properties":{"diskSizeGB":4,"provisioningState":"Succeeded"},"tags":tags,
                                })),
                            )
                        } else {
                            (StatusCode::NOT_FOUND, Json(json!({"error":"NotFound"})))
                        }
                    }
                }),
            );
            axum::serve(listener, router).await.unwrap();
        });
        let token = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(token.path(), "test-assertion").unwrap();
        let arm = access.arm.as_mut().unwrap();
        arm.endpoint = format!("http://{address}");
        arm.token_endpoint = format!("http://{address}/token");
        arm.token_file = token.path().to_str().unwrap().into();

        let workload_calls = Arc::new(Mutex::new(Vec::<String>::new()));
        let workload_record = workload_calls.clone();
        access.client = Client::new(
            service_fn(move |request: Request<Body>| {
                let calls = workload_record.clone();
                async move {
                    assert_eq!(request.method(), Method::GET);
                    calls.lock().unwrap().push(request.uri().path().to_owned());
                    Ok::<_, Infallible>(
                        Response::builder()
                            .status(StatusCode::NOT_FOUND)
                            .header("content-type", "application/json")
                            .body(AxumBody::from(r#"{"reason":"NotFound","code":404}"#))
                            .unwrap(),
                    )
                }
            }),
            "default",
        );
        let (namespace, _) = ownership::names(CATALOG, uid).unwrap();
        assert_eq!(
            finalize(
                management.clone(),
                &mut catalog,
                uid,
                &namespace,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await
            .unwrap(),
            Progress::Changed,
        );
        assert_eq!(
            finalize(
                management.clone(),
                &mut catalog,
                uid,
                &namespace,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await
            .unwrap(),
            Progress::Changed,
        );
        let order = workload_calls.lock().unwrap().clone();
        let paths = [
            format!("/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters/{cluster_name}"),
            format!("/api/v1/namespaces/{namespace}/secrets/{cluster_name}-superuser"),
            format!("/api/v1/namespaces/{namespace}/persistentvolumeclaims/{cluster_name}-1"),
            format!("/api/v1/persistentvolumes/pv-{cluster_name}-1"),
            format!("/api/v1/namespaces/{namespace}"),
        ];
        let positions: Vec<_> = paths
            .iter()
            .map(|path| {
                order
                    .iter()
                    .position(|seen| seen == path)
                    .expect("workload stage was inspected")
            })
            .collect();
        assert!(positions.windows(2).all(|pair| pair[0] < pair[1]));
        assert_eq!(state.create_intents[0].state, CreateState::Issued);
        assert!(!state.finalization.as_ref().unwrap().terminal_verified);
        assert_eq!(writes.lock().unwrap().1, 0);
        let lost_identity = access.arm.take();
        assert!(matches!(
            crate::finalize::azure::cleanup(
                management.clone(),
                &mut catalog,
                uid,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await,
            Err(ObserveError::Azure(_))
        ));
        access.arm = lost_identity;
        present.store(true, Ordering::SeqCst);
        assert_eq!(
            crate::finalize::azure::cleanup(
                management.clone(),
                &mut catalog,
                uid,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await
            .unwrap(),
            Progress::Changed,
        );
        assert_eq!(state.create_intents[0].state, CreateState::Observed);
        assert_eq!(writes.lock().unwrap().1, 0);
        fail_delete.store(true, Ordering::SeqCst);
        assert!(matches!(
            crate::finalize::azure::cleanup(
                management.clone(),
                &mut catalog,
                uid,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await,
            Err(ObserveError::Azure(_))
        ));
        assert_eq!(
            state.finalization.as_ref().unwrap().pending,
            vec![id.clone()]
        );
        assert_eq!(writes.lock().unwrap().1, 0);
        assert_eq!(
            crate::finalize::azure::cleanup(
                management.clone(),
                &mut catalog,
                uid,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await
            .unwrap(),
            Progress::Waiting,
        );
        assert_eq!(
            state.finalization.as_ref().unwrap().pending,
            vec![id.clone()]
        );
        assert_eq!(
            crate::finalize::azure::cleanup(
                management.clone(),
                &mut catalog,
                uid,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await
            .unwrap(),
            Progress::Changed,
        );
        assert_eq!(
            state.finalization.as_ref().unwrap().verified_absent,
            vec![id.clone()]
        );
        assert_eq!(
            crate::finalize::azure::cleanup(
                management.clone(),
                &mut catalog,
                uid,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await
            .unwrap(),
            Progress::Changed,
        );
        assert!(state.finalization.as_ref().unwrap().terminal_verified);
        assert_eq!(
            crate::finalize::azure::cleanup(
                management,
                &mut catalog,
                uid,
                &cluster_name,
                &mut state,
                1,
                &access
            )
            .await
            .unwrap(),
            Progress::Changed,
        );
        let current = current.lock().unwrap();
        assert!(!current.spec.entries.contains_key(uid));
        assert_eq!(current.spec.entries.len(), 2);
        assert_eq!(writes.lock().unwrap().1, 1);
        assert_eq!(
            *arm_calls.lock().unwrap(),
            vec![
                Method::GET,
                Method::GET,
                Method::GET,
                Method::DELETE,
                Method::GET,
                Method::DELETE,
                Method::GET,
                Method::GET,
                Method::GET
            ]
        );
        server.abort();
    }
}
