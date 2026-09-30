use tenant_admin_shared::{
    ADMIN_CONTAINER_PORT, ADMIN_NAMESPACE, ADMIN_RESOURCE_NAME, ADMIN_SERVICE_PORT,
    API_SCHEMA_NAME, API_SCHEMA_VERSION, ApiEnvelope, ApiError, ApiErrorCode, ApiErrorEnvelope,
    lifecycle::{
        CreationCapability, TenantCreateRequest, TenantCreateResponse, TenantDeleteRequest,
        TenantDeleteResponse, TenantDeleteState, TenantField, TenantFieldError,
        TenantMutationIdentity,
    },
    query::{
        ConditionStatus, DatabaseClusterIdentity, DatabaseClusterObservation, DatabaseCondition,
        DatabaseInstanceObservation, DatabaseInstanceRole, DatabaseNotApplicableReason,
        DatabaseObservation, DatabaseObservationFreshness, DatabasePvcHealth, DatabaseQueryRequest,
        DatabaseQueryResponse, DatabaseQueryResult, DatabaseServices, DatabaseUnavailableReason,
        DisplayAttribute, ManagementOverview, OverviewSnapshot, ProviderMode, ProviderStatusView,
        TenantCounts, TenantDetail, TenantProvider, TenantSnapshot, TenantSnapshotIdentity,
        TenantSummary, TopologyEdge, TopologyEdgeKind, TopologyGraph, TopologyHealth, TopologyNode,
        TopologyNodeKind, UnknownProviderView,
    },
    routes::{
        API_OVERVIEW_PATH, API_PREFIX, API_TENANT_CREATE_PATH, API_TENANT_DATABASE_QUERY_PATH,
        API_TENANT_DELETE_PATH, API_TENANT_PATH, API_TENANT_TOPOLOGY_PATH, API_TENANTS_PATH,
        FRONTEND_FALLBACK_PATH, HEALTH_PATH, READINESS_PATH, TENANT_ADMIN_UNSAFE_REQUEST_HEADER,
        TENANT_ADMIN_UNSAFE_REQUEST_VALUE,
    },
};

#[test]
fn deployment_and_route_constants_are_exact() {
    assert_eq!(API_SCHEMA_NAME, "tenant-admin");
    assert_eq!(API_SCHEMA_VERSION, 4);
    assert_eq!(ADMIN_RESOURCE_NAME, "tenant-admin");
    assert_eq!(ADMIN_NAMESPACE, "tenant-system");
    assert_eq!(ADMIN_CONTAINER_PORT, 8080);
    assert_eq!(ADMIN_SERVICE_PORT, 80);
    assert_eq!(API_PREFIX, "/api/v1");
    assert_eq!(API_OVERVIEW_PATH, "/api/v1/overview");
    assert_eq!(API_TENANTS_PATH, "/api/v1/tenants");
    assert_eq!(API_TENANT_PATH, "/api/v1/tenants/{name}");
    assert_eq!(API_TENANT_CREATE_PATH, API_TENANTS_PATH);
    assert_eq!(API_TENANT_DELETE_PATH, API_TENANT_PATH);
    assert_eq!(API_TENANT_TOPOLOGY_PATH, "/api/v1/tenants/{name}/topology");
    assert_eq!(
        API_TENANT_DATABASE_QUERY_PATH,
        "/api/v1/tenants/{name}/database/query"
    );
    assert_eq!(
        TENANT_ADMIN_UNSAFE_REQUEST_HEADER,
        "x-tenant-admin-unsafe-request"
    );
    assert_eq!(TENANT_ADMIN_UNSAFE_REQUEST_VALUE, "1");
    assert_eq!(HEALTH_PATH, "/healthz");
    assert_eq!(READINESS_PATH, "/readyz");
    assert_eq!(FRONTEND_FALLBACK_PATH, "/*");
}

