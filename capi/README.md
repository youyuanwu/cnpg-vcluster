# Cluster API Kamaji CloudNativePG lab

This directory is an independent Linux-only experiment for managing hosted
Kubernetes tenants with Cluster API. One kind management cluster runs Cluster
API core, kubeadm bootstrap, the Docker development infrastructure provider,
Kamaji, and the Kamaji control-plane provider. Explicit tenant specifications
select the hosted control plane, one to three exclusive Docker worker
containers, and one to three CloudNativePG instances.

The Azure profile provisions an independently managed AKS foundation with
Kamaji control planes, CAPZ-managed Azure worker machines, and the external
Azure cloud provider. The local and Azure profiles deliberately have different lifecycle surfaces.
Local tenants are Kubernetes `Tenant` resources reconciled by the Go
controller. Azure tenants retain the JSON specification and Python lifecycle
while CAPZ integration remains an independent experiment.

The proposed minimal Azure experiment is documented in
[`docs/azure-experiment-design.md`](docs/azure-experiment-design.md). It
deliberately prioritizes tenant control-plane, VMSS worker, cloud-provider,
targeted deletion, and recreation behavior over production infrastructure,
Azure Disk/CNPG workload validation, and operational hardening.

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
just tenant-delete azure tenant-example azure/tenant-example
just azure-test-tenant-lifecycle
```

The generic lifecycle journals and records the tenant control plane, CAPZ
worker pool, Azure resource identities, add-ons, and Ready evidence under
owner-only tenant-keyed runtime paths. Targeted deletion verifies exact
management UIDs and Azure resource IDs, lets CAPI/CAPZ delete the MachinePool
and VMSS, proves the shared foundation is unchanged, and then removes tenant
orchestration state. Pre-cutover Azure foundation inventory is rejected and
requires a clean redeploy. Tenant Azure resources remain billable until
targeted deletion removes the VMSS and related resources. The preserved
AKS, VNet, identity, and other shared foundation resources remain billable
until `just azure-destroy` completes.

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
just local-tenant-apply config/tenants/examples/local.yaml
just local-tenant-status tenant-example
just local-tenant-delete tenant-example
just destroy
```

Local tenants are declared through versioned YAML resources. Reapplying a
manifest is the reconcile/retry path, status reads generation-aware Kubernetes
conditions, and ordinary deletion is completed by the controller finalizer.
Apply is asynchronous; repeat `just local-tenant-status tenant-example` until
it exits zero. Tenant specifications are immutable; delete and recreate to
change capacity, versions, or networks. The bounded final E2E waits for one
explicitly selected Tenant's structural Ready contract, runs `SELECT 1`
through its PostgreSQL read/write service with the existing disposable SQL
probe, waits for ordinary Tenant deletion/finalization, and verifies that
management teardown restores a clean host:

```bash
just test-e2e
```

## Tenant specifications and clean cutover

The local Tenant API requires a name, Kubernetes version, worker count,
database count, Pod CIDR, and Service CIDR. Unknown fields, unsupported
versions, invalid types, and overlapping networks fail before mutation. Azure
continues to use schema `1` JSON specifications. Safe examples are in
[`config/tenants/examples/`](config/tenants/examples/).

The lifecycle does not infer a singleton tenant from environment variables.
Removed fixed tenant commands, old Azure foundation inventories, and legacy
local runtime layouts are not migrated or adopted. Local retries reapply the
same immutable Tenant manifest; Azure retries use the same JSON specification
and recorded foundation identity.

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
| `just local-tenant-apply <manifest.yaml>` | Strictly apply one declarative local Tenant. |
| `just local-tenant-status <name>` | Print generation-aware local Tenant conditions. |
| `just local-tenant-delete <name>` | Delete one local Tenant and wait for finalization. |
| `just tenant-create azure <spec.json>` | Reconcile one explicit Azure tenant on the recorded AKS/CAPZ foundation. |
| `just tenant-status azure <name>` | Inspect one Azure tenant without mutating state. |
| `just tenant-delete azure <name> azure/<name>` | Delete the exact tenant through CAPI/CAPZ and verify foundation preservation. |
| `just azure-test-tenant-lifecycle` | Destructively prove Azure create, Ready, targeted absence, foundation preservation, and recreation for the example tenant. |
| `just diagnose management` | Print management status, workloads, CRDs, and events without mutation. |
| `just destroy` | Remove recorded tenants, controllers, the management cluster, runtime state, and restore host settings. |

All mutating tenant paths validate pinned inputs and tenant networks before
changing state. Local lifecycle state is held in the Tenant resource,
controller-owned ConfigMaps, provider resources, and exact Docker identities;
the public local commands do not maintain a second filesystem journal or
readiness evaluator. Azure credentials, journals, Ready evidence, and identity
records remain owner-only below ignored `.runtime/`. Commands use explicit
kubeconfig paths and do not depend on the user's current Kubernetes context.

