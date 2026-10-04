use leptos::prelude::*;
use tenant_admin_shared::{
    API_SCHEMA_VERSION,
    lifecycle::{
        CreationCapability, TenantCreateResponse, TenantDeleteRequest, TenantDeleteResponse,
        TenantDeleteState, TenantField, TenantFieldError,
    },
    query::{
        AzureProviderView, ConditionStatus, LifecycleStageState, OverviewSnapshot,
        ProviderSpecificationView, ProviderStatusView, SectionAvailability, TenantCondition,
        TenantSnapshot, TenantSummary, TopologyGraph,
    },
    routes::{API_OVERVIEW_PATH, API_PREFIX},
};
use wasm_bindgen_futures::spawn_local;

use crate::{
    api::{delete_envelope, get_envelope, post_envelope},
    catalog_ui::CatalogPanel,
    error::{UiError, UiErrorKind},
    explorer::ExplorerModel,
    format::{
        classification_class, classification_label, condition_status_label, edge_kind_label,
        format_age, health_class, health_label, node_kind_label, optional_text, provider_label,
        provider_mode_label,
    },
    lifecycle::{
        CreateRecovery, DeleteRecovery, create_recovery, create_request, delete_recovery,
        requires_authoritative_read,
    },
    mutation_ui_state::exact_confirmation_enabled,
    resource_explorer_ui::ResourceExplorer,
    route::{
        AppRoute, TenantSection, parse_location, tenant_create_path, tenant_delete_path,
        tenant_href, tenant_resource_href, tenant_section_href,
    },
    tenant_ui::{lifecycle_stage_label, lifecycle_state_label},
    topology::layout_graph,
};

#[derive(Clone)]
enum LoadState<T> {
    Loading,
    Ready(T),
    Error(UiError),
}

#[derive(Clone)]
enum MutationState {
    Idle,
    Running,
    Error(UiError),
    Uncertain { message: String, href: String },
}

#[component]
pub fn App() -> impl IntoView {
    let route = web_sys::window().map_or(AppRoute::NotFound, |window| {
        let location = window.location();
        let path = location.pathname().unwrap_or_default();
        let search = location.search().unwrap_or_default();
        parse_location(&path, &search)
    });

    view! {
        <div id=tenant_admin_shared::ADMIN_RESOURCE_NAME>
            <header class="site-header">
                <div class="site-header__inner">
                    <a class="brand" href="/">
                        "Tenant Admin"
                        <span>"Tenant lifecycle and database administration"</span>
                    </a>
                </div>
            </header>
            {match route {
                AppRoute::Overview => view! { <OverviewPage/> }.into_any(),
                AppRoute::Tenant {
                    name,
                    section,
                    selected_resource,
                    invalid_selection,
                    selected_database,
                } => view! {
                    <TenantPage name section selected_resource invalid_selection selected_database/>
                }.into_any(),
                AppRoute::TenantSectionNotFound { name } => {
                    view! { <TenantRouteNotFound name/> }.into_any()
                }
                AppRoute::NotFound => view! { <RouteNotFound/> }.into_any(),
            }}
        </div>
    }
}

#[component]
fn OverviewPage() -> impl IntoView {
    let refresh = RwSignal::new(0_u32);
    let state = RwSignal::new(LoadState::<OverviewSnapshot>::Loading);

    Effect::new(move |_| {
        refresh.get();
        state.set(LoadState::Loading);
        spawn_local(async move {
            state.set(match fetch_dashboard().await {
                Ok(data) => LoadState::Ready(data),
                Err(error) => LoadState::Error(error),
            });
        });
    });

    view! {
        <main class="page">
            <div class="page-header">
                <div>
                    <p class="eyebrow">"Management cluster"</p>
                    <h1>"Tenant overview"</h1>
                    <p class="lede">
                        "Create, inspect, and delete Tenants through the management Kubernetes API. Data changes only when this page is loaded or refreshed."
                    </p>
                </div>
                <RefreshButton state refresh/>
            </div>
            <div aria-live="polite">
                {move || match state.get() {
                    LoadState::Loading => loading_state("Loading management overview"),
                    LoadState::Ready(data) => dashboard_view(data),
                    LoadState::Error(error) => error_state(error, refresh),
                }}
            </div>
        </main>
    }
}

#[component]
fn TenantPage(
    name: String,
    section: TenantSection,
    selected_resource: Option<String>,
    invalid_selection: bool,
    selected_database: Option<String>,
) -> impl IntoView {
    let refresh = RwSignal::new(0_u32);
    let catalog_refresh = RwSignal::new(0_u32);
    let refreshing = RwSignal::new(false);
    let resource_selection = RwSignal::new(selected_resource);
    let state = RwSignal::new(LoadState::<TenantSnapshot>::Loading);
    let requested_name = name.clone();

    Effect::new(move |_| {
        refresh.get();
        if !matches!(state.get_untracked(), LoadState::Loading) {
            refreshing.set(true);
        }
        let name = requested_name.clone();
        spawn_local(async move {
            state.set(match fetch_tenant_page(&name).await {
                Ok(data) => LoadState::Ready(data),
                Err(error) => LoadState::Error(error),
            });
            refreshing.set(false);
        });
    });

    view! {
        <main class="page">
            <a class="back-link" href="/">"← All Tenants"</a>
            <div class="page-header">
                <div>
                    <p class="eyebrow">"Tenant workspace"</p>
                    <h1>{name}</h1>
                    <p class="lede">
                        "Current status, tenant-scoped resources, database operations, and guarded lifecycle controls."
                    </p>
                </div>
                <button type="button"
                    disabled=move || refreshing.get() || matches!(state.get(), LoadState::Loading)
                    on:click=move |_| {
                        refresh.update(|version| *version = version.wrapping_add(1));
                        if section == TenantSection::Databases {
                            catalog_refresh.update(|version| *version = version.wrapping_add(1));
                        }
                    }>
                    {move || if refreshing.get() { "Refreshing…" } else { "Refresh" }}
                </button>
            </div>
            <div aria-live="polite">
                {move || match state.get() {
                    LoadState::Loading => loading_state("Loading Tenant detail"),
                    LoadState::Ready(data) => tenant_workspace_view(
                        data,
                        section,
                        resource_selection,
                        invalid_selection,
                        selected_database.clone(),
                        refresh,
                        catalog_refresh,
                    ),
                    LoadState::Error(error) => error_state(error, refresh),
                }}
            </div>
        </main>
    }
}

