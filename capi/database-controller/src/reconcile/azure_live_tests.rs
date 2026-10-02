use super::*;
use crate::api::{CatalogEntry, CatalogStatus, CreateIntent, TenantDatabaseCatalogSpec};
use axum::{
    Json, Router,
    body::Body as AxumBody,
    http::{Method, Request, Response, StatusCode},
    routing::any,
};
use http_body_util::BodyExt;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference;
use kube::client::Body;
use std::{
    collections::{BTreeMap, BTreeSet},
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
const CATALOG_PATH: &str = "/apis/tenancy.cnpg-vcluster.io/v1alpha1/namespaces/tenant-db-tenant-a/tenantdatabasecatalogs/tenant-a";

struct ClusterApi {
    catalog: TenantDatabaseCatalog,
    objects: BTreeMap<String, DynamicObject>,
    known_paths: BTreeSet<String>,
    deleted: BTreeMap<String, (String, String)>,
    verified_absent: BTreeSet<String>,
    events: Vec<String>,
    spec_updates: usize,
    observed_cloud_creates: usize,
}

fn resource_path(resource: &DynamicObject) -> String {
    let name = resource.name_any();
    let namespace = resource.namespace();
    match resource.types.as_ref().map(|kind| kind.kind.as_str()) {
        Some("Namespace") => format!("/api/v1/namespaces/{name}"),
        Some("PersistentVolume") => format!("/api/v1/persistentvolumes/{name}"),
        Some("PersistentVolumeClaim") => format!(
            "/api/v1/namespaces/{}/persistentvolumeclaims/{name}",
            namespace.unwrap(),
        ),
        Some("Secret") => format!("/api/v1/namespaces/{}/secrets/{name}", namespace.unwrap()),
        Some("Cluster") => format!(
            "/apis/postgresql.cnpg.io/v1/namespaces/{}/clusters/{name}",
            namespace.unwrap(),
        ),
        Some("Disk") => format!(
            "/apis/compute.azure.com/v1api20240302/namespaces/{}/disks/{name}",
            namespace.unwrap(),
        ),
        _ => panic!("unrecognized test resource"),
    }
}

fn insert(api: &mut ClusterApi, mut object: DynamicObject, id: &str) {
    object.metadata.uid = Some(id.into());
    let path = resource_path(&object);
    api.known_paths.insert(path.clone());
    api.objects.insert(path, object);
}

fn initial_state() -> (ClusterApi, Access, BTreeMap<String, Value>) {
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
                            deleting: true,
                        },
                    )
                })
                .collect(),
        },
    );
    catalog.metadata.namespace = Some("tenant-db-tenant-a".into());
    catalog.metadata.uid = Some(CATALOG.into());
    catalog.metadata.generation = Some(1);
    catalog.metadata.resource_version = Some("1".into());
    catalog.metadata.finalizers = Some(vec![crate::api::FINALIZER.into()]);
    catalog.metadata.owner_references = Some(vec![OwnerReference {
        api_version: "tenancy.cnpg-vcluster.io/v1alpha4".into(),
        kind: "Tenant".into(),
        name: "tenant-a".into(),
        uid: "tenant-uid".into(),
        ..Default::default()
    }]);
    let access = Access {
        client: Client::new(
            service_fn(|_| async { Ok::<_, Infallible>(Response::new(AxumBody::empty())) }),
            "default",
        ),
        capability_ready: true,
        storage_namespace: "tenant-db-storage-tenant-a".into(),
        storage_uid: "storage-uid".into(),
        group_id: GROUP.into(),
        location: "eastus".into(),
        arm: Some(Arm {
            client: reqwest::Client::new(),
            client_id: "test-client".into(),
            token_file: String::new(),
            token_endpoint: String::new(),
            endpoint: String::new(),
        }),
    };
    let mut api = ClusterApi {
        catalog: catalog.clone(),
        objects: BTreeMap::new(),
        known_paths: BTreeSet::new(),
        deleted: BTreeMap::new(),
        verified_absent: BTreeSet::new(),
        events: vec![],
        spec_updates: 0,
        observed_cloud_creates: 0,
    };
    let mut cloud = BTreeMap::new();
    let mut statuses = BTreeMap::new();
    for uid in ENTRIES {
        let (namespace, cluster_name) = ownership::names(CATALOG, uid).unwrap();
        let cluster_id = format!("cluster-{uid}");
        let mut state = entry(uid, 1, &access);
        state.phase = DatabasePhase::Ready;
        state.namespace = Some(ResourceIdentity {
            name: namespace.clone(),
            uid: format!("namespace-{uid}"),
        });
        state.cnpg_cluster = Some(ResourceIdentity {
            name: cluster_name.clone(),
            uid: cluster_id.clone(),
        });
        state.credentials = Some(ResourceIdentity {
            name: format!("{cluster_name}-superuser"),
            uid: format!("secret-{uid}"),
        });
        for (kind, name) in [
            ("Namespace", namespace.as_str()),
            ("Cluster", cluster_name.as_str()),
        ] {
            state.create_intents.push(CreateIntent {
                kind: kind.into(),
                name: name.into(),
                ordinal: 0,
                state: CreateState::Observed,
            });
        }
        let namespace_obj = local::object(
            "Namespace",
            &namespace,
            None,
            local::identity(&catalog, uid).unwrap(),
            None,
        )
        .unwrap();
        insert(&mut api, namespace_obj, &format!("namespace-{uid}"));
        insert(
            &mut api,
            cluster(&catalog, uid, &namespace, &cluster_name, 3).unwrap(),
            &cluster_id,
        );
        let mut secret = local::object(
            "Secret",
            &format!("{cluster_name}-superuser"),
            Some(&namespace),
            local::identity(&catalog, uid).unwrap(),
            None,
        )
        .unwrap();
        secret.data["type"] = json!("kubernetes.io/basic-auth");
        let owner = OwnerReference {
            api_version: "postgresql.cnpg.io/v1".into(),
            kind: "Cluster".into(),
            name: cluster_name.clone(),
            uid: cluster_id,
            ..Default::default()
        };
        secret.metadata.owner_references = Some(vec![owner.clone()]);
        insert(&mut api, secret, &format!("secret-{uid}"));
        for ordinal in 1..=3 {
            let name = disk_name(&cluster_name, ordinal);
            let id = arm_id(GROUP, &name);
            let pv_name = format!("pv-{name}");
            let pv_id = format!("pv-{uid}-{ordinal}");
            let pvc_id = format!("pvc-{uid}-{ordinal}");
            let disk_uid = format!("disk-{uid}-{ordinal}");
            let item = storage(&mut state, ordinal);
            item.arm_id = Some(id.clone());
            item.disk = Some(ResourceIdentity {
                name: name.clone(),
                uid: disk_uid.clone(),
            });
            item.pv = Some(ResourceIdentity {
                name: pv_name.clone(),
                uid: pv_id.clone(),
            });
            item.pvc = Some(ResourceIdentity {
                name: name.clone(),
                uid: pvc_id.clone(),
            });
            state.create_intents.extend([
                CreateIntent {
                    kind: "Disk".into(),
                    name: name.clone(),
                    ordinal,
                    state: CreateState::Issued,
                },
                CreateIntent {
                    kind: "PersistentVolume".into(),
                    name: pv_name,
                    ordinal,
                    state: CreateState::Observed,
                },
            ]);
            let pv = volume(&catalog, uid, &namespace, &cluster_name, ordinal, &id).unwrap();
            insert(&mut api, pv, &pv_id);
            let mut pvc = claim(&catalog, uid, &namespace, &cluster_name, ordinal).unwrap();
            pvc.metadata.owner_references = Some(vec![owner.clone()]);
            insert(&mut api, pvc, &pvc_id);
            let disk = disk(&catalog, uid, &access, &name).unwrap();
            insert(&mut api, disk, &disk_uid);
            cloud.insert(
                id.clone(),
                json!({
                    "id":id,"location":"eastus","sku":{"name":"StandardSSD_LRS"},
                    "properties":{"diskSizeGB":4,"provisioningState":"Succeeded"},
                    "tags":tags(&catalog, uid).unwrap(),
                }),
            );
        }
        statuses.insert(uid.into(), state);
    }
    catalog.status = Some(CatalogStatus {
        entries: statuses,
        observer: None,
    });
    api.catalog = catalog;
    (api, access, cloud)
}

