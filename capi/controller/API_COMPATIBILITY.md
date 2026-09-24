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
rather than depending on condition order or a particular reconciliation stage.
The supported local status command is the compatibility surface for exit
classification.

Deletion is ordinary Kubernetes DELETE guarded by the
`tenancy.cnpg-vcluster.io/finalizer`. Clients must not depend on preparatory
reservations, Leases, filesystem journals, force deletion, or provider
finalizer removal. A newer controller must continue to understand every
teardown checkpoint it may encounter in stored `v1alpha1` status, or provide an
explicit migration before rollout.

A second served version must not be added until conversion behavior, storage
version migration, downgrade behavior, and removal criteria are documented and
covered by conformance tests. Existing objects must never be silently re-read
under different semantics.

The Azure JSON TenantSpec and Python lifecycle are separate interfaces. They do
not imply that the local CRD is served on AKS or that local status/finalizer
semantics apply to CAPZ tenants.
