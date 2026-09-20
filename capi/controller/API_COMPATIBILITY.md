# Tenant API compatibility

`tenancy.cnpg-vcluster.io/v1alpha1` is experimental. The repository may make
incompatible changes while it remains the only served and storage version, but
each change must update the CRD, examples, tests, and documentation together.

A second served version must not be added until conversion behavior, storage
version migration, downgrade behavior, and removal criteria are documented and
covered by conformance tests. Existing objects must never be silently re-read
under different semantics.
