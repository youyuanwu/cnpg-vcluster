use leptos::prelude::*;
use tenant_admin_shared::query::{TopologyGraph, TopologyHealth};

use crate::{
    explorer::{
        ExplorerFilter, ExplorerModel, ResourceGroup, ResourceRelationship, display_identity,
        representation_label,
    },
    format::{edge_kind_label, health_class, health_label, node_kind_label},
    mutation_ui_state::{SelectionRefresh, selection_after_refresh},
    topology::layout_graph,
};

#[component]
pub fn ResourceExplorer(
    graph: TopologyGraph,
    selection: RwSignal<Option<String>>,
    invalid_selection: bool,
) -> impl IntoView {
    let model = ExplorerModel::new(graph);
    let selection_ids = model
        .filtered_nodes(&ExplorerFilter::default())
        .into_iter()
        .map(|node| node.id)
        .collect::<Vec<_>>();
    let stale_selection =
        selection_after_refresh(selection.get_untracked().as_deref(), selection_ids.iter())
            == SelectionRefresh::ClearedMissing;
    if stale_selection {
        selection.set(None);
    }
    let selected = selection;
    let selected_relationship = RwSignal::new(None::<String>);
    let query = RwSignal::new(String::new());
    let group = RwSignal::new(None::<ResourceGroup>);
    let health = RwSignal::new(String::new());
    let kind = RwSignal::new(String::new());
    let namespace = RwSignal::new(String::new());
    let namespace_not_applicable = RwSignal::new(false);

    let list_model = model.clone();
    let graph_model = model.clone();
    let inspector_model = model.clone();

    view! {
        <section class="resource-explorer" aria-labelledby="resource-explorer-heading">
            <div class="panel__header">
                <div>
                    <h2 id="resource-explorer-heading">"Tenant resources"</h2>
                    <p>"Current tenant-scoped inventory and relationships. Select an item to focus its direct neighborhood."</p>
                </div>
            </div>
            {(invalid_selection || stale_selection).then(|| view! {
                <p class="lifecycle-notice" role="status">
                    "The requested resource selection is invalid or no longer exists in this snapshot. The explorer opened at its summary."
                </p>
            })}
            <div class="explorer-controls">
                <label>
                    "Search resources"
                    <input type="search" placeholder="Name, kind, namespace, UID, or attribute"
                        on:input=move |event| query.set(event_target_value(&event))/>
                </label>
                <label>
                    "Health"
                    <select on:change=move |event| health.set(event_target_value(&event))>
                        <option value="">"All health states"</option>
                        <option value="ready">"Ready"</option>
                        <option value="progressing">"Progressing"</option>
                        <option value="degraded">"Degraded"</option>
                        <option value="failed">"Failed"</option>
                        <option value="deleting">"Deleting"</option>
                        <option value="unknown">"Unknown"</option>
                    </select>
                </label>
                <label>
                    "Kind"
                    <select on:change=move |event| kind.set(event_target_value(&event))>
                        <option value="">"All kinds"</option>
                        <option value="tenant">"Tenant"</option>
                        <option value="control-plane">"Control plane"</option>
                        <option value="worker-pool">"Worker pool"</option>
                        <option value="machine">"Machine"</option>
                        <option value="node">"Node"</option>
                        <option value="provider-resource">"Provider resource"</option>
                        <option value="add-on">"Add-on"</option>
                        <option value="database">"Database"</option>
                    </select>
                </label>
                <label>
                    "Exact namespace"
                    <input type="text" placeholder="tenant-a"
                        disabled=move || namespace_not_applicable.get()
                        on:input=move |event| namespace.set(event_target_value(&event))/>
                </label>
                <label class="explorer-checkbox">
                    <input type="checkbox"
                        on:change=move |event| namespace_not_applicable.set(event_target_checked(&event))/>
                    "Namespace not applicable"
                </label>
            </div>
            <div class="resource-group-tabs" role="group" aria-label="Resource groups">
                <button type="button" class:button--secondary=true
                    aria-pressed=move || group.get().is_none()
                    on:click=move |_| group.set(None)>"All"</button>
                {model.group_summaries().into_iter().map(|summary| {
                    let value = summary.group;
                    view! {
                        <button type="button" class:button--secondary=true
                            aria-pressed=move || group.get() == Some(value)
                            on:click=move |_| {
                                group.set(Some(value));
                                selected_relationship.set(None);
                            }>
                            <span>{format!("{} · {}", value.label(), summary.count)}</span>
                            <small>{format!(
                                "{} exact · {} logical · {} external · {} recorded · {} summaries",
                                summary.provenance.exact,
                                summary.provenance.database_logical,
                                summary.provenance.external,
                                summary.provenance.recorded,
                                summary.provenance.synthetic,
                            )}</small>
                        </button>
                    }
                }).collect_view()}
            </div>
            <div class="explorer-layout">
                <aside class="explorer-inventory" aria-label="Resource inventory">
                    <h3>"Inventory"</h3>
                    {move || {
                        let not_applicable = namespace_not_applicable.get();
                        let filter = ExplorerFilter {
                            query: query.get(),
                            group: group.get(),
                            health: parse_health(&health.get()),
                            kind: parse_kind(&kind.get()),
                            namespace: (!not_applicable)
                                .then(|| namespace.get())
                                .filter(|value| !value.is_empty()),
                            namespace_not_applicable: not_applicable,
                        };
                        let nodes = list_model.filtered_nodes(&filter);
                        let selection_hidden = selected.get().as_deref().is_some_and(|selected| {
                            list_model.node(selected).is_some()
                                && !nodes.iter().any(|node| node.id == selected)
                        });
                        let content = if nodes.is_empty() {
                            view! { <p class="empty">"No resources match these filters."</p> }.into_any()
                        } else {
                            view! {
                                <ul class="explorer-resource-list">
                                    {nodes.into_iter().map(|node| {
                                        let id = node.id.clone();
                                        let button_id = id.clone();
                                        let selected_id = id.clone();
                                        let class = health_class(node.health);
                                        view! {
                                            <li>
                                                <button type="button"
                                                    class="resource-select"
                                                    class:resource-select--selected=move || selected.get().as_deref() == Some(selected_id.as_str())
                                                    on:click=move |_| {
                                                        selected.set(Some(button_id.clone()));
                                                        selected_relationship.set(None);
                                                    }>
                                                    <span>
                                                        <strong>{node.label}</strong>
                                                        <small>{format!("{} · {}", node_kind_label(node.kind), representation_label(node.provenance))}</small>
                                                    </span>
                                                    <span class=format!("status status--{class}")>{health_label(node.health)}</span>
                                                </button>
                                            </li>
                                        }
                                    }).collect_view()}
                                </ul>
                            }.into_any()
                        };
                        view! {
                            {selection_hidden.then(|| view! {
                                <p class="lifecycle-notice" role="status">
                                    "The active selection is hidden by the current filters. It remains visible in the graph and inspector."
                                </p>
                            })}
                            {content}
                        }.into_any()
                    }}
                </aside>
                <div class="explorer-graph">
                    <h3>"Relationships"</h3>
                    {move || graph_view(
                        &graph_model,
                        selected.get().as_deref(),
                        group.get(),
                        selected,
                        selected_relationship,
                    )}
                </div>
                <aside class="explorer-inspector" aria-label="Selected resource details">
                    <h3>"Inspector"</h3>
                    {move || inspector_view(
                        &inspector_model,
                        selected.get().as_deref(),
                        selected_relationship.get().as_deref(),
                        selected_relationship,
                    )}
                </aside>
            </div>
        </section>
    }
}

