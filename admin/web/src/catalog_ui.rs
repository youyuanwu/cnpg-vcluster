use leptos::prelude::*;
use tenant_admin_shared::{
    catalog::{
        CatalogQueryResponse, CatalogView, DatabaseConditionView, DatabaseView, InstanceView,
        StorageView,
    },
    query::{DatabaseQueryResult, TenantClassification, TopologyGraph},
};
use wasm_bindgen_futures::spawn_local;

use crate::{
    api::{delete_envelope, get_envelope, post_envelope},
    catalog_state::{
        CatalogRecovery, add_disabled_reason, add_recovery, catalog_current, create_request,
        delete_recovery, delete_request, entry_actions_enabled, entry_deletable, query_instances,
        query_request, query_response_matches, visible_databases,
    },
    database_console::{
        DEFAULT_DATABASE, DEFAULT_SQL, QueryResultPresentation, format_query_duration,
        project_query_result,
    },
    error::{UiError, UiErrorKind},
    format::{health_class, health_label, optional_text, provider_label},
    mutation_ui_state::{
        RefreshIntent, exact_confirmation_enabled, refresh_effects, unsafe_operation_enabled,
    },
    route::{database_path, database_query_path, databases_path},
    topology::layout_graph,
};

#[derive(Clone)]
enum CatalogLoad {
    Loading,
    Ready(CatalogView),
    Error(UiError),
}

#[derive(Clone)]
enum QueryState {
    Idle,
    Running,
    Success(CatalogQueryResponse),
    Error(UiError),
}

enum RecoveryIntent {
    Add(String),
    Delete(Box<DatabaseView>),
}

fn invalid_identity(message: &str) -> UiError {
    UiError {
        kind: UiErrorKind::StaleIdentity,
        message: message.into(),
        retryable: false,
        field_errors: Vec::new(),
    }
}

async fn fetch_catalog(name: &str) -> Result<CatalogView, UiError> {
    let path = databases_path(name).ok_or_else(|| invalid_identity("Invalid Tenant name."))?;
    get_envelope(&path).await
}

fn ambiguous(error: &UiError) -> bool {
    matches!(
        error.kind,
        UiErrorKind::Network
            | UiErrorKind::KubernetesUnavailable
            | UiErrorKind::MutationOutcomeUnknown
    )
}

async fn recover(
    name: &str,
    previous: &CatalogView,
    intent: RecoveryIntent,
    state: RwSignal<CatalogLoad>,
    notice: RwSignal<Option<String>>,
    locked: RwSignal<bool>,
) {
    locked.set(true);
    match fetch_catalog(name).await {
        Ok(current) => {
            let outcome = match &intent {
                RecoveryIntent::Add(cluster) => add_recovery(previous, &current, cluster),
                RecoveryIntent::Delete(entry) => delete_recovery(previous, &current, entry),
            };
            state.set(CatalogLoad::Ready(current));
            let explanation = match outcome {
                CatalogRecovery::ExistingIdentity => "A cluster with this name is now in the catalog. Its identity may differ from this request; inspect it before taking another action.",
                CatalogRecovery::Absent => "The entry is absent from the authoritative catalog. Inspect the current state before another mutation.",
                CatalogRecovery::Deleting => "The exact cluster is now deleting. Inspect its progress before another action.",
                CatalogRecovery::Replaced => "The catalog or cluster identity was replaced. The old request must not be replayed.",
                CatalogRecovery::Unchanged => "The exact cluster is not marked deleting. Its outcome remains uncertain; do not resend the deletion.",
            };
            notice.set(Some(format!("{explanation} Refresh this page to re-enable controls.")));
        }
        Err(error) => notice.set(Some(format!(
            "The authoritative catalog reread failed: {} Do not retry without refreshing and inspecting the catalog.",
            error.message
        ))),
    }
}

