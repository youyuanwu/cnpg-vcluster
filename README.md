# CNPG multi-tenant lab

This repository contains one local CloudNativePG experiment:

- [`capi/`](capi/) uses Cluster API, CABPK, CAPD development resources,
  Kamaji, and the Kamaji control-plane provider. Its interface is `just` from
  inside that directory.

## Cluster API lab

The Cluster API lab creates one kind management cluster, two Kamaji hosted
control planes, six CAPD Docker workers, two isolated Docker-backed storage
profiles, and two tenant-owned three-instance CloudNativePG clusters:

```sh
cd capi
just tools
just prepare-host
just preflight
just create
just verify
just destroy
```

CAPD and the shared-host storage profile are local development mechanisms. The
design preserves a future path to an independently provisioned AKS management
cluster with Kamaji, CAPZ-managed tenant workers, the external Azure cloud
provider, and Azure CSI, but contains no executable Azure implementation.

See [`capi/README.md`](capi/README.md) and
[`capi/docs/high-level-design.md`](capi/docs/high-level-design.md).
