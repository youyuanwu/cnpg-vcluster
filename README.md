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
reconciled by a Rust/kube-rs operator. Their immutable specs contain only
`kubernetesVersion`, `workers`, and `databases`; the controller assigns the
endpoint and Pod/Service CIDRs. Change a tenant by deleting and reapplying its
manifest. Ready reflects current Kubernetes conditions and live component
health. Ordinary DELETE runs a fail-closed finalizer that does not require
tenant API access. Apply is asynchronous; repeat `local-tenant-status` until
it exits zero. A clean cutover is required from any Go-managed installation;
existing Tenants are not migrated. The Azure profile independently retains
the existing JSON-based `tenant-create`, `tenant-status`, and `tenant-delete`
commands.
CAPD and the shared-host storage profile remain local development mechanisms;
neither profile is a production hostile-tenant isolation boundary.

See [`capi/README.md`](capi/README.md) and
[`capi/docs/high-level-design.md`](capi/docs/high-level-design.md). The local
operator requires system-installed Rust/Cargo; no Go tool downloads are needed.