#[component]
pub fn CatalogPanel(
    name: String,
    tenant_uid: String,
    classification: TenantClassification,
    snapshot_refresh: Option<RwSignal<u32>>,
) -> impl IntoView {
    provide_context(snapshot_refresh);
    let state = RwSignal::new(CatalogLoad::Loading);
    let notice = RwSignal::new(None::<String>);
    let busy = RwSignal::new(false);
    let locked = RwSignal::new(false);
    let refresh = RwSignal::new(0_u32);
    let requested_name = name.clone();
    Effect::new(move |_| {
        refresh.get();
        state.set(CatalogLoad::Loading);
        notice.set(None);
        locked.set(false);
        let name = requested_name.clone();
        spawn_local(async move {
            state.set(match fetch_catalog(&name).await {
                Ok(catalog) => CatalogLoad::Ready(catalog),
                Err(error) => CatalogLoad::Error(error),
            });
        });
    });

    view! {
        <section class="panel database-panel" aria-labelledby="database-catalog-heading">
            <div class="panel__header">
                <div>
                    <h2 id="database-catalog-heading">"Database clusters"</h2>
                    <p>"Up to three independent PostgreSQL clusters. Data is read on load or refresh, not streamed."</p>
                </div>
                <button type="button"
                    disabled=move || busy.get() || matches!(state.get(), CatalogLoad::Loading)
                    on:click=move |_| refresh.update(|version| *version = version.wrapping_add(1))
                >"Refresh catalog"</button>
            </div>
            <div aria-live="polite">
                {move || notice.get().map(|message| view! {
                    <p class="lifecycle-notice" role="alert">{message}</p>
                })}
            </div>
            {move || match state.get() {
                CatalogLoad::Loading => view! {
                    <p role="status">"Loading authoritative database catalog…"</p>
                }.into_any(),
                CatalogLoad::Error(error) => view! {
                    <div class="database-warning" role="alert">
                        <h3>"Database catalog unavailable"</h3>
                        <p>{error.message}</p>
                        <p>"Creation, deletion and SQL are disabled until an authoritative catalog can be read."</p>
                    </div>
                }.into_any(),
                CatalogLoad::Ready(catalog) => view! {
                    <CatalogContent name=name.clone() tenant_uid=tenant_uid.clone()
                        classification catalog state notice busy locked/>
                }.into_any(),
            }}
        </section>
    }
}

#[component]
fn CatalogContent(
    name: String,
    tenant_uid: String,
    classification: TenantClassification,
    catalog: CatalogView,
    state: RwSignal<CatalogLoad>,
    notice: RwSignal<Option<String>>,
    busy: RwSignal<bool>,
    locked: RwSignal<bool>,
) -> impl IntoView {
    let count = catalog.databases.len();
    let current = catalog_current(&catalog, &name, &tenant_uid);
    let add_reason = add_disabled_reason(&catalog, &name, &tenant_uid, classification);
    let entries = visible_databases(&catalog).to_vec();
    let capacity = format!("{count} of 3 slots occupied (deleting clusters retain their slot)");
    view! {
        <p class="secondary" role="status">{capacity}</p>
        {(!current).then(|| view! {
            <p class="database-warning" role="alert">"Catalog identity does not match this Tenant. All unsafe controls are disabled."</p>
        })}
        {(count > 3).then(|| view! {
            <p class="database-warning" role="alert">"More than three entries were returned. Only the first three are displayed; mutation controls are disabled until the catalog is repaired."</p>
        })}
        <DatabaseAddPanel name=name.clone() tenant_uid=tenant_uid.clone() classification
            catalog=catalog.clone() state notice busy locked/>
        {if entries.is_empty() {
            view! { <p class="empty">"No database clusters have been added to this Tenant."</p> }.into_any()
        } else {
            view! {
                <div class="database-cluster-list">
                    {entries.into_iter().map(|entry| {
                        let can_act = entry_actions_enabled(&catalog, &name, &tenant_uid, classification, &entry)
                            && count <= 3;
                        let can_delete = entry_deletable(&catalog, &name, &tenant_uid, &entry)
                            && count <= 3;
                        view! {
                            <DatabaseCard name=name.clone() catalog=catalog.clone() entry
                                can_act can_delete state notice busy locked/>
                        }
                    }).collect_view()}
                </div>
            }.into_any()
        }}
        {add_reason.map(|reason| view! { <p class="secondary" role="status">{reason}</p> })}
    }
}

