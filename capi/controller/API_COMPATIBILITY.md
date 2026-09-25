# Tenant API compatibility

`tenancy.cnpg-vcluster.io/v1alpha1` is experimental. The repository may make
incompatible changes while it remains the only served and storage version, but
each change must update the CRD, examples, tests, and documentation together.

A `v1alpha1` Tenant is cluster-scoped. Its spec is immutable after creation and
contains Kubernetes version, worker count, database count, Pod CIDR, and
Service CIDR. Unknown fields are rejected by strict apply and the validating
webhook. Canonically equivalent Kubernetes versions, with or without a leading
`v`, are treated as the same immutable value.

Status is controller-owned and may add optional observational fields without
changing spec semantics. Clients must use `metadata.generation`,
`status.observedGeneration`, and the Ready condition's `observedGeneration`
rather than depending on condition order or internal reconciliation progress.
The status currently exposes the endpoint, foundation hash, and exact root
Cluster UID. Tenant-internal resources are disposable with the dedicated
cluster, so status has no tenant-API creation or cleanup checkpoint.
The supported local status command is the compatibility surface for exit
classification.

Deletion is ordinary Kubernetes DELETE guarded by the
`tenancy.cnpg-vcluster.io/finalizer`. Clients must not depend on preparatory
reservations, Leases, filesystem journals, force deletion, or provider
finalizer removal. Finalization deletes the exact recorded CAPI Cluster, waits
for provider descendants and dedicated workers to disappear, then removes
only the exactly owned storage volume, Namespace/credentials, endpoint
allocation, and finalizer, in that order. It does not connect to the tenant API.
Management and host ownership checks remain fail-closed.

The `disposable-cluster-v3` lifecycle epoch removes tenant-API creation
authorization and successful-cleanup status fields, along with the cleanup
catalog. This stored-contract change requires a clean cutover from
`desired-state-v2`; no status migration is supported. Lifecycle epoch changes
scale the old controller to zero and reject existing Tenant/provider/host
residue; status from older epochs is not silently migrated.

A second served version must not be added until conversion behavior, storage
version migration, downgrade behavior, and removal criteria are documented and
covered by conformance tests. Existing objects must never be silently re-read
under different semantics.

The Azure JSON TenantSpec and Python lifecycle are separate interfaces. They do
not imply that the local CRD is served on AKS or that local status/finalizer
semantics apply to CAPZ tenants.
