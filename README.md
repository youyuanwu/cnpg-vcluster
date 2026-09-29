# CNPG multi-tenant lab

This repository contains one Cluster API and CloudNativePG experiment:

- [`capi/`](capi/) uses Cluster API, CABPK, CAPD development resources,
  CAPZ, Kamaji, and the Kamaji control-plane provider. Its interface is
  `just` from inside that directory.

## Cluster API lab

The local profile creates one kind management cluster and accepts explicit
tenant specifications for Kamaji hosted control planes, CAPD Docker workers,
isolated Docker-backed storage, and tenant-owned CloudNativePG clusters:

```sh
cd capi
just cache
just tools
just prepare-host
just preflight
just create-management
just local-tenant-apply config/tenants/examples/local.yaml
just local-tenant-status tenant-example
just local-tenant-delete tenant-example
just destroy
```

Local tenants are declarative `tenancy.cnpg-vcluster.io/v1alpha2` resources
reconciled by a Rust/kube-rs operator. Their immutable specs contain common
`kubernetesVersion` and `workers` fields plus a tagged provider; local
manifests use `provider.type: local` and `provider.databases`. The controller
assigns the endpoint and Pod/Service CIDRs under `status.provider.allocation`.
Change a tenant by deleting and reapplying its manifest. Ready reflects current
Kubernetes conditions and live component health. Ordinary DELETE runs a
fail-closed finalizer that does not require tenant API access. Apply is
asynchronous; repeat `local-tenant-status` until it exits zero.

The provider-discriminated shape is a breaking in-place redesign of
experimental `v1alpha2`; flat `databases` fields, flat local status, and the
former Python Azure tenant runtime are not migrated or converted. Existing
objects must be deleted and recreated, and installation requires a clean
environment. The same Rust manager runs in an explicit `local` or `azure`
provider mode. Azure commands accept the retained JSON client specification,
submit an Azure `Tenant`, and use its generation-aware status and ordinary
finalization. Shared Azure foundation provisioning remains external.
CAPD and the shared-host storage profile remain local development mechanisms;
neither profile is a production hostile-tenant isolation boundary.

See [`capi/README.md`](capi/README.md) and
[`capi/docs/high-level-design.md`](capi/docs/high-level-design.md). The local
operator uses the Rust 1.98.1 toolchain declared in
[`rust-toolchain.toml`](rust-toolchain.toml); no Go tool downloads are needed.