The retained workflow is a development optimization, not a final gate. It
retains only the explicitly bound management foundation:

```bash
just dev-bootstrap
just local-tenant-apply config/tenants/examples/local.yaml
just dev-clean
```

`dev-bootstrap` validates the retained binding and management identity.
Local Tenant lifecycle remains declarative through the Tenant API.
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

The Tenant controller is the single networking writer. It transforms the
verified Calico asset, builds the repository-owned `capi-kube-proxy` objects,
and applies each object directly through the tenant client with exact Tenant,
specification, foundation, and resource-role markers. Same-name replacements
with foreign ownership markers make the Tenant `OwnershipInvalid`. Missing
non-root owned children are recreated; a missing or replaced root CAPI Cluster
is refused after its UID has been recorded.

Network, storage, and CNPG objects are applied in dependency-ordered batches:
Namespaces and CRDs first, supporting configuration/RBAC/storage next, then
workloads. Every object in a batch is checked and applied without a per-object
requeue. CRDs must be Established and their served versions discoverable before
dependent batches proceed; same-name create races still require exact ownership.

Worker image delivery is bootstrap-owned rather than a second reconciliation
loop. The `KubeadmConfigTemplate` verifies archive checksums, imports the
required images into containerd, creates exact digest/tag aliases, configures
the offline mirror when enabled, and installs the offline egress rules before
kubeadm runs. Bootstrap failure prevents the Machine from becoming Ready.

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

Reconciliation and deletion are fail-closed:

- management and tenant kubeconfigs are bound to exact ownership, active
  context, endpoint, CA, and required permissions;
- Machine replacement is allowed only after proving the
  Machine-to-MachineSet-to-MachineDeployment ownership chain;
- inspection distinguishes present, canonical Kubernetes `NotFound`, and
  inspection failure;
- one finalizer deletes the exact recorded CAPI Cluster, waits
  for provider objects and CAPD containers, removes the exact owned volume,
  deletes the Namespace and its credentials, releases the endpoint, and
  removes the finalizer last;
- tenant-internal resources and bootstrap RBAC are disposable with the
  dedicated tenant cluster; finalization does not contact the tenant API or
  require a cleanup checkpoint. Management/host ownership remains fail-closed.
  The `disposable-cluster-v3` epoch requires a clean cutover from older stored
  status contracts; existing Tenants are not migrated.

The controller does not persist a creation program counter or child-resource
UID ledger. Missing children are discovered from live state, and Ready or
Degraded Tenants are resynchronized every 30 seconds. Expected progress uses a
fixed poll interval rather than rate-limited requeue backoff.

Each normal reconciliation reads the foundation ConfigMap directly and checks
its immutable checksum, lifecycle hash, controller image, and mutation gate.
Successful management-container, network, active-cache, and offline-registry
host checks are cached in memory for at most one minute per foundation hash.
A changed hash or controller restart requires fresh host checks; failures are
not cached. Deletion does not use this cache and retains live host ownership
checks before destructive operations.

## Status, conditions, and exits

`just local-tenant-status <name>` returns `0` only when status and the Ready
condition observe the current generation and the phase is `Ready`. It returns
`1` for progressing, deleting, degraded, failed, ownership-invalid, stale, or
absent resources. Azure `tenant-status` retains its provider-neutral envelope.
Mutating commands and test recipes return `0` on success and `1` on validation,
ownership, admission, reconciliation, timeout, or cleanup failure.

Condition checks require the current resource generation rather than accepting
a stale `True` condition. A healthy local Tenant requires:

- current affirmative provider conditions for the CAPI `Cluster`, CAPD
  `DevCluster`, and `KamajiControlPlane`, plus an initialized, unpaused control
  plane and matching endpoints;
- exact Ready Machine, DevMachine, Docker container, and Node inventories;
- available Calico, CoreDNS, and repository-owned kube-proxy workloads;
- the expected static StorageClass and exact owned Docker volume;
- a healthy CNPG Cluster using the pinned PostgreSQL image, the requested one
  to three Ready PostgreSQL Pods, and the same number of Bound PVCs.

Canonical Kubernetes `Error from server (NotFound):` is the only accepted
absence proof in fail-closed lifecycle inspections. Other API errors are
failures, not absent or healthy states.

## Break-glass finalizer removal

Normal recovery is:

1. for local, run `just local-tenant-status <name>` and
   `just diagnose management`; for Azure, run
   `just tenant-status azure <name>`;
2. restore the failed controller or dependency;
3. retry local reconciliation with
   `just local-tenant-apply <manifest.yaml>`, retry deletion with
   `just local-tenant-delete <name>`, or run `just destroy`; Azure keeps
   `tenant-create` and `tenant-delete`.

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

Management images are imported before controller installation. Worker
`preKubeadmCommands` import the exact required archives before kubeadm and
networking start. The controller image contains only the static manager binary
and checksum-verified Calico and CNPG assets.

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

## Continuous integration

