---
date: 2026-09-01T22:24:17+00:00
git_commit: fe4ce55
branch: feature/private-node-cnpg-tenants
repository: cnpg-vcluster
topic: "Private Node CNPG Tenants"
tags: [research, codebase, documentation, automation]
status: complete
last_updated: 2026-09-01
---

# Research: Private Node CNPG Tenants

## Research Question

Where does the repository currently define the local CloudNativePG/vCluster
experiment, what assumptions and implementation shape does it record, and what
repository infrastructure exists for implementing and validating the approved
private-node topology?

## Summary

The repository is a documentation-only baseline. Its sole design document
describes one shared-node vCluster whose workloads, services, claims, and
volumes are translated into a kind host cluster. It records the desired
separation between declarative configuration and orchestration, but none of
those directories, scripts, manifests, entry points, or tests currently exist.

The design already locates the CNPG operator and CRDs inside a tenant API and
defines a three-instance PostgreSQL lifecycle test. The private-node
specification replaces the document's shared-node scheduling, host storage,
resource translation, and single-tenant assumptions.

## Documentation System

- **Framework**: Plain Markdown; no documentation generator configuration is
  present.
- **Docs Directory**: `docs/`
- **Navigation Config**: N/A.
- **Style Conventions**: A single high-level design uses sentence-case
  headings, prose, bullet lists, tables, numbered flows, fenced Mermaid, and
  linked upstream references (`docs/high-level-design.md:1-349`).
- **Build Command**: N/A.
- **Standard Files**: `docs/high-level-design.md` and `LICENSE`; no README,
  CHANGELOG, or CONTRIBUTING file is present.

## Verification Commands

- **Test Command**: None exists.
- **Lint Command**: None exists.
- **Build Command**: None exists.
- **Type Check**: None exists.
- **Current generic check**: `git diff --check`.

## Detailed Findings

### Existing Experiment Definition

- The document defines a repeatable local CNPG evaluation hosted by kind and
  explicitly classifies it as development/evaluation rather than production
  (`docs/high-level-design.md:7-15`).
- Existing goals place PostgreSQL pods on kind through shared nodes, bind claims
  through host storage, and expose translated resources for inspection
  (`docs/high-level-design.md:17-26`).
- The current architecture installs CNPG in the tenant API but translates the
  operator's generated pods, services, secrets, and claims into a host namespace
  (`docs/high-level-design.md:37-53`).
- The Mermaid diagram contains one tenant and routes logical PostgreSQL
  instances through the syncer to kind nodes and host storage
  (`docs/high-level-design.md:55-91`).
- The resource ownership table identifies CRDs and the `Cluster` as
  tenant-only, while operator workloads, PostgreSQL workloads, claims, and
  volumes are represented or owned on the host (`docs/high-level-design.md:93-101`).

### Existing Host and Tenant Assumptions

- Docker is the selected local runtime, while alternative runtimes are deferred
  (`docs/high-level-design.md:103-109`).
- The current host topology contains one control-plane and two kind workers;
  the host is assigned workload scheduling, DNS, networking, and default
  storage responsibilities (`docs/high-level-design.md:111-128`).
- The document records node-local volume behavior and the distinction between
  database replication and volume mobility (`docs/high-level-design.md:130-132`).
- The current vCluster section explicitly selects shared nodes, enables default
  synchronization, and leaves the tenant scheduler disabled
  (`docs/high-level-design.md:134-150`).

### Existing CloudNativePG Shape

- CNPG CRDs, RBAC, admission webhooks, and operator resources are assigned to
  the tenant API (`docs/high-level-design.md:152-155`).
- The database shape is three PostgreSQL instances, small persistent claims, a
  pinned operand image, default replication, and no backup, monitoring,
  tablespaces, or separate WAL volume (`docs/high-level-design.md:157-164`).
- The document states that three instances do not provide local infrastructure
  high availability because the nodes share one workstation and runtime
  (`docs/high-level-design.md:166-168`).
- Client validation uses an in-cluster PostgreSQL client and optional
  port-forwarding, with credentials read from generated secrets rather than
  committed manifests (`docs/high-level-design.md:170-180`).

### Existing Lifecycle and Diagnostics

- The recorded flow creates kind, starts one tenant, installs CNPG, creates one
  database, translates runtime resources to kind, and returns status to the
  tenant (`docs/high-level-design.md:182-200`).
- Bootstrap, CNPG installation, database lifecycle, and teardown are described
  as four phases (`docs/high-level-design.md:202-243`).
- The database lifecycle includes readiness, claim binding, SQL write/read,
  replica restart, primary disruption, post-failover read, and scaling
  (`docs/high-level-design.md:224-236`).
- Existing observability focuses on tenant CNPG state plus translated host
  resources and syncer logs (`docs/high-level-design.md:245-265`).
- Existing risks cover local capacity, vCluster API compatibility, ephemeral
  storage, node-local volumes, context confusion, floating versions, and
  teardown leftovers (`docs/high-level-design.md:267-277`).

### Existing Implementation Boundaries

- Host-installed CNPG synchronization and vind are documented alternatives
  rather than the selected architecture (`docs/high-level-design.md:279-294`).
- The expected repository shape separates `config/`, `manifests/`, and
  `scripts/`, with optional top-level task entry points
  (`docs/high-level-design.md:303-324`).
- No files from that expected shape exist in the current repository; the
  implementation must introduce the first executable automation and
  configuration.
- The existing acceptance criteria cover reproducible setup, tenant-only CNPG
  CRDs, operator and three-instance readiness, claim binding, SQL access,
  failover, resource correlation, and scoped teardown
  (`docs/high-level-design.md:326-339`).

## Code References

- `docs/high-level-design.md:7-35` - Purpose, goals, and non-goals.
- `docs/high-level-design.md:37-101` - Shared-node architecture and ownership.
- `docs/high-level-design.md:103-180` - Runtime, host, vCluster, CNPG, and
  client components.
- `docs/high-level-design.md:182-243` - Resource flow and experiment phases.
- `docs/high-level-design.md:245-277` - Diagnostics and risks.
- `docs/high-level-design.md:279-324` - Alternatives and expected repository
  structure.
- `docs/high-level-design.md:326-349` - Acceptance criteria and references.

## Architecture Documentation

The existing document is both the architecture record and operator-facing
experiment description. It separates logical components, traces resource
ownership and control flow, defines lifecycle phases, and ends with acceptance
criteria and upstream references. There is no separate README or command
reference.

## Open Questions

- The final Platform exposure mechanism and private-node container bootstrap
  adjustments require runtime validation because the repository contains no
  prior automation or environment state.