#[component]
fn DatabaseAddPanel(
    name: String,
    tenant_uid: String,
    classification: TenantClassification,
    catalog: CatalogView,
    state: RwSignal<CatalogLoad>,
    notice: RwSignal<Option<String>>,
    busy: RwSignal<bool>,
    locked: RwSignal<bool>,
) -> impl IntoView {
    let snapshot_refresh = use_context::<Option<RwSignal<u32>>>().flatten();
    let cluster_name = RwSignal::new(String::new());
    let instances = RwSignal::new("1".to_owned());
    let error = RwSignal::new(None::<String>);
    let disabled = add_disabled_reason(&catalog, &name, &tenant_uid, classification).is_some()
        || catalog.databases.len() > 3;
    let submit_catalog = catalog.clone();
    let submit_name = name.clone();
    let submit = move |event: leptos::ev::SubmitEvent| {
        event.prevent_default();
        if disabled || busy.get_untracked() || locked.get_untracked() {
            return;
        }
        let request = match create_request(
            &submit_catalog,
            &cluster_name.get_untracked(),
            &instances.get_untracked(),
        ) {
            Ok(request) => request,
            Err(message) => {
                error.set(Some(message.into()));
                return;
            }
        };
        let Some(path) = databases_path(&submit_name) else {
            error.set(Some("Invalid Tenant name.".into()));
            return;
        };
        error.set(None);
        busy.set(true);
        let prior = submit_catalog.clone();
        let name = submit_name.clone();
        spawn_local(async move {
            match post_envelope::<_, CatalogView>(&path, &request).await {
                Ok(current)
                    if current.catalog_uid == prior.catalog_uid
                        && current.tenant_uid == prior.tenant_uid
                        && current
                            .databases
                            .iter()
                            .any(|entry| entry.name == request.name) =>
                {
                    state.set(CatalogLoad::Ready(current));
                    let effects = refresh_effects(RefreshIntent::MutationCommitted);
                    if !effects.preserve_catalog_lock {
                        locked.set(false);
                    }
                    if effects.snapshot {
                        if let Some(snapshot_refresh) = snapshot_refresh {
                            snapshot_refresh.update(|version| *version = version.wrapping_add(1));
                        }
                    }
                    notice.set(Some(format!(
                        "Cluster {} was accepted. Refresh to observe readiness.",
                        request.name
                    )));
                }
                Ok(_) => {
                    recover(
                        &name,
                        &prior,
                        RecoveryIntent::Add(request.name),
                        state,
                        notice,
                        locked,
                    )
                    .await
                }
                Err(error) if ambiguous(&error) => {
                    notice.set(Some(format!(
                        "Create response was uncertain: {}",
                        error.message
                    )));
                    recover(
                        &name,
                        &prior,
                        RecoveryIntent::Add(request.name),
                        state,
                        notice,
                        locked,
                    )
                    .await;
                }
                Err(error) => {
                    notice.set(Some(error.message));
                    if matches!(
                        error.kind,
                        UiErrorKind::Conflict | UiErrorKind::StaleIdentity
                    ) {
                        recover(
                            &name,
                            &prior,
                            RecoveryIntent::Add(request.name),
                            state,
                            notice,
                            locked,
                        )
                        .await;
                    }
                }
            }
            busy.set(false);
        });
    };
    view! {
        <section class="database-add" aria-labelledby="database-add-heading">
            <h3 id="database-add-heading">"Add PostgreSQL cluster"</h3>
            <form class="lifecycle-form" novalidate=true on:submit=submit>
                <div class="form-field">
                    <label for="database-add-name">"Cluster name"</label>
                    <input id="database-add-name" type="text" maxlength="30" autocomplete="off"
                        required disabled=move || disabled || busy.get() || locked.get()
                        aria-describedby="database-add-error"
                        on:input=move |event| cluster_name.set(event_target_value(&event))/>
                </div>
                <div class="form-field">
                    <label for="database-add-instances">"Instances (1–3)"</label>
                    <input id="database-add-instances" type="number" min="1" max="3" step="1"
                        value="1" required disabled=move || disabled || busy.get() || locked.get()
                        aria-describedby="database-add-error"
                        on:input=move |event| instances.set(event_target_value(&event))/>
                </div>
                <div class="form-actions">
                    <button type="submit" disabled=move || disabled || busy.get() || locked.get()>
                        {move || if busy.get() { "Submitting…" } else { "Add cluster" }}
                    </button>
                </div>
                <p id="database-add-error" class="field-error" role="alert">{move || error.get()}</p>
            </form>
        </section>
    }
}