#[test]
fn lifecycle_contracts_are_versioned_provider_neutral_and_uid_bound() {
    let create = TenantCreateRequest {
        name: "demo".into(),
        workers: 2,
    };
    assert_eq!(
        serde_json::to_string(&create).unwrap(),
        r#"{"name":"demo","workers":2}"#
    );
    let identity = TenantMutationIdentity {
        name: "demo".into(),
        uid: "tenant-uid".into(),
        generation: Some(1),
    };
    let created = ApiEnvelope::new(TenantCreateResponse {
        identity: identity.clone(),
        provider: TenantProvider::Local,
        kubernetes_version: "1.36.4".into(),
    });
    let deleted = ApiEnvelope::new(TenantDeleteResponse {
        identity,
        state: TenantDeleteState::Accepted,
    });
    let request = TenantDeleteRequest {
        uid: "tenant-uid".into(),
        confirmation: "demo".into(),
    };
    assert_eq!(serde_json::to_value(created).unwrap()["schemaVersion"], 4);
    assert_eq!(
        serde_json::to_value(deleted).unwrap()["data"]["state"],
        "accepted"
    );
    assert_eq!(
        serde_json::to_string(&request).unwrap(),
        r#"{"uid":"tenant-uid","confirmation":"demo"}"#
    );
    let error = ApiError::new(ApiErrorCode::InvalidRequest, "invalid fields", false)
        .with_field_errors(vec![TenantFieldError {
            field: TenantField::Workers,
            code: "invalid-count".into(),
            message: "Workers must be from 1 through 3.".into(),
        }]);
    assert_eq!(
        serde_json::to_value(ApiErrorEnvelope::new(error)).unwrap()["error"]["fieldErrors"][0]["field"],
        "workers"
    );
}

#[test]
fn envelopes_have_stable_versioned_json() {
    let success = ApiEnvelope::new(vec!["alpha", "beta"]);
    assert_eq!(
        serde_json::to_string(&success).expect("success envelope serializes"),
        r#"{"schemaVersion":4,"data":["alpha","beta"]}"#
    );

    let error = ApiErrorEnvelope::new(ApiError::new(
        ApiErrorCode::KubernetesUnavailable,
        "management API unavailable",
        true,
    ));
    assert_eq!(
        serde_json::to_string(&error).expect("error envelope serializes"),
        r#"{"schemaVersion":4,"error":{"code":"kubernetes-unavailable","message":"management API unavailable","retryable":true}}"#
    );

    let decoded: ApiErrorEnvelope =
        serde_json::from_str(&serde_json::to_string(&error).expect("error serializes"))
            .expect("error round trips");
    assert_eq!(decoded, error);
}

#[test]
fn topology_serialization_is_deterministic() {
    let graph = TopologyGraph {
        tenant_name: "demo".into(),
        provider: TenantProvider::Local,
        nodes: vec![TopologyNode {
            id: "tenant/demo".into(),
            kind: TopologyNodeKind::Tenant,
            label: "demo".into(),
            health: TopologyHealth::Ready,
            resource: None,
            attributes: vec![DisplayAttribute {
                label: "Kubernetes".into(),
                value: "v1.36.0".into(),
            }],
        }],
        edges: vec![TopologyEdge {
            id: "tenant-to-control-plane".into(),
            source: "tenant/demo".into(),
            target: "control-plane/demo".into(),
            kind: TopologyEdgeKind::Owns,
            label: None,
        }],
    };

    let first = serde_json::to_string(&graph).expect("topology serializes");
    let second = serde_json::to_string(&graph).expect("topology serializes again");
    assert_eq!(first, second);
    assert_eq!(
        first,
        r#"{"tenantName":"demo","provider":"local","nodes":[{"id":"tenant/demo","kind":"tenant","label":"demo","health":"ready","resource":null,"attributes":[{"label":"Kubernetes","value":"v1.36.0"}]}],"edges":[{"id":"tenant-to-control-plane","source":"tenant/demo","target":"control-plane/demo","kind":"owns","label":null}]}"#
    );

    let decoded: TopologyGraph = serde_json::from_str(&first).expect("topology round trips");
    assert_eq!(decoded, graph);
}

#[test]
fn provider_views_preserve_sanitized_unknown_data() {
    let status = ProviderStatusView::Unknown(UnknownProviderView {
        provider_type: "future-provider".into(),
        summary: Some("unsupported provider status".into()),
    });

    assert_eq!(
        serde_json::to_string(&status).expect("provider view serializes"),
        r#"{"provider":"unknown","status":{"providerType":"future-provider","summary":"unsupported provider status"}}"#
    );
}