#[component]
fn RefreshButton<T: Clone + Send + Sync + 'static>(
    state: RwSignal<LoadState<T>>,
    refresh: RwSignal<u32>,
) -> impl IntoView {
    view! {
        <button
            type="button"
            disabled=move || matches!(state.get(), LoadState::Loading)
            on:click=move |_| refresh.update(|version| *version = version.wrapping_add(1))
        >
            {move || {
                if matches!(state.get(), LoadState::Loading) {
                    "Refreshing…"
                } else {
                    "Refresh"
                }
            }}
        </button>
    }
}

fn loading_state(label: &'static str) -> AnyView {
    view! {
        <section class="state-panel" role="status">
            <span class="loading-indicator" aria-hidden="true"></span>
            <h2>{label}</h2>
            <p>"Reading current management-cluster state."</p>
        </section>
    }
    .into_any()
}

fn error_state(error: UiError, refresh: RwSignal<u32>) -> AnyView {
    let show_retry = error.retryable;
    let schema_mismatch = error.kind == UiErrorKind::SchemaMismatch;
    view! {
        <section class="state-panel state-panel--error" role="alert">
            <h2>{error.title()}</h2>
            <p>{error.message}</p>
            {if show_retry {
                view! {
                    <button
                        type="button"
                        on:click=move |_| refresh.update(|version| *version = version.wrapping_add(1))
                    >
                        "Try again"
                    </button>
                }
                .into_any()
            } else {
                view! {
                    <div>
                        {schema_mismatch.then(|| view! {
                            <p class="secondary">
                                "Refresh after the deployed UI and server versions have been aligned."
                            </p>
                        })}
                        <p><a href="/">"Return to all Tenants"</a></p>
                    </div>
                }
                .into_any()
            }}
        </section>
    }
    .into_any()
}

fn dashboard_view(mut data: OverviewSnapshot) -> AnyView {
    data.tenants
        .sort_by(|left, right| left.name.cmp(&right.name));
    let counts = data.overview.tenants.clone();
    let components = data.overview.components;
    let provider_mode = data.overview.provider_mode;
    let creation = data.overview.creation;
    let provider = provider_mode_label(provider_mode);
    let tenants = data.tenants;

    view! {
        <section aria-labelledby="summary-heading">
            <div class="panel__header">
                <div>
                    <h2 id="summary-heading">"Current state"</h2>
                    <p>{format!("{provider} provider mode · API schema v{API_SCHEMA_VERSION}")}</p>
                </div>
            </div>
            <div class="metrics">
                <Metric label="Total" value=counts.total/>
                <Metric label="Ready" value=counts.ready/>
                <Metric label="Progressing" value=counts.progressing/>
                <Metric label="Degraded" value=counts.degraded/>
                <Metric label="Failed" value=counts.failed/>
                <Metric label="Deleting" value=counts.deleting/>
            </div>
        </section>

        <TenantCreatePanel capability=creation/>

        <section class="panel" aria-labelledby="tenants-heading">
            <div class="panel__header">
                <div>
                    <h2 id="tenants-heading">"Tenants"</h2>
                    <p>"Select a Tenant for provider and topology details."</p>
                </div>
            </div>
            {if tenants.is_empty() {
                view! {
                    <div class="empty">
                        <h3>"No Tenants found"</h3>
                        <p>"This management cluster currently has no Tenant resources."</p>
                    </div>
                }
                .into_any()
            } else {
                tenant_table(tenants)
            }}
        </section>

        <section class="panel" aria-labelledby="components-heading">
            <div class="panel__header">
                <div>
                    <h2 id="components-heading">"Management components"</h2>
                    <p>"Component health reported by the management cluster."</p>
                </div>
            </div>
            {if components.is_empty() {
                view! { <p class="empty">"No component summary is available."</p> }.into_any()
            } else {
                view! {
                    <ul class="component-list">
                        {components
                            .into_iter()
                            .map(|component| {
                                let class = if component.ready { "ready" } else { "degraded" };
                                let status = if component.ready { "Ready" } else { "Not ready" };
                                let identity = component.identity.map(|identity| {
                                    format!(
                                        "{} {}",
                                        identity.kind,
                                        namespaced_name(identity.namespace.as_deref(), &identity.name)
                                    )
                                });
                                view! {
                                    <li>
                                        <div class="condition-heading">
                                            <strong>{component.name}</strong>
                                            <span class=format!("status status--{class}")>{status}</span>
                                        </div>
                                        {identity.map(|value| view! { <p>{value}</p> })}
                                        {component.message.map(|message| view! { <p>{message}</p> })}
                                    </li>
                                }
                            })
                            .collect_view()}
                    </ul>
                }
                .into_any()
            }}
        </section>
    }
    .into_any()
}

