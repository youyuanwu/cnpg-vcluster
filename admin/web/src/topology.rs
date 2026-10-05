use std::collections::BTreeMap;

use tenant_admin_shared::query::{
    TopologyEdgeKind, TopologyGraph, TopologyHealth, TopologyNodeKind,
};

const LEFT_MARGIN: f32 = 44.0;
const TOP_MARGIN: f32 = 52.0;
const LAYER_GAP: f32 = 300.0;
const ROW_GAP: f32 = 138.0;
const NODE_HEIGHT: f32 = 88.0;
const MIN_NODE_WIDTH: f32 = 176.0;
const MAX_NODE_WIDTH: f32 = 272.0;
const MAX_LABEL_CHARACTERS: usize = 30;

#[derive(Clone, Debug, PartialEq)]
pub struct TopologyLayout {
    pub width: f32,
    pub height: f32,
    pub nodes: Vec<LayoutNode>,
    pub edges: Vec<LayoutEdge>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LayoutNode {
    pub id: String,
    pub kind: TopologyNodeKind,
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
    source_nodes.sort_by_key(|node| (layer(node.kind), safe_label(&node.label), node.id.clone()));

    let mut layer_rows = BTreeMap::<u8, usize>::new();
    let mut nodes = Vec::with_capacity(source_nodes.len());
    for node in source_nodes {
        let node_layer = layer(node.kind);
        let row = layer_rows.entry(node_layer).or_default();
        let label = safe_label(&node.label);
        let width = node_width(&label);
        nodes.push(LayoutNode {
            id: node.id.clone(),
            kind: node.kind,
            health: node.health,
            label,
            x: LEFT_MARGIN + f32::from(node_layer) * LAYER_GAP,
            y: TOP_MARGIN + *row as f32 * ROW_GAP,
            width,
            height: NODE_HEIGHT,
        });
        *row += 1;
    }

    let positions = nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<BTreeMap<_, _>>();
    let mut source_edges = graph.edges.iter().collect::<Vec<_>>();
    source_edges.sort_by_key(|edge| (edge.source.as_str(), edge.target.as_str(), edge.id.as_str()));
    let edges = source_edges
        .into_iter()
        .filter_map(|edge| {
            let source = positions.get(edge.source.as_str())?;
            let target = positions.get(edge.target.as_str())?;
            let y1 = source.y + source.height / 2.0;
            let y2 = target.y + target.height / 2.0;
            let (path, label_x, label_y) = if source.x == target.x {
                let x1 = source.x + source.width;
                let x2 = target.x + target.width;
                let route_x = x1.max(x2) + 32.0;
                (
                    format!("M {x1} {y1} H {route_x} V {y2} H {x2}"),
                    route_x + 6.0,
                    (y1 + y2) / 2.0 - 7.0,
                )
            } else {
                let (x1, x2) = if source.x < target.x {
                    (source.x + source.width, target.x)
                } else {
                    (source.x, target.x + target.width)
                };
                (
                    format!("M {x1} {y1} L {x2} {y2}"),
                    (x1 + x2) / 2.0,
                    (y1 + y2) / 2.0 - 7.0,
                )
            };
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
        .fold(960.0_f32, f32::max);
    let height = nodes
        .iter()
        .map(|node| node.y + node.height + TOP_MARGIN)
        .fold(360.0_f32, f32::max);
    TopologyLayout {
        width,
        height,
        nodes,
        edges,
    }
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
    let approximate = 52.0 + label.chars().count().min(MAX_LABEL_CHARACTERS + 1) as f32 * 7.2;
    approximate.clamp(MIN_NODE_WIDTH, MAX_NODE_WIDTH)
}

const fn layer(kind: TopologyNodeKind) -> u8 {
    match kind {
        TopologyNodeKind::Tenant => 0,
        TopologyNodeKind::ControlPlane
        | TopologyNodeKind::ProviderResource
        | TopologyNodeKind::AddOn
        | TopologyNodeKind::Database => 1,
        TopologyNodeKind::WorkerPool => 2,
        TopologyNodeKind::Machine => 3,
        TopologyNodeKind::Node => 4,
    }
}

#[cfg(test)]
mod tests {
    use tenant_admin_shared::query::{
        TenantProvider, TopologyEdge, TopologyEdgeKind, TopologyGraph, TopologyHealth,
        TopologyNode, TopologyNodeKind, TopologyNodeProvenance, TopologyOwnership,
        TopologySemanticKind,
    };

    use super::{layout_graph, node_width, safe_label};

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
    }

    #[test]
    fn labels_and_dimensions_are_svg_safe_and_bounded() {
        assert_eq!(safe_label("\0\n"), "Unnamed resource");
        let long = "x".repeat(100);
        assert!(safe_label(&long).ends_with('…'));
        assert!((176.0..=272.0).contains(&node_width("short")));
        assert_eq!(node_width(&long), 272.0);
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
