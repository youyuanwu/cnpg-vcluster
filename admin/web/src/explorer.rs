use std::collections::{BTreeMap, BTreeSet};

use tenant_admin_shared::query::{
    DisplayAttribute, ResourceIdentityView, TopologyEdge, TopologyEdgeKind, TopologyGraph,
    TopologyHealth, TopologyNode, TopologyNodeKind, TopologyNodeProvenance,
};

pub const MAX_VISIBLE_NODES: usize = 20;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum ResourceGroup {
    Tenant,
    ControlPlane,
    Compute,
    Databases,
    AddOns,
    ProviderInfrastructure,
    Other,
}

impl ResourceGroup {
    pub const ALL: [Self; 7] = [
        Self::Tenant,
        Self::ControlPlane,
        Self::Compute,
        Self::Databases,
        Self::AddOns,
        Self::ProviderInfrastructure,
        Self::Other,
    ];

    pub const fn label(self) -> &'static str {
        match self {
            Self::Tenant => "Tenant",
            Self::ControlPlane => "Control plane",
            Self::Compute => "Compute",
            Self::Databases => "Databases",
            Self::AddOns => "Add-ons",
            Self::ProviderInfrastructure => "Provider infrastructure",
            Self::Other => "Other",
        }
    }

    const fn id(self) -> &'static str {
        match self {
            Self::Tenant => "tenant",
            Self::ControlPlane => "control-plane",
            Self::Compute => "compute",
            Self::Databases => "databases",
            Self::AddOns => "add-ons",
            Self::ProviderInfrastructure => "provider-infrastructure",
            Self::Other => "other",
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct HealthCounts {
    pub ready: usize,
    pub progressing: usize,
    pub degraded: usize,
    pub failed: usize,
    pub deleting: usize,
    pub unknown: usize,
}

impl HealthCounts {
    fn add(&mut self, health: TopologyHealth) {
        match health {
            TopologyHealth::Ready => self.ready += 1,
            TopologyHealth::Progressing => self.progressing += 1,
            TopologyHealth::Degraded => self.degraded += 1,
            TopologyHealth::Failed => self.failed += 1,
            TopologyHealth::Deleting => self.deleting += 1,
            TopologyHealth::Unknown => self.unknown += 1,
        }
    }

    pub const fn attention(&self) -> usize {
        self.degraded + self.failed + self.deleting + self.unknown
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ProvenanceCounts {
    pub exact: usize,
    pub database_logical: usize,
    pub external: usize,
    pub recorded: usize,
    pub synthetic: usize,
}

impl ProvenanceCounts {
    fn add(&mut self, provenance: TopologyNodeProvenance) {
        match provenance {
            TopologyNodeProvenance::ExactKubernetesResource => self.exact += 1,
            TopologyNodeProvenance::DatabaseLogicalRepresentation => {
                self.database_logical += 1;
            }
            TopologyNodeProvenance::ExternalProviderRepresentation => self.external += 1,
            TopologyNodeProvenance::RecordedResourceRepresentation => self.recorded += 1,
            TopologyNodeProvenance::SyntheticSummary => self.synthetic += 1,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GroupSummary {
    pub group: ResourceGroup,
    pub count: usize,
    pub health: HealthCounts,
    pub provenance: ProvenanceCounts,
    pub synthetic_health: HealthCounts,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResourceRelationship {
    pub id: String,
    pub kind: TopologyEdgeKind,
    pub label: Option<String>,
    pub source_id: String,
    pub source_label: String,
    pub target_id: String,
    pub target_label: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResourceInspection {
    pub node: TopologyNode,
    pub group: ResourceGroup,
    pub incoming: Vec<ResourceRelationship>,
    pub outgoing: Vec<ResourceRelationship>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ExplorerFilter {
    pub query: String,
    pub group: Option<ResourceGroup>,
    pub health: Option<TopologyHealth>,
    pub kind: Option<TopologyNodeKind>,
    pub namespace: Option<String>,
    pub namespace_not_applicable: bool,
}

#[derive(Clone)]
pub struct ExplorerModel {
    graph: TopologyGraph,
    nodes: BTreeMap<String, TopologyNode>,
    edges: Vec<TopologyEdge>,
}

impl ExplorerModel {
    pub fn new(graph: TopologyGraph) -> Self {
        let nodes = graph
            .nodes
            .iter()
            .cloned()
            .map(|node| (node.id.clone(), node))
            .collect();
        Self {
            edges: graph.edges.clone(),
            graph,
            nodes,
        }
    }

    pub fn node(&self, id: &str) -> Option<&TopologyNode> {
        self.nodes.get(id)
    }

    pub fn group_summaries(&self) -> Vec<GroupSummary> {
        let mut summaries = ResourceGroup::ALL
            .into_iter()
            .map(|group| {
                (
                    group,
                    GroupSummary {
                        group,
                        count: 0,
                        health: HealthCounts::default(),
                        provenance: ProvenanceCounts::default(),
                        synthetic_health: HealthCounts::default(),
                    },
                )
            })
            .collect::<BTreeMap<_, _>>();
        for node in self.nodes.values() {
            let group = resource_group(node.kind);
            let summary = summaries.get_mut(&group).expect("known group");
            summary.provenance.add(node.provenance);
            if ordinary_resource(node) || node.kind == TopologyNodeKind::Tenant {
                summary.count += 1;
                summary.health.add(node.health);
            } else if node.provenance == TopologyNodeProvenance::SyntheticSummary {
                summary.synthetic_health.add(node.health);
            }
        }
        let mut values = summaries
            .into_values()
            .map(|mut summary| {
                if summary.count == 0 {
                    summary.health = summary.synthetic_health.clone();
                }
                summary
            })
            .filter(|summary| summary.count > 0 || summary.provenance.synthetic > 0)
            .collect::<Vec<_>>();
        values.sort_by_key(|summary| (usize::MAX - summary.health.attention(), summary.group));
        values
    }

    pub fn filtered_nodes(&self, filter: &ExplorerFilter) -> Vec<TopologyNode> {
        let query = filter.query.to_ascii_lowercase();
        let mut nodes = self
            .nodes
            .values()
            .filter(|node| node.provenance != TopologyNodeProvenance::SyntheticSummary)
            .filter(|node| {
                filter
                    .group
                    .is_none_or(|group| resource_group(node.kind) == group)
            })
            .filter(|node| filter.health.is_none_or(|health| node.health == health))
            .filter(|node| filter.kind.is_none_or(|kind| node.kind == kind))
            .filter(|node| {
                filter.namespace.as_ref().is_none_or(|namespace| {
                    node.resource
                        .as_ref()
                        .and_then(|resource| resource.namespace.as_ref())
                        == Some(namespace)
                })
            })
            .filter(|node| {
                !filter.namespace_not_applicable
                    || node
                        .resource
                        .as_ref()
                        .and_then(|resource| resource.namespace.as_ref())
                        .is_none()
            })
            .filter(|node| query.is_empty() || searchable_text(node).contains(&query))
            .cloned()
            .collect::<Vec<_>>();
        nodes.sort_by_key(|node| {
            (
                health_priority(node.health),
                resource_group(node.kind),
                node.label.to_ascii_lowercase(),
                node.id.clone(),
            )
        });
        nodes
    }

    pub fn inspection(&self, id: &str) -> Option<ResourceInspection> {
        let node = self.nodes.get(id)?.clone();
        let mut incoming = Vec::new();
        let mut outgoing = Vec::new();
        for edge in &self.edges {
            let Some(relationship) = self.relationship(edge) else {
                continue;
            };
            if edge.target == id {
                incoming.push(relationship);
            } else if edge.source == id {
                outgoing.push(relationship);
            }
        }
        incoming.sort_by(|left, right| left.id.cmp(&right.id));
        outgoing.sort_by(|left, right| left.id.cmp(&right.id));
        Some(ResourceInspection {
            group: resource_group(node.kind),
            node,
            incoming,
            outgoing,
        })
    }

    pub fn relationship(&self, edge: &TopologyEdge) -> Option<ResourceRelationship> {
        let source = self.nodes.get(&edge.source)?;
        let target = self.nodes.get(&edge.target)?;
        Some(ResourceRelationship {
            id: edge.id.clone(),
            kind: edge.kind,
            label: edge.label.clone(),
            source_id: source.id.clone(),
            source_label: source.label.clone(),
            target_id: target.id.clone(),
            target_label: target.label.clone(),
        })
    }

    pub fn relationship_by_id(&self, id: &str) -> Option<ResourceRelationship> {
        self.edges
            .iter()
            .find(|edge| edge.id == id)
            .and_then(|edge| self.relationship(edge))
    }

    pub fn focused_graph(&self, selected: Option<&str>) -> TopologyGraph {
        let Some(selected) = selected.filter(|id| self.nodes.contains_key(*id)) else {
            return self.summary_graph();
        };
        let mut ids = BTreeSet::from([selected.to_owned()]);
        for edge in &self.edges {
            if edge.source == selected {
                ids.insert(edge.target.clone());
            } else if edge.target == selected {
                ids.insert(edge.source.clone());
            }
        }
        let mut ranked = ids
            .into_iter()
            .filter_map(|id| self.nodes.get(&id).cloned())
            .collect::<Vec<_>>();
        ranked.sort_by_key(|node| {
            (
                node.id != selected,
                health_priority(node.health),
                node.id.clone(),
            )
        });
        ranked.truncate(MAX_VISIBLE_NODES);
        let retained = ranked
            .iter()
            .map(|node| node.id.as_str())
            .collect::<BTreeSet<_>>();
        let edges = self
            .edges
            .iter()
            .filter(|edge| {
                retained.contains(edge.source.as_str()) && retained.contains(edge.target.as_str())
            })
            .cloned()
            .collect();
        TopologyGraph {
            tenant_name: self.graph.tenant_name.clone(),
            provider: self.graph.provider,
            nodes: ranked,
            edges,
        }
    }

    pub fn group_graph(&self, group: ResourceGroup) -> TopologyGraph {
        let tenant = self
            .nodes
            .values()
            .find(|node| node.kind == TopologyNodeKind::Tenant)
            .cloned();
        let mut nodes = tenant.into_iter().collect::<Vec<_>>();
        let remaining = MAX_VISIBLE_NODES.saturating_sub(nodes.len());
        let mut group_nodes = self
            .nodes
            .values()
            .filter(|node| resource_group(node.kind) == group && ordinary_resource(node))
            .cloned()
            .collect::<Vec<_>>();
        group_nodes.sort_by_key(|node| {
            (
                health_priority(node.health),
                node.label.to_ascii_lowercase(),
                node.id.clone(),
            )
        });
        group_nodes.truncate(remaining);
        nodes.extend(group_nodes);
        let retained = nodes
            .iter()
            .map(|node| node.id.as_str())
            .collect::<BTreeSet<_>>();
        let edges = self
            .edges
            .iter()
            .filter(|edge| {
                retained.contains(edge.source.as_str()) && retained.contains(edge.target.as_str())
            })
            .cloned()
            .collect();
        TopologyGraph {
            tenant_name: self.graph.tenant_name.clone(),
            provider: self.graph.provider,
            nodes,
            edges,
        }
    }

    pub fn relationship_graph(
        &self,
        selected: Option<&str>,
        relationship_id: &str,
    ) -> TopologyGraph {
        let mut graph = self.focused_graph(selected);
        let Some(edge) = self.edges.iter().find(|edge| edge.id == relationship_id) else {
            return graph;
        };
        let required = BTreeSet::from([
            edge.source.as_str(),
            edge.target.as_str(),
            selected.unwrap_or_default(),
        ]);
        for endpoint in [&edge.source, &edge.target] {
            if graph.nodes.iter().any(|node| node.id == *endpoint) {
                continue;
            }
            if graph.nodes.len() >= MAX_VISIBLE_NODES
                && let Some(index) = graph
                    .nodes
                    .iter()
                    .rposition(|node| !required.contains(node.id.as_str()))
            {
                graph.nodes.remove(index);
            }
            if let Some(node) = self.nodes.get(endpoint) {
                graph.nodes.push(node.clone());
            }
        }
        let retained = graph
            .nodes
            .iter()
            .map(|node| node.id.as_str())
            .collect::<BTreeSet<_>>();
        graph.edges = self
            .edges
            .iter()
            .filter(|candidate| {
                retained.contains(candidate.source.as_str())
                    && retained.contains(candidate.target.as_str())
            })
            .cloned()
            .collect();
        graph
    }

    pub fn selected_group_graph(&self, selected: &str, group: ResourceGroup) -> TopologyGraph {
        let mut graph = self.focused_graph(Some(selected));
        let mut retained = graph
            .nodes
            .iter()
            .map(|node| node.id.clone())
            .collect::<BTreeSet<_>>();
        let mut additions = self
            .nodes
            .values()
            .filter(|node| resource_group(node.kind) == group && ordinary_resource(node))
            .cloned()
            .collect::<Vec<_>>();
        additions.sort_by_key(|node| {
            (
                health_priority(node.health),
                node.label.to_ascii_lowercase(),
                node.id.clone(),
            )
        });
        for node in additions {
            if retained.contains(&node.id) {
                continue;
            }
            if graph.nodes.len() >= MAX_VISIBLE_NODES {
                let Some(index) = graph
                    .nodes
                    .iter()
                    .rposition(|candidate| candidate.id != selected)
                else {
                    break;
                };
                let removed = graph.nodes.remove(index);
                retained.remove(&removed.id);
            }
            retained.insert(node.id.clone());
            graph.nodes.push(node);
        }
        graph.edges = self
            .edges
            .iter()
            .filter(|edge| retained.contains(&edge.source) && retained.contains(&edge.target))
            .cloned()
            .collect();
        graph
    }

    pub fn attention_nodes(&self) -> Vec<TopologyNode> {
        let mut nodes = self
            .nodes
            .values()
            .filter(|node| ordinary_resource(node))
            .filter(|node| {
                matches!(
                    node.health,
                    TopologyHealth::Degraded
                        | TopologyHealth::Failed
                        | TopologyHealth::Deleting
                        | TopologyHealth::Unknown
                )
            })
            .cloned()
            .collect::<Vec<_>>();
        for node in self.nodes.values().filter(|node| {
            node.provenance == TopologyNodeProvenance::SyntheticSummary && unhealthy(node.health)
        }) {
            let group = resource_group(node.kind);
            if !nodes
                .iter()
                .any(|candidate| resource_group(candidate.kind) == group)
            {
                nodes.push(node.clone());
            }
        }
        nodes.sort_by_key(|node| (health_priority(node.health), node.id.clone()));
        nodes
    }

    fn summary_graph(&self) -> TopologyGraph {
        let tenant = self
            .nodes
            .values()
            .find(|node| node.kind == TopologyNodeKind::Tenant)
            .cloned();
        let tenant_id = tenant
            .as_ref()
            .map_or_else(|| "tenant".to_owned(), |node| node.id.clone());
        let mut nodes = tenant.into_iter().collect::<Vec<_>>();
        let mut edges = Vec::new();
        for summary in self
            .group_summaries()
            .into_iter()
            .filter(|summary| summary.group != ResourceGroup::Tenant)
            .take(7)
        {
            let id = format!("group:{}", summary.group.id());
            nodes.push(TopologyNode {
                id: id.clone(),
                kind: group_kind(summary.group),
                provenance: TopologyNodeProvenance::SyntheticSummary,
                label: format!("{} ({})", summary.group.label(), summary.count),
                health: summary_health(&summary.health),
                resource: None,
                attributes: vec![DisplayAttribute {
                    label: "Resources".into(),
                    value: summary.count.to_string(),
                }],
            });
            edges.push(TopologyEdge {
                id: format!("edge:{tenant_id}:{id}"),
                source: tenant_id.clone(),
                target: id,
                kind: TopologyEdgeKind::Contains,
                label: Some(summary.group.label().into()),
            });
        }
        TopologyGraph {
            tenant_name: self.graph.tenant_name.clone(),
            provider: self.graph.provider,
            nodes,
            edges,
        }
    }
}

pub const fn resource_group(kind: TopologyNodeKind) -> ResourceGroup {
    match kind {
        TopologyNodeKind::Tenant => ResourceGroup::Tenant,
        TopologyNodeKind::ControlPlane => ResourceGroup::ControlPlane,
        TopologyNodeKind::WorkerPool | TopologyNodeKind::Machine | TopologyNodeKind::Node => {
            ResourceGroup::Compute
        }
        TopologyNodeKind::Database => ResourceGroup::Databases,
        TopologyNodeKind::AddOn => ResourceGroup::AddOns,
        TopologyNodeKind::ProviderResource => ResourceGroup::ProviderInfrastructure,
    }
}

fn group_kind(group: ResourceGroup) -> TopologyNodeKind {
    match group {
        ResourceGroup::Tenant => TopologyNodeKind::Tenant,
        ResourceGroup::ControlPlane => TopologyNodeKind::ControlPlane,
        ResourceGroup::Compute => TopologyNodeKind::WorkerPool,
        ResourceGroup::Databases => TopologyNodeKind::Database,
        ResourceGroup::AddOns => TopologyNodeKind::AddOn,
        ResourceGroup::ProviderInfrastructure | ResourceGroup::Other => {
            TopologyNodeKind::ProviderResource
        }
    }
}

fn ordinary_resource(node: &TopologyNode) -> bool {
    node.kind != TopologyNodeKind::Tenant
        && node.provenance != TopologyNodeProvenance::SyntheticSummary
}

fn unhealthy(health: TopologyHealth) -> bool {
    matches!(
        health,
        TopologyHealth::Degraded
            | TopologyHealth::Failed
            | TopologyHealth::Deleting
            | TopologyHealth::Unknown
    )
}

fn searchable_text(node: &TopologyNode) -> String {
    let mut values = vec![
        node.label.clone(),
        format!("{:?}", node.kind),
        format!("{:?}", node.health),
        format!("{:?}", node.provenance),
    ];
    if let Some(resource) = &node.resource {
        values.extend([
            resource.api_version.clone(),
            resource.kind.clone(),
            resource.namespace.clone().unwrap_or_default(),
            resource.name.clone(),
            resource.uid.clone().unwrap_or_default(),
        ]);
    }
    values.extend(
        node.attributes
            .iter()
            .flat_map(|attribute| [attribute.label.clone(), attribute.value.clone()]),
    );
    values.join(" ").to_ascii_lowercase()
}

const fn health_priority(health: TopologyHealth) -> u8 {
    match health {
        TopologyHealth::Failed => 0,
        TopologyHealth::Degraded => 1,
        TopologyHealth::Deleting => 2,
        TopologyHealth::Unknown => 3,
        TopologyHealth::Progressing => 4,
        TopologyHealth::Ready => 5,
    }
}

fn summary_health(counts: &HealthCounts) -> TopologyHealth {
    if counts.failed > 0 {
        TopologyHealth::Failed
    } else if counts.degraded > 0 {
        TopologyHealth::Degraded
    } else if counts.deleting > 0 {
        TopologyHealth::Deleting
    } else if counts.unknown > 0 {
        TopologyHealth::Unknown
    } else if counts.progressing > 0 {
        TopologyHealth::Progressing
    } else {
        TopologyHealth::Ready
    }
}

pub fn representation_label(provenance: TopologyNodeProvenance) -> &'static str {
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

pub fn display_identity(node: &TopologyNode) -> Vec<(String, String)> {
    if let Some(ResourceIdentityView {
        api_version,
        kind,
        namespace,
        name,
        uid,
    }) = &node.resource
    {
        return vec![
            ("API version".into(), api_version.clone()),
            ("Kind".into(), kind.clone()),
            (
                "Namespace".into(),
                namespace.clone().unwrap_or_else(|| "Not applicable".into()),
            ),
            ("Name".into(), name.clone()),
            (
                "UID".into(),
                uid.clone().unwrap_or_else(|| "Not available".into()),
            ),
        ];
    }
    vec![
        (
            "Representation".into(),
            representation_label(node.provenance).into(),
        ),
        ("Kind".into(), format!("{:?}", node.kind)),
        ("Namespace".into(), "Not applicable".into()),
        ("Name".into(), node.label.clone()),
        ("Node ID".into(), node.id.clone()),
    ]
}

#[cfg(test)]
mod tests {
    use std::time::Instant;

    use tenant_admin_shared::query::TenantProvider;

    use super::*;

    fn node(id: &str, kind: TopologyNodeKind, health: TopologyHealth, label: &str) -> TopologyNode {
        TopologyNode {
            id: id.into(),
            kind,
            provenance: TopologyNodeProvenance::ExactKubernetesResource,
            label: label.into(),
            health,
            resource: Some(ResourceIdentityView {
                api_version: "v1".into(),
                kind: format!("{kind:?}"),
                namespace: Some("tenant-a".into()),
                name: label.into(),
                uid: Some(id.into()),
            }),
            attributes: Vec::new(),
        }
    }

    fn graph(nodes: Vec<TopologyNode>, edges: Vec<TopologyEdge>) -> TopologyGraph {
        TopologyGraph {
            tenant_name: "tenant-a".into(),
            provider: TenantProvider::Local,
            nodes,
            edges,
        }
    }

    #[test]
    fn groups_prioritize_attention_and_keep_provenance_subtotals() {
        let mut logical = node(
            "database:a",
            TopologyNodeKind::Database,
            TopologyHealth::Ready,
            "database",
        );
        logical.provenance = TopologyNodeProvenance::DatabaseLogicalRepresentation;
        logical.resource = None;
        let model = ExplorerModel::new(graph(
            vec![
                node(
                    "tenant",
                    TopologyNodeKind::Tenant,
                    TopologyHealth::Ready,
                    "tenant-a",
                ),
                node(
                    "machine:a",
                    TopologyNodeKind::Machine,
                    TopologyHealth::Failed,
                    "same-name",
                ),
                node(
                    "machine:b",
                    TopologyNodeKind::Machine,
                    TopologyHealth::Ready,
                    "same-name",
                ),
                logical,
            ],
            Vec::new(),
        ));
        let summaries = model.group_summaries();
        assert_eq!(summaries[0].group, ResourceGroup::Compute);
        assert_eq!(summaries[0].health.failed, 1);
        assert_eq!(
            summaries
                .iter()
                .find(|summary| summary.group == ResourceGroup::Databases)
                .unwrap()
                .provenance
                .database_logical,
            1
        );
        assert_eq!(model.filtered_nodes(&ExplorerFilter::default()).len(), 4);
    }

    #[test]
    fn focus_is_bounded_cycle_safe_and_relationships_keep_direction() {
        let mut nodes = vec![node(
            "tenant",
            TopologyNodeKind::Tenant,
            TopologyHealth::Ready,
            "tenant-a",
        )];
        let mut edges = Vec::new();
        for index in 0..40 {
            nodes.push(node(
                &format!("machine:{index}"),
                TopologyNodeKind::Machine,
                TopologyHealth::Ready,
                "duplicate",
            ));
            edges.push(TopologyEdge {
                id: format!("edge:{index}"),
                source: "tenant".into(),
                target: format!("machine:{index}"),
                kind: TopologyEdgeKind::Owns,
                label: None,
            });
        }
        nodes.push(node(
            "database:expanded",
            TopologyNodeKind::Database,
            TopologyHealth::Ready,
            "expanded database",
        ));
        edges.push(TopologyEdge {
            id: "cycle".into(),
            source: "machine:0".into(),
            target: "tenant".into(),
            kind: TopologyEdgeKind::DependsOn,
            label: Some("cycle".into()),
        });
        let model = ExplorerModel::new(graph(nodes, edges));
        let focused = model.focused_graph(Some("tenant"));
        assert_eq!(focused.nodes.len(), MAX_VISIBLE_NODES);
        let relationship = model.relationship_graph(Some("tenant"), "edge:39");
        assert!(
            relationship
                .nodes
                .iter()
                .any(|node| node.id == "machine:39")
        );
        assert!(relationship.edges.iter().any(|edge| edge.id == "edge:39"));
        let expanded = model.selected_group_graph("tenant", ResourceGroup::Databases);
        assert_eq!(expanded.nodes.len(), MAX_VISIBLE_NODES);
        assert!(expanded.nodes.iter().any(|node| node.id == "tenant"));
        assert!(
            expanded
                .nodes
                .iter()
                .any(|node| node.id == "database:expanded")
        );
        let inspection = model.inspection("tenant").unwrap();
        assert!(inspection.incoming.iter().any(|edge| edge.id == "cycle"));
        assert!(
            inspection
                .outgoing
                .iter()
                .any(|edge| edge.target_id == "machine:0")
        );
        assert_eq!(model.summary_graph().nodes.len(), 3);
    }

    #[test]
    fn filtering_selection_and_identity_handle_missing_namespace() {
        let mut external = node(
            "provider:vmss",
            TopologyNodeKind::ProviderResource,
            TopologyHealth::Unknown,
            "pool",
        );
        external.provenance = TopologyNodeProvenance::ExternalProviderRepresentation;
        external.resource = None;
        let model = ExplorerModel::new(graph(vec![external.clone()], Vec::new()));
        let filtered = model.filtered_nodes(&ExplorerFilter {
            namespace_not_applicable: true,
            ..ExplorerFilter::default()
        });
        assert_eq!(filtered, vec![external.clone()]);
        assert_eq!(
            display_identity(&external)[2],
            ("Namespace".into(), "Not applicable".into())
        );
        assert!(model.inspection("missing").is_none());
    }

    #[test]
    fn exact_kind_namespace_and_synthetic_attention_are_preserved() {
        let exact = node(
            "machine:a",
            TopologyNodeKind::Machine,
            TopologyHealth::Ready,
            "duplicate",
        );
        let mut synthetic = node(
            "database:unavailable",
            TopologyNodeKind::Database,
            TopologyHealth::Degraded,
            "Databases unavailable",
        );
        synthetic.provenance = TopologyNodeProvenance::SyntheticSummary;
        synthetic.resource = None;
        let model = ExplorerModel::new(graph(vec![exact.clone(), synthetic.clone()], Vec::new()));
        let filtered = model.filtered_nodes(&ExplorerFilter {
            kind: Some(TopologyNodeKind::Machine),
            namespace: Some("tenant-a".into()),
            ..ExplorerFilter::default()
        });
        assert_eq!(filtered, vec![exact]);
        let database = model
            .group_summaries()
            .into_iter()
            .find(|summary| summary.group == ResourceGroup::Databases)
            .unwrap();
        assert_eq!(database.count, 0);
        assert_eq!(database.health.degraded, 1);
        assert_eq!(model.attention_nodes(), vec![synthetic]);
    }

    #[test]
    fn operations_remain_fast_at_supported_bound() {
        let mut nodes = vec![node(
            "tenant",
            TopologyNodeKind::Tenant,
            TopologyHealth::Ready,
            "tenant-a",
        )];
        let mut edges = Vec::new();
        for index in 0..1_999 {
            let id = format!("machine:{index}");
            nodes.push(node(
                &id,
                TopologyNodeKind::Machine,
                if index % 9 == 0 {
                    TopologyHealth::Degraded
                } else {
                    TopologyHealth::Ready
                },
                &format!("worker-{index}"),
            ));
        }
        for index in 0..5_000 {
            edges.push(TopologyEdge {
                id: format!("edge:{index}"),
                source: if index % 2 == 0 {
                    "tenant".into()
                } else {
                    format!("machine:{}", index % 1_999)
                },
                target: format!("machine:{}", (index + 1) % 1_999),
                kind: TopologyEdgeKind::Owns,
                label: None,
            });
        }
        let started = Instant::now();
        let model = ExplorerModel::new(graph(nodes, edges));
        let _ = model.group_summaries();
        let _ = model.filtered_nodes(&ExplorerFilter {
            query: "worker-19".into(),
            health: Some(TopologyHealth::Ready),
            ..ExplorerFilter::default()
        });
        let _ = model.focused_graph(Some("machine:19"));
        let _ = model.group_graph(ResourceGroup::Compute);
        let _ = model.inspection("machine:19");
        assert!(started.elapsed().as_secs_f32() < 1.0);
    }
}