#[component]
fn TenantCreatePanel(capability: CreationCapability) -> impl IntoView {
    let name = RwSignal::new(String::new());
    let workers = RwSignal::new("1".to_owned());
    let field_errors = RwSignal::new(Vec::<TenantFieldError>::new());
    let state = RwSignal::new(MutationState::Idle);
    let capability_available = capability.available;
    let version = capability
        .supported_kubernetes_version
        .clone()
        .unwrap_or_else(|| "Unavailable".into());
    let unavailable_reason = capability.reason.clone();

    let submit = move |event: leptos::ev::SubmitEvent| {
        event.prevent_default();
        if !capability_available || matches!(state.get(), MutationState::Running) {
            return;
        }
        let request = match create_request(&name.get_untracked(), &workers.get_untracked()) {
            Ok(request) => request,
            Err(errors) => {
                field_errors.set(errors);
                return;
            }
        };
        field_errors.set(Vec::new());
        state.set(MutationState::Running);
        spawn_local(async move {
            match post_envelope::<_, TenantCreateResponse>(tenant_create_path(), &request).await {
                Ok(response) => navigate_to_tenant(&response.identity.name),
                Err(error) if requires_authoritative_read(error.kind) => {
                    match fetch_tenant_page(&request.name).await {
                        Ok(_) => match create_recovery(true) {
                            CreateRecovery::InspectExisting => {
                                state.set(MutationState::Uncertain {
                                    message: "A Tenant with this name exists after an uncertain create response. Inspect its current identity and specification before retrying.".into(),
                                    href: tenant_href(&request.name).unwrap_or_else(|| "/".into()),
                                });
                            }
                            CreateRecovery::PreserveError => unreachable!(),
                        },
                        Err(observed) if observed.kind == UiErrorKind::NotFound => {
                            state.set(MutationState::Error(error));
                        }
                        Err(_) => state.set(MutationState::Error(UiError {
                            kind: UiErrorKind::Conflict,
                            message: format!(
                                "The create outcome is uncertain. Inspect Tenant {} before retrying.",
                                request.name
                            ),
                            retryable: false,
                            field_errors: Vec::new(),
                        })),
                    }
                }
                Err(error) => {
                    field_errors.set(error.field_errors.clone());
                    state.set(MutationState::Error(error));
                }
            }
        });
    };

    view! {
        <section class="panel lifecycle-panel" aria-labelledby="create-tenant-heading">
            <div class="panel__header">
                <div>
                    <h2 id="create-tenant-heading">"Create Tenant"</h2>
                    <p>"Submit one immutable Tenant request to the active lifecycle controller."</p>
                </div>
            </div>
            {unavailable_reason.map(|reason| view! {
                <div class="lifecycle-notice" role="status">
                    <strong>"Creation unavailable"</strong>
                    <p>{reason}</p>
                </div>
            })}
            <form class="lifecycle-form" novalidate=true on:submit=submit>
                <div class="form-field">
                    <label for="tenant-create-name">"Tenant name"</label>
                    <input
                        id="tenant-create-name"
                        type="text"
                        maxlength="30"
                        disabled=move || !capability_available || matches!(state.get(), MutationState::Running)
                        aria-describedby="tenant-create-name-error"
                        on:input=move |event| name.set(event_target_value(&event))
                    />
                    <FieldError id="tenant-create-name-error" errors=field_errors field=TenantField::Name/>
                </div>
                <div class="form-field">
                    <label for="tenant-create-workers">"Workers"</label>
                    <input
                        id="tenant-create-workers"
                        type="number"
                        min="1"
                        max="3"
                        value="1"
                        disabled=move || !capability_available || matches!(state.get(), MutationState::Running)
                        aria-describedby="tenant-create-workers-error"
                        on:input=move |event| workers.set(event_target_value(&event))
                    />
                    <FieldError id="tenant-create-workers-error" errors=field_errors field=TenantField::Workers/>
                </div>
                <div class="form-field">
                    <span class="form-label">"Kubernetes version"</span>
                    <strong>{version}</strong>
                </div>
                <div class="form-actions">
                    <button
                        type="submit"
                        disabled=move || !capability_available || matches!(state.get(), MutationState::Running)
                    >
                        {move || if matches!(state.get(), MutationState::Running) {
                            "Creating…"
                        } else {
                            "Create Tenant"
                        }}
                    </button>
                </div>
                <div aria-live="polite">
                    {move || match state.get() {
                        MutationState::Error(error) => Some(view! {
                            <p class="form-error" role="alert">{error.message}</p>
                        }.into_any()),
                        MutationState::Running => Some(view! {
                            <p class="secondary" role="status">"Submitting Tenant creation…"</p>
                        }.into_any()),
                        MutationState::Uncertain { message, href } => Some(view! {
                            <p class="form-error" role="alert">
                                {message}
                                " "
                                <a href=href>"Inspect Tenant"</a>
                            </p>
                        }.into_any()),
                        MutationState::Idle => None,
                    }}
                </div>
            </form>
        </section>
    }
}

#[component]
fn FieldError(
    id: &'static str,
    errors: RwSignal<Vec<TenantFieldError>>,
    field: TenantField,
) -> impl IntoView {
    view! {
        <p id=id class="field-error">
            {move || errors.get().into_iter().find(|error| error.field == field).map(|error| error.message)}
        </p>
    }
}

#[component]
fn Metric(label: &'static str, value: u32) -> impl IntoView {
    view! {
        <div class="metric">
            <span class="metric__label">{label}</span>
            <strong class="metric__value">{value}</strong>
        </div>
    }
}

fn tenant_table(tenants: Vec<TenantSummary>) -> AnyView {
    view! {
        <div class="table-scroll">
            <table>
                <caption>"Tenant status"</caption>
                <thead>
                    <tr>
                        <th scope="col">"Name"</th>
                        <th scope="col">"Provider"</th>
                        <th scope="col">"Status"</th>
                        <th scope="col">"Kubernetes"</th>
                        <th scope="col">"Capacity"</th>
                        <th scope="col">"Endpoint"</th>
                        <th scope="col">"Age"</th>
                        <th scope="col">"Conditions"</th>
                    </tr>
                </thead>
                <tbody>
                    {tenants.into_iter().map(tenant_row).collect_view()}
                </tbody>
            </table>
        </div>
    }
    .into_any()
}

fn tenant_row(tenant: TenantSummary) -> AnyView {
    let href = tenant_href(&tenant.name).unwrap_or_else(|| "/".to_owned());
    let classification = tenant.classification;
    let status_class = classification_class(classification);
    let status_label = classification_label(classification);
    let capacity = format!(
        "{} worker{}",
        tenant.requested_workers,
        plural(tenant.requested_workers)
    );
    let conditions = condition_summary(&tenant.conditions);
    let endpoint = optional_text(tenant.endpoint.as_deref()).to_owned();
    let age = format_age(tenant.created_at.as_deref());

    view! {
        <tr>
            <td><a class="tenant-name" href=href>{tenant.name}</a></td>
            <td>{provider_label(tenant.provider)}</td>
            <td><span class=format!("status status--{status_class}")>{status_label}</span></td>
            <td>{tenant.kubernetes_version}</td>
            <td>{capacity}</td>
            <td>{endpoint}</td>
            <td>{age}</td>
            <td>{conditions}</td>
        </tr>
    }
    .into_any()
}

fn tenant_workspace_view(
    data: TenantSnapshot,
    section: TenantSection,
    resource_selection: RwSignal<Option<String>>,
    invalid_selection: bool,
    selected_database: Option<String>,
    snapshot_refresh: RwSignal<u32>,
    catalog_refresh: RwSignal<u32>,
) -> AnyView {
    let detail = data.detail.clone();
    let summary = detail.summary.clone();
    let tenant_name = summary.name.clone();
    let tenant_uid = detail.uid.clone();
    let classification = summary.classification;
    let status_class = classification_class(classification);
    let age = format_age(summary.created_at.as_deref());

    view! {
        <section class="tenant-context" aria-label="Tenant context">
            <div>
                <span class=format!("status status--{status_class}")>
                    {classification_label(classification)}
                </span>
                <strong>{provider_label(summary.provider)}</strong>
                <span>{format!("Kubernetes {}", summary.kubernetes_version)}</span>
                <span>{format!("{} worker{}", summary.requested_workers, plural(summary.requested_workers))}</span>
                <span>{format!("Age {age}")}</span>
            </div>
            <p class="secondary">{format!("UID {} · Snapshot {}", detail.uid, data.observed_at)}</p>
        </section>
        <nav class="tenant-nav" aria-label="Tenant sections">
            {TenantSection::ALL.into_iter().map(|candidate| {
                let href = tenant_section_href(&tenant_name, candidate).unwrap_or_else(|| "/".into());
                view! {
                    <a href=href aria-current=(candidate == section).then_some("page")>
                        {tenant_section_label(candidate)}
                    </a>
                }
            }).collect_view()}
        </nav>
        {match section {
            TenantSection::Overview => tenant_overview_view(&data),
            TenantSection::Resources => resources_section_view(
                &data,
                resource_selection,
                invalid_selection,
            ),
            TenantSection::Databases => view! {
                <CatalogPanel name=tenant_name tenant_uid classification
                    selected_uid=selected_database snapshot_refresh=Some(snapshot_refresh)
                    external_refresh=Some(catalog_refresh)/>
            }.into_any(),
            TenantSection::Status => tenant_status_view(&data),
            TenantSection::Settings => tenant_settings_view(data),
        }}
    }
    .into_any()
}

