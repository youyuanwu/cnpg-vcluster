# CNPG Private-Node vCluster Lab

This directory contains a local two-tenant CloudNativePG environment for one
Docker host:

- One kind control cluster.
- vCluster Platform and two linked tenant control planes on kind.
- Three exclusive private worker containers per tenant.
- Tenant-owned networking, storage, CNPG operator/CRDs, and one three-instance
  PostgreSQL cluster per tenant.
- No tenant workload, Service, PVC, PV, or CNPG resource synchronization into
  kind.

See [the high-level design](docs/high-level-design.md) for topology and
isolation details.

## Requirements

- Linux Docker Engine with privileged-container and cgroup v2 support.
- At least 12 CPUs, 24 GiB memory, and 30 GiB free Docker storage.
- Internet access to GitHub, Kubernetes, Helm, GHCR, vCluster charts,
  `admin.loft.sh`, and the Platform Loft Router endpoint.
- A browser and email account for one-time vCluster Platform Free activation.
  Private Nodes are a Free-tier feature, not an unlicensed OSS feature.

The validated host had Docker 29.7.2, overlayfs, 16 CPUs, and about 31 GiB RAM.
All other command-line tools are downloaded into `vcluster/.tools/` with
pinned checksums when commands are run from the repository root.

## Create

```sh
make create
```

On the first run, Platform may require Free-tier activation. If setup reports
`platform-free-tier-activation-required`:

1. Open the Platform URL shown in the error.
2. Log in as `admin`; the generated password is in
   `vcluster/.runtime/credentials/platform-admin-password` when running from
   the repository root.
3. Complete the upstream Free activation flow.
4. Run `make create` again.

Setup fails rather than using shared nodes, unlinked tenants, vind, or the
vCluster Docker driver.

Use `SKIP_CNPG=1 make create` to stop after both tenants and their private
workers are ready.

## Inspect and verify

```sh
make status
make diagnose
make diagnose TENANT=tenant-a
make verify
```

`make verify` writes distinct tenant markers, checks tenant/central isolation,
restarts a replica, confirms PVC reuse, deletes the current primary, waits for
a different primary, and rereads the marker.

Generated kubeconfigs, Platform credentials, join scripts, and logs live under
`vcluster/.runtime/` with restrictive permissions when commands are run from
the repository root. They are ignored by Git.

## Test

```sh
make test-static
make test-e2e
```

The end-to-end test exercises clean-state status, preflight failure, creation,
idempotent creation, verification, repeated teardown, and preservation of an
unrelated sentinel container. If Platform Free activation is unavailable, it
validates the fail-closed blocker and teardown path, writes
`vcluster/.tools/cache/e2e-last-result.log`, and exits with status 2 without
claiming the database checks passed. The same blocked result is used for a
verified unsupported worker-container substrate.

## Remove

```sh
make destroy
```

Teardown is idempotent and targets only the `cnpg-private` kind cluster,
workers, volumes, tenant namespaces, Platform namespace, and generated runtime
state.

## Important limitation

vCluster documents private nodes on supported systemd-capable Linux machines,
not privileged Docker containers. These Ubuntu worker containers are an
experimental local approximation. Every container still shares one Docker
host, kernel, runtime, and failure domain; this lab is not production
multi-tenancy or high availability.