#[test]
fn page_snapshots_keep_identity_and_page_data_together() {
    let summary = TenantSummary {
        name: "demo".into(),
        provider: TenantProvider::Local,
        classification: tenant_admin_shared::query::TenantClassification::Progressing,
        kubernetes_version: "1.36.0".into(),
        requested_workers: 1,
        endpoint: None,
        created_at: None,
        conditions: Vec::new(),
    };
    let overview = OverviewSnapshot {
        overview: ManagementOverview {
            provider_mode: ProviderMode::Local,
            creation: CreationCapability::available("1.36.4"),
            tenants: TenantCounts {
                total: 1,
                progressing: 1,
                ..TenantCounts::default()
            },
            components: Vec::new(),
        },
        tenants: vec![summary.clone()],
    };
    let overview_json = serde_json::to_value(ApiEnvelope::new(overview)).expect("overview");
    assert_eq!(overview_json["data"]["tenants"][0]["name"], "demo");

    let detail = TenantDetail {
        summary,
        uid: "tenant-uid".into(),
        generation: 7,
        observed_generation: Some(6),
        specification: tenant_admin_shared::query::TenantSpecificationView {
            kubernetes_version: "1.36.0".into(),
            workers: 1,
            provider: tenant_admin_shared::query::ProviderSpecificationView::Local,
        },
        provider_status: ProviderStatusView::Unknown(UnknownProviderView {
            provider_type: "local".into(),
            summary: None,
        }),
        blockers: Vec::new(),
        management_resources: Vec::new(),
    };
    let tenant = TenantSnapshot {
        identity: TenantSnapshotIdentity {
            uid: detail.uid.clone(),
            generation: detail.generation,
            observed_generation: detail.observed_generation,
        },
        database: DatabaseObservation::Unavailable {
            observed_at: "2026-09-29T20:50:16Z".into(),
            freshness: DatabaseObservationFreshness::Live,
            reason: tenant_admin_shared::query::DatabaseUnavailableReason::Pending,
            message: "Managed database status is pending".into(),
            retryable: true,
        },
        topology: TopologyGraph {
            tenant_name: detail.summary.name.clone(),
            provider: detail.summary.provider,
            nodes: Vec::new(),
            edges: Vec::new(),
        },
        detail,
    };
    let tenant_json = serde_json::to_value(ApiEnvelope::new(tenant)).expect("tenant");
    assert_eq!(tenant_json["data"]["identity"]["uid"], "tenant-uid");
    assert_eq!(tenant_json["data"]["identity"]["generation"], 7);
    assert_eq!(tenant_json["data"]["identity"]["observedGeneration"], 6);
    assert_eq!(tenant_json["data"]["database"]["state"], "unavailable");
}

