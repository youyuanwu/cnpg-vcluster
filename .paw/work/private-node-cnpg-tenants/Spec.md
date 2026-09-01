# Feature Specification: Private Node CNPG Tenants

**Branch**: `feature/private-node-cnpg-tenants`  |  **Created**: 2026-09-01  |  **Status**: Approved for Planning
**Input Brief**: Provide a reproducible local environment with two isolated private-node tenants, each operating an independent three-instance PostgreSQL cluster.

## Overview

A platform engineer needs to create a complete local multi-tenant database
environment on one development workstation. A central control cluster provides
tenant control planes and management, while exactly two tenant clusters each
receive exclusive worker capacity and retain ownership of their networking,
storage, workloads, and database operator.

The environment must demonstrate private-node behavior rather than shared-node
translation. Tenant nodes and runtime resources must exist only in their tenant
views, and the central cluster must not receive synchronized tenant workloads,
volumes, or database custom resources.

Each tenant must independently install and operate its own database operator
and one three-instance PostgreSQL replica set. Repository-owned commands must
create, inspect, verify, and remove the full environment predictably, including
data-path and failover checks. Because the local worker substrate shares one
Docker host, the result is an evaluation environment, not a production
availability or kernel-isolation boundary.

## Objectives

- Reproduce the complete two-tenant environment from repository-owned inputs.
- Give each tenant exclusive worker nodes and independent cluster services.
- Demonstrate that tenant workloads and storage are absent from the central
  cluster.
- Demonstrate independent three-instance PostgreSQL clusters with functioning
  replication, persistence, connectivity, and failover.
- Provide concise operational status and deterministic, scoped teardown.
- State the support and isolation limits of containerized private workers.

## User Scenarios & Testing

### User Story P1 – Create the Complete Local Topology

Narrative: A platform engineer starts from a functioning Docker host and creates
one central control cluster, a management plane, exactly two tenant control
planes, and exclusive worker capacity for each tenant.

Independent Test: Run the documented setup entry point on a clean host and
observe one central cluster, two ready tenant APIs, and the expected exclusive
worker nodes in each tenant.

Acceptance Scenarios:

1. Given the documented prerequisites, when setup completes, then one central
   cluster and exactly two tenant clusters are reachable.
2. Given the two tenant clusters, when nodes are listed in each tenant, then
   every listed worker belongs exclusively to that tenant.
3. Given an unavailable or incompatible private-worker substrate, when setup
   runs, then it fails clearly without substituting shared nodes or independent
   clusters.

### User Story P1 – Verify Tenant Isolation and Ownership

Narrative: A platform engineer confirms that each tenant owns its networking,
storage, workloads, database APIs, and worker nodes while the central cluster
hosts only management and tenant control-plane components.

Independent Test: Run the isolation verification and confirm that tenant
runtime and database resources are present in their tenant APIs and absent from
the central API.

Acceptance Scenarios:

1. Given running tenant workloads, when the central API is inspected, then no
   tenant pods, services, claims, volumes, database custom resources, or tenant
   worker nodes are visible there.
2. Given both tenants, when networking and storage components are inspected,
   then each tenant has its own ready components and default storage class.
3. Given one tenant's kubeconfig, when cluster resources are listed, then only
   that tenant's nodes, database resources, claims, volumes, and data marker are
   visible.

### User Story P1 – Operate Independent PostgreSQL Clusters

Narrative: A database evaluator uses each tenant as an independent Kubernetes
cluster, with its own database operator and one replicated PostgreSQL cluster.

Independent Test: Write a tenant-specific value through each tenant's
read/write endpoint, read it back, trigger a primary transition, and read the
same value again.

Acceptance Scenarios:

1. Given a ready tenant, when its database resources are deployed, then one
   three-instance PostgreSQL cluster becomes healthy.
2. Given both healthy databases, when tenant-specific data is written and read,
   then each tenant returns only its own value.
3. Given a healthy three-instance cluster, when the current primary is
   disrupted, then a replica is promoted and the prior value remains readable.
4. Given persistent claims, when a database pod restarts, then its data remains
   available from its node-local tenant storage.

