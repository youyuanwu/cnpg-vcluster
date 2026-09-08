# Cluster API Kamaji CloudNativePG lab

This directory is an independent Linux-only experiment for managing hosted
Kubernetes tenants with Cluster API. One kind management cluster runs Cluster
API core, kubeadm bootstrap, the Docker development infrastructure provider,
Kamaji, and the Kamaji control-plane provider. Each tenant receives a hosted
control plane, three exclusive Docker worker containers, and a tenant-owned
three-instance CloudNativePG cluster.

The local implementation is designed to preserve a future path to an
independently provisioned AKS management cluster with Kamaji control planes,
CAPZ-managed Azure worker machines, the external Azure cloud provider, and
Azure CSI. This repository does not create or configure Azure resources.

Kamaji uses the public `26.8.6-edge` source release. The edge channel is
experimental, but it requires no account, activation key, or paid artifact.
CAPD `DevCluster` and `DevMachine` resources are development-only and are not
a production worker substrate.

## Prerequisites

- Linux Docker Engine with cgroup v2, non-rootless operation, the buildx
  plugin, and permission to run privileged containers.
- At least 12 logical CPUs, 24 GiB of Docker memory, and 30 GiB free in
  Docker's storage filesystem.
- Permission to raise the runtime-only host inotify limits to
  `fs.inotify.max_user_instances=1024` and
  `fs.inotify.max_user_watches=524288`.
- `git`, `python3`, `curl`, `tar`, and host-installed `just` 1.58.0.

The lab downloads its own pinned kind, kubectl, Helm, and clusterctl binaries
under ignored `.tools/`. It does not install or replace `just`.

## Quick start

```bash
cd capi
just tools
just prepare-host
just preflight
just create
just status
just verify
just destroy
```

`just create` reconciles both tenants. `just verify` performs the expensive
two-tenant isolation and disruption checks. The bounded final E2E intentionally
proves only that one representative three-instance PostgreSQL cluster can
become healthy and that teardown restores a clean host:

```bash
just test-e2e
```

Use the targeted suites during development instead of repeatedly running the
full isolation gate:

```bash
just test-unit
just test-static
just test-management
just test-endpoint-negative
just test-network-negative
just test-storage-negative
just test-persistence-negative
just test-tenant-lifecycle
```

## Lifecycle commands

| Command | Purpose |
|---|---|
| `just tools` | Download and verify immutable tools, charts, manifests, source archives, and OCI provenance. |
| `just prepare-host` | Securely record and raise runtime inotify values. |
| `just preflight` | Check tools, inputs, Docker capacity, CIDRs, image digests, ownership collisions, and privileged-container support. |
| `just create-management` | Reconcile the kind management cluster and lifecycle controllers. |
| `just create` | Reconcile both tenant control planes, workers, networking, storage, and databases. |
| `just repair tenant-a` | Explicitly repair one owned tenant while proving the other tenant is unchanged. |
| `just verify` | Verify two-tenant endpoint, credential, worker, storage, and PostgreSQL isolation plus replacement/failover behavior. |
| `just status` | Print read-only JSON status for management and tenant layers. |
| `just diagnose management` | Print management status, workloads, CRDs, and events without mutation. |
| `just destroy-tenant tenant-a` | Delete one tenant through its live API and prove the survivor remains healthy. |
| `just destroy` | Remove both tenants, controllers, the management cluster, runtime state, and restore host settings. |

All mutating create and repair paths validate the pinned inputs before changing
surviving state. Generated credentials and identity records are owner-only
files below ignored `.runtime/`. Commands use explicit kubeconfig paths and do
not depend on the user's current Kubernetes context.

## Local isolation model

Tenant control planes, CAPI resources, kubeconfigs, CIDRs, DNS domains, API
VIPs, workers, Docker volumes, static PVs, and PostgreSQL credentials are
distinct. Tenant Nodes and database objects do not appear in the management
API. Opposite Kubernetes and PostgreSQL credentials are tested against
reachable endpoints and must be rejected.

The worker boundary is not hostile-tenant isolation. Every worker is a
privileged Docker container sharing the host kernel, Docker daemon, physical
storage, network, power, and failure domain. The experiment proves Kubernetes
and lifecycle separation, not protection from a malicious workload with host
access.

## Storage and persistence

Each tenant owns one Docker volume. Its host mountpoint is mounted at the same
container path in all three tenant workers. Prebound static hostPath PVs have
no node affinity, so a PostgreSQL Pod can move to a replacement CAPD worker
while retaining its PVC, PV, and bytes.

This is a local persistence proof only. It does not model Azure Disk
attach/detach, fencing, availability zones, snapshots, or failure domains. The
future Azure profile replaces the Docker volume and hostPath implementation
with Azure CSI volumes.

## Networking and add-ons

Calico and a repository-owned `capi-kube-proxy` are delivered initially
through a ClusterResourceSet. Source ConfigMaps are deterministically split at
YAML document boundaries, limited to 900 KiB each, capped at 100 references,
and verified against a complete SHA-256 inventory before apply.