#[test]
fn database_observation_contract_is_bounded_and_secret_free() {
    let observation = DatabaseObservation::Available {
        observed_at: "2026-09-29T20:50:16Z".into(),
        freshness: DatabaseObservationFreshness::Live,
        cluster: Box::new(DatabaseClusterObservation {
            identity: DatabaseClusterIdentity {
                api_version: "postgresql.cnpg.io/v1".into(),
                kind: "Cluster".into(),
                namespace: "database".into(),
                name: "capi-postgres".into(),
                uid: Some("cluster-uid".into()),
                generation: 8,
            },
            phase: Some("Cluster in healthy state".into()),
            reason: None,
            desired_instances: 3,
            observed_instances: 3,
            ready_instances: 3,
            current_primary: Some("capi-postgres-1".into()),
            target_primary: Some("capi-postgres-1".into()),
            current_primary_since: Some("2026-09-29T20:40:00Z".into()),
            target_primary_requested_at: Some("2026-09-29T20:39:59Z".into()),
            current_primary_failing_since: None,
            image: Some("ghcr.io/cloudnative-pg/postgresql:18".into()),
            timeline: Some(4),
            services: DatabaseServices {
                read: Some("capi-postgres-r".into()),
                write: Some("capi-postgres-rw".into()),
            },
            topology_available: true,
            nodes_used: Some(3),
            instances: vec![DatabaseInstanceObservation {
                name: "capi-postgres-1".into(),
                role: DatabaseInstanceRole::Primary,
                status: Some("healthy".into()),
                timeline: Some(4),
                node: Some("worker-a".into()),
                zone: Some("local".into()),
            }],
            storage: DatabasePvcHealth {
                total: 3,
                healthy: 3,
                ..DatabasePvcHealth::default()
            },
            conditions: vec![DatabaseCondition {
                condition_type: "Ready".into(),
                status: ConditionStatus::True,
                reason: Some("ClusterIsReady".into()),
                message: Some("Cluster is ready".into()),
                observed_generation: Some(8),
                last_transition_time: Some("2026-09-29T20:40:00Z".into()),
            }],
        }),
    };

    let json = serde_json::to_string(&observation).expect("database observation serializes");
    let value = serde_json::to_value(&observation).expect("database observation value");
    let mut keys: Vec<_> = value
        .as_object()
        .expect("database observation object")
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(keys, ["cluster", "freshness", "observedAt", "state"]);
    assert_eq!(value["observedAt"], "2026-09-29T20:50:16Z");
    assert!(value.get("observed_at").is_none());
    assert!(json.contains(r#""state":"available""#));
    assert!(json.contains(r#""role":"primary""#));
    assert!(!json.contains("observed_at"));
    for forbidden in [
        "kubeconfig",
        "client-key-data",
        "resourceVersion",
        "systemID",
        "internalIP",
        "managedRoles",
        "labels",
        "metrics",
    ] {
        assert!(
            !json.contains(forbidden),
            "{forbidden} leaked into contract"
        );
    }

    let decoded: DatabaseObservation =
        serde_json::from_str(&json).expect("database observation round trips");
    assert_eq!(decoded, observation);

    for (observation, expected_keys) in [
        (
            DatabaseObservation::Unavailable {
                observed_at: "2026-09-29T20:50:16Z".into(),
                freshness: DatabaseObservationFreshness::Live,
                reason: DatabaseUnavailableReason::Pending,
                message: "pending".into(),
                retryable: true,
            },
            vec![
                "freshness",
                "message",
                "observedAt",
                "reason",
                "retryable",
                "state",
            ],
        ),
        (
            DatabaseObservation::NotApplicable {
                observed_at: "2026-09-29T20:50:16Z".into(),
                freshness: DatabaseObservationFreshness::Live,
                reason: DatabaseNotApplicableReason::ProviderUnsupported,
            },
            vec!["freshness", "observedAt", "reason", "state"],
        ),
    ] {
        let value = serde_json::to_value(observation).expect("variant serializes");
        let mut keys: Vec<_> = value
            .as_object()
            .expect("variant object")
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(keys, expected_keys);
        assert!(value.get("observed_at").is_none());
    }
}

#[test]
fn database_query_contract_supports_unrestricted_multi_result_sql_without_credentials() {
    let request = DatabaseQueryRequest {
        instance: "capi-postgres-1".into(),
        database: "postgres".into(),
        sql: "CREATE TABLE demo(id integer); SELECT NULL::text AS value;".into(),
    };
    assert_eq!(
        serde_json::to_string(&request).expect("query request serializes"),
        r#"{"instance":"capi-postgres-1","database":"postgres","sql":"CREATE TABLE demo(id integer); SELECT NULL::text AS value;"}"#
    );

    let response = DatabaseQueryResponse {
        tenant: "demo".into(),
        cluster: "capi-postgres".into(),
        instance: "capi-postgres-1".into(),
        database: "postgres".into(),
        executed_at: "2026-09-29T22:40:00Z".into(),
        duration_ms: 17,
        truncated: false,
        results: vec![
            DatabaseQueryResult {
                columns: Vec::new(),
                rows: Vec::new(),
                affected_rows: 0,
                truncated: false,
            },
            DatabaseQueryResult {
                columns: vec!["value".into()],
                rows: vec![vec![None]],
                affected_rows: 1,
                truncated: false,
            },
        ],
    };
    let json = serde_json::to_string(&ApiEnvelope::new(response.clone()))
        .expect("query response serializes");
    assert_eq!(
        json,
        r#"{"schemaVersion":4,"data":{"tenant":"demo","cluster":"capi-postgres","instance":"capi-postgres-1","database":"postgres","executedAt":"2026-09-29T22:40:00Z","durationMs":17,"truncated":false,"results":[{"columns":[],"rows":[],"affectedRows":0,"truncated":false},{"columns":["value"],"rows":[[null]],"affectedRows":1,"truncated":false}]}}"#
    );
    for forbidden in ["password", "username", "uri", "pgpass", "kubeconfig"] {
        assert!(
            !json.contains(forbidden),
            "{forbidden} leaked into query response"
        );
    }
    let decoded: ApiEnvelope<DatabaseQueryResponse> =
        serde_json::from_str(&json).expect("query response round trips");
    assert_eq!(decoded.data, response);
}

#[test]
fn query_limit_and_unknown_outcome_error_codes_are_stable() {
    assert_eq!(
        serde_json::to_string(&ApiErrorCode::QueryResponseTooLarge)
            .expect("response limit code serializes"),
        r#""query-response-too-large""#
    );
    assert_eq!(
        serde_json::to_string(&ApiErrorCode::QueryOutcomeUnknown)
            .expect("unknown outcome code serializes"),
        r#""query-outcome-unknown""#
    );
}