fn require_absence(api: &ClusterApi, path: &str) {
    assert!(
        api.verified_absent.contains(path),
        "dependent delete before NotFound: {path}"
    );
}

fn management_client(
    mock: Arc<Mutex<ClusterApi>>,
    cloud: Arc<Mutex<BTreeMap<String, Value>>>,
) -> Client {
    Client::new(
        service_fn(move |request: Request<Body>| {
            let mock = mock.clone();
            let cloud = cloud.clone();
            async move {
                let method = request.method().clone();
                let path = request.uri().path().to_owned();
                let bytes = request.into_body().collect().await.unwrap().to_bytes();
                let mut mock = mock.lock().unwrap();
                let (code, value) = match (method.clone(), path.as_str()) {
                    (Method::GET, CATALOG_PATH) => (StatusCode::OK, json!(mock.catalog)),
                    (Method::GET, "/apis/tenancy.cnpg-vcluster.io/v1alpha4/tenants/tenant-a") => (
                        StatusCode::OK,
                        json!({
                            "apiVersion":"tenancy.cnpg-vcluster.io/v1alpha4","kind":"Tenant",
                            "metadata":{"name":"tenant-a","uid":"tenant-uid",
                                "finalizers":["tenancy.cnpg-vcluster.io/finalizer"]},
                            "spec":{"provider":{"type":"azure"}},
                            "status":{"catalogCreateIntent":{
                                "namespace":"tenant-db-tenant-a","name":"tenant-a","tenantUID":"tenant-uid"},
                                "databaseCapability":{"namespace":"tenant-db-tenant-a",
                                    "namespaceUID":"catalog-namespace-uid","catalogUID":CATALOG}}
                        }),
                    ),
                    (Method::GET, "/api/v1/namespaces/tenant-db-tenant-a") => (
                        StatusCode::OK,
                        json!({
                            "apiVersion":"v1","kind":"Namespace",
                            "metadata":{"name":"tenant-db-tenant-a","uid":"catalog-namespace-uid",
                                "labels":{"tenancy.cnpg-vcluster.io/tenant-uid":"tenant-uid"}}
                        }),
                    ),
                    (Method::GET, _) => {
                        assert!(mock.known_paths.contains(&path), "unexpected GET {path}");
                        if let Some(resource) = mock.objects.get(&path) {
                            (StatusCode::OK, json!(resource))
                        } else {
                            assert!(mock.deleted.contains_key(&path), "unissued GET 404 {path}");
                            mock.verified_absent.insert(path.clone());
                            mock.events.push(format!("KUBE_NOT_FOUND:{path}"));
                            (
                                StatusCode::NOT_FOUND,
                                json!({"reason":"NotFound","code":404}),
                            )
                        }
                    }
                    (Method::DELETE, _) => {
                        let params: Value = serde_json::from_slice(&bytes).unwrap();
                        let live = mock
                            .objects
                            .remove(&path)
                            .expect("only live objects may be deleted");
                        assert_eq!(
                            params.pointer("/preconditions/uid").and_then(Value::as_str),
                            live.uid().as_deref(),
                        );
                        let owner = live
                            .metadata
                            .labels
                            .as_ref()
                            .and_then(|labels| labels.get(ownership::ENTRY_LABEL))
                            .expect("all workload and disk resources bind an entry");
                        let kind = live.types.as_ref().unwrap().kind.clone();
                        let (namespace, cluster) = ownership::names(CATALOG, owner).unwrap();
                        let secret =
                            format!("/api/v1/namespaces/{namespace}/secrets/{cluster}-superuser");
                        let ordinal = live
                            .name_any()
                            .rsplit('-')
                            .next()
                            .and_then(|last| last.parse::<i32>().ok());
                        match kind.as_str() {
                            "Cluster" => {}
                            "Secret" => require_absence(
                                &mock,
                                &format!(
                                    "/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters/{cluster}"
                                ),
                            ),
                            "PersistentVolumeClaim" => {
                                require_absence(&mock, &secret);
                                if let Some(prior) = ordinal.filter(|n| *n > 1) {
                                    require_absence(
                                        &mock,
                                        &format!(
                                            "/api/v1/persistentvolumes/pv-{cluster}-{}",
                                            prior - 1
                                        ),
                                    );
                                }
                            }
                            "PersistentVolume" => require_absence(
                                &mock,
                                &format!(
                                    "/api/v1/namespaces/{namespace}/persistentvolumeclaims/{cluster}-{}",
                                    ordinal.unwrap()
                                ),
                            ),
                            "Namespace" => {
                                for ordinal in 1..=3 {
                                    require_absence(
                                        &mock,
                                        &format!(
                                            "/api/v1/persistentvolumes/pv-{cluster}-{ordinal}"
                                        ),
                                    );
                                }
                            }
                            "Disk" => {
                                require_absence(&mock, &format!("/api/v1/namespaces/{namespace}"));
                                if let Some(prior) = ordinal.filter(|n| *n > 1) {
                                    require_absence(
                                        &mock,
                                        &format!(
                                            "/apis/compute.azure.com/v1api20240302/namespaces/tenant-db-storage-tenant-a/disks/{cluster}-{}",
                                            prior - 1
                                        ),
                                    );
                                    assert!(mock.events.contains(&format!(
                                        "ARM_GET_NOT_FOUND:{}",
                                        arm_id(GROUP, &format!("{cluster}-{}", prior - 1))
                                    )));
                                }
                            }
                            _ => panic!("unexpected Kubernetes deletion kind {kind}"),
                        }
                        mock.deleted.insert(path.clone(), (owner.clone(), kind));
                        mock.events.push(format!(
                            "{}:{}",
                            live.types.as_ref().unwrap().kind,
                            live.name_any(),
                        ));
                        (
                            StatusCode::OK,
                            json!({
                                "apiVersion":"v1","kind":"Status","status":"Success","code":200
                            }),
                        )
                    }
                    (Method::PUT, p)
                        if p == format!("{CATALOG_PATH}/status") || p == CATALOG_PATH =>
                    {
                        let mut next: TenantDatabaseCatalog =
                            serde_json::from_slice(&bytes).unwrap();
                        assert_eq!(
                            next.metadata.resource_version,
                            mock.catalog.metadata.resource_version
                        );
                        if p == CATALOG_PATH {
                            let removed: Vec<_> = mock
                                .catalog
                                .spec
                                .entries
                                .keys()
                                .filter(|uid| !next.spec.entries.contains_key(*uid))
                                .cloned()
                                .collect();
                            assert_eq!(removed.len(), 1);
                            let proof = mock
                                .catalog
                                .status
                                .as_ref()
                                .unwrap()
                                .entries
                                .get(&removed[0])
                                .unwrap()
                                .finalization
                                .as_ref()
                                .unwrap();
                            assert!(proof.terminal_verified && proof.pending.is_empty());
                            assert_eq!(proof.verified_absent.len(), 3);
                            let (_, cluster) = ownership::names(CATALOG, &removed[0]).unwrap();
                            let expected: BTreeSet<_> = (1..=3)
                                .map(|ordinal| arm_id(GROUP, &format!("{cluster}-{ordinal}")))
                                .collect();
                            assert_eq!(
                                proof
                                    .verified_absent
                                    .iter()
                                    .cloned()
                                    .collect::<BTreeSet<_>>(),
                                expected
                            );
                            for id in &proof.verified_absent {
                                assert!(!cloud.lock().unwrap().contains_key(id));
                                let deleted = mock
                                    .events
                                    .iter()
                                    .position(|event| event == &format!("ARM:{id}"))
                                    .expect("exact ARM DELETE");
                                let absent = mock
                                    .events
                                    .iter()
                                    .rposition(|event| event == &format!("ARM_GET_NOT_FOUND:{id}"))
                                    .expect("direct post-DELETE NotFound");
                                assert!(deleted < absent);
                            }
                            assert!(mock.deleted.iter().all(|(deleted_path, (uid, _))| {
                                uid != &removed[0] || mock.verified_absent.contains(deleted_path)
                            }));
                            for (uid, entry) in &next.spec.entries {
                                assert_eq!(mock.catalog.spec.entries.get(uid), Some(entry));
                            }
                            mock.spec_updates += 1;
                            mock.events.push(format!("CATALOG_REMOVE:{}", removed[0]));
                        } else {
                            assert_eq!(next.spec, mock.catalog.spec);
                            let mut observed = 0;
                            for (uid, next_entry) in &next.status.as_ref().unwrap().entries {
                                let previous = mock
                                    .catalog
                                    .status
                                    .as_ref()
                                    .unwrap()
                                    .entries
                                    .get(uid)
                                    .unwrap();
                                for intent in &next_entry.create_intents {
                                    if intent.kind == "Disk"
                                        && intent.state == CreateState::Observed
                                        && previous.create_intents.iter().any(|old| {
                                            old.kind == "Disk"
                                                && old.ordinal == intent.ordinal
                                                && old.name == intent.name
                                                && old.state == CreateState::Issued
                                        })
                                    {
                                        assert!(mock.objects.values().any(|object| {
                                            object
                                                .types
                                                .as_ref()
                                                .is_some_and(|kind| kind.kind == "Disk")
                                                && object.name_any() == intent.name
                                                && object
                                                    .metadata
                                                    .labels
                                                    .as_ref()
                                                    .and_then(|labels| {
                                                        labels.get(ownership::ENTRY_LABEL)
                                                    })
                                                    .is_some_and(|label| label == uid)
                                        }));
                                        assert!(
                                            cloud
                                                .lock()
                                                .unwrap()
                                                .contains_key(&arm_id(GROUP, &intent.name))
                                        );
                                        assert_eq!(
                                            mock.events.last(),
                                            Some(&format!(
                                                "ARM_GET_SUCCEEDED:{}",
                                                arm_id(GROUP, &intent.name)
                                            ))
                                        );
                                        observed += 1;
                                    }
                                }
                            }
                            mock.observed_cloud_creates += observed;
                        }
                        let version = mock
                            .catalog
                            .metadata
                            .resource_version
                            .as_deref()
                            .unwrap()
                            .parse::<usize>()
                            .unwrap()
                            + 1;
                        next.metadata.resource_version = Some(version.to_string());
                        mock.catalog = next.clone();
                        (StatusCode::OK, json!(next))
                    }
                    _ => panic!("unexpected Kubernetes operation {method} {path}"),
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
    )
}

#[tokio::test]
async fn three_three_instance_entries_delete_workloads_then_aso_then_each_arm_disk() {
    let (initial, mut access, cloud) = initial_state();
    let cloud = Arc::new(Mutex::new(cloud));
    let mock = Arc::new(Mutex::new(initial));
    let client = management_client(mock.clone(), cloud.clone());
    access.client = client.clone();
    let token = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(token.path(), "test-assertion").unwrap();
    access.arm.as_mut().unwrap().token_file = token.path().to_str().unwrap().into();
    let event_state = mock.clone();
    let cloud_state = cloud.clone();
    let known_arm_ids: Arc<BTreeSet<String>> =
        Arc::new(cloud.lock().unwrap().keys().cloned().collect());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let router = Router::new().route(
            "/{*path}",
            any(move |request: Request<AxumBody>| {
                let events = event_state.clone();
                let cloud = cloud_state.clone();
                let known_ids = known_arm_ids.clone();
                async move {
                    if request.uri().path() == "/token" && request.method() == Method::POST {
                        return (StatusCode::OK, Json(json!({"access_token":"test-token"})));
                    }
                    let path = request.uri().path().to_owned();
                    if !known_ids.contains(&path)
                        || request.uri().query() != Some("api-version=2024-03-02")
                        || request
                            .headers()
                            .get("authorization")
                            .and_then(|v| v.to_str().ok())
                            != Some("Bearer test-token")
                    {
                        return (StatusCode::FORBIDDEN, Json(json!({"error":"denied"})));
                    }
                    if request.method() == Method::DELETE {
                        let mut cloud = cloud.lock().unwrap();
                        let disk = cloud.get(&path).expect("only expected disks are removed");
                        let uid = disk["tags"]["cnpg-vcluster-entry-uid"].as_str().unwrap();
                        let disk_name = path.rsplit('/').next().unwrap();
                        assert!(events.lock().unwrap().objects.values().all(|object| {
                            object
                                .metadata
                                .labels
                                .as_ref()
                                .and_then(|labels| labels.get(ownership::ENTRY_LABEL))
                                .is_none_or(|label| label != uid)
                                || (object
                                    .types
                                    .as_ref()
                                    .is_some_and(|types| types.kind == "Disk")
                                    && object.name_any() != disk_name)
                        }));
                        let disk_path = format!(
                            "/apis/compute.azure.com/v1api20240302/namespaces/tenant-db-storage-tenant-a/disks/{disk_name}"
                        );
                        assert!(events.lock().unwrap().verified_absent.contains(&disk_path));
                        cloud.remove(&path);
                        events.lock().unwrap().events.push(format!("ARM:{path}"));
                        return (StatusCode::ACCEPTED, Json(Value::Null));
                    }
                    let disk = cloud.lock().unwrap().get(&path).cloned();
                    match disk {
                        Some(disk) => {
                            events.lock().unwrap().events.push(format!("ARM_GET_SUCCEEDED:{path}"));
                            (StatusCode::OK, Json(disk))
                        }
                        None => {
                            let mut events = events.lock().unwrap();
                            assert!(events.events.contains(&format!("ARM:{path}")));
                            events.events.push(format!("ARM_GET_NOT_FOUND:{path}"));
                            (StatusCode::NOT_FOUND, Json(json!({"error":"NotFound"})))
                        }
                    }
                }
            }),
        );
        axum::serve(listener, router).await.unwrap();
    });
    let arm = access.arm.as_mut().unwrap();
    arm.endpoint = format!("http://{address}");
    arm.token_endpoint = format!("http://{address}/token");
    let mut catalog = mock.lock().unwrap().catalog.clone();
    for (index, uid) in ENTRIES.iter().enumerate() {
        let (namespace, cluster_name) = ownership::names(CATALOG, uid).unwrap();
        for _ in 0..80 {
            let mut state = catalog.status.as_ref().unwrap().entries[*uid].clone();
            finalize(
                client.clone(),
                &mut catalog,
                uid,
                &namespace,
                &cluster_name,
                &mut state,
                3,
                &access,
            )
            .await
            .unwrap();
            if !catalog.spec.entries.contains_key(*uid) {
                break;
            }
        }
        assert!(!catalog.spec.entries.contains_key(*uid));
        assert_eq!(catalog.spec.entries.len(), 2 - index);
        assert_eq!(cloud.lock().unwrap().len(), 6 - index * 3);
        let events = mock.lock().unwrap().events.clone();
        let position = |event: &str| events.iter().position(|seen| seen == event).unwrap();
        let cluster = position(&format!("Cluster:{cluster_name}"));
        let cluster_absent = position(&format!(
            "KUBE_NOT_FOUND:/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters/{cluster_name}"
        ));
        let secret = position(&format!("Secret:{cluster_name}-superuser"));
        let secret_absent = position(&format!(
            "KUBE_NOT_FOUND:/api/v1/namespaces/{namespace}/secrets/{cluster_name}-superuser"
        ));
        let namespace_event = position(&format!("Namespace:{namespace}"));
        let namespace_absent = position(&format!("KUBE_NOT_FOUND:/api/v1/namespaces/{namespace}"));
        let removed = position(&format!("CATALOG_REMOVE:{uid}"));
        assert!(cluster < cluster_absent && cluster_absent < secret && secret < secret_absent);
        let mut previous_pv_absent = secret_absent;
        let mut previous_arm_absent = namespace_absent;
        for ordinal in 1..=3 {
            let pvc = position(&format!("PersistentVolumeClaim:{cluster_name}-{ordinal}"));
            let pvc_absent = position(&format!(
                "KUBE_NOT_FOUND:/api/v1/namespaces/{namespace}/persistentvolumeclaims/{cluster_name}-{ordinal}"
            ));
            let pv = position(&format!("PersistentVolume:pv-{cluster_name}-{ordinal}"));
            let pv_absent = position(&format!(
                "KUBE_NOT_FOUND:/api/v1/persistentvolumes/pv-{cluster_name}-{ordinal}"
            ));
            assert!(
                previous_pv_absent < pvc && pvc < pvc_absent && pvc_absent < pv && pv < pv_absent
            );
            assert!(pv_absent < namespace_event);
            previous_pv_absent = pv_absent;
            let name = disk_name(&cluster_name, ordinal);
            let disk_path = format!(
                "/apis/compute.azure.com/v1api20240302/namespaces/tenant-db-storage-tenant-a/disks/{name}"
            );
            let id = arm_id(GROUP, &name);
            let terminal_create = position(&format!("ARM_GET_SUCCEEDED:{id}"));
            let aso = position(&format!("Disk:{name}"));
            let aso_absent = position(&format!("KUBE_NOT_FOUND:{disk_path}"));
            let arm = position(&format!("ARM:{id}"));
            let arm_absent = position(&format!("ARM_GET_NOT_FOUND:{id}"));
            assert!(namespace_absent < terminal_create && terminal_create < aso);
            assert!(
                previous_arm_absent < aso
                    && aso < aso_absent
                    && aso_absent < arm
                    && arm < arm_absent
                    && arm_absent < removed
            );
            previous_arm_absent = arm_absent;
        }
        assert!(previous_pv_absent < namespace_event && namespace_event < namespace_absent);
    }
    assert_eq!(mock.lock().unwrap().spec_updates, 3);
    assert_eq!(mock.lock().unwrap().observed_cloud_creates, 9);
    assert_eq!(
        mock.lock()
            .unwrap()
            .events
            .iter()
            .filter(|event| event.starts_with("ARM:"))
            .count(),
        9
    );
    assert!(cloud.lock().unwrap().is_empty());
    server.abort();
}
