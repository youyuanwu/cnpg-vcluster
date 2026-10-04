# Tenant and database catalog API compatibility

The installed cluster-scoped Tenant CRD serves and stores only
`tenancy.cnpg-vcluster.io/v1alpha4`. Its immutable spec has a DNS-label name,
supported three-part Kubernetes version, one to three workers and one tagged
`provider.type: local|azure`. Neither provider carries `databases`; Tenant
creation creates no PostgreSQL workload. Structural OpenAPI/CEL enforces
immutable spec and provider shape; `fieldValidation=Strict` rejects unknown
fields, while Warn/Ignore may prune them. Repository clients use Strict. A
provider-mode mismatch is unsupported, not a request to adopt the Tenant
([Tenant API](src/api.rs#L15-L52);
[generated CRD](config/crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml#L1-L75)).

Each Tenant owns exactly one namespaced
`tenancy.cnpg-vcluster.io/v1alpha1` `TenantDatabaseCatalog`, initially empty
in `tenant-db-<tenant>`. Its spec fixes `tenantName` and `tenantUID`; the
catalog Kubernetes UID lives in metadata. Spec map keys are immutable logical UUIDs,
different for a deleted-and-recreated display name. Entry names are unique
1-30-character lowercase DNS labels, instance counts are integers from one
through three, and all entries, including deleting entries, count against
the maximum of three. `closed` and `deleting` only advance to true. A status
subresource contains controller-owned per-entry phase, identities,
observation, create intents and terminal proof. OpenAPI/CEL rejects
reassigning an entry, reopening a catalog, or removing an entry without
its exact terminal evidence
([catalog API](../database-controller/src/api.rs#L11-L107);
[catalog transitions](../database-controller/src/api.rs#L231-L330);
[CEL rules](../database-controller/src/api.rs#L403-L459)).

**Concurrency and readiness:** Admin and the Tenant controller conditionally
update the catalog spec with live UID/resourceVersion; the independent
database-controller writes `/status` and removes proven terminal entries.
A conflict demands a fresh exact read; an ambiguous write is never
automatically replayed. The Tenant controller records
`status.catalogCreateIntent` and the namespace identities before catalog
CREATE and publishes `status.databaseCapability` with namespace UID,
catalog UID and optional Azure storage namespace UID. An unknown CREATE
outcome retains the finalizer and cannot be converted into absence by an
immediate NotFound. Tenant infrastructure Ready is independent of catalog
capability and entry status
([Tenant catalog creation](src/reconcile/mod.rs#L103-L209);
[Tenant catalog drain](src/reconcile/mod.rs#L385-L424);
[database-controller identity](../database-controller/src/reconcile.rs#L62-L155)).

**Deletion:** Tenant DELETE first closes the catalog and marks every entry
deleting with a UID/resourceVersion patch. The database-controller verifies
per-UID workload, credential, local subtree or Azure disk absence before
terminal status and spec removal. Only an empty catalog and status can lose
the catalog finalizer; provider infrastructure finalization follows. Foreign
or replacement resources are never adopted or deleted. No manual finalizer
stripping is part of normal recovery
([catalog drain](../database-runtime/src/catalog_runtime.rs#L277-L409);
[Azure disk cleanup](../database-controller/src/finalize/azure.rs#L20-L160)).

**Breaking experimental migration:** The former v1alpha3
`provider.databases`/implicit `capi-postgres` contract is not converted.
Normally delete old Tenants and independently verify legacy workloads and
storage removed before a clean, create-locked in-place storage-version
transition. The former v1alpha2-to-v1alpha3 transition is historical, not
the installed contract. A draft `TenantDatabase` CRD from an abandoned
experiment is never automatically retired: its mere presence blocks the
installer before mutation. To authorize its removal separately, first prove
all served versions empty, every in-flight CREATE outcome terminal across
API servers, and legacy workload/storage/admission cleanup. There is no
automatic adoption, data migration, or rollback of an incompatible old
object into a new catalog
([legacy CRD guard](../scripts/lib/database_controller.py#L324-L335)).

Temporary Tenant and catalog CREATE-deny policies protect the cutover. The
installer checks exact policies/bindings and effective denials, releases only
Tenant CREATE for one UID-bound empty bootstrap catalog probe, checks the
database-controller's exact observation and normal probe cleanup, then
releases general catalog CREATE. Uncertain or interrupted probes preserve
the owner-only record and fences. This is **not** a permanent database
admission webhook, reservation or quota
([installer cutover](../scripts/lib/controller.py#L761-L803);
[local unknown-outcome recovery](../scripts/lib/controller.py#L1069-L1216)).
The managed Azure API cannot use the local kind management-container restart
protocol; it stays fenced until an independent terminal proof is available.

The public Admin envelope uses `schemaVersion: 5`. Clients must switch from
the singular local-only query route to
`GET/POST /api/v1/tenants/{name}/databases`,
`DELETE /api/v1/tenants/{name}/databases/{uid}` and
`POST /api/v1/tenants/{name}/databases/{uid}/query`, supplying the displayed
catalog UID, logical UID and exact observed instance UID as appropriate.
The retained singular route rejects catalog-capable Tenants; do not use it
to infer a default database. Browser mutations require the same-authority
local/IP Origin and unsafe-request header; authenticated service-proxy
requests without Origin remain possible
([routes](../admin/shared/src/routes.rs#L1-L14);
[catalog DTOs](../admin/shared/src/catalog.rs#L6-L62);
[handlers](../admin/server/src/app.rs#L151-L238)).

The Azure JSON `TenantSpec` is still a schema-1 CLI input, translated to
v1alpha4, not a second lifecycle authority. New served versions require a
separately designed conversion, storage migration, downgrade and removal
contract with conformance tests; never silently reinterpret retained state.