fn graph_view(
    model: &ExplorerModel,
    selected_id: Option<&str>,
    expanded_group: Option<ResourceGroup>,
    selected: RwSignal<Option<String>>,
    selected_relationship: RwSignal<Option<String>>,
) -> AnyView {
    let selected_edge_id = selected_relationship.get();
    let selected_edge = selected_edge_id
        .as_deref()
        .and_then(|id| model.relationship_by_id(id));
    let graph = selected_edge_id.as_deref().map_or_else(
        || {
            selected_id.map_or_else(
                || {
                    expanded_group.map_or_else(
                        || model.focused_graph(None),
                        |group| model.group_graph(group),
                    )
                },
                |selected_id| {
                    expanded_group.map_or_else(
                        || model.focused_graph(Some(selected_id)),
                        |group| model.selected_group_graph(selected_id, group),
                    )
                },
            )
        },
        |edge_id| model.relationship_graph(selected_id, edge_id),
    );
    let endpoint_ids = selected_edge
        .as_ref()
        .map(|edge| (edge.source_id.clone(), edge.target_id.clone()));
    let layout = layout_graph(&graph);
    let view_box = format!("0 0 {} {}", layout.width, layout.height);
    view! {
        <div class="topology-scroll explorer-graph__canvas">
            <svg class="topology" viewBox=view_box role="img"
                aria-label="Focused Tenant resource relationships" preserveAspectRatio="xMinYMin meet">
                <defs>
                    <marker id="explorer-arrow" markerWidth="8" markerHeight="8"
                        refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
                        <path d="M 0 0 L 8 4 L 0 8 z" fill="#8296a5"/>
                    </marker>
                </defs>
                {layout.edges.into_iter().map(|edge| {
                    let edge_id = edge.id.clone();
                    let selected_edge = edge.id.clone();
                    let label = edge.label.unwrap_or_else(|| edge_kind_label(edge.kind).into());
                    let accessible_label = label.clone();
                    view! {
                        <g class:edge--selected=move || selected_relationship.get().as_deref() == Some(selected_edge.as_str())
                            aria-label=format!("Relationship {accessible_label}")
                            on:click=move |_| selected_relationship.set(Some(edge_id.clone()))>
                            <path class="edge" d=edge.path marker-end="url(#explorer-arrow)"/>
                            <text class="edge-label" x=edge.label_x y=edge.label_y>{label}</text>
                        </g>
                    }
                }).collect_view()}
                {layout.nodes.into_iter().map(|node| {
                    let id = node.id.clone();
                    let selected_node = node.id.clone();
                    let class = health_class(node.health);
                    let kind = node_kind_label(node.kind);
                    let health = health_label(node.health);
                    let accessible_label = node.label.clone();
                    let endpoint_node = endpoint_ids.as_ref().is_some_and(|(source, target)| {
                        source == &node.id || target == &node.id
                    });
                    view! {
                        <g class=format!("node node--{class}")
                            class:node--selected=move || selected.get().as_deref() == Some(selected_node.as_str()) || endpoint_node
                            aria-label=format!("{accessible_label}; {kind}; status {health}")
                            transform=format!("translate({}, {})", node.x, node.y)
                            on:click=move |_| {
                                selected.set(Some(id.clone()));
                                selected_relationship.set(None);
                            }>
                            <rect width=node.width height=node.height rx="9" ry="9"/>
                            <text class="node-label" x="14" y="29">{node.label}</text>
                            <text class="node-meta" x="14" y="53">{kind}</text>
                            <text class="node-meta" x="14" y="72">{health}</text>
                        </g>
                    }
                }).collect_view()}
            </svg>
        </div>
    }.into_any()
}