ClusterResourceSet is the bootstrap and source-change delivery mechanism. Once
the source objects are handed off, arbitrary target drift is detected by
status and repaired explicitly; the lab does not assume ClusterResourceSet
will continuously repair every target mutation.

The custom kube-proxy name prevents Kamaji from cleaning the repository-owned
resources. `conntrack.maxPerCore: 0` avoids the nested-container
`nf_conntrack_max` write failure. Kamaji remains unpaused and continues normal
control-plane reconciliation.

## Endpoints and ownership

One MetalLB VIP is authoritative for each tenant. The same host and port must
appear in the CAPI `Cluster`, CAPD `DevCluster`, `KamajiControlPlane`,
kubeconfig active context, and kubeadm bootstrap data. CAPD's development
HAProxy container remains required for provider readiness but is never allowed
to become the authoritative tenant endpoint.

Kubernetes controllers own CAPI and provider resource reconciliation. Host
code owns only exact kind/CAPD Docker identities, tenant Docker volumes,
runtime records, and host setting restoration. Same-named objects without the
expected labels and records are refused rather than adopted or deleted.

## Repair and interruption recovery

Repair and deletion are fail-closed:

- management and tenant kubeconfigs are bound to exact ownership, active
  context, endpoint, CA, and required permissions;
- Machine replacement is allowed only after proving the
  Machine-to-MachineSet-to-MachineDeployment ownership chain;
- inspection distinguishes present, canonical Kubernetes `NotFound`, and
  inspection failure;
- deletion journals bind the exact Cluster UID after live API cleanup, making
  retries safe before or after Cluster deletion;
- an absent Docker volume with a valid retained identity record is treated as
  an interrupted cleanup and completed safely.

## Status, conditions, and exits

`just status` returns `0` only when every observed layer is healthy. It returns
`1` when the lab is absent, incomplete, drifted, unavailable, or cannot be
inspected. Mutating commands and test recipes return `0` on success and `1` on
validation, ownership, admission, reconciliation, timeout, or cleanup failure.
The settings reserve exit `2` for a possible blocked outcome, but the current
CAPI implementation does not emit it.

Condition checks require the current resource generation rather than accepting
a stale `True` condition. A healthy tenant requires:

- management API readiness, four available CAPI providers, required
  controller workloads, ready webhooks, Kamaji, and its datastore;
- CAPI `Cluster` and `KamajiControlPlane` `Available=True`, initialized and
  unpaused control plane, plus matching Cluster/DevCluster/Kamaji endpoints;
- exact Ready Machine, DevMachine, Docker container, Node, and bootstrap
  Secret inventories;
- Ready Calico, CoreDNS, Konnectivity, and repository-owned kube-proxy;
- owned Docker volume and static PV/PVC state without PV node affinity;
- digest-pinned CNPG operator, `Cluster in healthy state`, three Ready
  PostgreSQL Pods on distinct workers, three Bound PVCs, and a reachable
  read-write endpoint.

Canonical Kubernetes `Error from server (NotFound):` is the only accepted
absence proof in fail-closed lifecycle inspections. Other API errors are
failures, not absent or healthy states.

## Break-glass finalizer removal

Normal recovery is:

1. run `just status` and `just diagnose management`;
2. restore the failed controller or dependency;
3. retry `just repair`, `just destroy-tenant`, or `just destroy`.

Finalizer removal is exceptional and should be used only when controller
recovery has been exhausted and the exact deleting resource has been
independently inspected:

```bash
just break-glass <kind> <namespace> <name> <uid>
```

The command supports only an allowlisted set of CAPI, CAPD, Kamaji, and test
resource kinds. It requires the exact owned resource UID and deletion
timestamp, writes an owner-only diagnostic snapshot, and uses atomic JSON
Patch tests for UID, resource version, and the selected finalizer before
removing only that finalizer. It does not delete Docker objects or bypass host
ownership checks.

Do not use break-glass to adopt foreign resources, remove arbitrary
finalizers, or compensate for an unreachable API. Never use broad Docker prune
commands as part of recovery.

## Supply chain

Large upstream YAML is not committed. `just tools` fetches pinned release
assets into `.tools/inputs/`, checks SHA-256 values, verifies annotated tags
against peeled source commits, and verifies image tag provenance against OCI
digests. Runtime transforms replace expected tags only and fail on changed
image counts or unresolved placeholders.

Remote registry availability is required. Bounded retries cover transient
registry 5xx and transport failures; digest mismatches and nontransient errors
remain fatal.

Exact versions, URLs, checksums, source commits, and image digests are in
[`config/versions.env`](config/versions.env). See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for upstream attribution.

## Limitations

- Linux Docker only; CAPD is a development provider.
- Privileged container workers share the host kernel and are not a production
  security boundary.
- Kamaji is pinned to an experimental edge release.
- Local static hostPath storage is not an Azure storage simulation.
- The management kind node mounts the host Docker socket for CAPD.
- `just create` reconciles tenants sequentially, favoring deterministic
  diagnostics over speed.
- No Azure provider, credentials, resources, commands, or executable manifests
  are included.

See [`docs/high-level-design.md`](docs/high-level-design.md) for the as-built
architecture and future Azure mapping.
