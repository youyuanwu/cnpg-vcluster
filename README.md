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
just tools
just prepare-host
just preflight
just create-management
just local-tenant-apply config/tenants/examples/local.yaml
just local-tenant-status tenant-example
just local-tenant-delete tenant-example
just destroy
```

Local tenants are declarative `tenancy.cnpg-vcluster.io/v1alpha1` resources.
Their specifications are immutable; change a tenant by deleting and
reapplying its manifest. The Azure profile independently retains the existing
JSON-based `tenant-create`, `tenant-status`, and `tenant-delete` commands.
CAPD and the shared-host storage profile remain local development mechanisms;
neither profile is a production hostile-tenant isolation boundary.

See [`capi/README.md`](capi/README.md) and
[`capi/docs/high-level-design.md`](capi/docs/high-level-design.md).