### User Story P2 – Inspect and Remove the Environment

Narrative: An operator quickly understands component health and can remove only
the resources created by this experiment.

Independent Test: Run the status command, then teardown, and verify that all
named clusters and worker containers are removed while unrelated resources
remain untouched.

Acceptance Scenarios:

1. Given a partially or fully running environment, when status runs, then it
   reports central, tenant, node, networking, storage, operator, and database
   health without changing resources.
2. Given the complete environment, when teardown runs, then tenant workloads,
   tenant clusters, worker nodes, management components, and the named central
   cluster are removed in a safe order.
3. Given repeated teardown, when no managed resources remain, then the command
   completes without deleting unrelated Docker or Kubernetes resources.

### Edge Cases

- A required command is absent or below its supported version: setup stops
  before creating resources and reports the missing prerequisite.
- Docker has insufficient CPU, memory, disk, privileges, or kernel features:
  setup stops with a diagnostic before or during worker creation.
- The management endpoint is unreachable from worker nodes: node joining fails
  with connectivity and service diagnostics.
- One tenant fails while the other succeeds: status identifies the failed
  tenant, setup remains safely retryable, and teardown covers both.
- A worker container cannot satisfy node bootstrap requirements: the workflow
  reports the unsupported lab substrate and does not change tenancy mode.
- A database claim cannot bind: verification reports tenant storage class,
  provisioner, node, claim, and volume state.
- Primary failover exceeds the timeout: verification captures database and
  cluster events and returns failure without claiming success.
- Teardown is interrupted: rerunning it continues from the remaining named
  resources.

## Requirements

### Functional Requirements

- FR-001: The repository shall create exactly one named central Kubernetes
  control cluster on the local container host. (Stories: P1 Create)
- FR-002: The repository shall install a centrally accessible tenant management
  plane on the central cluster. (Stories: P1 Create)
- FR-003: The repository shall create exactly two linked tenant clusters with
  private-node mode enabled from initial creation. (Stories: P1 Create, P1
  Isolation)
- FR-004: Each tenant shall have at least three exclusive worker nodes so its
  three database instances can be spread across three distinct local node
  containers. (Stories: P1 Create, P1 Database)
- FR-005: Setup shall never silently substitute shared nodes, translated host
  workloads, or independent local clusters. (Stories: P1 Create)
- FR-006: Each tenant shall provide its own functional pod networking, service
  routing, DNS, default storage class, dynamic provisioner, claims, and volumes.
  (Stories: P1 Isolation, P1 Database)
- FR-007: The central cluster shall not contain synchronized tenant pods,
  services, claims, volumes, database custom resources, or tenant nodes.
  (Stories: P1 Isolation)
- FR-008: Each tenant shall independently install and own its database operator,
  admission services, RBAC, and database CRDs. (Stories: P1 Isolation, P1
  Database)
- FR-009: Each tenant shall contain exactly one database `Cluster` resource
  configured for three PostgreSQL instances. (Stories: P1 Database)
- FR-010: Database instances shall request tenant-owned persistent storage and
  the three instances shall be distributed across three distinct exclusive
  tenant nodes.
  (Stories: P1 Database)
- FR-011: Verification shall confirm central management health, tenant linkage,
  tenant API availability, and private-node readiness. (Stories: P1 Create, P2)
- FR-012: Verification shall confirm tenant CNI, DNS, service routing, default
  storage, claim binding, and volume ownership. (Stories: P1 Isolation, P2)
- FR-013: Verification shall confirm per-tenant database CRD/operator ownership
  and the absence of those CRDs and resources from the central cluster.
  (Stories: P1 Isolation, P2)
- FR-014: Verification shall write and read distinct data in both tenant
  databases through their read/write services. (Stories: P1 Database)
- FR-015: Verification shall disrupt each tenant's current primary, observe a
  new primary, and confirm the previously written value remains readable.
  (Stories: P1 Database)
- FR-016: A read-only status entry point shall summarize all managed layers and
  provide focused diagnostic commands. (Stories: P2)
- FR-017: Teardown shall remove database resources, tenant control planes,
  joined worker state, exclusive worker containers, management components, and
  the named central cluster in dependency order. (Stories: P2)