GitHub Actions runs Python unit/static checks and Go generation, vet, unit,
and envtest checks in **CAPI fast checks**, independently of the destructive
**CAPI end-to-end** job. Parallel jobs allow image acquisition and fast checks
to overlap; E2E builds the controller image during management bootstrap.
The final **CAPI tests** check requires both jobs to succeed on every PR
(including fork PRs), manual dispatch, and the weekly Monday 04:23 UTC schedule.
Keep **CAPI tests** as the required branch-protection check: its always-running
gate rejects failed, cancelled, or unexpectedly skipped prerequisite jobs.
Pushes to `main` run fast checks only, avoiding an immediate repeat of the PR's
destructive E2E. Concurrency cancels superseded runs of the same event/ref,
without a `main` push cancelling a scheduled or manually dispatched full gate.

Fast checks use `just controller-tools` to acquire only checksum-pinned Go and
envtest archives, without Docker, management assets, or OCI image acquisition.
Acquisition and extraction hold the shared E2E lock followed by the exclusive
tools lock, just like other tool commands; an E2E child inherits its parent's
E2E exclusion and takes only the tools lock.
The cache allow-list contains those two compressed archives (about 119 MiB)
and `.tools/go-mod-cache` (about 238 MiB with current pins), keyed by OS,
architecture, tool pins, and module manifests as applicable. E2E may restore
the module cache from earlier runs but does not wait for or depend on a cache
hit. Archives are checksum-verified before installation even on cache hits.
Cache parent directories are created owner-only before restoration so the
lab's private-path checks also work on clean GitHub-hosted runners.
The 18 GiB `.tools/cache` OCI store, Docker layers, compiled Go cache, and
runtime/kubeconfig state are **never uploaded to Actions caches**.

The custom Go wrapper installs to `.tools/bin/go`, with `GOROOT=.tools/go`,
`GOMODCACHE=.tools/go-mod-cache`, and local `GOCACHE=.tools/go-cache`.
Envtest uses `.tools/envtest/envtest` via `KUBEBUILDER_ASSETS`; these paths are
resolved against the absolute `capi` directory by the Python harness, rather
than relying on `setup-go` defaults. For Docker-free local checks:

```bash
just controller-tools
just test-unit
just test-static
just controller-verify
just controller-vet
just controller-test
```

## Lifecycle timing

The clean-to-clean gates print one `CAPI_TIMING` JSON record for each of:
`tools_cache`, `initial_cleanup`, `host_preparation`,
`management_bootstrap`, `tenant_convergence`, `tenant_sql_probe`,
`tenant_deletion_finalization`, and `management_teardown_host_restoration`.
`CAPI_PHASE_START` lines identify the active phase immediately. Timing records contain
only schema, phase, passed/failed/skipped status, and elapsed seconds. They do
not include commands, environment values, credentials, or exception text.
The tools phase measures local cache verification/installation; online image
acquisition remains the separate `Acquire pinned tools and images` CI step.
The deletion phase captures the live Tenant UID, management object UIDs,
endpoint allocation, full worker-container IDs, and exact storage volume name.
After Tenant absence, it verifies those Namespace/provider roots, allocations,
containers, and volume are absent **before** management teardown. A leak or
inspection failure fails deletion even if the subsequent full cleanup succeeds.

During convergence, `CAPI_TENANT_TRANSITION` JSON lines report elapsed seconds,
classification, generation, and each condition's type/status/reason/observed
generation. The initial observation and meaningful transitions are flushed
immediately; unchanged polls, condition ordering, messages, and timestamp-only
changes do not spam the log. These observations
show whichever component conditions the controller currently publishes,
without controller stage fields. Initial creation can retain a generic
`Progressing` condition until component readiness is observed; condition logs
alone cannot subdivide that interval into control-plane, worker, or add-on time.
Condition messages are omitted, and timeout diagnostics use the shared
redaction helper.
The enforced-offline gate also prints `CAPI_OFFLINE_EGRESS` records with the
node and counted reject-rule packets, plus `CAPI_OFFLINE_MIRROR` records for
each exact digest-qualified image exercised through the local mirror.

Azure create and delete operations additionally print redacted tenant-specific
`TENANT_TIMING` records and persist owner-only timing evidence.

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
- Local reconciliation uses leader election and one bounded reconcile worker;
  multiple deleting Tenants can still make independent progress without a
  lab-wide deletion lock.
- The Azure profile is an experiment with a shared resource group, VNet,
  subnet, and broad resource-group Contributor identity.
- CAPZ `v1.21.1` requires the narrowly scoped external-control-plane webhook
  compatibility selector documented in
  [`docs/azure-experiment-design.md`](docs/azure-experiment-design.md).

See [`docs/high-level-design.md`](docs/high-level-design.md) for the shared
as-built lifecycle architecture and
[`docs/azure-experiment-design.md`](docs/azure-experiment-design.md) for the
Azure profile.
