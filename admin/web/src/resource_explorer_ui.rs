use leptos::prelude::*;
use tenant_admin_shared::query::{TopologyGraph, TopologyHealth};

use crate::{
    explorer::{
        ExplorerFilter, ExplorerModel, ResourceGroup, ResourceRelationship, display_identity,
        representation_class, representation_label,
    },
    format::{
        database_instance_role_class, database_instance_role_label, edge_kind_label, health_class,
        health_label, node_kind_label, topology_ownership_class, topology_ownership_label,
        topology_semantic_class, topology_semantic_label,
    },
    mutation_ui_state::{SelectionRefresh, selection_after_refresh},
    topology::{LayoutNode, layout_graph},
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
    let selection_model = model.clone();

    Effect::new(move |_| {
        let filter = current_filter(
            query.get(),
            group.get(),
            health.get(),
            kind.get(),
            namespace.get(),
            namespace_not_applicable.get(),
        );
        let eligible = selection_model
            .filtered_nodes(&filter)
            .into_iter()
            .map(|node| node.id)
            .collect::<std::collections::BTreeSet<_>>();
        if selected
            .get()
            .as_ref()
            .is_some_and(|id| !eligible.contains(id))
        {
            selected.set(None);
            selected_relationship.set(None);
        }
        if selected_relationship
            .get()
            .as_ref()
            .is_some_and(|id| !selection_model.relationship_matches_filter(id, &filter))
        {
            selected_relationship.set(None);
        }
    });

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
                        let filter = current_filter(
                            query.get(),
                            group.get(),
                            health.get(),
                            kind.get(),
                            namespace.get(),
                            namespace_not_applicable.get(),
                        );
                        let nodes = list_model.filtered_nodes(&filter);
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
                                        let semantic_details = inventory_semantic_details(&node);
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
                                                        <small>{format!(
                                                            "{} · {}",
                                                            topology_semantic_label(node.semantic_kind),
                                                            topology_ownership_label(node.ownership),
                                                        )}</small>
                                                        <small>{format!(
                                                            "{} · {}",
                                                            node_kind_label(node.kind),
                                                            representation_label(node.provenance),
                                                        )}</small>
                                                        {semantic_details.map(|details| view! {
                                                            <small>{details}</small>
                                                        })}
                                                    </span>
                                                    <span class=format!("status status--{class}")>{health_label(node.health)}</span>
                                                </button>
                                            </li>
                                        }
                                    }).collect_view()}
                                </ul>
                            }.into_any()
                        };
                        content
                    }}
                </aside>
                <div class="explorer-graph">
                    <h3>"Relationships"</h3>
                    {move || graph_view(
                        &graph_model,
                        &current_filter(
                            query.get(),
                            group.get(),
                            health.get(),
                            kind.get(),
                            namespace.get(),
                            namespace_not_applicable.get(),
                        ),
                        selected.get().as_deref(),
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
    filter: &ExplorerFilter,
    selected_id: Option<&str>,
    selected: RwSignal<Option<String>>,
    selected_relationship: RwSignal<Option<String>>,
) -> AnyView {
    let selected_edge_id = selected_relationship.get();
    let selected_edge = selected_edge_id
        .as_deref()
        .and_then(|id| model.relationship_by_id(id));
    let graph = model.graph_for_filter(filter, selected_id, selected_edge_id.as_deref());
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
                {layout.bands.into_iter().map(|band| view! {
                    <g class=format!("topology-band topology-band--{}", band.id)>
                        <rect x=band.x y=band.y width=band.width height=band.height rx="12" ry="12"/>
                        <text class="topology-band__label" x=band.x + 14.0 y=band.y + 22.0>{band.label}</text>
                    </g>
                }).collect_view()}
                {layout.edges.into_iter().map(|edge| {
                    let edge_id = edge.id.clone();
                    let selected_edge = edge.id.clone();
                    let accessible_label = edge.label.clone().unwrap_or_else(|| edge_kind_label(edge.kind).into());
                    view! {
                        <g class:edge--selected=move || selected_relationship.get().as_deref() == Some(selected_edge.as_str())
                            aria-label=format!("Relationship {accessible_label}")
                            on:click=move |_| selected_relationship.set(Some(edge_id.clone()))>
                            <path class="edge" d=edge.path marker-end="url(#explorer-arrow)"/>
                            {edge.label.map(|label| view! {
                                <text class="edge-label" x=edge.label_x y=edge.label_y>{label}</text>
                            })}
                        </g>
                    }
                }).collect_view()}
                {layout.nodes.into_iter().map(|node| {
                    let id = node.id.clone();
                    let selected_node = node.id.clone();
                    let class = health_class(node.health);
                    let semantic = topology_semantic_label(node.semantic_kind);
                    let health = health_label(node.health);
                    let ownership = topology_ownership_label(node.ownership);
                    let role = node.database_role.map(database_instance_role_label);
                    let role_class = node.database_role.map(database_instance_role_class);
                    let placement = compact_placement(&node);
                    let accessible_label = node_accessible_label(&node);
                    let endpoint_node = endpoint_ids.as_ref().is_some_and(|(source, target)| {
                        source == &node.id || target == &node.id
                    });
                    view! {
                        <g class=format!(
                                "node node--{class} node-role--{} ownership--{} provenance--{}{}",
                                topology_semantic_class(node.semantic_kind),
                                topology_ownership_class(node.ownership),
                                representation_class(node.provenance),
                                role_class.map_or_else(String::new, |role| format!(" database-role--{role}")),
                            )
                            class:node--selected=move || selected.get().as_deref() == Some(selected_node.as_str()) || endpoint_node
                            aria-label=accessible_label
                            transform=format!("translate({}, {})", node.x, node.y)
                            on:click=move |_| {
                                selected.set(Some(id.clone()));
                                selected_relationship.set(None);
                            }>
                            <rect width=node.width height=node.height rx="9" ry="9"/>
                            <text class="node-label" x="14" y="25">{node.label}</text>
                            <text class="node-meta node-meta--role" x="14" y="47">
                                {role.map_or_else(|| semantic.into(), |role| format!("{semantic} · {role}"))}
                            </text>
                            <text class="node-meta" x="14" y="68">{format!("{health} · {ownership}")}</text>
                            {placement.first().map(|value| view! {
                                <text class="node-meta" x="14" y="91">{value.clone()}</text>
                            })}
                            {placement.get(1).map(|value| view! {
                                <text class="node-meta" x="14" y="111">{value.clone()}</text>
                            })}
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

fn current_filter(
    query: String,
    group: Option<ResourceGroup>,
    health: String,
    kind: String,
    namespace: String,
    namespace_not_applicable: bool,
) -> ExplorerFilter {
    ExplorerFilter {
        query,
        group,
        health: parse_health(&health),
        kind: parse_kind(&kind),
        namespace: (!namespace_not_applicable)
            .then_some(namespace)
            .filter(|value| !value.is_empty()),
        namespace_not_applicable,
    }
}

fn compact_placement(node: &LayoutNode) -> Vec<String> {
    let Some(placement) = &node.placement else {
        return Vec::new();
    };
    let mut values = Vec::new();
    match node.semantic_kind {
        tenant_admin_shared::query::TopologySemanticKind::DatabaseInstance => {
            if let Some(worker_node) = &placement.worker_node {
                values.push(format!("Node · {worker_node}"));
            }
            if let Some(zone) = &placement.zone {
                values.push(format!("Zone · {zone}"));
            }
        }
        tenant_admin_shared::query::TopologySemanticKind::WorkerNode => {
            if let Some(worker_pool) = &placement.worker_pool {
                values.push(format!("Pool · {worker_pool}"));
            }
            if let Some(zone) = &placement.zone {
                values.push(format!("Zone · {zone}"));
            }
        }
        _ => {
            if let Some(worker_pool) = &placement.worker_pool {
                values.push(format!("Pool · {worker_pool}"));
            }
            if let Some(worker_node) = &placement.worker_node {
                values.push(format!("Node · {worker_node}"));
            }
            if values.len() < 2
                && let Some(zone) = &placement.zone
            {
                values.push(format!("Zone · {zone}"));
            }
        }
    }
    values.truncate(2);
    values
}

fn inventory_semantic_details(node: &tenant_admin_shared::query::TopologyNode) -> Option<String> {
    let mut values = Vec::new();
    if let Some(role) = node.database_role {
        values.push(database_instance_role_label(role).to_owned());
    }
    if let Some(placement) = &node.placement {
        if let Some(worker_pool) = &placement.worker_pool {
            values.push(format!("Pool {worker_pool}"));
        }
        if let Some(worker_node) = &placement.worker_node {
            values.push(format!("Node {worker_node}"));
        }
        if let Some(zone) = &placement.zone {
            values.push(format!("Zone {zone}"));
        }
    }
    (!values.is_empty()).then(|| values.join(" · "))
}

fn node_accessible_label(node: &LayoutNode) -> String {
    let mut values = vec![
        node.label.clone(),
        topology_semantic_label(node.semantic_kind).into(),
        format!("status {}", health_label(node.health)),
        topology_ownership_label(node.ownership).into(),
        representation_label(node.provenance).into(),
    ];
    if let Some(role) = node.database_role {
        values.push(format!(
            "database role {}",
            database_instance_role_label(role)
        ));
    }
    match node.semantic_kind {
        tenant_admin_shared::query::TopologySemanticKind::DatabaseInstance => {
            values.push(format!(
                "worker node {}",
                node.placement
                    .as_ref()
                    .and_then(|placement| placement.worker_node.as_deref())
                    .unwrap_or("not reported")
            ));
            values.push(format!(
                "zone {}",
                node.placement
                    .as_ref()
                    .and_then(|placement| placement.zone.as_deref())
                    .unwrap_or("not reported")
            ));
        }
        tenant_admin_shared::query::TopologySemanticKind::WorkerNode => {
            values.push(format!(
                "worker pool {}",
                node.placement
                    .as_ref()
                    .and_then(|placement| placement.worker_pool.as_deref())
                    .unwrap_or("not reported")
            ));
            values.push(format!(
                "zone {}",
                node.placement
                    .as_ref()
                    .and_then(|placement| placement.zone.as_deref())
                    .unwrap_or("not reported")
            ));
        }
        _ => {}
    }
    values.join("; ")
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

#[cfg(test)]
mod tests {
    use tenant_admin_shared::query::{
        DatabaseInstanceRole, TopologyHealth, TopologyNodeKind, TopologyNodeProvenance,
        TopologyOwnership, TopologyPlacement, TopologySemanticKind,
    };

    use super::{LayoutNode, compact_placement, node_accessible_label};

    fn instance(placement: Option<TopologyPlacement>) -> LayoutNode {
        LayoutNode {
            id: "database:instance:one".into(),
            kind: TopologyNodeKind::Database,
            semantic_kind: TopologySemanticKind::DatabaseInstance,
            ownership: TopologyOwnership::TenantOwned,
            provenance: TopologyNodeProvenance::DatabaseLogicalRepresentation,
            database_role: Some(DatabaseInstanceRole::Primary),
            placement,
            health: TopologyHealth::Ready,
            label: "orders-1".into(),
            x: 0.0,
            y: 0.0,
            width: 220.0,
            height: 124.0,
        }
    }

    #[test]
    fn database_placement_is_compact_and_accessible_when_partial_or_missing() {
        let placed = instance(Some(TopologyPlacement {
            worker_pool: None,
            worker_node: Some("worker-a".into()),
            zone: Some("zone-a".into()),
        }));
        assert_eq!(
            compact_placement(&placed),
            ["Node · worker-a", "Zone · zone-a"]
        );
        let partial = instance(Some(TopologyPlacement {
            worker_pool: None,
            worker_node: Some("worker-a".into()),
            zone: None,
        }));
        assert_eq!(compact_placement(&partial), ["Node · worker-a"]);
        assert!(node_accessible_label(&partial).contains("zone not reported"));
        let missing = instance(None);
        let accessible = node_accessible_label(&missing);
        assert!(accessible.contains("worker node not reported"));
        assert!(accessible.contains("zone not reported"));
    }
}
