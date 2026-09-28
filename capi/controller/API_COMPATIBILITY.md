# Tenant API compatibility

`tenancy.cnpg-vcluster.io/v1alpha2` is the only served and stored Tenant
version. It is experimental and was redesigned in place from the earlier flat
local shape to the provider-discriminated contract described below. This is a
breaking change: there is no conversion or migration for old flat `v1alpha2`
objects or `v1alpha1` Go-managed objects. Existing objects must be deleted and
recreated, and the current installer requires unsupported legacy state to be
removed manually. It verifies both `spec.versions` and
`status.storedVersions`, then starts the manager with one immutable foundation
snapshot and accepted configuration identity. Future incompatible changes
require an explicit version transition rather than another in-place redesign.

A Tenant is cluster-scoped. Its immutable spec has common
`kubernetesVersion` and `workers` fields plus exactly one tagged `provider`.
The local variant contains `type: local` and `databases`; the Azure variant
contains `type: azure`, `podCIDR`, and `serviceCIDR`. OpenAPI requires numeric
three-part version syntax (optional leading `v`), integer counts from one to
three, provider-specific fields, canonical IPv4 Azure networks, non-overlap,
and an Azure Service CIDR no smaller than `/28`. CEL constrains the name to a
1-30 character lowercase DNS label and compares the *whole literal spec* to
`oldSelf.spec` on updates. The controller checks the supported version
(`1.36.4`) separately and normalizes an initial `v` for the canonical spec
hash. A `v` spelling change on an existing object is still prohibited by CEL.
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
`status.provider.clusterUID`. Azure status currently contains only
`status.provider.type: azure`. Clients must compare `metadata.generation`,
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

Python resolves the tracked slot catalog and publishes a checksum-verified
schema-3 foundation. One non-expiring namespaced allocation Lease claims the
endpoint/Pod CIDR/Service CIDR tuple. Its exact name and markers bind Tenant
UID, canonical spec hash, foundation hash and slot identity; a restart can
recover a claim created before status publication. A missing, malformed,
foreign, or changed status-bound claim fails closed during creation and
nonterminal deletion. Leader election uses a distinct renewable Lease.

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

The CRD admits structurally valid Azure provider intent, but the installed
manager has no Azure lifecycle implementation. It reports
`ProviderUnsupported`, does not add the controller finalizer, and makes no
local or Azure lifecycle calls. Once an unsupported object without that
finalizer is deleting, reconciliation is a read-only no-op. If an unsupported
object carries the controller finalizer, the controller retains it, makes no
provider calls, and reports `ProviderFinalizerUnsupported` with phase `Failed`
before deletion or `Deleting` during deletion.

The Azure JSON TenantSpec and Python/CAPZ lifecycle remain separate interfaces.
The CRD placeholder does not imply that it is used on AKS or that local
status, allocation, or finalizer semantics apply to CAPZ tenants.