- FR-018: Setup and teardown shall be safely retryable and shall target only
  repository-defined names. (Stories: P1 Create, P2)
- FR-019: Direct repository inputs, including downloaded tools, charts, images,
  and manifests, shall use explicit compatible versions. Transitive assets
  supplied by a pinned parent release shall be fixed by that parent and reported
  through runtime version/image inventory. (Stories: P1 Create)
- FR-020: Documentation shall accurately describe topology, operation,
  verification, teardown, resource ownership, and the shared-host/kernel
  limitation. (Stories: P1 Create, P1 Isolation, P1 Database, P2)
- FR-021: Verification shall compare both tenant views and prove that node,
  database, claim, volume, and SQL marker identities are tenant-specific and
  disjoint. (Stories: P1 Isolation, P1 Database)

### Key Entities

- Central Control Cluster: The sole kind cluster hosting management and the two
  tenant control planes.
- Tenant Cluster: One of exactly two isolated Kubernetes API/control-plane
  environments.
- Private Worker: A tenant-exclusive, systemd-capable local node container that
  joins only one tenant.
- Tenant Database Stack: The tenant-owned database CRDs, operator, and one
  three-instance PostgreSQL cluster.
- Tenant Storage: The tenant's provisioner, StorageClass, claims, volumes, and
  node-local backing paths.

### Cross-Cutting / Non-Functional

- Commands shall stop on errors and emit the failing layer and tenant name.
- Every Kubernetes operation shall identify its intended context or kubeconfig.
- Credentials and generated join tokens shall not be committed.
- Runtime state shall be stored in ignored repository-local paths with
  restrictive permissions.
- Default validation timeouts shall be finite and configurable.

## Success Criteria

- SC-001: One setup command creates one central cluster and exactly two reachable
  tenants from a clean documented host. (FR-001, FR-002, FR-003)
- SC-002: Each tenant reports at least three Ready workers, and no worker name is
  shared across tenants or visible in the central cluster. (FR-004, FR-007)
- SC-003: Every tenant database pod runs on that tenant's exclusive workers,
  the three instances use three distinct worker names, and all three instance
  claims bind to tenant-owned volumes. (FR-006, FR-009, FR-010, FR-012)
- SC-004: The central API returns zero tenant CNPG CRDs, `Cluster` resources,
  workloads, claims, and volumes. (FR-007, FR-013)
- SC-005: Both tenant database operators become ready and both three-instance
  clusters report three ready instances within the configured timeout. (FR-008,
  FR-009, FR-011)
- SC-006: Automated SQL checks write and read a different marker in each tenant
  with zero cross-tenant result matches. (FR-014)
- SC-007: In each tenant, automated primary disruption results in a different
  ready primary and successful reading of the original marker. (FR-015)
- SC-008: Status reports all managed layers without mutation and returns
  non-zero when a required layer is unhealthy. (FR-016)
- SC-009: Teardown leaves zero repository-named kind clusters, tenant worker
  containers, or runtime-state artifacts and does not remove an unrelated
  sentinel container. (FR-017, FR-018)
- SC-010: Documentation and configuration name every direct version pin,
  explain the transitive pinning policy, report resolved transitive versions
  and images at runtime, and clearly state that container workers are
  experimental and all containers share one host kernel and failure domain.
  (FR-019, FR-020)
- SC-011: Comparing the two tenant kubeconfigs produces disjoint worker,
  database, claim, volume, and SQL marker identities. (FR-021)
- SC-012: Restarting one replica in each tenant reuses its expected persistent
  claim and volume, returns the instance to readiness, and preserves the
  tenant's data marker. (FR-010, FR-014)

## Assumptions

- The host is Linux with Docker 29.7.2, overlayfs, 16 CPUs, and approximately
  31 GiB RAM available.
- Internet access is available for pinned tool, chart, manifest, and image
  downloads and for the Platform endpoint required by private-node VPN.
- The environment is for local evaluation only and does not need data
  durability after full teardown.
- Three worker containers per tenant are acceptable to demonstrate
  instance-level node separation within the available host resources.
