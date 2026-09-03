# CNPG multi-tenant labs

This repository contains two independent local CloudNativePG experiments:

- [`vcluster/`](vcluster/) uses vCluster Free. The existing root Makefile
  remains its interface.
- [`kamaji/`](kamaji/) uses the public Kamaji edge release with no account or
  activation. Its interface is `just` from inside that directory.

## vCluster lab

The vCluster lab creates one kind control cluster and two isolated vCluster
tenants, each running a three-instance CloudNativePG cluster. Existing root
commands remain vCluster-only:

```sh
make create
make verify
make destroy
```

See [`vcluster/README.md`](vcluster/README.md) for its requirements and
activation details.

## Kamaji lab

The Kamaji lab creates one kind management cluster, two hosted tenant control
planes, six exclusive container workers, and two tenant-owned three-instance
CloudNativePG clusters:

```sh
cd kamaji
just tools
just prepare-host
just create
just verify
just destroy
```

See [`kamaji/README.md`](kamaji/README.md) for its experimental worker,
licensing, verification, and teardown boundaries.