#[component]
fn DatabaseCard(
    name: String,
    catalog: CatalogView,
    entry: DatabaseView,
    can_act: bool,
    can_delete: bool,
    state: RwSignal<CatalogLoad>,
    notice: RwSignal<Option<String>>,
    busy: RwSignal<bool>,
    locked: RwSignal<bool>,
) -> impl IntoView {
    let uid = entry.logical_uid.clone();
    let heading = format!("cluster-{uid}-heading");
    let topology_heading = format!("cluster-{uid}-topology-heading");
    let phase = if entry.deleting {
        "deleting"
    } else {
        entry.phase.as_str()
    };
    let status_class = match phase {
        "ready" => "ready",
        "deleting" => "deleting",
        "degraded" | "ownership-invalid" => "degraded",
        _ => "progressing",
    };
    let storage = entry.storage.iter().take(3).cloned().collect::<Vec<_>>();
    let conditions = entry.conditions.iter().take(8).cloned().collect::<Vec<_>>();
    let instances = entry
        .instance_topology
        .iter()
        .take(3)
        .cloned()
        .collect::<Vec<_>>();
    let blockers = entry.blockers.iter().take(8).cloned().collect::<Vec<_>>();
    let topology = entry.topology.clone();
    let identity = format!("{} · {}", entry.name, entry.logical_uid);
    view! {
        <article class="database-card database-cluster" aria-labelledby=heading.clone()>
            <div class="panel__header">
                <div>
                    <h3 id=heading.clone()>{entry.name.clone()}</h3>
                    <p class="secondary">{format!("Logical UID: {}", entry.logical_uid)}</p>
                </div>
                <span class=format!("status status--{status_class}")>{phase.to_owned()}</span>
            </div>
            <dl class="definition-list">
                <dt>"Provider"</dt><dd>{entry.provider.clone().unwrap_or_else(|| provider_label(topology.provider).into())}</dd>
                <dt>"CNPG cluster"</dt><dd>{optional_text(entry.cluster.as_deref()).to_owned()}</dd>
                <dt>"Namespace"</dt><dd>{optional_text(entry.namespace.as_deref()).to_owned()}</dd>
                <dt>"Observed generation"</dt><dd>{entry.observed_generation.map_or_else(|| "Pending".into(), |value| value.to_string())}</dd>
                <dt>"Readiness"</dt><dd>{format!("{} of {} instances ready", entry.ready_instances, entry.instances)}</dd>
                <dt>"Storage"</dt><dd>{format!("{} of {} volumes healthy · {} bytes requested", entry.storage_healthy, entry.storage.len(), entry.storage_requested_bytes)}</dd>
            </dl>
            {entry.deleting.then(|| view! {
                <p class="lifecycle-notice" role="status">"Deletion is in progress; SQL and deletion controls are disabled until exact cleanup completes."</p>
            })}
            {entry.finalization.as_ref().map(|finalization| view! {
                <p class="secondary">{format!("Cleanup: {} absent, {} pending · terminal verified: {}",
                    finalization.verified_absent_count, finalization.pending_count, finalization.terminal_verified)}</p>
            })}
            {if blockers.is_empty() { None } else { Some(view! {
                <div class="database-subsection">
                    <h4>"Reconciliation blockers"</h4>
                    <ul class="blocker-list">
                        {blockers.into_iter().map(|blocker| view! {
                            <li><strong>{blocker.code}</strong><p>{blocker.message}</p></li>
                        }).collect_view()}
                    </ul>
                </div>
            })}}
            <div class="database-detail-grid database-detail-grid--secondary">
                <section class="database-subsection">
                    <h4>"Instances"</h4>
                    {instance_list(instances)}
                </section>
                <section class="database-subsection">
                    <h4>"Storage volumes"</h4>
                    {storage_list(storage)}
                </section>
            </div>
            <section class="database-subsection">
                <h4>"Conditions"</h4>
                {condition_list(conditions)}
            </section>
            <section class="database-subsection" aria-labelledby=topology_heading.clone()>
                <h4 id=topology_heading.clone()>{format!("{} topology", identity)}</h4>
                {topology_view(&uid, &entry.name, topology)}
            </section>
            <DatabaseDeletePanel name=name.clone() catalog=catalog.clone() entry=entry.clone()
                can_act=can_delete state notice busy locked/>
            <DatabaseConsole name catalog entry can_act busy locked/>
        </article>
    }
}