fn tenant_overview_view(data: &TenantSnapshot) -> AnyView {
    let detail = &data.detail;
    let model = ExplorerModel::new(data.topology.clone());
    let summaries = model.group_summaries();
    let attention = model.attention_nodes();
    let capacity = &detail.worker_capacity;
    let available = capacity
        .available
        .map_or_else(|| "Unknown".into(), |value| value.to_string());
    let unavailable = capacity
        .unavailable
        .map_or_else(|| "Unknown".into(), |value| value.to_string());
    let name = detail.summary.name.clone();
    view! {
        <section class="panel" aria-labelledby="readiness-heading">
            <div class="panel__header">
                <div>
                    <h2 id="readiness-heading">"Current snapshot"</h2>
                    <p>"Readiness and lifecycle are current observations, not historical telemetry."</p>
                </div>
            </div>
            <div class="readiness-grid">
                {summaries.into_iter()
                    .filter(|summary| summary.group != crate::explorer::ResourceGroup::Tenant)
                    .map(|summary| view! {
                    <div class="readiness-card">
                        <strong>{summary.group.label()}</strong>
                        <span>{format!("{} resources", summary.count)}</span>
                        <span class="secondary">{format!(
                            "{} ready · {} progressing · {} degraded · {} failed · {} deleting · {} unknown",
                            summary.health.ready,
                            summary.health.progressing,
                            summary.health.degraded,
                            summary.health.failed,
                            summary.health.deleting,
                            summary.health.unknown,
                        )}</span>
                        <span class="secondary">{format!(
                            "{} exact · {} logical · {} external · {} recorded",
                            summary.provenance.exact,
                            summary.provenance.database_logical,
                            summary.provenance.external,
                            summary.provenance.recorded,
                        )}</span>
                    </div>
                }).collect_view()}
            </div>
        </section>
        <div class="detail-grid">
            <section class="panel" aria-labelledby="capacity-heading">
                <h2 id="capacity-heading">"Worker capacity"</h2>
                <dl class="definition-list">
                    <dt>"Desired"</dt><dd>{capacity.desired}</dd>
                    <dt>"Available"</dt><dd>{available}</dd>
                    <dt>"Unavailable"</dt><dd>{unavailable}</dd>
                    <dt>"Ready Machines"</dt>
                    <dd>{capacity.diagnostic_ready_machines.map_or_else(|| "Not applicable".into(), |value| value.to_string())}</dd>
                </dl>
            </section>
            <section class="panel" aria-labelledby="lifecycle-heading">
                <h2 id="lifecycle-heading">"Lifecycle"</h2>
                <ol class="lifecycle-steps">
                    {detail.lifecycle.iter().map(|stage| {
                        let class = lifecycle_class(stage.state);
                        view! {
                            <li class=format!("lifecycle-step lifecycle-step--{class}")>
                                <strong>{lifecycle_stage_label(stage.stage)}</strong>
                                <span>{lifecycle_state_label(stage.state)}</span>
                            </li>
                        }
                    }).collect_view()}
                </ol>
            </section>
        </div>
        <section class="panel" aria-labelledby="attention-heading">
            <div class="panel__header">
                <div>
                    <h2 id="attention-heading">"Needs attention"</h2>
                    <p>"Unhealthy tenant-associated resources in this snapshot."</p>
                </div>
            </div>
            {if !matches!(data.sections.resources, SectionAvailability::Available) {
                view! {
                    <p class="lifecycle-notice">
                        "Resource inventory is unavailable. Attention links are disabled until an authoritative inventory can be read."
                    </p>
                }.into_any()
            } else if attention.is_empty() {
                view! { <p class="empty">"No resources need attention."</p> }.into_any()
            } else {
                view! {
                    <ul class="attention-list">
                        {attention.into_iter().map(|node| {
                            let href = tenant_resource_href(&name, &node.id)
                                .unwrap_or_else(|| tenant_section_href(&name, TenantSection::Resources).unwrap());
                            let class = health_class(node.health);
                            view! {
                                <li>
                                    <a href=href><strong>{node.label}</strong></a>
                                    <span class=format!("status status--{class}")>{health_label(node.health)}</span>
                                </li>
                            }
                        }).collect_view()}
                    </ul>
                }.into_any()
            }}
        </section>
    }.into_any()
}

fn resources_section_view(
    data: &TenantSnapshot,
    resource_selection: RwSignal<Option<String>>,
    invalid_selection: bool,
) -> AnyView {
    match &data.sections.resources {
        SectionAvailability::Available => view! {
            <ResourceExplorer graph=data.topology.clone() selection=resource_selection invalid_selection/>
        }.into_any(),
        SectionAvailability::Unavailable { message, retryable, .. } => view! {
            <section class="state-panel state-panel--error" role="alert">
                <h2>"Resource inventory unavailable"</h2>
                <p>{message.clone()}</p>
                <p class="secondary">{if *retryable {
                    "Refresh to retry the authoritative management inventory."
                } else {
                    "The inventory exceeded a service boundary and requires operator action."
                }}</p>
            </section>
        }.into_any(),
    }
}

