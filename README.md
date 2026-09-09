# CNPG multi-tenant labs

This repository contains two independent local CloudNativePG experiments:

- [`kamaji/`](kamaji/) uses the public Kamaji edge release with no account or
  activation. Its interface is `just` from inside that directory.
- [`capi/`](capi/) uses Cluster API, CABPK, CAPD development resources,
  Kamaji, and the Kamaji control-plane provider. Its interface is `just` from
  inside that directory.

## Kamaji lab

The Kamaji lab creates one kind management cluster, two hosted tenant control
planes, six exclusive container workers, and two tenant-owned three-instance
CloudNativePG clusters:

```sh
cd kamaji
just tools
just prepare-host
just preflight
just create
just verify
just destroy
```

See [`kamaji/README.md`](kamaji/README.md) for its experimental worker,
licensing, verification, and teardown boundaries.

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