fn instance_list(instances: Vec<InstanceView>) -> AnyView {
    if instances.is_empty() {
        return view! { <p class="empty">"No instances observed yet."</p> }.into_any();
    }
    view! {
        <ul class="resource-list">
            {instances.into_iter().map(|instance| view! {
                <li><strong>{instance.name}</strong>
                    <span class="secondary">{format!(" · {} · {}", instance.role, if instance.ready { "Ready" } else { "Not ready" })}</span>
                    <p class="secondary">{format!("Instance UID: {}", instance.uid)}</p>
                </li>
            }).collect_view()}
        </ul>
    }.into_any()
}

fn storage_list(storage: Vec<StorageView>) -> AnyView {
    if storage.is_empty() {
        return view! { <p class="empty">"No storage observed yet."</p> }.into_any();
    }
    view! {
        <ul class="resource-list">
            {storage.into_iter().map(|volume| view! {
                <li><strong>{format!("Volume {}", volume.ordinal)}</strong>
                    <span class="secondary">{format!(" · {} · {} bytes", if volume.healthy { "Healthy" } else { "Not healthy" }, volume.requested_bytes)}</span>
                    <p class="secondary">{format!("PV: {} · PVC: {} · disk: {}",
                        optional_text(volume.pv_uid.as_deref()), optional_text(volume.pvc_uid.as_deref()),
                        optional_text(volume.disk_uid.as_deref()))}</p>
                </li>
            }).collect_view()}
        </ul>
    }.into_any()
}

fn condition_list(conditions: Vec<DatabaseConditionView>) -> AnyView {
    if conditions.is_empty() {
        return view! { <p class="empty">"No conditions reported yet."</p> }.into_any();
    }
    view! {
        <ul class="condition-list">
            {conditions.into_iter().map(|condition| view! {
                <li><strong>{condition.condition_type}</strong>
                    <span class="secondary">{format!(" · {} · {}", condition.status, condition.reason)}</span>
                    <p>{condition.message}</p>
                </li>
            }).collect_view()}
        </ul>
    }.into_any()
}

fn topology_view(uid: &str, name: &str, graph: TopologyGraph) -> AnyView {
    let layout = layout_graph(&graph);
    if layout.nodes.is_empty() {
        return view! { <p class="empty">"Topology has not been observed yet."</p> }.into_any();
    }
    let title_id = format!("database-{uid}-svg-title");
    let desc_id = format!("database-{uid}-svg-description");
    let label = format!("{title_id} {desc_id}");
    let view_box = format!("0 0 {} {}", layout.width, layout.height);
    view! {
        <div class="topology-scroll">
            <svg class="topology" viewBox=view_box role="img" aria-labelledby=label preserveAspectRatio="xMinYMin meet">
                <title id=title_id>{format!("{name} PostgreSQL topology")}</title>
                <desc id=desc_id>"Exact database entry and its observed instances; status is also listed above."</desc>
                {layout.edges.into_iter().map(|edge| view! {
                    <path class="edge" d=edge.path/>
                }).collect_view()}
                {layout.nodes.into_iter().map(|node| view! {
                    <g class=format!("node node--{}", health_class(node.health)) role="group"
                        aria-label=format!("{}; status {}", node.label, health_label(node.health))
                        tabindex="0" transform=format!("translate({}, {})", node.x, node.y)>
                        <rect width=node.width height=node.height rx="9" ry="9"/>
                        <text class="node-label" x="14" y="29">{node.label.clone()}</text>
                        <text class="node-meta" x="14" y="53">{health_label(node.health)}</text>
                    </g>
                }).collect_view()}
            </svg>
        </div>
    }.into_any()
}

