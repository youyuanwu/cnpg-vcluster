use tenant_admin_shared::{
    ADMIN_CONTAINER_PORT, ADMIN_NAMESPACE, ADMIN_RESOURCE_NAME, ADMIN_SERVICE_PORT,
    API_SCHEMA_NAME, API_SCHEMA_VERSION, ApiEnvelope, ApiError, ApiErrorCode, ApiErrorEnvelope,
    query::{
        DisplayAttribute, ManagementOverview, OverviewSnapshot, ProviderMode, ProviderStatusView,
        TenantCounts, TenantDetail, TenantProvider, TenantSnapshot, TenantSnapshotIdentity,
        TenantSummary, TopologyEdge, TopologyEdgeKind, TopologyGraph, TopologyHealth, TopologyNode,
        TopologyNodeKind, UnknownProviderView,
    },
    routes::{
        API_OVERVIEW_PATH, API_PREFIX, API_TENANT_PATH, API_TENANT_TOPOLOGY_PATH, API_TENANTS_PATH,
        FRONTEND_FALLBACK_PATH, HEALTH_PATH, READINESS_PATH,
    },
};

#[test]
fn deployment_and_route_constants_are_exact() {
    assert_eq!(API_SCHEMA_NAME, "tenant-admin");
    assert_eq!(API_SCHEMA_VERSION, 1);
    assert_eq!(ADMIN_RESOURCE_NAME, "tenant-admin");
    assert_eq!(ADMIN_NAMESPACE, "tenant-system");
    assert_eq!(ADMIN_CONTAINER_PORT, 8080);
    assert_eq!(ADMIN_SERVICE_PORT, 80);
    assert_eq!(API_PREFIX, "/api/v1");
    assert_eq!(API_OVERVIEW_PATH, "/api/v1/overview");
    assert_eq!(API_TENANTS_PATH, "/api/v1/tenants");
    assert_eq!(API_TENANT_PATH, "/api/v1/tenants/{name}");
    assert_eq!(API_TENANT_TOPOLOGY_PATH, "/api/v1/tenants/{name}/topology");
    assert_eq!(HEALTH_PATH, "/healthz");
    assert_eq!(READINESS_PATH, "/readyz");
    assert_eq!(FRONTEND_FALLBACK_PATH, "/*");
}

#[test]
fn envelopes_have_stable_versioned_json() {
    let success = ApiEnvelope::new(vec!["alpha", "beta"]);
    assert_eq!(
        serde_json::to_string(&success).expect("success envelope serializes"),
        r#"{"schemaVersion":1,"data":["alpha","beta"]}"#
    );

    let error = ApiErrorEnvelope::new(ApiError::new(
        ApiErrorCode::KubernetesUnavailable,
        "management API unavailable",
        true,
    ));
    assert_eq!(
        serde_json::to_string(&error).expect("error envelope serializes"),
        r#"{"schemaVersion":1,"error":{"code":"kubernetes-unavailable","message":"management API unavailable","retryable":true}}"#
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
        requested_databases: Some(1),
        endpoint: None,
        created_at: None,
        conditions: Vec::new(),
    };
    let overview = OverviewSnapshot {
        overview: ManagementOverview {
            provider_mode: ProviderMode::Local,
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
            provider: tenant_admin_shared::query::ProviderSpecificationView::Local { databases: 1 },
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
}
