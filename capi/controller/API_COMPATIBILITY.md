# Tenant API compatibility

`tenancy.cnpg-vcluster.io/v1alpha2` is the only served and stored local
Tenant version. It is experimental; incompatible changes require an explicit
version transition with updated CRD, examples, tests and documentation.
There is no conversion or migration of `v1alpha1` Go-managed objects. The
`rust-operator-v1` lifecycle epoch requires old controller Pods and all
Tenant/provider/host state to be absent before the old CRD/webhook stack is
removed and v1alpha2 installed mutation-disabled. The installer verifies
both `spec.versions` and `status.storedVersions` before enabling creation.

A Tenant is cluster-scoped. Its immutable spec has exactly
`kubernetesVersion`, `workers`, and `databases`. OpenAPI requires all three
fields, numeric three-part version syntax (optional leading `v`), and integer
counts from one to three. CEL constrains the name to a 1-30 character lowercase
DNS label and compares the *whole literal spec* to `oldSelf.spec` on updates.
The controller checks the supported version (`1.36.4`) separately and
normalizes an initial `v` for the canonical spec hash. A `v` spelling change
on an existing object is still prohibited by CEL. Change a spec by ordinary
DELETE and recreate, not by in-place update.

The structural CRD prunes unsupported fields under
`fieldValidation=Warn` (warning returned) or `Ignore` (no warning); only
`fieldValidation=Strict` rejects unknown fields. Repository local clients
request Strict. No validating webhook is installed, so callers must not rely
on Warn/Ignore to reject extra input. Status is controller-owned through its
subresource and may add optional observational fields without changing spec
semantics. It exposes the allocated `slotId`, endpoint, Pod CIDR and Service
CIDR under `status.allocation`, `foundationHash`, the exact `clusterUID`,
phase and conditions. Clients must compare `metadata.generation`,
`status.observedGeneration`, and the Ready condition's observed generation;
do not depend on condition order, cached True conditions, or an internal
reconciliation stage. The supported local status command owns exit
classification.

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
`tenancy.cnpg-vcluster.io/finalizer`, even when creation mutation is disabled.
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

The Azure JSON TenantSpec and Python/CAPZ lifecycle are separate interfaces.
They do not imply that the local CRD is served on AKS or that local
status/allocation/finalizer semantics apply to CAPZ tenants.
