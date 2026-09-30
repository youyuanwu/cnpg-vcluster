# Tenant API compatibility

`tenancy.cnpg-vcluster.io/v1alpha3` is the only served and stored Tenant
version. It intentionally replaces experimental `v1alpha2`; there is no
conversion or migration. Existing objects must complete ordinary deletion
before cutover. The installer denies new Tenant creation, verifies empty
Tenant/provider inventories around old-controller shutdown, applies an
in-place dual-version CRD with `v1alpha2` no longer served, rechecks emptiness,
advances `status.storedVersions`, installs the single v1alpha3 generation,
verifies the allocator-capable controller, and then removes the create lock.
A request admitted through a lagging API server is retained and blocks
completion rather than being deleted by CRD replacement. The installer
verifies both `spec.versions` and `status.storedVersions`.

A Tenant is cluster-scoped. Its immutable spec has common
`kubernetesVersion` and `workers` fields plus exactly one tagged `provider`.
The local variant contains `type: local` and `databases`; the Azure variant
contains only `type: azure`. OpenAPI requires numeric three-part version syntax
(optional leading `v`), integer counts from one to three, and provider-specific
fields. Azure networks come from the separately approved allocation catalog.
CEL constrains the name to a
1-30 character lowercase DNS label and compares the *whole literal spec* to
`oldSelf.spec` on updates. Each controller checks its configured supported version separately and
normalizes an initial `v` for the canonical spec hash. A `v` spelling change
on an existing object is still prohibited by CEL.
Change a spec by ordinary DELETE and recreate, not by in-place update.

The structural CRD prunes unsupported fields under
`fieldValidation=Warn` (warning returned) or `Ignore` (no warning); only
`fieldValidation=Strict` rejects unknown fields. Repository local clients
request Strict. No validating webhook is installed, so callers must not rely
on Warn/Ignore to reject extra input. Status is controller-owned through its
subresource and may add optional observational fields without changing spec
semantics. Common status contains phase, conditions, and observed generation.
Local status exposes the allocated `slotId`, endpoint, Pod CIDR and Service
CIDR under `status.provider.allocation`, plus
`status.provider.foundationHash` and the exact
`status.provider.clusterUID`. Azure status contains the exact immutable
foundation/specification binding, endpoint, management UIDs, kubeconfig
UID/hash, VMSS and Node identities, add-on identities, provider descendants,
deletion barriers, and `networkAllocation` with slot, CIDRs, catalog
UID/hash, and Lease name/UID. Clients must compare `metadata.generation`,
`status.observedGeneration`, and the Ready condition's observed generation;
do not depend on condition order, cached True conditions, or an internal
reconciliation stage. A provider/status discriminator mismatch is invalid
durable identity and is reported as `OwnershipInvalid`; it is never repaired
across providers. The supported local status command owns exit classification.

Every status and finalizer write is an exact merge patch containing the
observed UID and current resourceVersion. The controller revalidates UID,
generation, literal spec, and deletion timestamp before writing and rejects a
replacement response. General status conflicts may retry only after a fresh
exact read; finalizer conflicts wait for another reconciliation pass.
Unchanged status/finalizer state is a no-op, and clearing allocation emits an
explicit JSON `null`.

Python resolves the tracked local slot catalog and publishes a
checksum-verified schema-3 foundation. One non-expiring namespaced allocation Lease claims the
endpoint/Pod CIDR/Service CIDR tuple. Its exact name and markers bind Tenant
UID, canonical spec hash, foundation hash and slot identity; a restart can
recover a claim created before status publication. A missing, malformed,
foreign, or changed status-bound claim fails closed during creation and
nonterminal deletion. Leader election uses a distinct renewable Lease.

Azure uses the same durable claim pattern with approved
`tenant-azure-allocation` content and `tenant-azure-slot-*` Leases. Catalog
validation covers every slot pair, reserved management networks, active-claim
network overlap, and exact operator-approved hash. Existing recorded
allocations remain observable and finalizable when the current catalog is
missing or invalid; new allocation and repair writes fail closed.

Static tenant resources are created when absent but are not continuously
repaired or generically content-audited. Existing resources must retain exact
Tenant ownership markers; bootstrap Roles and RoleBindings also retain
explicit content validation because they establish administrative access.
Dynamic CAPI roots and the CNPG database `Cluster` retain targeted,
identity-bound repair. Foundation and root Cluster bindings precede external
mutation and root replacement is refused after its UID is recorded.

Deletion is ordinary Kubernetes DELETE guarded by
`tenancy.cnpg-vcluster.io/finalizer`, even when creation-only foundation
validation is blocked.
No tenant API access or tenant-resource cleanup checkpoint is needed: the
dedicated cluster's contents are disposable. The finalizer verifies
management/host/provider/Lease ownership from live reads; deletes the exact
recorded CAPI Cluster and residual provider roots with UID/resourceVersion
preconditions; waits for descendants and CAPD workers/load balancer; removes
the exactly owned volume, Namespace/credentials, then allocation Lease; and
removes itself last. Inspection uncertainty retains the finalizer. Only after
authoritative absence of *all* old external residue can an absent old Lease or
a successor-owned Lease count as an already-completed release; the successor
claim is never modified. No manual finalizer stripping is part of normal
recovery.

A second served version must not be added until conversion behavior, storage
version migration, downgrade behavior and removal criteria are documented and
covered by conformance tests. Existing objects must never be silently re-read
under different semantics.

The manager starts with exactly one provider implementation. A local Tenant in
Azure mode, or an Azure Tenant in local mode, reports `ProviderUnsupported`
without acquiring a new finalizer. Azure mode reconciles the Tenant through
Kubernetes APIs only and delegates Azure mutation to CAPZ/ASO. Its finalizer
uses exact UID/resourceVersion preconditions and durable status identities;
external Python proof is observational and does not replace finalization.

The Azure JSON TenantSpec remains a client input compatibility format. It is
translated to the CRD and is not a separate Python lifecycle. Existing
filesystem Azure identity/journal state is not migrated or adopted.
