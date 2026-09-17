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

The proposed minimal Azure experiment is documented in
[`docs/azure-experiment-design.md`](docs/azure-experiment-design.md). It
deliberately prioritizes a one-tenant AKS, Kamaji, CAPZ, VMSS, and CNPG proof
over production infrastructure and operational hardening.

Azure foundation operations use the ignored owner-only
`config/azure.local.env` selectors. Tenant creation and status use the same
explicit tenant specification interface as the local profile:

```bash
just azure-preflight
just azure-create-foundation
just azure-create-management
just azure-foundation-status
just tenant-create azure config/tenants/examples/azure.json
just tenant-status azure tenant-example
```

The generic create operation journals and records the tenant control plane,
CAPZ worker pool, Azure resource identities, add-ons, and Ready evidence under
owner-only tenant-keyed runtime paths. Pre-cutover Azure foundation inventory
is rejected and requires a clean redeploy. Targeted Azure tenant deletion is
implemented and live-validated in the next lifecycle phase. Azure resources
remain billable until `just azure-destroy` starts deletion of the exact
recorded experiment resource group.

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

The lab caches its own pinned kind, kubectl, Helm, clusterctl, charts,
manifests, source provenance, and OCI images under ignored owner-only
`.tools/`. It does not install or replace `just`.

## Quick start

```bash
cd capi
just cache
just tools
just prepare-host
just preflight
just create-management
just tenant-create local config/tenants/examples/local.json
just tenant-status local tenant-example
just tenant-delete local tenant-example local/tenant-example
just destroy
```

Local tenants are selected only through explicit JSON specifications. Repeating
`tenant-create` is the public reconcile/retry path. `tenant-status` is
read-only, and `tenant-delete` requires the exact `profile/name` confirmation
token. The bounded final E2E proves that one explicitly selected PostgreSQL
tenant can become healthy and that teardown restores a clean host:

```bash
just test-e2e
```

`just cache` is the explicit online acquisition and provenance-refresh step.
After it succeeds, `just tools` and `just preflight` verify and use the local
cache without Git or registry lookups. `just test-e2e-offline` applies the same
policy to the clean-to-clean gate by blocking acquisition commands, forcing
Docker consumers to `--pull=never`, and denying external HTTP/HTTPS inside the
disposable nodes. The pinned Kamaji release renders tenant API-server and
Konnectivity containers with `imagePullPolicy: Always` and exposes no supported
pull-policy field. Offline management therefore starts an owner-labeled
Distribution registry only on the private kind Docker network. Its read-only
storage is generated from the verified active cache, maps the original
`registry.k8s.io` tags and digests to their pinned OCI manifests, and is
configured as the management node's only local mirror before tenant
reconciliation. External registry egress remains denied.

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
| `just cache` | Online-only acquisition of every pinned input and OCI image into a verified immutable generation. |
| `just tools` | Install and verify tools and inputs from the active local cache without provenance refresh. |
| `just prepare-host` | Securely record and raise runtime inotify values. |
| `just preflight` | Check tools, inputs, Docker capacity, CIDRs, image digests, ownership collisions, and privileged-container support. |
| `just create-management` | Reconcile the kind management cluster and lifecycle controllers. |
| `just dev-bootstrap` | Prepare and bind a retained management context to the current user, host, Docker daemon, branch, revision, configuration, and exact management identity. |
| `just dev-clean` | Run authoritative cleanup for retained tenant, management, runtime, and host state. |
| `just tenant-create local <spec.json>` | Validate the existing foundation and reconcile exactly the selected local tenant. |
| `just tenant-status local <name>` | Print the selected tenant's read-only lifecycle envelope. |
| `just tenant-delete local <name> local/<name>` | Delete exactly the selected tenant after survivor validation and explicit approval. |
| `just diagnose management` | Print management status, workloads, CRDs, and events without mutation. |
| `just destroy` | Remove recorded tenants, controllers, the management cluster, runtime state, and restore host settings. |

All mutating tenant paths validate pinned inputs and recorded tenant networks
before changing state. Generated credentials, journals, Ready evidence, and
identity records are owner-only files below ignored `.runtime/`. Commands use
explicit kubeconfig paths and do not depend on the user's current Kubernetes
context.

