use leptos::prelude::*;
use tenant_admin_shared::{
    API_SCHEMA_VERSION,
    query::{
        AzureProviderView, ConditionStatus, OverviewSnapshot, ProviderSpecificationView,
        ProviderStatusView, TenantCondition, TenantSnapshot, TenantSummary, TopologyGraph,
    },
    routes::{API_OVERVIEW_PATH, API_PREFIX},
};
use wasm_bindgen_futures::spawn_local;

use crate::{
    api::get_envelope,
    error::{UiError, UiErrorKind},
    format::{
        classification_class, classification_label, condition_status_label, edge_kind_label,
        format_age, health_class, health_label, node_kind_label, optional_text, provider_label,
        provider_mode_label,
    },
    route::{AppRoute, parse_route, tenant_href},
    topology::layout_graph,
};

#[derive(Clone)]
enum LoadState<T> {
    Loading,
    Ready(T),
    Error(UiError),
}

#[component]
pub fn App() -> impl IntoView {
    let route = web_sys::window()
        .and_then(|window| window.location().pathname().ok())
        .map_or(AppRoute::NotFound, |path| parse_route(&path));

    view! {
        <div id=tenant_admin_shared::ADMIN_RESOURCE_NAME>
            <header class="site-header">
                <div class="site-header__inner">
                    <a class="brand" href="/">
                        "Tenant Admin"
                        <span>"Read-only management view"</span>
                    </a>
                </div>
            </header>
            {match route {
                AppRoute::Overview => view! { <OverviewPage/> }.into_any(),
                AppRoute::Tenant(name) => view! { <TenantPage name/> }.into_any(),
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
                        "Live, sanitized status from the management Kubernetes API. Data changes only when this page is loaded or refreshed."
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
fn TenantPage(name: String) -> impl IntoView {
    let refresh = RwSignal::new(0_u32);
    let state = RwSignal::new(LoadState::<TenantSnapshot>::Loading);
    let requested_name = name.clone();

    Effect::new(move |_| {
        refresh.get();
        state.set(LoadState::Loading);
        let name = requested_name.clone();
        spawn_local(async move {
            state.set(match fetch_tenant_page(&name).await {
                Ok(data) => LoadState::Ready(data),
                Err(error) => LoadState::Error(error),
            });
        });
    });

    view! {
        <main class="page">
            <a class="back-link" href="/">"← All Tenants"</a>
            <div class="page-header">
                <div>
                    <p class="eyebrow">"Tenant detail"</p>
                    <h1>{name}</h1>
                    <p class="lede">
                        "Specification, reconciliation status, management resources, and provider-neutral topology."
                    </p>
                </div>
                <RefreshButton state refresh/>
            </div>
            <div aria-live="polite">
                {move || match state.get() {
                    LoadState::Loading => loading_state("Loading Tenant detail"),
                    LoadState::Ready(data) => tenant_detail_view(data),
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
    let not_found = error.kind == UiErrorKind::NotFound;
    view! {
        <section class="state-panel state-panel--error" role="alert">
            <h2>{error.title()}</h2>
            <p>{error.message}</p>
            {if not_found {
                view! { <p><a href="/">"Return to all Tenants"</a></p> }.into_any()
            } else if show_retry {
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
                    <p class="secondary">
                        "Refresh after the deployed UI and server versions have been aligned."
                    </p>
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
    let provider = provider_mode_label(data.overview.provider_mode);
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
    let capacity = match tenant.requested_databases {
        Some(databases) => format!(
            "{} worker{}, {} database{}",
            tenant.requested_workers,
            plural(tenant.requested_workers),
            databases,
            plural(databases)
        ),
        None => format!(
            "{} worker{}",
            tenant.requested_workers,
            plural(tenant.requested_workers)
        ),
    };
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

fn tenant_detail_view(data: TenantSnapshot) -> AnyView {
    let detail = data.detail;
    let summary = detail.summary.clone();
    let classification = summary.classification;
    let status_class = classification_class(classification);
    let provider_specification = detail.specification.provider.clone();
    let provider_status = detail.provider_status.clone();
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
        <section class="metrics" aria-label="Tenant status summary">
            <div class="metric">
                <span class="metric__label">"Status"</span>
                <span class=format!("status status--{status_class}")>
                    {classification_label(classification)}
                </span>
            </div>
            <div class="metric">
                <span class="metric__label">"Provider"</span>
                <strong class="metric__value">{provider_label(summary.provider)}</strong>
            </div>
            <div class="metric">
                <span class="metric__label">"Workers"</span>
                <strong class="metric__value">{summary.requested_workers}</strong>
            </div>
            <div class="metric">
                <span class="metric__label">"Databases"</span>
                <strong class="metric__value">
                    {summary.requested_databases.map_or_else(|| "—".to_owned(), |value| value.to_string())}
                </strong>
            </div>
            <div class="metric">
                <span class="metric__label">"Kubernetes"</span>
                <strong>{summary.kubernetes_version.clone()}</strong>
            </div>
            <div class="metric">
                <span class="metric__label">"Age"</span>
                <strong>{format_age(summary.created_at.as_deref())}</strong>
            </div>
        </section>

        <div class="detail-grid">
            <section class="panel" aria-labelledby="specification-heading">
                <h2 id="specification-heading">"Immutable specification"</h2>
                <dl class="definition-list">
                    <dt>"Tenant UID"</dt><dd>{detail.uid}</dd>
                    <dt>"Generation"</dt><dd>{generation_status}</dd>
                    <dt>"Kubernetes version"</dt><dd>{detail.specification.kubernetes_version}</dd>
                    <dt>"Workers"</dt><dd>{detail.specification.workers}</dd>
                    {provider_specification_rows(provider_specification)}
                </dl>
            </section>
            <section class="panel" aria-labelledby="endpoint-heading">
                <h2 id="endpoint-heading">"Access endpoint"</h2>
                <dl class="definition-list">
                    <dt>"Endpoint"</dt>
                    <dd>{optional_text(summary.endpoint.as_deref()).to_owned()}</dd>
                    <dt>"Provider"</dt><dd>{provider_label(summary.provider)}</dd>
                </dl>
                <p class="secondary">
                    "Credentials and kubeconfig material are never returned to this browser."
                </p>
            </section>
        </div>

        <div class="detail-grid">
            {conditions_panel(summary.conditions)}
            {blockers_panel(detail.blockers)}
        </div>

        {provider_panel(provider_status)}
        {management_resources_panel(detail.management_resources)}
        {topology_panel(data.topology)}
    }
    .into_any()
}

fn provider_specification_rows(specification: ProviderSpecificationView) -> AnyView {
    match specification {
        ProviderSpecificationView::Local { databases } => view! {
            <dt>"Provider configuration"</dt>
            <dd>{format!("{databases} local database{}", plural(databases))}</dd>
        }
        .into_any(),
        ProviderSpecificationView::Azure {
            pod_cidr,
            service_cidr,
        } => view! {
            <dt>"Pod CIDR"</dt><dd>{pod_cidr}</dd>
            <dt>"Service CIDR"</dt><dd>{service_cidr}</dd>
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
                ProviderStatusView::Azure(azure) => azure_provider_view(azure),
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
                                    let midpoint_x = (edge.x1 + edge.x2) / 2.0;
                                    let midpoint_y = (edge.y1 + edge.y2) / 2.0 - 7.0;
                                    view! {
                                        <g>
                                            <line
                                                class="edge"
                                                x1=edge.x1
                                                y1=edge.y1
                                                x2=edge.x2
                                                y2=edge.y2
                                                marker-end="url(#topology-arrow)"
                                            />
                                            <text class="edge-label" x=midpoint_x y=midpoint_y>{label}</text>
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
                <p>"Tenant Admin supports the overview and individual Tenant detail pages."</p>
                <p><a href="/">"Return to all Tenants"</a></p>
            </section>
        </main>
    }
}

async fn fetch_dashboard() -> Result<OverviewSnapshot, UiError> {
    get_envelope(API_OVERVIEW_PATH).await
}

async fn fetch_tenant_page(name: &str) -> Result<TenantSnapshot, UiError> {
    let Some(href) = tenant_href(name) else {
        return Err(UiError {
            kind: UiErrorKind::InvalidRequest,
            message: "The Tenant name is not valid.".to_owned(),
            retryable: false,
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