fn tenant_status_view(data: &TenantSnapshot) -> AnyView {
    let name = data.detail.summary.name.clone();
    let resource_available = matches!(data.sections.resources, SectionAvailability::Available);
    view! {
        <div class="detail-grid">
            {conditions_panel(data.detail.summary.conditions.clone())}
            <section class="panel" aria-labelledby="blockers-heading">
                <h2 id="blockers-heading">"Blockers"</h2>
                {if data.detail.blockers.is_empty() {
                    view! { <p class="empty">"No active blockers are reported."</p> }.into_any()
                } else {
                    view! {
                        <ul class="blocker-list">
                            {data.detail.blockers.iter().cloned().map(|blocker| {
                                let href = blocker.target_node_id.as_deref()
                                    .filter(|_| resource_available)
                                    .and_then(|target| tenant_resource_href(&name, target));
                                view! {
                                    <li>
                                        <strong>{blocker.code}</strong>
                                        {blocker.condition_type.map(|value| view! {
                                            <span class="secondary">{format!(" · {value}")}</span>
                                        })}
                                        <p>{blocker.message}</p>
                                        {href.map(|href| view! { <a href=href>"Inspect affected resource"</a> })}
                                    </li>
                                }
                            }).collect_view()}
                        </ul>
                    }.into_any()
                }}
            </section>
        </div>
        <section class="panel" aria-labelledby="availability-heading">
            <h2 id="availability-heading">"Section availability"</h2>
            <dl class="definition-list">
                <dt>"Resources and topology"</dt><dd>{section_availability_label(&data.sections.resources)}</dd>
                <dt>"Databases"</dt><dd>{section_availability_label(&data.sections.databases)}</dd>
            </dl>
        </section>
    }.into_any()
}

fn tenant_settings_view(data: TenantSnapshot) -> AnyView {
    let detail = data.detail;
    let summary = detail.summary.clone();
    let generation_status = match detail.observed_generation {
        Some(observed) if observed == detail.generation => {
            format!("Current (generation {})", detail.generation)
        }
        Some(observed) => format!(
            "Reconciling generation {} (observed {})",
            detail.generation, observed
        ),
        None => format!("Generation {} has not been observed", detail.generation),
    };
    view! {
        <div class="detail-grid">
            <section class="panel" aria-labelledby="specification-heading">
                <h2 id="specification-heading">"Immutable specification"</h2>
                <dl class="definition-list">
                    <dt>"Tenant UID"</dt><dd>{detail.uid.clone()}</dd>
                    <dt>"Generation"</dt><dd>{generation_status}</dd>
                    <dt>"Kubernetes version"</dt><dd>{detail.specification.kubernetes_version}</dd>
                    <dt>"Workers"</dt><dd>{detail.specification.workers}</dd>
                    {provider_specification_rows(detail.specification.provider)}
                </dl>
            </section>
            <section class="panel" aria-labelledby="endpoint-heading">
                <h2 id="endpoint-heading">"Access endpoint"</h2>
                <dl class="definition-list">
                    <dt>"Endpoint"</dt><dd>{optional_text(summary.endpoint.as_deref()).to_owned()}</dd>
                    <dt>"Provider"</dt><dd>{provider_label(summary.provider)}</dd>
                </dl>
                <p class="secondary">"Credentials and kubeconfig material are never returned to this browser."</p>
            </section>
        </div>
        {provider_panel(detail.provider_status)}
        <TenantDeletePanel name=summary.name uid=detail.uid/>
    }.into_any()
}

fn tenant_section_label(section: TenantSection) -> &'static str {
    match section {
        TenantSection::Overview => "Overview",
        TenantSection::Resources => "Resources",
        TenantSection::Databases => "Databases",
        TenantSection::Status => "Status",
        TenantSection::Settings => "Settings",
    }
}

fn lifecycle_class(state: LifecycleStageState) -> &'static str {
    match state {
        LifecycleStageState::Completed => "completed",
        LifecycleStageState::Current => "current",
        LifecycleStageState::Blocked => "blocked",
        LifecycleStageState::Pending => "pending",
        LifecycleStageState::NotApplicable => "not-applicable",
        LifecycleStageState::Unknown => "unknown",
    }
}

fn section_availability_label(section: &SectionAvailability) -> String {
    match section {
        SectionAvailability::Available => "Available".into(),
        SectionAvailability::Unavailable { message, .. } => format!("Unavailable · {message}"),
    }
}

#[component]
fn TenantDeletePanel(name: String, uid: String) -> impl IntoView {
    let confirmation = RwSignal::new(String::new());
    let state = RwSignal::new(MutationState::Idle);
    let requested_name = name.clone();
    let requested_uid = uid.clone();
    let submit = move |event: leptos::ev::SubmitEvent| {
        event.prevent_default();
        if !exact_confirmation_enabled(
            &requested_name,
            &confirmation.get_untracked(),
            matches!(state.get_untracked(), MutationState::Running),
            false,
        ) {
            return;
        }
        state.set(MutationState::Running);
        let name = requested_name.clone();
        let uid = requested_uid.clone();
        spawn_local(async move {
            let Some(path) = tenant_delete_path(&name) else {
                state.set(MutationState::Error(UiError {
                    kind: UiErrorKind::InvalidRequest,
                    message: "The Tenant name is invalid.".into(),
                    retryable: false,
                    field_errors: Vec::new(),
                }));
                return;
            };
            let request = TenantDeleteRequest {
                uid: uid.clone(),
                confirmation: name.clone(),
            };
            match delete_envelope::<_, TenantDeleteResponse>(&path, &request).await {
                Ok(response) => match response.state {
                    TenantDeleteState::Completed => navigate_to_overview(),
                    TenantDeleteState::Accepted => reload_page(),
                },
                Err(error) if requires_authoritative_read(error.kind) => {
                    match fetch_tenant_page(&name).await {
                        Ok(snapshot) => match delete_recovery(
                            &uid,
                            Some((
                                &snapshot.identity.uid,
                                snapshot.detail.summary.classification,
                            )),
                        ) {
                            DeleteRecovery::StaleIdentity => state
                                .set(MutationState::Error(UiError {
                                kind: UiErrorKind::StaleIdentity,
                                message:
                                    "A replacement Tenant now uses this name; it was not deleted."
                                        .into(),
                                retryable: false,
                                field_errors: Vec::new(),
                            })),
                            DeleteRecovery::Reload => reload_page(),
                            DeleteRecovery::Overview => navigate_to_overview(),
                            DeleteRecovery::InspectCurrent => reload_page(),
                        },
                        Err(observed) if observed.kind == UiErrorKind::NotFound => {
                            match delete_recovery(&uid, None) {
                                DeleteRecovery::Overview => navigate_to_overview(),
                                _ => unreachable!(),
                            }
                        }
                        Err(_) => state.set(MutationState::Error(error)),
                    }
                }
                Err(error) => state.set(MutationState::Error(error)),
            }
        });
    };
    let button_name = name.clone();

    view! {
        <section class="panel lifecycle-panel lifecycle-panel--danger" aria-labelledby="delete-tenant-heading">
            <div class="panel__header">
                <div>
                    <h2 id="delete-tenant-heading">"Delete Tenant"</h2>
                    <p>"Deletion is asynchronous and permanently removes the Tenant's owned infrastructure and databases."</p>
                </div>
            </div>
            <div class="destructive-warning" role="alert">
                <strong>"Destructive administrator action"</strong>
                <p>"Type the exact Tenant name to confirm. A stale page cannot delete a same-name replacement."</p>
            </div>
            <form class="lifecycle-form" on:submit=submit>
                <div class="form-field">
                    <label for="tenant-delete-confirmation">
                        {format!("Type {name} to confirm")}
                    </label>
                    <input
                        id="tenant-delete-confirmation"
                        type="text"
                        autocomplete="off"
                        disabled=move || matches!(state.get(), MutationState::Running)
                        on:input=move |event| confirmation.set(event_target_value(&event))
                    />
                </div>
                <div class="form-actions">
                    <button
                        class="button--danger"
                        type="submit"
                        disabled=move || !exact_confirmation_enabled(
                            &button_name,
                            &confirmation.get(),
                            matches!(state.get(), MutationState::Running),
                            false,
                        )
                    >
                        {move || if matches!(state.get(), MutationState::Running) {
                            "Deleting…"
                        } else {
                            "Delete Tenant"
                        }}
                    </button>
                </div>
                <div aria-live="polite">
                    {move || match state.get() {
                        MutationState::Error(error) => Some(view! {
                            <p class="form-error" role="alert">{error.message}</p>
                        }.into_any()),
                        MutationState::Running => Some(view! {
                            <p class="secondary" role="status">"Submitting Tenant deletion…"</p>
                        }.into_any()),
                        MutationState::Uncertain { message, href } => Some(view! {
                            <p class="form-error" role="alert">{message}" "<a href=href>"Inspect Tenant"</a></p>
                        }.into_any()),
                        MutationState::Idle => None,
                    }}
                </div>
            </form>
        </section>
    }
}