fn inspector_view(
    model: &ExplorerModel,
    selected_id: Option<&str>,
    selected_relationship: Option<&str>,
    relationship_signal: RwSignal<Option<String>>,
) -> AnyView {
    if let Some(edge_id) = selected_relationship {
        if let Some(edge) = model.relationship_by_id(edge_id) {
            return relationship_view(edge, relationship_signal);
        }
    }
    let Some(inspection) = selected_id.and_then(|id| model.inspection(id)) else {
        return view! {
            <div class="empty">
                <p>"Select a resource to inspect identity, attributes, health, and relationships."</p>
            </div>
        }.into_any();
    };
    let node = inspection.node;
    let class = health_class(node.health);
    view! {
        <div class="resource-inspection">
            <div class="resource-heading">
                <strong>{node.label.clone()}</strong>
                <span class=format!("status status--{class}")>{health_label(node.health)}</span>
            </div>
            <p class="secondary">{format!("{} · {}", inspection.group.label(), representation_label(node.provenance))}</p>
            <dl class="definition-list">
                {display_identity(&node).into_iter().map(|(label, value)| view! {
                    <dt>{label}</dt><dd>{value}</dd>
                }).collect_view()}
                {node.attributes.into_iter().map(|attribute| view! {
                    <dt>{attribute.label}</dt><dd>{attribute.value}</dd>
                }).collect_view()}
            </dl>
            <RelationshipList title="Incoming" relationships=inspection.incoming signal=relationship_signal/>
            <RelationshipList title="Outgoing" relationships=inspection.outgoing signal=relationship_signal/>
        </div>
    }.into_any()
}

