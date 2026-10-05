use std::collections::BTreeMap;

use tenant_admin_shared::query::{
    DatabaseInstanceRole, TopologyEdgeKind, TopologyGraph, TopologyHealth, TopologyNode,
    TopologyNodeKind, TopologyNodeProvenance, TopologyOwnership, TopologyPlacement,
    TopologySemanticKind,
};

use crate::format::{
    database_instance_role_label, health_label, topology_ownership_label, topology_semantic_label,
};

const LEFT_MARGIN: f32 = 44.0;
const TOP_MARGIN: f32 = 24.0;
const BAND_HEADER: f32 = 34.0;
const BAND_PADDING: f32 = 18.0;
const BAND_GAP: f32 = 24.0;
const COLUMN_GAP: f32 = 310.0;
const ROW_GAP: f32 = 146.0;
const NODE_HEIGHT: f32 = 124.0;
const MIN_NODE_WIDTH: f32 = 208.0;
const MAX_NODE_WIDTH: f32 = 286.0;
const MAX_LABEL_CHARACTERS: usize = 30;

#[derive(Clone, Debug, PartialEq)]
pub struct TopologyLayout {
    pub width: f32,
    pub height: f32,
    pub bands: Vec<LayoutBand>,
    pub nodes: Vec<LayoutNode>,
    pub edges: Vec<LayoutEdge>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LayoutBand {
    pub id: String,
    pub label: String,
    pub x: f32,
    pub y: f32,
    pub width: f32,
    pub height: f32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LayoutNode {
    pub id: String,
    pub kind: TopologyNodeKind,
    pub semantic_kind: TopologySemanticKind,
    pub ownership: TopologyOwnership,
    pub provenance: TopologyNodeProvenance,
    pub database_role: Option<DatabaseInstanceRole>,
    pub placement: Option<TopologyPlacement>,
    pub health: TopologyHealth,
    pub label: String,
    pub x: f32,
    pub y: f32,
    pub width: f32,
    pub height: f32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LayoutEdge {
    pub id: String,
    pub kind: TopologyEdgeKind,
    pub label: Option<String>,
    pub path: String,
    pub label_x: f32,
    pub label_y: f32,
}

pub fn layout_graph(graph: &TopologyGraph) -> TopologyLayout {
    let mut source_nodes = graph.nodes.iter().collect::<Vec<_>>();
    source_nodes.sort_by_key(|node| {
        (
            band_rank(node.semantic_kind),
            column_rank(node.semantic_kind),
            database_role_rank(node.database_role),
            safe_label(&node.label).to_ascii_lowercase(),
            node.id.clone(),
        )
    });
    let database_rows = database_row_assignments(graph, &source_nodes);

    let mut counts = BTreeMap::<(u8, u8), usize>::new();
    for node in &source_nodes {
        *counts
            .entry((
                band_rank(node.semantic_kind),
                column_rank(node.semantic_kind),
            ))
            .or_default() += 1;
    }
    let mut band_tops = BTreeMap::<u8, f32>::new();
    let mut bands = Vec::new();
    let mut next_y = TOP_MARGIN;
    let present_bands = source_nodes
        .iter()
        .map(|node| band_rank(node.semantic_kind))
        .collect::<std::collections::BTreeSet<_>>();
    for band in present_bands {
        let rows = if band == band_rank(TopologySemanticKind::DatabaseCluster) {
            database_rows
                .values()
                .copied()
                .max()
                .map_or(1, |row| row + 1)
        } else {
            counts
                .iter()
                .filter(|((candidate, _), _)| *candidate == band)
                .map(|(_, count)| *count)
                .max()
                .unwrap_or(1)
        };
        let height = BAND_HEADER + BAND_PADDING * 2.0 + rows as f32 * ROW_GAP;
        band_tops.insert(band, next_y);
        bands.push(LayoutBand {
            id: band_id(band).into(),
            label: band_label(band).into(),
            x: 16.0,
            y: next_y,
            width: 0.0,
            height,
        });
        next_y += height + BAND_GAP;
    }

    let mut column_rows = BTreeMap::<(u8, u8), usize>::new();
    let mut nodes = Vec::with_capacity(source_nodes.len());
    for node in source_nodes {
        let band = band_rank(node.semantic_kind);
        let column = column_rank(node.semantic_kind);
        let row = database_rows.get(&node.id).copied().unwrap_or_else(|| {
            let row = column_rows.entry((band, column)).or_default();
            let current = *row;
            *row += 1;
            current
        });
        let label = safe_label(&node.label);
        let width = node_width(&label);
        nodes.push(LayoutNode {
            id: node.id.clone(),
            kind: node.kind,
            semantic_kind: node.semantic_kind,
            ownership: node.ownership,
            provenance: node.provenance,
            database_role: node.database_role,
            placement: node.placement.clone(),
            health: node.health,
            label,
            x: LEFT_MARGIN + f32::from(column) * COLUMN_GAP,
            y: band_tops[&band] + BAND_HEADER + BAND_PADDING + row as f32 * ROW_GAP,
            width,
            height: NODE_HEIGHT,
        });
    }

    let positions = nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<BTreeMap<_, _>>();
    let mut source_edges = graph.edges.iter().collect::<Vec<_>>();
    source_edges.sort_by_key(|edge| {
        (
            edge.source.as_str(),
            edge.target.as_str(),
            edge_kind_rank(edge.kind),
            edge.id.as_str(),
        )
    });
    let mut parallel_lanes = BTreeMap::<(&str, &str), usize>::new();
    let mut max_edge_x = 0.0_f32;
    let mut max_edge_y = 0.0_f32;
    let edges = source_edges
        .into_iter()
        .filter_map(|edge| {
            let source = positions.get(edge.source.as_str())?;
            let target = positions.get(edge.target.as_str())?;
            let y1 = source.y + source.height / 2.0;
            let y2 = target.y + target.height / 2.0;
            let lane = parallel_lanes
                .entry((edge.source.as_str(), edge.target.as_str()))
                .or_default();
            let lane_offset = *lane as f32 * 9.0;
            *lane += 1;
            let (path, label_x, label_y, route_x, route_y) = if source.x == target.x {
                let x1 = source.x + source.width;
                let x2 = target.x + target.width;
                let route_x = x1.max(x2) + 32.0 + lane_offset;
                (
                    format!("M {x1} {y1} H {route_x} V {y2} H {x2}"),
                    route_x + 6.0,
                    (y1 + y2) / 2.0 - 7.0,
                    route_x,
                    y1.max(y2),
                )
            } else {
                let (x1, x2) = if source.x < target.x {
                    (source.x + source.width, target.x)
                } else {
                    (source.x, target.x + target.width)
                };
                let route_x = (x1 + x2) / 2.0 + lane_offset;
                if (y1 - y2).abs() < f32::EPSILON {
                    let route_y = y1 + 22.0 + *lane as f32 * 12.0;
                    (
                        format!("M {x1} {y1} H {route_x} V {route_y} H {x2} V {y2}"),
                        route_x,
                        route_y - 7.0,
                        route_x,
                        route_y,
                    )
                } else {
                    (
                        format!("M {x1} {y1} H {route_x} V {y2} H {x2}"),
                        route_x,
                        (y1 + y2) / 2.0 - 7.0,
                        route_x,
                        y1.max(y2),
                    )
                }
            };
            max_edge_x = max_edge_x.max(route_x + 18.0);
            max_edge_y = max_edge_y.max(route_y + 18.0);
            Some(LayoutEdge {
                id: edge.id.clone(),
                kind: edge.kind,
                label: edge.label.as_deref().map(safe_label),
                path,
                label_x,
                label_y,
            })
        })
        .collect::<Vec<_>>();

    let width = nodes
        .iter()
        .map(|node| node.x + node.width + LEFT_MARGIN)
        .fold(960.0_f32, f32::max)
        .max(max_edge_x + LEFT_MARGIN);
    for band in &mut bands {
        band.width = width - 32.0;
    }
    let height = (next_y - BAND_GAP + TOP_MARGIN)
        .max(max_edge_y + TOP_MARGIN)
        .max(360.0);
    TopologyLayout {
        width,
        height,
        bands,
        nodes,
        edges,
    }
}

pub fn compact_placement(node: &LayoutNode) -> Vec<String> {
    let Some(placement) = &node.placement else {
        return Vec::new();
    };
    let mut values = Vec::new();
    match node.semantic_kind {
        TopologySemanticKind::DatabaseInstance => {
            if let Some(worker_node) = &placement.worker_node {
                values.push(format!("Node · {worker_node}"));
            }
            if let Some(zone) = &placement.zone {
                values.push(format!("Zone · {zone}"));
            }
        }
        TopologySemanticKind::WorkerNode => {
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

pub fn accessible_node_label(node: &LayoutNode) -> String {
    let mut values = vec![
        node.label.clone(),
        topology_semantic_label(node.semantic_kind).into(),
        format!("status {}", health_label(node.health)),
        topology_ownership_label(node.ownership).into(),
        provenance_label(node.provenance).into(),
    ];
    if let Some(role) = node.database_role {
        values.push(format!(
            "database role {}",
            database_instance_role_label(role)
        ));
    }
    match node.semantic_kind {
        TopologySemanticKind::DatabaseInstance => {
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
        TopologySemanticKind::WorkerNode => {
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

const fn provenance_label(provenance: TopologyNodeProvenance) -> &'static str {
    match provenance {
        TopologyNodeProvenance::ExactKubernetesResource => "Exact Kubernetes resource",
        TopologyNodeProvenance::DatabaseLogicalRepresentation => "Database logical representation",
        TopologyNodeProvenance::ExternalProviderRepresentation => {
            "External provider representation"
        }
        TopologyNodeProvenance::RecordedResourceRepresentation => {
            "Recorded resource representation"
        }
        TopologyNodeProvenance::SyntheticSummary => "Summary",
    }
}

fn database_row_assignments(
    graph: &TopologyGraph,
    nodes: &[&TopologyNode],
) -> BTreeMap<String, usize> {
    let nodes_by_id = nodes
        .iter()
        .map(|node| (node.id.as_str(), *node))
        .collect::<BTreeMap<_, _>>();
    let mut parent_candidates = BTreeMap::<&str, Vec<(&str, &str)>>::new();
    for edge in &graph.edges {
        if edge.kind != TopologyEdgeKind::Represents {
            continue;
        }
        let Some(source) = nodes_by_id.get(edge.source.as_str()) else {
            continue;
        };
        let Some(target) = nodes_by_id.get(edge.target.as_str()) else {
            continue;
        };
        if source.semantic_kind == TopologySemanticKind::DatabaseCluster
            && target.semantic_kind == TopologySemanticKind::DatabaseInstance
        {
            parent_candidates
                .entry(edge.target.as_str())
                .or_default()
                .push((edge.id.as_str(), edge.source.as_str()));
        }
    }
    let parents = parent_candidates
        .into_iter()
        .filter_map(|(target, mut candidates)| {
            candidates.sort_unstable();
            candidates
                .first()
                .map(|(_, source)| (target, source.to_string()))
        })
        .collect::<BTreeMap<_, _>>();

    let mut clusters = nodes
        .iter()
        .copied()
        .filter(|node| node.semantic_kind == TopologySemanticKind::DatabaseCluster)
        .collect::<Vec<_>>();
    clusters.sort_by_key(|node| {
        (
            safe_label(&node.label).to_ascii_lowercase(),
            node.id.clone(),
        )
    });
    let mut instances_by_parent = BTreeMap::<String, Vec<&TopologyNode>>::new();
    let mut orphans = Vec::new();
    for instance in nodes
        .iter()
        .copied()
        .filter(|node| node.semantic_kind == TopologySemanticKind::DatabaseInstance)
    {
        if let Some(parent) = parents.get(instance.id.as_str())
            && nodes_by_id
                .get(parent.as_str())
                .is_some_and(|node| node.semantic_kind == TopologySemanticKind::DatabaseCluster)
        {
            instances_by_parent
                .entry(parent.clone())
                .or_default()
                .push(instance);
            continue;
        }
        orphans.push(instance);
    }
    for instances in instances_by_parent.values_mut() {
        instances.sort_by_key(|node| {
            (
                database_role_rank(node.database_role),
                safe_label(&node.label).to_ascii_lowercase(),
                node.id.clone(),
            )
        });
    }
    orphans.sort_by_key(|node| {
        (
            database_role_rank(node.database_role),
            safe_label(&node.label).to_ascii_lowercase(),
            node.id.clone(),
        )
    });

    let mut rows = BTreeMap::new();
    let mut next_row = 0;
    for cluster in clusters {
        rows.insert(cluster.id.clone(), next_row);
        let instances = instances_by_parent.remove(&cluster.id).unwrap_or_default();
        for (offset, instance) in instances.iter().enumerate() {
            rows.insert(instance.id.clone(), next_row + offset);
        }
        next_row += instances.len().max(1) + 1;
    }
    for (_, instances) in instances_by_parent {
        orphans.extend(instances);
    }
    orphans.sort_by_key(|node| {
        (
            database_role_rank(node.database_role),
            safe_label(&node.label).to_ascii_lowercase(),
            node.id.clone(),
        )
    });
    for instance in orphans {
        rows.insert(instance.id.clone(), next_row);
        next_row += 1;
    }
    rows
}

pub fn safe_label(value: &str) -> String {
    let cleaned = value
        .chars()
        .filter(|character| !character.is_control())
        .take(MAX_LABEL_CHARACTERS)
        .collect::<String>();
    if cleaned.trim().is_empty() {
        "Unnamed resource".to_owned()
    } else if value
        .chars()
        .filter(|character| !character.is_control())
        .count()
        > MAX_LABEL_CHARACTERS
    {
        format!("{cleaned}…")
    } else {
        cleaned
    }
}

pub fn node_width(label: &str) -> f32 {
    let approximate = 64.0 + label.chars().count().min(MAX_LABEL_CHARACTERS + 1) as f32 * 7.2;
    approximate.clamp(MIN_NODE_WIDTH, MAX_NODE_WIDTH)
}

const fn band_rank(kind: TopologySemanticKind) -> u8 {
    match kind {
        TopologySemanticKind::Tenant => 0,
        TopologySemanticKind::ControlPlane => 1,
        TopologySemanticKind::WorkerPool
        | TopologySemanticKind::ComputeMachine
        | TopologySemanticKind::WorkerNode => 2,
        TopologySemanticKind::DatabaseCluster | TopologySemanticKind::DatabaseInstance => 3,
        TopologySemanticKind::AddOn => 4,
        TopologySemanticKind::ProviderInfrastructure => 5,
        TopologySemanticKind::Other => 6,
    }
}

const fn column_rank(kind: TopologySemanticKind) -> u8 {
    match kind {
        TopologySemanticKind::ComputeMachine | TopologySemanticKind::DatabaseInstance => 1,
        TopologySemanticKind::WorkerNode => 2,
        TopologySemanticKind::Tenant
        | TopologySemanticKind::ControlPlane
        | TopologySemanticKind::WorkerPool
        | TopologySemanticKind::DatabaseCluster
        | TopologySemanticKind::AddOn
        | TopologySemanticKind::ProviderInfrastructure
        | TopologySemanticKind::Other => 0,
    }
}

const fn database_role_rank(role: Option<DatabaseInstanceRole>) -> u8 {
    match role {
        Some(DatabaseInstanceRole::Primary) => 0,
        Some(DatabaseInstanceRole::Standby) => 1,
        Some(DatabaseInstanceRole::Unknown) => 2,
        None => 3,
    }
}

const fn edge_kind_rank(kind: TopologyEdgeKind) -> u8 {
    match kind {
        TopologyEdgeKind::Owns => 0,
        TopologyEdgeKind::Contains => 1,
        TopologyEdgeKind::Manages => 2,
        TopologyEdgeKind::Provides => 3,
        TopologyEdgeKind::Represents => 4,
        TopologyEdgeKind::DependsOn => 5,
    }
}

const fn band_id(rank: u8) -> &'static str {
    match rank {
        0 => "tenant",
        1 => "control-plane",
        2 => "compute",
        3 => "databases",
        4 => "add-ons",
        5 => "provider-infrastructure",
        _ => "other",
    }
}

const fn band_label(rank: u8) -> &'static str {
    match rank {
        0 => "Tenant",
        1 => "Control plane",
        2 => "Compute",
        3 => "Databases",
        4 => "Add-ons",
        5 => "Provider infrastructure",
        _ => "Other",
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use tenant_admin_shared::query::{
        DatabaseInstanceRole, TenantProvider, TopologyEdge, TopologyEdgeKind, TopologyGraph,
        TopologyHealth, TopologyNode, TopologyNodeKind, TopologyNodeProvenance, TopologyOwnership,
        TopologyPlacement, TopologySemanticKind,
    };

    use super::{accessible_node_label, compact_placement, layout_graph, node_width, safe_label};

    fn node(id: &str, kind: TopologyNodeKind, label: &str) -> TopologyNode {
        TopologyNode {
            id: id.to_owned(),
            kind,
            semantic_kind: match kind {
                TopologyNodeKind::Tenant => TopologySemanticKind::Tenant,
                TopologyNodeKind::ControlPlane => TopologySemanticKind::ControlPlane,
                TopologyNodeKind::WorkerPool => TopologySemanticKind::WorkerPool,
                TopologyNodeKind::Machine => TopologySemanticKind::ComputeMachine,
                TopologyNodeKind::Node => TopologySemanticKind::WorkerNode,
                TopologyNodeKind::ProviderResource => TopologySemanticKind::ProviderInfrastructure,
                TopologyNodeKind::AddOn => TopologySemanticKind::AddOn,
                TopologyNodeKind::Database => TopologySemanticKind::DatabaseCluster,
            },
            ownership: if kind == TopologyNodeKind::ProviderResource {
                TopologyOwnership::ProviderOwned
            } else {
                TopologyOwnership::TenantOwned
            },
            provenance: TopologyNodeProvenance::ExactKubernetesResource,
            database_role: None,
            placement: None,
            label: label.to_owned(),
            health: TopologyHealth::Ready,
            resource: None,
            attributes: Vec::new(),
        }
    }

    #[test]
    fn ordering_and_layout_are_stable() {
        let graph = TopologyGraph {
            tenant_name: "demo".to_owned(),
            provider: TenantProvider::Local,
            nodes: vec![
                node("node-z", TopologyNodeKind::Node, "zeta"),
                node("tenant", TopologyNodeKind::Tenant, "demo"),
                node("node-a", TopologyNodeKind::Node, "alpha"),
            ],
            edges: vec![TopologyEdge {
                id: "represents".to_owned(),
                source: "tenant".to_owned(),
                target: "node-a".to_owned(),
                kind: TopologyEdgeKind::Represents,
                label: None,
            }],
        };
        let first = layout_graph(&graph);
        let second = layout_graph(&graph);
        assert_eq!(first, second);
        assert_eq!(
            first
                .nodes
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>(),
            vec!["tenant", "node-a", "node-z"]
        );
        assert!(first.nodes[0].x < first.nodes[1].x);
        assert_eq!(first.edges.len(), 1);
        assert_eq!(
            first
                .bands
                .iter()
                .map(|band| band.label.as_str())
                .collect::<Vec<_>>(),
            ["Tenant", "Compute"]
        );
    }

    #[test]
    fn database_clusters_instances_and_roles_have_stable_semantic_geometry() {
        let mut cluster = node("database:cluster", TopologyNodeKind::Database, "orders");
        cluster.semantic_kind = TopologySemanticKind::DatabaseCluster;
        let mut standby = node(
            "database:instance:two",
            TopologyNodeKind::Database,
            "orders-2",
        );
        standby.semantic_kind = TopologySemanticKind::DatabaseInstance;
        standby.database_role = Some(DatabaseInstanceRole::Standby);
        let mut primary = node(
            "database:instance:one",
            TopologyNodeKind::Database,
            "orders-1",
        );
        primary.semantic_kind = TopologySemanticKind::DatabaseInstance;
        primary.database_role = Some(DatabaseInstanceRole::Primary);
        primary.placement = Some(TopologyPlacement {
            worker_pool: None,
            worker_node: Some("worker-a".into()),
            zone: Some("zone-a".into()),
        });
        let edges = vec![
            TopologyEdge {
                id: "cluster-two".into(),
                source: cluster.id.clone(),
                target: standby.id.clone(),
                kind: TopologyEdgeKind::Represents,
                label: None,
            },
            TopologyEdge {
                id: "cluster-one".into(),
                source: cluster.id.clone(),
                target: primary.id.clone(),
                kind: TopologyEdgeKind::Represents,
                label: None,
            },
        ];
        let first = layout_graph(&TopologyGraph {
            tenant_name: "demo".into(),
            provider: TenantProvider::Local,
            nodes: vec![standby.clone(), cluster.clone(), primary.clone()],
            edges: edges.clone(),
        });
        let second = layout_graph(&TopologyGraph {
            tenant_name: "demo".into(),
            provider: TenantProvider::Local,
            nodes: vec![primary, cluster, standby],
            edges: edges.into_iter().rev().collect(),
        });

        assert_eq!(first, second);
        let cluster = first
            .nodes
            .iter()
            .find(|node| node.semantic_kind == TopologySemanticKind::DatabaseCluster)
            .unwrap();
        let instances = first
            .nodes
            .iter()
            .filter(|node| node.semantic_kind == TopologySemanticKind::DatabaseInstance)
            .collect::<Vec<_>>();
        assert!(instances.iter().all(|instance| cluster.x < instance.x));
        assert_eq!(
            instances
                .iter()
                .map(|node| node.database_role)
                .collect::<Vec<_>>(),
            [
                Some(DatabaseInstanceRole::Primary),
                Some(DatabaseInstanceRole::Standby)
            ]
        );
        assert_eq!(
            compact_placement(instances[0]),
            ["Node · worker-a", "Zone · zone-a"]
        );
        let mut missing = (*instances[0]).clone();
        missing.placement = None;
        let accessible = accessible_node_label(&missing);
        assert!(accessible.contains("worker node not reported"));
        assert!(accessible.contains("zone not reported"));
    }

    #[test]
    fn orphan_instances_and_parallel_edges_remain_deterministic() {
        let mut source = node("database:cluster", TopologyNodeKind::Database, "orders");
        source.semantic_kind = TopologySemanticKind::DatabaseCluster;
        let mut target = node(
            "database:instance:one",
            TopologyNodeKind::Database,
            "orders-1",
        );
        target.semantic_kind = TopologySemanticKind::DatabaseInstance;
        target.database_role = Some(DatabaseInstanceRole::Standby);
        let mut orphan = node(
            "database:instance:orphan",
            TopologyNodeKind::Database,
            "orphan",
        );
        orphan.semantic_kind = TopologySemanticKind::DatabaseInstance;
        orphan.database_role = Some(DatabaseInstanceRole::Primary);
        let graph = TopologyGraph {
            tenant_name: "demo".into(),
            provider: TenantProvider::Local,
            nodes: vec![target, source, orphan],
            edges: vec![
                TopologyEdge {
                    id: "one".into(),
                    source: "database:cluster".into(),
                    target: "database:instance:one".into(),
                    kind: TopologyEdgeKind::Represents,
                    label: None,
                },
                TopologyEdge {
                    id: "two".into(),
                    source: "database:cluster".into(),
                    target: "database:instance:one".into(),
                    kind: TopologyEdgeKind::DependsOn,
                    label: None,
                },
            ],
        };
        let layout = layout_graph(&graph);
        assert_eq!(layout.nodes.len(), 3);
        assert_eq!(layout.edges.len(), 2);
        assert_ne!(layout.edges[0].path, layout.edges[1].path);
        assert!(
            layout.width
                > layout
                    .edges
                    .iter()
                    .map(|edge| edge.label_x)
                    .fold(0.0_f32, f32::max)
        );
        assert!(
            layout.height
                > layout
                    .edges
                    .iter()
                    .map(|edge| edge.label_y)
                    .fold(0.0_f32, f32::max)
        );
    }

    #[test]
    fn database_instances_follow_their_surviving_parent_cluster() {
        let mut cluster_a = node("cluster-a", TopologyNodeKind::Database, "alpha");
        cluster_a.semantic_kind = TopologySemanticKind::DatabaseCluster;
        let mut cluster_b = node("cluster-b", TopologyNodeKind::Database, "beta");
        cluster_b.semantic_kind = TopologySemanticKind::DatabaseCluster;
        let mut instance_a = node("instance-a", TopologyNodeKind::Database, "alpha-2");
        instance_a.semantic_kind = TopologySemanticKind::DatabaseInstance;
        instance_a.database_role = Some(DatabaseInstanceRole::Standby);
        let mut instance_b = node("instance-b", TopologyNodeKind::Database, "beta-1");
        instance_b.semantic_kind = TopologySemanticKind::DatabaseInstance;
        instance_b.database_role = Some(DatabaseInstanceRole::Primary);
        let graph = TopologyGraph {
            tenant_name: "demo".into(),
            provider: TenantProvider::Local,
            nodes: vec![instance_b, cluster_a, instance_a, cluster_b],
            edges: vec![
                TopologyEdge {
                    id: "b-parent".into(),
                    source: "cluster-b".into(),
                    target: "instance-b".into(),
                    kind: TopologyEdgeKind::Represents,
                    label: None,
                },
                TopologyEdge {
                    id: "a-parent".into(),
                    source: "cluster-a".into(),
                    target: "instance-a".into(),
                    kind: TopologyEdgeKind::Represents,
                    label: None,
                },
            ],
        };
        let layout = layout_graph(&graph);
        let positions = layout
            .nodes
            .iter()
            .map(|node| (node.id.as_str(), node.y))
            .collect::<BTreeMap<_, _>>();
        assert_eq!(positions["cluster-a"], positions["instance-a"]);
        assert_eq!(positions["cluster-b"], positions["instance-b"]);
        assert_ne!(positions["cluster-a"], positions["instance-b"]);
    }

    #[test]
    fn labels_and_dimensions_are_svg_safe_and_bounded() {
        assert_eq!(safe_label("\0\n"), "Unnamed resource");
        let long = "x".repeat(100);
        assert!(safe_label(&long).ends_with('…'));
        assert!((208.0..=286.0).contains(&node_width("short")));
        assert_eq!(node_width(&long), 286.0);
    }

    #[test]
    fn same_column_edges_route_around_nodes() {
        let graph = TopologyGraph {
            tenant_name: "demo".to_owned(),
            provider: TenantProvider::Azure,
            nodes: vec![
                node("source", TopologyNodeKind::ProviderResource, "source"),
                node("target", TopologyNodeKind::ProviderResource, "target"),
            ],
            edges: vec![TopologyEdge {
                id: "owns".to_owned(),
                source: "source".to_owned(),
                target: "target".to_owned(),
                kind: TopologyEdgeKind::Owns,
                label: None,
            }],
        };
        let layout = layout_graph(&graph);
        assert_eq!(layout.edges.len(), 1);
        assert!(layout.edges[0].path.contains(" H "));
        assert!(layout.edges[0].path.contains(" V "));
    }
}
