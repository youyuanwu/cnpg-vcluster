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
just tenant-create local config/tenants/examples/local.json
just tenant-status local tenant-example
just tenant-delete local tenant-example local/tenant-example
just destroy
```

The Azure profile independently provisions an AKS management foundation and
uses the same specification-driven create, status, and delete interface for
Kamaji control planes and CAPZ-managed VMSS workers. CAPD and the shared-host
storage profile remain local development mechanisms; neither profile is a
production hostile-tenant isolation boundary.

See [`capi/README.md`](capi/README.md) and
[`capi/docs/high-level-design.md`](capi/docs/high-level-design.md).