fn provider_specification_rows(specification: ProviderSpecificationView) -> AnyView {
    match specification {
        ProviderSpecificationView::Local => view! {
            <dt>"Provider configuration"</dt>
            <dd>"Local workers and storage"</dd>
        }
        .into_any(),
        ProviderSpecificationView::Azure => view! {
            <dt>"Provider configuration"</dt><dd>"Controller-managed Azure networking"</dd>
        }
        .into_any(),
        ProviderSpecificationView::Unknown { provider_type } => view! {
            <dt>"Provider type"</dt><dd>{provider_type}</dd>
        }
        .into_any(),
    }
}

fn conditions_panel(conditions: Vec<TenantCondition>) -> AnyView {
    view! {
        <section class="panel" aria-labelledby="conditions-heading">
            <h2 id="conditions-heading">"Conditions"</h2>
            {if conditions.is_empty() {
                view! { <p class="empty">"No conditions have been reported."</p> }.into_any()
            } else {
                view! {
                    <ul class="condition-list">
                        {conditions
                            .into_iter()
                            .map(|condition| {
                                let (class, status) = condition_style(condition.status);
                                let detail = condition
                                    .message
                                    .or(condition.reason)
                                    .unwrap_or_else(|| "No detail reported.".to_owned());
                                let generation = condition
                                    .observed_generation
                                    .map(|value| format!("Observed generation {value}"));
                                view! {
                                    <li>
                                        <div class="condition-heading">
                                            <strong>{condition.condition_type}</strong>
                                            <span class=format!("status status--{class}")>{status}</span>
                                        </div>
                                        <p>{detail}</p>
                                        {generation.map(|value| view! { <p class="secondary">{value}</p> })}
                                    </li>
                                }
                            })
                            .collect_view()}
                    </ul>
                }
                .into_any()
            }}
        </section>
    }
    .into_any()
}

fn blockers_panel(blockers: Vec<tenant_admin_shared::query::TenantBlocker>) -> AnyView {
    view! {
        <section class="panel" aria-labelledby="blockers-heading">
            <h2 id="blockers-heading">"Blockers"</h2>
            {if blockers.is_empty() {
                view! { <p class="empty">"No active blockers are reported."</p> }.into_any()
            } else {
                view! {
                    <ul class="blocker-list">
                        {blockers
                            .into_iter()
                            .map(|blocker| view! {
                                <li>
                                    <strong>{blocker.code}</strong>
                                    {blocker.condition_type.map(|value| view! {
                                        <span class="secondary">{format!(" · {value}")}</span>
                                    })}
                                    <p>{blocker.message}</p>
                                </li>
                            })
                            .collect_view()}
                    </ul>
                }
                .into_any()
            }}
        </section>
    }
    .into_any()
}

fn provider_panel(status: ProviderStatusView) -> AnyView {
    view! {
        <section class="panel" aria-labelledby="provider-heading">
            <div class="panel__header">
                <div>
                    <h2 id="provider-heading">"Provider status"</h2>
                    <p>"Sanitized provider observations from Tenant status."</p>
                </div>
            </div>
            {match status {
                ProviderStatusView::Local(local) => {
                    let allocation = local.allocation.map(|allocation| view! {
                        <dl class="definition-list">
                            <dt>"Slot"</dt><dd>{allocation.slot_id}</dd>
                            <dt>"Endpoint"</dt><dd>{allocation.endpoint}</dd>
                            <dt>"Pod CIDR"</dt><dd>{allocation.pod_cidr}</dd>
                            <dt>"Service CIDR"</dt><dd>{allocation.service_cidr}</dd>
                        </dl>
                    });
                    view! {
                        <div class="provider-grid">
                            <div>
                                <h3>"Allocation"</h3>
                                {allocation
                                    .map(|view| view.into_any())
                                    .unwrap_or_else(|| view! { <p>"Not allocated yet."</p> }.into_any())}
                            </div>
                            <div>
                                <h3>"Management identity"</h3>
                                <dl class="definition-list">
                                    <dt>"Cluster UID"</dt>
                                    <dd>{optional_text(local.cluster_uid.as_deref()).to_owned()}</dd>
                                    <dt>"Foundation hash"</dt>
                                    <dd>{optional_text(local.foundation_hash.as_deref()).to_owned()}</dd>
                                </dl>
                            </div>
                        </div>
                    }
                    .into_any()
                }
                ProviderStatusView::Azure(azure) => azure_provider_view(*azure),
                ProviderStatusView::Unknown(unknown) => view! {
                    <dl class="definition-list">
                        <dt>"Provider type"</dt><dd>{unknown.provider_type}</dd>
                        <dt>"Summary"</dt><dd>{optional_text(unknown.summary.as_deref()).to_owned()}</dd>
                    </dl>
                }
                .into_any(),
            }}
        </section>
    }
    .into_any()
}