#[component]
fn DatabaseDeletePanel(
    name: String,
    catalog: CatalogView,
    entry: DatabaseView,
    can_act: bool,
    state: RwSignal<CatalogLoad>,
    notice: RwSignal<Option<String>>,
    busy: RwSignal<bool>,
    locked: RwSignal<bool>,
) -> impl IntoView {
    let snapshot_refresh = use_context::<Option<RwSignal<u32>>>().flatten();
    let confirmation = RwSignal::new(String::new());
    let heading = format!("cluster-{}-delete-heading", entry.logical_uid);
    let button_name = entry.name.clone();
    let submit_entry = entry.clone();
    let submit_catalog = catalog.clone();
    let submit_name = name.clone();
    let submit = move |event: leptos::ev::SubmitEvent| {
        event.prevent_default();
        if !can_act
            || !exact_confirmation_enabled(
                &submit_entry.name,
                &confirmation.get_untracked(),
                busy.get_untracked(),
                locked.get_untracked(),
            )
        {
            return;
        }
        let Some(path) = database_path(&submit_name, &submit_entry.logical_uid) else {
            notice.set(Some(
                "Invalid exact database identity; refresh the catalog.".into(),
            ));
            locked.set(true);
            return;
        };
        let Some(request) = delete_request(
            &submit_catalog,
            &submit_entry,
            &confirmation.get_untracked(),
        ) else {
            notice.set(Some(
                "The exact database identity or confirmation is no longer current. Refresh before deleting."
                    .into(),
            ));
            locked.set(true);
            return;
        };
        busy.set(true);
        let prior = submit_catalog.clone();
        let entry = submit_entry.clone();
        let name = submit_name.clone();
        spawn_local(async move {
            match delete_envelope::<_, CatalogView>(&path, &request).await {
                Ok(current)
                    if current.catalog_uid == prior.catalog_uid
                        && current.tenant_uid == prior.tenant_uid
                        && matches!(
                            delete_recovery(&prior, &current, &entry),
                            CatalogRecovery::Deleting | CatalogRecovery::Absent
                        ) =>
                {
                    state.set(CatalogLoad::Ready(current));
                    let effects = refresh_effects(RefreshIntent::MutationCommitted);
                    if !effects.preserve_catalog_lock {
                        locked.set(false);
                    }
                    if effects.snapshot {
                        if let Some(snapshot_refresh) = snapshot_refresh {
                            snapshot_refresh.update(|version| *version = version.wrapping_add(1));
                        }
                    }
                    notice.set(Some(format!(
                        "Deletion of {} was accepted for logical UID {}.",
                        entry.name, entry.logical_uid
                    )));
                }
                Ok(_) => {
                    recover(
                        &name,
                        &prior,
                        RecoveryIntent::Delete(Box::new(entry)),
                        state,
                        notice,
                        locked,
                    )
                    .await
                }
                Err(error) if ambiguous(&error) => {
                    recover(
                        &name,
                        &prior,
                        RecoveryIntent::Delete(Box::new(entry)),
                        state,
                        notice,
                        locked,
                    )
                    .await;
                }
                Err(error) => {
                    notice.set(Some(error.message));
                    if matches!(
                        error.kind,
                        UiErrorKind::Conflict | UiErrorKind::StaleIdentity | UiErrorKind::NotFound
                    ) {
                        recover(
                            &name,
                            &prior,
                            RecoveryIntent::Delete(Box::new(entry)),
                            state,
                            notice,
                            locked,
                        )
                        .await;
                    }
                }
            }
            busy.set(false);
        });
    };
    view! {
        <section class="database-delete" aria-labelledby=heading.clone()>
            <h4 id=heading.clone()>{format!("Delete {}", entry.name)}</h4>
            <p class="secondary">"This permanently removes only the exact selected cluster and its owned storage."</p>
            <form class="database-delete__form" on:submit=submit>
                <label>
                    {format!("Type {} to confirm deletion", entry.name)}
                    <input type="text" autocomplete="off" disabled=move || !can_act || busy.get() || locked.get()
                        on:input=move |event| confirmation.set(event_target_value(&event))/>
                </label>
                <button class="button--danger" type="submit"
                    disabled=move || !can_act || !exact_confirmation_enabled(
                        &button_name,
                        &confirmation.get(),
                        busy.get(),
                        locked.get(),
                    )
                >"Delete this cluster"</button>
            </form>
        </section>
    }
}