#[component]
fn RelationshipList(
    title: &'static str,
    relationships: Vec<ResourceRelationship>,
    signal: RwSignal<Option<String>>,
) -> impl IntoView {
    view! {
        <section class="relationship-list">
            <h4>{title}</h4>
            {if relationships.is_empty() {
                view! { <p class="secondary">"None"</p> }.into_any()
            } else {
                view! {
                    <ul>
                        {relationships.into_iter().map(|relationship| {
                            let id = relationship.id.clone();
                            view! {
                                <li>
                                    <button type="button" class="button--link"
                                        on:click=move |_| signal.set(Some(id.clone()))>
                                        {format!("{} → {} · {}",
                                            relationship.source_label,
                                            relationship.target_label,
                                            relationship.label.as_deref().unwrap_or_else(|| edge_kind_label(relationship.kind)))}
                                    </button>
                                </li>
                            }
                        }).collect_view()}
                    </ul>
                }.into_any()
            }}
        </section>
    }
}

fn relationship_view(
    relationship: ResourceRelationship,
    signal: RwSignal<Option<String>>,
) -> AnyView {
    view! {
        <div class="resource-inspection">
            <button type="button" class="button--link" on:click=move |_| signal.set(None)>
                "← Resource details"
            </button>
            <p class="eyebrow">"Selected relationship"</p>
            <h4>{relationship.label.clone().unwrap_or_else(|| edge_kind_label(relationship.kind).into())}</h4>
            <dl class="definition-list">
                <dt>"Source"</dt><dd>{relationship.source_label}</dd>
                <dt>"Target"</dt><dd>{relationship.target_label}</dd>
                <dt>"Type"</dt><dd>{edge_kind_label(relationship.kind)}</dd>
            </dl>
        </div>
    }.into_any()
}

fn parse_health(value: &str) -> Option<TopologyHealth> {
    match value {
        "ready" => Some(TopologyHealth::Ready),
        "progressing" => Some(TopologyHealth::Progressing),
        "degraded" => Some(TopologyHealth::Degraded),
        "failed" => Some(TopologyHealth::Failed),
        "deleting" => Some(TopologyHealth::Deleting),
        "unknown" => Some(TopologyHealth::Unknown),
        _ => None,
    }
}

fn parse_kind(value: &str) -> Option<tenant_admin_shared::query::TopologyNodeKind> {
    use tenant_admin_shared::query::TopologyNodeKind;
    match value {
        "tenant" => Some(TopologyNodeKind::Tenant),
        "control-plane" => Some(TopologyNodeKind::ControlPlane),
        "worker-pool" => Some(TopologyNodeKind::WorkerPool),
        "machine" => Some(TopologyNodeKind::Machine),
        "node" => Some(TopologyNodeKind::Node),
        "provider-resource" => Some(TopologyNodeKind::ProviderResource),
        "add-on" => Some(TopologyNodeKind::AddOn),
        "database" => Some(TopologyNodeKind::Database),
        _ => None,
    }
}
