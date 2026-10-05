# Rust Tenant and database-controller contracts

The Tenant manager and independent database-controller are two separately
packaged, leader-elected Rust binaries in the root Cargo workspace. Both
are pinned to Rust 1.98.1 and independently checked against a 12,000-line
production-Rust ceiling. Run these commands from the repository root after `just cache`
(or `just cache-refresh` when an explicit online provenance refresh is
required) prepares the pinned tools and images, then run `just
controller-fetch` to prepare the locked Rust workspace dependencies:

```sh
just controller-verify
just controller-lint
just controller-test
just controller-metrics
just controller-build
just database-controller-verify
just database-controller-lint
just database-controller-test
just database-controller-metrics
just database-controller-build
```

Both `*-verify` commands compare generated CRDs, RBAC and management
manifests without silently rewriting them. The Tenant generator serves
v1alpha4; the database-controller generator serves the v1alpha1
`TenantDatabaseCatalog`. The old per-database `TenantDatabase` CRD,
admission webhook and gate/quota deployments are not installation inputs
([Tenant generator](src/bin/generate.rs#L1-L65);
[catalog generator](../database-controller/src/bin/generate.rs#L1-L85)).
See [API_COMPATIBILITY.md](API_COMPATIBILITY.md) for the breaking
v1alpha3/implicit-cluster cutover and v5 Admin client contract.

## Tenant controller

One deployment starts with `--provider=local|azure`, never both. Local mode
uses Docker, the checksum-verified schema-3 local foundation and an ordered
allocation Lease; Azure mode uses the approved Azure provider and network
allocation ConfigMaps and delegates cloud mutation to CAPZ/ASO. Both
validate UID, resourceVersion, generation, literal spec and deletion
timestamp on Tenant status/finalizer writes; general status conflicts may
retry only after an exact reread, and finalizer conflicts requeue. Provider
mismatches do not acquire a new finalizer
([reconciliation](src/reconcile/mod.rs#L482-L570);
[status writes](src/status.rs#L15-L102)).

The Tenant controller records exact catalog create intent and namespace
identity before catalog CREATE, and persists observed catalog UID in
`status.databaseCapability`. This capability is distinct from Tenant
infrastructure Ready. A pending or ambiguous CREATE is not proof of absence.
On Tenant deletion it closes the catalog, marks all entries deleting,
waits for their status and workloads to disappear and removes the catalog
before proceeding to the provider's infrastructure finalization
([catalog creation](src/reconcile/mod.rs#L103-L209);
[drain](../database-runtime/src/catalog_runtime.rs#L277-L409)).
It never unilaterally removes a database-controller entry or releases a
Tenant finalizer while a disk or create outcome is uncertain.

Generated management-resource inventory declares exact API identity, scope,
ownership and watch routing; both Rust and Python use it for provider
permissions, installation checks and deletion evidence. The local and
Azure RBAC roles have the same installed name in **different** clusters,
with distinct provider-only verbs. Bootstrap Roles and RoleBindings validate
administrative content; static resources otherwise use exact identity
checks, while dynamic roots use UID/resourceVersion-bound apply
([management catalog](../database-runtime/src/management.rs#L1-L93);
[resource application](src/reconcile/objects.rs#L77-L191)).

## Database-controller

The catalog spec is the resourceVersion compare-and-swap point shared by
Admin additions and the Tenant controller's closure. Logical UUID keys
remain immutable across replacement; a deleting entry continues to consume
one of three slots until the database-controller proves terminal absence
and removes it. The database-controller alone writes catalog `/status`:
per-entry phase, identity, instance topology, bounded conditions, create
intents (`Planned`, `Issued`, `Rejected`, `Observed`) and finalization
receipts. Kubernetes OpenAPI/CEL validates transition invariants; exact
Tenant/catalog/namespace UID checks reject foreign resources
([catalog type](../database-controller/src/api.rs#L11-L107);
[transition checks](../database-controller/src/api.rs#L231-L330);
[identity validation](../database-controller/src/reconcile.rs#L62-L155)).

Local entries each use a UID-derived CNPG namespace, at least 1 GiB static
hostPath PV/PVC per ordinal, and fd-relative no-follow access to the exact
subtree of the shared Tenant Docker volume. Deleting one entry does not
remove the shared volume or sibling paths
([local adapter](../database-controller/src/reconcile/local.rs#L19-L25);
[path safety](../database-controller/src/local_path.rs#L1-L109)).
Azure entries use a separate Tenant storage namespace, per-ordinal ASO
Disk and static Azure Disk CSI PV/PVC binding (4 GiB StandardSSD_LRS
each). The pinned database-controller workload identity reads/deletes only
the recorded ARM IDs, and requires direct ARM NotFound before terminal receipt.
Unresolved CREATE or ARM responses block removal
([Azure adapter](../database-controller/src/reconcile/azure.rs#L26-L52);
[disk finalization](../database-controller/src/finalize/azure.rs#L20-L160)).

The manager exposes `/healthz`, `/readyz` and an exact observer receipt
used by staged cutover. It cannot acknowledge an unrelated or changed
catalog UID/resourceVersion as ready; a controller restart resumes durable
catalog status, not an in-memory deletion journal
([manager](../database-controller/src/bin/manager.rs#L35-L140)).

## Admin and operational boundary

Admin exact-reads the current Tenant and catalog for every spec mutation.
Add also requires Tenant readiness, database capability and a current
database-controller rollout; delete remains possible when those are
unavailable. Only queries validate provider-specific Tenant credential
bindings and per-entry database credentials. Admin has no catalog `/status`
or direct disk deletion permission. Schema v5 sends catalog UID for add,
catalog and logical UID plus name confirmation for delete, and additionally
a current Pod UID for unsafe SQL. The web UI never receives raw credentials
([Admin routes](../admin/server/src/app.rs#L151-L238);
[catalog reads](../admin/server/src/source/catalog.rs#L65-L136);
[spec replacements](../admin/server/src/source/catalog.rs#L362-L451);
[query credentials](../admin/server/src/source/catalog.rs#L577-L653)).

The local installer fences Tenant and catalog CREATE during incompatible
cutover and records an owner-only probe if Tenant CREATE could have been
issued. If the outcome is unknown, leave fences and record intact; only
the proven local kind container-restart protocol can settle that probe.
Azure stays fenced pending independent terminal proof. Neither controller
nor `break-glass` may be used to erase an uncertain identity or force
release ([recovery](../scripts/lib/controller.py#L1069-L1216)).

The offline local three-by-three E2E passed with exact entry replacement,
stale UID rejection, SQL marker persistence and host cleanup. Azure
credentialed nine-disk destructive proof and real browser/service-proxy
agreement are **unmet release-acceptance gates**; generated contracts and
offline tests do not replace either live proof.