#[component]
fn DatabaseConsole(
    name: String,
    catalog: CatalogView,
    entry: DatabaseView,
    can_act: bool,
    busy: RwSignal<bool>,
    locked: RwSignal<bool>,
) -> impl IntoView {
    let options = query_instances(&entry);
    let selected = RwSignal::new(
        options
            .first()
            .map(|instance| instance.uid.clone())
            .unwrap_or_default(),
    );
    let database = RwSignal::new(DEFAULT_DATABASE.to_owned());
    let sql = RwSignal::new(DEFAULT_SQL.to_owned());
    let result = RwSignal::new(QueryState::Idle);
    let query_enabled = can_act && !options.is_empty();
    let has_ready_instance = !options.is_empty();
    let submit_entry = entry.clone();
    let submit_catalog = catalog.clone();
    let submit_name = name.clone();
    let submit = move |event: leptos::ev::SubmitEvent| {
        event.prevent_default();
        if !unsafe_operation_enabled(
            query_enabled,
            has_ready_instance,
            busy.get_untracked(),
            locked.get_untracked(),
        ) || matches!(result.get_untracked(), QueryState::Running)
        {
            return;
        }
        let Some(request) = query_request(
            &submit_catalog,
            &submit_entry,
            &selected.get_untracked(),
            &database.get_untracked(),
            &sql.get_untracked(),
        ) else {
            result.set(QueryState::Error(invalid_identity(
                "The selected instance is not Ready in this cluster. Refresh before querying.",
            )));
            return;
        };
        let Some(path) = database_query_path(&submit_name, &submit_entry.logical_uid) else {
            result.set(QueryState::Error(invalid_identity(
                "Invalid database identity. Refresh before querying.",
            )));
            return;
        };
        result.set(QueryState::Running);
        busy.set(true);
        spawn_local(async move {
            result.set(match post_envelope::<_, CatalogQueryResponse>(&path, &request).await {
                Ok(response) if query_response_matches(&request, &response) => QueryState::Success(response),
                Ok(_) => QueryState::Error(invalid_identity("The server returned a different cluster or instance identity. Refresh before querying.")),
                Err(error) => QueryState::Error(error),
            });
            busy.set(false);
        });
    };
    let heading = format!("cluster-{}-console-heading", entry.logical_uid);
    view! {
        <section class="database-console" aria-labelledby=heading.clone()>
            <div class="database-console__warning">
                <p class="eyebrow">"Dangerous operation"</p>
                <h4 id=heading.clone()>{format!("Unsafe SQL console · {}", entry.name)}</h4>
                <p>"Queries run as the PostgreSQL superuser and may modify or destroy data. Credentials remain server-side."</p>
            </div>
            <form class="database-console__form" on:submit=submit>
                <div class="database-console__controls">
                    <label>"Ready instance in this cluster"
                        <select disabled=move || !query_enabled || busy.get() || locked.get()
                            prop:value=move || selected.get()
                            on:change=move |event| selected.set(event_target_value(&event))>
                            {options.into_iter().map(|instance| view! {
                                <option value=instance.uid>{format!("{} ({})", instance.name, instance.role)}</option>
                            }).collect_view()}
                        </select>
                    </label>
                    <label>"Database name"
                        <input type="text" autocomplete="off" required
                            disabled=move || !query_enabled || busy.get() || locked.get()
                            prop:value=move || database.get()
                            on:input=move |event| database.set(event_target_value(&event))/>
                    </label>
                </div>
                <label class="database-console__sql">"SQL statement"
                    <textarea rows="6" required spellcheck="false"
                        disabled=move || !query_enabled || busy.get() || locked.get()
                        prop:value=move || sql.get()
                        on:input=move |event| sql.set(event_target_value(&event))></textarea>
                </label>
                {(!query_enabled).then(|| view! {
                    <p class="database-console__notice" role="status">"SQL is disabled until this exact cluster and an observed instance are Ready and the Tenant catalog is current."</p>
                })}
                <div class="database-console__actions">
                    <button class="database-console__execute" type="submit"
                        disabled=move || !query_enabled || busy.get() || locked.get() || matches!(result.get(), QueryState::Running)
                    >"Execute SQL"</button>
                </div>
            </form>
            <div class="database-console__output" aria-live="polite">
                {move || query_result_view(result.get())}
            </div>
        </section>
    }
}