fn azure_provider_view(mut azure: AzureProviderView) -> AnyView {
    azure
        .nodes
        .sort_by(|left, right| left.name.cmp(&right.name));
    azure.add_ons.sort_by_key(identity_key);
    azure
        .resources
        .sort_by_key(|resource| identity_key(&resource.identity));

    let binding = azure.binding.map(|binding| {
        view! {
            <dl class="definition-list">
                <dt>"Cluster name"</dt><dd>{binding.cluster_name}</dd>
                <dt>"Resource group"</dt><dd>{binding.resource_group}</dd>
                <dt>"Binding hash"</dt><dd>{binding.binding_hash}</dd>
            </dl>
        }
    });
    let allocation = azure.allocation.map(|allocation| {
        view! {
            <dl class="definition-list">
                <dt>"Slot"</dt><dd>{allocation.slot_id}</dd>
                <dt>"Pod CIDR"</dt><dd>{allocation.pod_cidr}</dd>
                <dt>"Service CIDR"</dt><dd>{allocation.service_cidr}</dd>
            </dl>
        }
    });
    let worker_pool = azure.worker_pool.map(|pool| view! {
        <dl class="definition-list">
            <dt>"Name"</dt><dd>{pool.name}</dd>
            <dt>"Scale set"</dt><dd>{optional_text(pool.scale_set_name.as_deref()).to_owned()}</dd>
            <dt>"Replicas"</dt><dd>{format!("{} ready / {} desired", pool.ready_replicas, pool.desired_replicas)}</dd>
        </dl>
    });
    let management = azure.management.map(|management| view! {
        <dl class="definition-list">
            <dt>"Cluster UID"</dt><dd>{optional_text(management.cluster_uid.as_deref()).to_owned()}</dd>
            <dt>"Infrastructure UID"</dt><dd>{optional_text(management.infrastructure_uid.as_deref()).to_owned()}</dd>
            <dt>"Control plane UID"</dt><dd>{optional_text(management.control_plane_uid.as_deref()).to_owned()}</dd>
        </dl>
    });

    view! {
        <div class="provider-grid">
            <div>
                <h3>"Azure binding"</h3>
                {binding
                    .map(|view| view.into_any())
                    .unwrap_or_else(|| view! { <p>"Binding is not available yet."</p> }.into_any())}
            </div>
            <div>
                <h3>"Worker pool"</h3>
                {worker_pool
                    .map(|view| view.into_any())
                    .unwrap_or_else(|| view! { <p>"Worker pool is not available yet."</p> }.into_any())}
            </div>
            <div>
                <h3>"Network allocation"</h3>
                {allocation
                    .map(|view| view.into_any())
                    .unwrap_or_else(|| view! { <p>"Network allocation is not available yet."</p> }.into_any())}
            </div>
            <div>
                <h3>"Management identities"</h3>
                {management
                    .map(|view| view.into_any())
                    .unwrap_or_else(|| view! { <p>"Management identities are not available yet."</p> }.into_any())}
            </div>
            <div>
                <h3>"Endpoint"</h3>
                <p>{optional_text(azure.endpoint.as_deref()).to_owned()}</p>
            </div>
        </div>
        <div class="resource-grid">
            <div>
                <h3>"Nodes"</h3>
                {if azure.nodes.is_empty() {
                    view! { <p>"No Nodes are linked yet."</p> }.into_any()
                } else {
                    view! {
                        <ul class="resource-list">
                            {azure.nodes
                                .into_iter()
                                .map(|node| view! {
                                    <li>
                                        <div class="resource-heading">
                                            <strong>{node.name}</strong>
                                            <span class=format!(
                                                "status status--{}",
                                                if node.ready { "ready" } else { "degraded" }
                                            )>
                                                {if node.ready { "Ready" } else { "Not ready" }}
                                            </span>
                                        </div>
                                        <p>{format!(
                                            "Internal IP: {}",
                                            optional_text(node.internal_ip.as_deref())
                                        )}</p>
                                    </li>
                                })
                                .collect_view()}
                        </ul>
                    }
                    .into_any()
                }}
            </div>
            <div>
                <h3>"Add-ons and provider resources"</h3>
                {if azure.add_ons.is_empty() && azure.resources.is_empty() {
                    view! { <p>"No linked provider resources are reported."</p> }.into_any()
                } else {
                    view! {
                        <ul class="resource-list">
                            {azure
                                .add_ons
                                .into_iter()
                                .map(|identity| {
                                    let name = namespaced_name(identity.namespace.as_deref(), &identity.name);
                                    view! { <li><strong>{identity.kind}</strong><p>{name}</p></li> }
                                })
                                .collect_view()}
                            {azure
                                .resources
                                .into_iter()
                                .map(|resource| {
                                    let name = namespaced_name(
                                        resource.identity.namespace.as_deref(),
                                        &resource.identity.name,
                                    );
                                    view! {
                                        <li>
                                            <strong>{resource.identity.kind}</strong>
                                            <p>{name}</p>
                                            {resource.resource_id.map(|id| view! {
                                                <p class="secondary">{id}</p>
                                            })}
                                        </li>
                                    }
                                })
                                .collect_view()}
                        </ul>
                    }
                    .into_any()
                }}
            </div>
        </div>
    }
    .into_any()
}

fn management_resources_panel(
    mut resources: Vec<tenant_admin_shared::query::ManagementResourceView>,
) -> AnyView {
    resources.sort_by(|left, right| {
        left.role
            .cmp(&right.role)
            .then_with(|| identity_key(&left.identity).cmp(&identity_key(&right.identity)))
    });
    view! {
        <section class="panel" aria-labelledby="resources-heading">
            <div class="panel__header">
                <div>
                    <h2 id="resources-heading">"Management resources"</h2>
                    <p>"Resources associated through exact status and ownership identities."</p>
                </div>
            </div>
            {if resources.is_empty() {
                view! { <p class="empty">"No linked management resources are reported."</p> }.into_any()
            } else {
                view! {
                    <ul class="resource-list">
                        {resources
                            .into_iter()
                            .map(|resource| {
                                let class = health_class(resource.health);
                                let status = health_label(resource.health);
                                let name = namespaced_name(
                                    resource.identity.namespace.as_deref(),
                                    &resource.identity.name,
                                );
                                view! {
                                    <li>
                                        <div class="resource-heading">
                                            <strong>{format!("{} · {}", resource.role, resource.identity.kind)}</strong>
                                            <span class=format!("status status--{class}")>{status}</span>
                                        </div>
                                        <p>{name}</p>
                                        {resource.message.map(|message| view! { <p>{message}</p> })}
                                    </li>
                                }
                            })
                            .collect_view()}
                    </ul>
                }
                .into_any()
            }}
        </section>
    }
    .into_any()
}