- The supported vCluster-provided CNI, service proxy, and local-path storage
  defaults are preferred over custom tenant add-ons.
- A privileged Ubuntu systemd container is accepted only as an explicitly
  qualified approximation of a supported Linux machine; failure to pass
  private-node bootstrap is a visible unsupported-substrate result, not grounds
  for changing the architecture.
- If Platform Free activation or the required Platform endpoint is unavailable,
  or bounded remediation cannot make the private workers Ready, the repository
  still delivers complete automation and evidence-backed documentation, while
  recording environment-dependent criteria SC-001 through SC-007, SC-011, and
  SC-012 as blocked by activation, the external endpoint, or unsupported
  substrate rather than claiming success.

## Scope

In Scope:

- One local Docker host and one kind central cluster.
- vCluster Platform and exactly two linked private-node tenants.
- Six exclusive private-worker containers, three per tenant.
- Per-tenant networking, DNS, service routing, local dynamic storage, CNPG
  operator/CRDs, and one three-instance PostgreSQL cluster.
- Setup, verification, failover exercise, status, diagnostics, and teardown.
- Updated high-level design and operator-facing usage documentation.

Out of Scope:

- Shared-node vClusters, host-synchronized tenant workloads, and vind clusters.
- Production security, hard multi-tenant kernel isolation, high availability,
  backup/restore, disaster recovery, external load balancers, and performance
  benchmarking.
- Surviving loss of the single Docker host or kernel.
- Cloud node providers, autoscaling, managed CSI, object storage, and
  cross-host networking.

## Dependencies

- Linux Docker host with privileged-container and cgroup support.
- kind 0.33.0 with Kubernetes 1.36.4 for the central cluster, Kubernetes
  1.36.0 for tenant control planes/workers, kubectl 1.36.4, Helm 3.19.0,
  vCluster CLI/chart 0.36.1, vCluster Platform 4.11.2, CloudNativePG 1.30.0,
  PostgreSQL 18.4, and Ubuntu 24.04 worker userspace.
- External image/chart/download endpoints and a Platform URL reachable by
  worker containers.
- Host capacity for the central cluster, six private workers, two tenant
  control planes, two operators, and six PostgreSQL instances.

## Risks & Mitigations

- Container workers are outside vCluster's documented node support matrix:
  bootstrap may fail. Mitigation: use a supported Ubuntu userspace with systemd
  and required privileges, detect failures explicitly, document the boundary,
  never fall back to shared nodes, and record blocked runtime criteria if
  bounded remediation cannot satisfy bootstrap.
- All nodes share one host kernel and runtime: apparent node separation can be
  mistaken for infrastructure fault isolation. Mitigation: state the shared
  failure domain prominently in design and status output.
- Local node storage is immobile: a lost worker can strand its volume.
  Mitigation: test database replication and primary failover, not volume
  relocation, and classify storage as ephemeral.
- Platform endpoint or external downloads may be unavailable: setup cannot join
  nodes. Mitigation: preflight connectivity and preserve diagnostics for retry.
- Resource pressure may destabilize six database instances. Mitigation: use
  small explicit requests, bounded concurrency, readiness waits, and capacity
  checks without reducing tenant or instance counts.
- Destructive automation could affect unrelated resources. Mitigation: use
  fixed prefixes, explicit contexts, ownership labels, and teardown assertions.

## References

- Research: `.paw/work/private-node-cnpg-tenants/SpecResearch.md`
- Existing design: `docs/high-level-design.md`
- vCluster Private Nodes Quick Start:
  <https://www.vcluster.com/docs/vcluster/quick-start/private-nodes>
- vCluster Node Requirements:
  <https://www.vcluster.com/docs/vcluster/deploy/worker-nodes/private-nodes/node-requirements>
- vCluster Private Nodes:
  <https://www.vcluster.com/docs/vcluster/deploy/worker-nodes/private-nodes>
- vCluster AddOns:
  <https://www.vcluster.com/docs/vcluster/configure/vcluster-yaml/deploy>
- CloudNativePG Supported Releases:
  <https://cloudnative-pg.io/docs/devel/supported_releases>
