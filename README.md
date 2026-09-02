# CNPG vCluster Experiments

The private-node vCluster lab is contained in [`vcluster/`](vcluster/).
It creates one kind control cluster and two isolated vCluster tenants, each
running its own three-instance CloudNativePG cluster.

See [`vcluster/README.md`](vcluster/README.md) for requirements, architecture,
setup, verification, and teardown instructions.

The root Makefile delegates lab commands to the subdirectory, so existing
commands continue to work:

```sh
make create
make verify
make destroy
```