fn topology_panel(graph: TopologyGraph) -> AnyView {
    let layout = layout_graph(&graph);
    let title_id = format!("topology-{}-title", graph.tenant_name);
    let description_id = format!("topology-{}-description", graph.tenant_name);
    let labelled_by = format!("{title_id} {description_id}");
    let view_box = format!("0 0 {} {}", layout.width, layout.height);
    let graph_title = format!("{} management topology", graph.tenant_name);
    let provider = provider_label(graph.provider);

    view! {
        <section class="panel" aria-labelledby="topology-heading">
            <div class="panel__header">
                <div>
                    <h2 id="topology-heading">"Topology"</h2>
                    <p>{format!("{provider} management-plane resources; layout is deterministic.")}</p>
                </div>
            </div>
            {if layout.nodes.is_empty() {
                view! {
                    <div class="empty">
                        <h3>"Topology is not available yet"</h3>
                        <p>"The Tenant can still be inspected while management resources reconcile."</p>
                    </div>
                }
                .into_any()
            } else {
                view! {
                    <div class="topology-scroll">
                        <svg
                            class="topology"
                            viewBox=view_box
                            role="img"
                            aria-labelledby=labelled_by
                            preserveAspectRatio="xMinYMin meet"
                        >
                            <title id=title_id>{graph_title}</title>
                            <desc id=description_id>
                                "A left-to-right graph of the Tenant and associated management resources. Each node includes its resource type and health."
                            </desc>
                            <defs>
                                <marker
                                    id="topology-arrow"
                                    markerWidth="8"
                                    markerHeight="8"
                                    refX="7"
                                    refY="4"
                                    orient="auto"
                                    markerUnits="strokeWidth"
                                >
                                    <path d="M 0 0 L 8 4 L 0 8 z" fill="#8296a5"/>
                                </marker>
                            </defs>
                            {layout
                                .edges
                                .into_iter()
                                .map(|edge| {
                                    let label = edge
                                        .label
                                        .unwrap_or_else(|| edge_kind_label(edge.kind).to_owned());
                                    view! {
                                        <g>
                                            <path
                                                class="edge"
                                                d=edge.path
                                                marker-end="url(#topology-arrow)"
                                            />
                                            <text class="edge-label" x=edge.label_x y=edge.label_y>{label}</text>
                                        </g>
                                    }
                                })
                                .collect_view()}
                            {layout
                                .nodes
                                .into_iter()
                                .map(|node| {
                                    let class = health_class(node.health);
                                    let kind = node_kind_label(node.kind);
                                    let health = health_label(node.health);
                                    let accessible = format!("{}; {kind}; status {health}", node.label);
                                    view! {
                                        <g
                                            class=format!("node node--{class}")
                                            role="group"
                                            aria-label=accessible
                                            tabindex="0"
                                            transform=format!("translate({}, {})", node.x, node.y)
                                        >
                                            <rect
                                                width=node.width
                                                height=node.height
                                                rx="9"
                                                ry="9"
                                            />
                                            <text class="node-label" x="14" y="29">{node.label}</text>
                                            <text class="node-meta" x="14" y="53">{kind}</text>
                                            <text class="node-meta" x="14" y="72">{health}</text>
                                        </g>
                                    }
                                })
                                .collect_view()}
                        </svg>
                    </div>
                }
                .into_any()
            }}
        </section>
    }
    .into_any()
}

#[component]
fn RouteNotFound() -> impl IntoView {
    view! {
        <main class="page">
            <section class="state-panel state-panel--error">
                <h1>"Page not found"</h1>
                <p>"The requested Tenant Admin page does not exist."</p>
                <p><a href="/">"Return to all Tenants"</a></p>
            </section>
        </main>
    }
}

#[component]
fn TenantRouteNotFound(name: String) -> impl IntoView {
    let tenant = tenant_section_href(&name, TenantSection::Overview).unwrap_or_else(|| "/".into());
    view! {
        <main class="page">
            <section class="state-panel state-panel--error">
                <h1>"Tenant section not found"</h1>
                <p>"The requested section is not available."</p>
                <p><a href=tenant>"Return to Tenant overview"</a></p>
                <p><a href="/">"Return to all Tenants"</a></p>
            </section>
        </main>
    }
}

async fn fetch_dashboard() -> Result<OverviewSnapshot, UiError> {
    get_envelope(API_OVERVIEW_PATH).await
}

fn navigate_to_tenant(name: &str) {
    if let Some(href) = tenant_href(name) {
        let _ = web_sys::window().map(|window| window.location().set_href(&href));
    }
}

fn navigate_to_overview() {
    let _ = web_sys::window().map(|window| window.location().set_href("/"));
}

fn reload_page() {
    let _ = web_sys::window().map(|window| window.location().reload());
}

async fn fetch_tenant_page(name: &str) -> Result<TenantSnapshot, UiError> {
    let Some(href) = tenant_href(name) else {
        return Err(UiError {
            kind: UiErrorKind::InvalidRequest,
            message: "The Tenant name is not valid.".to_owned(),
            retryable: false,
            field_errors: Vec::new(),
        });
    };
    let detail_path = format!("{API_PREFIX}{href}");
    get_envelope(&detail_path).await
}

fn condition_summary(conditions: &[TenantCondition]) -> String {
    if conditions.is_empty() {
        return "None reported".to_owned();
    }
    let noteworthy = conditions
        .iter()
        .filter(|condition| condition.status != ConditionStatus::True)
        .take(2)
        .map(|condition| {
            format!(
                "{} {}",
                condition.condition_type,
                condition_status_label(condition.status)
            )
        })
        .collect::<Vec<_>>();
    if noteworthy.is_empty() {
        "All reported conditions true".to_owned()
    } else {
        noteworthy.join(", ")
    }
}

fn condition_style(status: ConditionStatus) -> (&'static str, &'static str) {
    match status {
        ConditionStatus::True => ("ready", "True"),
        ConditionStatus::False => ("degraded", "False"),
        ConditionStatus::Unknown => ("unknown", "Unknown"),
    }
}

fn plural(count: u32) -> &'static str {
    if count == 1 { "" } else { "s" }
}

fn namespaced_name(namespace: Option<&str>, name: &str) -> String {
    namespace.map_or_else(
        || name.to_owned(),
        |namespace| format!("{namespace}/{name}"),
    )
}

fn identity_key(identity: &tenant_admin_shared::query::ResourceIdentityView) -> String {
    format!(
        "{}|{}|{}|{}",
        identity.api_version,
        identity.kind,
        identity.namespace.as_deref().unwrap_or_default(),
        identity.name
    )
}