The retained workflow is a development optimization, not a final gate. It
retains only the explicitly bound management foundation:

```bash
just dev-bootstrap
just tenant-create local config/tenants/examples/local.json
just dev-clean
```

`dev-bootstrap` validates the retained binding and management identity.
Tenant lifecycle remains specification-driven through the generic commands.
Always run `just test-e2e` or `just test-e2e-offline` before treating a change
as lifecycle-complete.

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

Before the ClusterResourceSet is applied, the lab restores exact worker images
from the active cache and verifies concurrent imports in all pre-CNI worker
containerd stores. Replacement validation invokes the same import barrier
before accepting network readiness.

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

## Retry and interruption recovery

Create retry and deletion are fail-closed:

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

`just tenant-status <profile> <name>` returns `0` only for Ready or
authoritatively absent. It returns `1` for progressing, deleting, degraded,
failed, or ownership-invalid. Mutating commands and test recipes return `0` on
success and `1` on validation, ownership, admission, reconciliation, timeout,
or cleanup failure. The settings reserve exit `2` for a possible blocked
outcome, but the current CAPI implementation does not emit it.

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

1. run `just tenant-status <profile> <name>` and `just diagnose management`;
2. restore the failed controller or dependency;
3. retry `just tenant-create`, `just tenant-delete`, or `just destroy`.

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

Large upstream YAML and OCI archives are not committed. `just cache` stages a
new private generation, checks release SHA-256 values, verifies annotated tags
against peeled source commits, verifies image tag provenance against OCI
digests, validates the archived `linux/amd64` manifest and blobs, and switches
the active pointer only after the whole generation passes. A failed refresh
leaves the previous generation active.

Normal preflight compares the active generation and inventory with the current
pins and records a private verification stamp after checking archive
checksums, OCI identities, authored inputs, and owner-only file boundaries.
An unchanged generation uses kernel-controlled inode, size, mtime, and ctime
metadata to reuse that result; any cache, inventory, permission, authored
input, or pin change forces full content verification again. Local tool
versions are still checked on every preflight. Preflight does not silently
acquire missing content. Missing, changed, symlinked, broad-permission,
platform-mismatched, or stale entries require a new online `just cache`.

Management images are imported before controller installation. Worker images
are imported before ClusterResourceSet workloads and retain exact
digest-qualified runtime validation. Runtime transforms still replace expected
tags only and fail on changed image counts or unresolved placeholders.

The enforced-offline path additionally verifies and restores the pinned
Kubernetes API-server, controller-manager, scheduler, Konnectivity server, and
Distribution images. It creates an owner-only registry storage tree from the
active immutable generation, verifies every served manifest/blob digest, and
starts the registry without a published host port. The registry is reachable
only on the disposable management Docker network. Containerd's
`registry.k8s.io` host configuration points to that endpoint while preserving
the upstream server as a fail-closed fallback; the node egress rule rejects any
fallback attempt. Cleanup requires the exact recorded container ID, image ID,
labels, network, address, generation-backed file inventory, and checksums.

## Lifecycle timing

The clean-to-clean gates print one `CAPI_TIMING` JSON record for each of:
`tools_cache`, `initial_cleanup`, `host_preparation`,
`management_bootstrap`, `tenant_control_plane`,
`tenant_workers_network`, `cnpg_readiness_sql`, and `teardown`. Records contain
only schema, phase, passed/failed/skipped status, and elapsed seconds. They do
not include commands, environment values, credentials, or exception text.
The enforced-offline gate also prints `CAPI_OFFLINE_EGRESS` records with the
node and counted reject-rule packets, plus `CAPI_OFFLINE_MIRROR` records for
each exact digest-qualified image exercised through the local mirror.

Generic create and delete operations additionally print redacted
tenant-specific `TENANT_TIMING` records and persist owner-only timing evidence.

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
- The offline registry is an ephemeral, unauthenticated service on the private
  disposable kind Docker network only; it has no published host port and is
  removed by authoritative teardown.
- Local tenant mutation is serialized by profile and reconciles one explicit
  specification at a time.
- No Azure provider, credentials, resources, commands, or executable manifests
  are included.

See [`docs/high-level-design.md`](docs/high-level-design.md) for the as-built
architecture and future Azure mapping.