fn query_result_view(state: QueryState) -> AnyView {
    match state {
        QueryState::Idle => view! { <p class="secondary">"No query executed in this browser session."</p> }.into_any(),
        QueryState::Running => view! { <p role="status">"Executing SQL on the selected instance…"</p> }.into_any(),
        QueryState::Error(error) => view! {
            <div class="database-console__error" role="alert">
                <strong>{error.title()}</strong><p>{error.message}</p>
                {(error.kind == UiErrorKind::QueryOutcomeUnknown).then(|| view! {
                    <p>"The statement may have executed. Inspect its effects before submitting another query."</p>
                })}
            </div>
        }.into_any(),
        QueryState::Success(response) => {
            let duration = format_query_duration(response.duration_ms);
            let identity = format!("{} · instance {} ({})", response.logical_uid, response.instance, response.instance_uid);
            view! {
                <section role="status">
                    <h5>"Query completed"</h5>
                    <p>{format!("Exact cluster {identity} · {} · {duration}", response.executed_at)}</p>
                    {response.truncated.then(|| view! { <p class="database-console__truncation">"Response truncated by the server."</p> })}
                    {response.results.into_iter().enumerate().map(|(index, result)| query_table(index + 1, result)).collect_view()}
                </section>
            }.into_any()
        }
    }
}

fn query_table(index: usize, result: DatabaseQueryResult) -> AnyView {
    let truncated = result.truncated;
    match project_query_result(&result) {
        QueryResultPresentation::Command { affected_rows } => view! {
            <section class="database-console__result">
                <h6>{format!("Result set {index}")}</h6>
                <p>{format!("{affected_rows} rows affected")}</p>
                {truncated.then(|| view! { <p class="database-console__truncation">"Result truncated."</p> })}
            </section>
        }.into_any(),
        QueryResultPresentation::Rows { .. } => view! {
            <section class="database-console__result">
                <h6>{format!("Result set {index}")}</h6>
                <div class="table-scroll">
                    <table class="database-console__table">
                        <caption>{format!("SQL result set {index}")}</caption>
                        <thead><tr>{result.columns.into_iter().map(|column| view! { <th scope="col">{column}</th> }).collect_view()}</tr></thead>
                        <tbody>{result.rows.into_iter().map(|row| view! {
                            <tr>{row.into_iter().map(|cell| view! {
                                <td>{match cell {
                                    Some(value) => view! { <span class="database-console__value">{value}</span> }.into_any(),
                                    None => view! { <span class="database-console__null" aria-label="NULL value">"NULL"</span> }.into_any(),
                                }}</td>
                            }).collect_view()}</tr>
                        }).collect_view()}</tbody>
                    </table>
                </div>
                {truncated.then(|| view! { <p class="database-console__truncation">"Result truncated."</p> })}
            </section>
        }.into_any(),
    }
}
