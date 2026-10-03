# Cluster API Kamaji CloudNativePG lab

This directory is an independent Linux-only experiment for managing hosted
Kubernetes tenants with Cluster API. One kind management cluster runs Cluster
API core, kubeadm bootstrap, the Docker development infrastructure provider,
Kamaji, and the Kamaji control-plane provider. Explicit tenant specifications
select the hosted control plane, one to three exclusive Docker worker
containers. Database workloads are explicit catalog entries, not implicit
Tenant resources.

The Azure profile provisions an independently managed AKS foundation with a
shared ACR, Kamaji control planes, CAPZ-managed Azure worker machines, and the
external Azure cloud provider. `azure-create-management` publishes the static
Tenant manager to ACR, pins the deployed digest, and starts it in Azure mode.
Azure database capability is installed separately from infrastructure readiness:
supervise `just azure-database-runtime-watch` on the management host to retry
CNPG and Azure Disk CSI installs for Ready Azure Tenants, or run
`just azure-database-runtime-once` for a bounded attempt. Both commands require
the verified local cache prepared by `just cache`; they install the pinned
charts from that cache, not from live chart URLs. Database capability stays
unavailable until the installed runtime is observed, while Tenant
infrastructure Ready does not depend on chart installation.
Local and Azure tenants are Kubernetes `Tenant` resources reconciled by the
Rust/kube-rs controller. Each manager deployment installs exactly one provider:
local mode retains Docker and the local foundation, while Azure mode runs in
AKS without Docker or Azure credentials and delegates infrastructure mutation
to CAPZ/ASO. A separate database-controller uses the dedicated Azure workload
identity to read and delete only managed disks in the foundation resource group.
It reconciles catalog entries against the pinned CNPG/CSI runtime and a
non-default `cnpg-azure-disk` class. Each entry has its own workload namespace;
its ASO disks live in the Tenant's separate storage namespace. Static CSI
volumes retain each 4-GiB disk until workload removal and direct ARM-ID
NotFound proof. Catalog additions are gated on absence of both temporary
catalog cutover locks, current Tenant/database-controller rollouts,
exact Tenant readiness, and effective catalog permissions; provider
adapters and packaging are staged, not a live Azure release claim.

Both management profiles install the provider-neutral `tenant-admin`
application: a Leptos WebAssembly frontend served by an Axum/kube-rs backend.
It reads management topology and creates/deletes top-level Tenants through
exact provider-specific lifecycle RBAC. The schema-v5 backend exposes
`GET/POST /api/v1/tenants/{name}/databases`,
`DELETE /api/v1/tenants/{name}/databases/{uid}`, and
`POST /api/v1/tenants/{name}/databases/{uid}/query`. Mutations require the
displayed catalog UID; deletion additionally requires the logical UID and exact
database name, and ambiguous writes only reread authoritative catalog state.
Local and Azure queries require an exact Ready, non-deleting catalog entry and
observed instance identity. Tenant kubeconfig and per-cluster PostgreSQL
credentials are validated and used only in memory; queries use an ephemeral
Kubernetes port-forward. The SQL console limits requests to 128 KiB (64 KiB
SQL), execution to 30 seconds, and retained results to 32 sets, 128 columns,
1,000 rows, 16 KiB per value and 2 MiB response text. The frontend renders
catalog-scoped cards, topology, and SQL controls instead of an implicit Tenant
database. The service validates effective management permissions and has no
Azure cloud credentials, application database, persistent cache, or browser
credential exposure. See
[`docs/admin-ui-design.md`](docs/admin-ui-design.md).

The Azure experiment is documented in
[`docs/azure-experiment-design.md`](docs/azure-experiment-design.md). It
prioritizes tenant control-plane, VMSS worker, cloud-provider, targeted
deletion, and recreation behavior. Azure database workloads and destructive
disk cleanup still require the separate credentialed validation gates before
a live Azure rollout can be claimed.

GitHub Actions does not receive Azure credentials and does not run destructive
Azure lifecycle tests. An operator with a recorded experiment foundation can
run read-only `just azure-foundation-status` and, after separately confirming
the target subscription and resource group, destructive
`just azure-test-tenant-lifecycle` outside CI. The local PR gate runs
`just test-e2e-offline` with the separately packaged database controller.
`just controller-metrics` and `just database-controller-metrics` enforce
independent 12,000-line production-Rust ceilings.
As of 2026-10-01, a read-only `just azure-foundation-status` returned
`{"blockers":["Azure foundation inventory is absent"],"foundation":"unhealthy","healthy":false,"schema":1}`
and exited 1: the exact owner-only `.runtime/azure/resources.json` is absent,
and `gh secret list` found zero `CAPI_AZURE_*` repository secrets. The
credentialed three-by-three Azure destructive run and nine-disk ASO/ARM
absence proof have **not** run. Real browser-versus-service-proxy agreement
and leak inspection have **not** run either. Both remain release-acceptance
gates, not passing checks; full unit/static/local/offline success does not
substitute for them.

If local catalog bootstrap is interrupted after Tenant CREATE was issued,
`create-management` preserves the owner-only
`.runtime/management/catalog-bootstrap-probe.json` record and re-fences Tenant
and catalog CREATE. Only the local kind profile can settle an unknown result:
it verifies the recorded management container and kubeconfig, restarts that
exact container to terminate in-flight API requests, waits for the management
API and controller leadership, then reads the exact Tenant identity. A matching
probe is deleted through ordinary UID-preconditioned deletion; an absent probe
still requires catalog and namespace cleanup proof before the record is
removed. Do not delete the record, bypass a finalizer, or remove the retained
cluster while recovery is uncertain. Azure's managed API cannot use this
restart protocol and remains fenced pending independent terminal proof.
Inspect the exact record, management identity and both policy/binding
identities without mutation before resuming; an immediate Tenant NotFound is
not proof that an in-flight CREATE cannot arrive. Never clear a recorded
catalog/disk identity or force-remove a finalizer merely to unblock a rollout.

Azure foundation operations use the ignored owner-only
`config/azure.local.env` selectors. Azure Tenant creation uses a schema-1 JSON
specification as a command input; the client converts it to the Azure Tenant
CR shape and observes operator status:

The foundation normally creates a resource-group-scoped custom role containing
only managed-disk read and delete. If the Azure tenant has exhausted its custom
role-definition quota, the owner may explicitly set
`AZURE_DATABASE_DISK_ROLE_DEFINITION_ID=afc680e2-a938-412d-b213-9a49efa7fb83`
to use the built-in Azure Backup Snapshot Contributor role. The assignment
remains scoped to the experiment resource group, but the built-in role grants
additional disk, restore-point, and VM operations; omit the setting whenever
custom role creation is available.

```bash
just azure-preflight
just azure-create-foundation
just azure-create-management
just azure-foundation-status
just tenant-create azure config/tenants/examples/azure.json
just tenant-status azure tenant-example
just tenant-delete azure tenant-example azure/tenant-example
just azure-test-tenant-lifecycle
just azure-destroy
```

Azure durable identity lives in Tenant status: exact foundation/specification
binding, management UIDs, kubeconfig hash, VMSS instances, Nodes, add-ons,
provider descendants, and deletion barriers. Ordinary Tenant deletion lets the
operator and CAPI/CAPZ finalizers remove exact Kubernetes roots; the Python
client waits for Tenant absence and does not retain a second deletion
checkpoint. The destructive gate captures exact external proof identity only
in memory for its active run and is the only Python path allowed to inject
`az vmss delete-instances`. Pre-ACR Azure foundation inventory is rejected and
requires a clean redeploy. Tenant Azure resources remain billable until
ordinary deletion removes the VMSS and related resources. The preserved AKS,
VNet, identity, and other shared foundation resources remain billable until
`just azure-destroy` completes. The first invocation starts asynchronous
resource-group deletion. After Azure reports the exact group absent, rerunning
the command proves absence and removes the recorded kubeconfig, inventory, and
Azure-only rendered manifests.

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
- `git`, `python3`, `curl`, `tar`, host-installed `just` 1.58.0, and Rustup
  or compatible system `rustc`/Cargo honoring the repository's Rust 1.98.1
  `rust-toolchain.toml` declaration.

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
just admin-status
just admin-port-forward
just local-tenant-delete tenant-example
just destroy
```

Local tenants are declared through versioned YAML resources. Reapplying a
manifest is the reconcile/retry path, status reads generation-aware Kubernetes
conditions, and ordinary deletion is completed by the controller finalizer.
The Tenant Admin overview provides the equivalent provider-aware create action,
and each detail page provides UID-bound destructive deletion. The CLI commands
remain available for automation and recovery.
Apply is asynchronous; repeat `just local-tenant-status tenant-example` until
it exits zero. Tenant specifications are immutable; delete and recreate to
change capacity or versions. Local manifests select `provider.type: local`;
endpoint and networks are assigned in provider status. The same CRD supports Azure provider intent. The JSON Azure example remains a
CLI input format, not a second lifecycle authority.
The bounded final E2E creates three explicit three-instance catalog entries,
checks per-entry SQL markers, failover and restart, deletes and recreates an
entry with stale-UID rejection, then waits for ordinary Tenant
deletion/finalization and verifies that management teardown restores a clean
host:

```bash
just test-e2e
```

## Tenant specifications and runtime configuration

The cluster-scoped `tenancy.cnpg-vcluster.io/v1alpha4` API requires a name and
an immutable spec with common `kubernetesVersion` and `workers` fields plus
one tagged `provider`. Local manifests use `type: local`; Azure requests
use `type: azure`. Neither provider carries a database count. The controller claims
canonical, non-overlapping Pod and Service CIDRs from the approved finite
Azure allocation catalog. A schema-3 foundation supplies ordered local slots
that bind one endpoint, Pod CIDR, and Service CIDR per Tenant; those values
appear in `status.provider.allocation`. OpenAPI/CEL reject invalid names, worker counts, version syntax, provider
fields, and spec updates. The Azure controller
separately validates the approved allocation catalog and active claims. Each
controller checks its configured supported Kubernetes version. The API server
prunes unknown fields under `fieldValidation=Warn` or `Ignore`, but rejects
them under `Strict`, as used by the repository's local clients. No validating
webhook is installed.

The v1alpha4 contract intentionally replaces experimental v1alpha3. There is
no conversion or migration: existing Tenants and their implicit database
workloads must complete ordinary deletion and storage cleanup before the
create-locked clean CRD cutover. A leftover draft `TenantDatabase` CRD
blocks installer preflight **before mutation**; do not delete it until all
served versions and in-flight CREATE outcomes are proven terminal and
workload/storage/admission cleanup is independently approved. Azure-mode managers
reconcile only Azure Tenants; local-mode managers reconcile
only local Tenants. Provider mismatches remain unsupported and do not acquire a
new finalizer. Azure lifecycle commands continue to accept schema `1` JSON
specifications and translate them to the CRD. Safe examples are in
[`config/tenants/examples/`](config/tenants/examples/).

The lifecycle does not infer a singleton tenant from environment variables.
The installer supports only v1alpha4 and never deletes retained Tenants or
the draft database CRD during cutover. Separate temporary Tenant and catalog
CREATE policies guard migration, then a UID-bound empty bootstrap catalog
probe must pass before release. A loss of certainty retains/reapplies the
fences. The manager loads one foundation snapshot at startup.
Same-identity restarts resume active Tenants; changed configuration requires
an empty Tenant/provider/host inventory and a candidate-bound activation
ticket. Failed pre-activation replacement restores the prior controller.
Removed fixed tenant commands, old Azure foundation inventories, legacy local
runtime layouts, and Python Azure tenant journals are not migrated or adopted.
Local retries reapply the same immutable Tenant manifest; Azure retries submit
the same JSON-derived Tenant specification.

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
| `just admin-status` | Validate the local admin Deployment, Service, provider-specific effective RBAC, health, and typed API responses. |
| `just admin-port-forward` | Forward `tenant-system/tenant-admin` to `127.0.0.1:8080` until interrupted. |
| `just admin-fetch` | Fetch the locked workspace dependency graph. |
| `just admin-generate-check` | Verify generated least-privilege admin lifecycle resources are current. |
| `just admin-lint` | Run Rust formatting and Clippy for all admin crates. |
| `just admin-test` | Run locked/offline tests for shared DTOs, Axum projection, and Leptos view logic. |
| `just admin-metrics` | Report the informational admin production-Rust baseline and delta. |
| `just admin-package-check` | Build the static server and browser bundle twice offline and compare the exact output inventory. |
| `just dev-bootstrap` | Prepare and bind a retained management context to the current user, host, Docker daemon, branch, revision, configuration, and exact management identity. |
| `just dev-clean` | Run authoritative cleanup for retained tenant, management, runtime, and host state. |
| `just local-tenant-apply <manifest.yaml>` | Strictly apply one declarative local Tenant. |
| `just local-tenant-status <name>` | Print generation-aware local Tenant conditions. |
| `just local-tenant-delete <name>` | Delete one local Tenant and wait for finalization. |
| `just local-tenant-kubeconfig-clear <name>` | Remove one exact owner-only Tenant kubeconfig cache. |
| `just local-tenant-kubeconfig-clear-all` | Remove all validated Tenant kubeconfig caches. |
| `just tenant-create azure <spec.json>` | Reconcile one explicit Azure tenant on the recorded AKS/CAPZ foundation. |
| `just tenant-status azure <name>` | Inspect one Azure tenant without mutating state. |
| `just tenant-delete azure <name> azure/<name>` | Delete the exact tenant through Kubernetes/CAPI/CAPZ and wait for Tenant absence. |
| `just azure-database-runtime-once` / `just azure-database-runtime-watch` | Install or retry pinned CNPG and Azure Disk CSI runtime independently of Tenant infrastructure readiness. |
| `just azure-test-tenant-lifecycle` | Destructively prove three workers, three three-instance database entries, SQL/failover/restart, nine exact disk identities and post-deletion ASO/ARM absence, targeted VMSS replacement, foundation preservation, and recreation. Requires the matching credentialed experiment foundation. |
| `just database-controller-verify` / `just database-controller-metrics` | Verify generated catalog CRD/RBAC and enforce the separate production-Rust ceiling. |
| `just diagnose management` | Print management status, workloads, CRDs, and events without mutation. |
| `just destroy` | Remove recorded tenants, controllers, the management cluster, runtime state, and restore host settings. |

All mutating tenant paths validate pinned inputs and tenant networks before
changing state. Local lifecycle state is held in the Tenant resource, schema-3 foundation
ConfigMap, per-slot allocation Leases, provider resources, and exact Docker identities;
the public local commands do not maintain a second filesystem journal or
readiness evaluator. Azure lifecycle identity is held in the Tenant resource.
Ordinary Tenant create/status/delete commands do not persist a second Tenant
specification or deletion journal below `.runtime/`. During management
installation, the guarded catalog cutover **does** keep owner-only
`catalog-bootstrap-probe.json` and activation identity in `.runtime/management/`
until exact terminal proof; the destructive Azure gate holds proof in memory.
Local validation caches a Tenant kubeconfig on demand only when it
must invoke `kubectl` against that Tenant API. The cache is validated against
the live Secret, removed after Tenant deletion, and removable with the explicit
cache-clear commands. Foundation inventory and the management kubeconfig remain
below ignored owner-only `.runtime/` because they belong to the shared
management foundation. Commands use explicit kubeconfig paths and do not depend
on the user's current Kubernetes context. Validation evidence is printed to
stdout and is persisted only when the caller explicitly redirects it to a
selected path.

Azure implementation responsibilities live under `scripts/lib/azure/`:
`foundation` owns shared infrastructure and digest-pinned manager packaging,
`operator` submits and observes Tenant resources, `proof` verifies external
absence, `ownership` observes Azure/tag identity, and `gate` owns destructive
three-worker replacement validation. `scripts/azure.py` remains only the
foundation command facade.

Azure reconciliation re-reads `tenant-system/tenant-azure-provider` before
every mutation and requires the exact startup UID and canonical typed values.
Generated RBAC is provider-specific: local installs `role.yaml`, while Azure
installs `role-azure.yaml`; neither role grants the other provider's root
permissions.

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
API, except for the owned catalog and its bounded entry status. Opposite
Kubernetes and PostgreSQL credentials are tested against
reachable endpoints and must be rejected.

The worker boundary is not hostile-tenant isolation. Every worker is a
privileged Docker container sharing the host kernel, Docker daemon, physical
storage, network, power, and failure domain. The experiment proves Kubernetes
and lifecycle separation, not protection from a malicious workload with host
access.

## Storage and persistence

Each tenant owns one Docker volume. Its host mountpoint is mounted at the same
container path in all three tenant workers. Each catalog logical UID owns
one isolated namespace and per-ordinal directory, static PV and PVC (at least
1 GiB per instance); deletion verifies its own subtree absent without
removing the shared Tenant volume. Prebound static hostPath PVs have no node
affinity, so a PostgreSQL Pod can move to a replacement CAPD worker while
retaining its PVC, PV, and bytes.

This is a local persistence proof only. It does not model Azure Disk
attach/detach, fencing, availability zones, snapshots, or failure domains.
Azure uses separate Tenant storage and per-entry workload namespaces,
ASO-managed 4-GiB StandardSSD_LRS disks, and static CSI PV/PVC bindings.
The database-controller records expected ARM IDs before CREATE and waits
for direct NotFound after exact deletion; its credentialed live gate remains
unverified.

## Networking and add-ons

The Tenant controller is the single networking writer. It transforms the
verified Calico asset, builds the repository-owned `capi-kube-proxy` objects,
and creates missing objects through the tenant client with exact Tenant,
specification, foundation, and resource-role markers. Same-name replacements
with foreign ownership markers make the Tenant `OwnershipInvalid`. Existing
owned static objects are validated for identity, not generically rewritten;
missing non-root owned children are recreated. A missing or replaced root
CAPI Cluster is refused after its UID has been recorded.

Tenant networking/runtime bootstrap objects are reconciled in
dependency-ordered batches: Namespaces and CRDs first, supporting
configuration/RBAC/storage next, then workloads. Every object in a batch
is checked and created if missing without a per-object requeue. CRDs must
be Established and their served versions discoverable before dependent
batches proceed; same-name create races still require exact ownership.
Dynamic management roots use targeted identity-bound server-side apply;
the separate database-controller reconciles each catalog entry's CNPG
Cluster and storage.

Worker image delivery is bootstrap-owned rather than a second reconciliation
loop. The `KubeadmConfigTemplate` verifies archive checksums, imports the
required images into containerd, creates exact digest/tag aliases, configures
the offline mirror when enabled, and installs the offline egress rules before
kubeadm runs. Bootstrap failure prevents the Machine from becoming Ready.
After worker desired state is created, the controller applies networking
immediately; it does not wait for or probe the worker's internal containerd
socket.

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
- the Tenant finalizer first closes and drains its catalog, then deletes
  the exact recorded CAPI Cluster, waits
  for provider objects and CAPD containers, removes the exact owned volume,
  deletes the Namespace and its credentials, releases the exact allocation
  Lease, and
  removes the finalizer last;
- after the database-controller verifies each entry's workload, local path
  or Azure disk absent, remaining tenant-internal networking/bootstrap
  resources are disposable with the dedicated tenant cluster. Management,
  Tenant API and host inspection remain fail-closed.
  Unsupported live legacy Kubernetes/provider/Docker residue blocks
  installation and is never migrated. Only recognized private local
  compatibility file shapes are removed by explicit cleanup; unknown
  descendants remain fail-closed.

The Tenant controller records catalog creation intent/outcome and exact
catalog namespace/UID status, but no generic infrastructure creation
program counter or child-resource UID ledger. Missing children are
discovered from live state. Static bootstrap
objects are created when absent and ownership-validated while creation is
progressing; existing objects are not continuously rewritten or generically
content-audited. Bootstrap Roles and RoleBindings retain explicit content
validation because they establish required administrative access. Dynamic CAPI
roots retain targeted repair; the database-controller owns CNPG `Cluster`
identity and reconciliation.
Expected progress uses a fixed poll interval rather than rate-limited requeue
backoff.

The manager reads and validates the foundation ConfigMap once at startup.
Reconciliation uses that immutable snapshot and each Tenant's recorded
foundation hash. Creation-only slot, image-archive, cache, and registry errors
do not prevent deletion through the validated identity/storage core.
Installer-owned tool versions and cache-state digests are not controller
compatibility checks. Deletion retains uncached live host ownership checks
before destructive operations. The resolved slot catalog is part of schema 3;
its immutable hash excludes only controller image identity. Leader
election uses a separate renewable Lease, not an allocation slot.

The generated management-resource catalog owns exact API identity, scope,
Tenant/worker/kubeconfig naming, watch suffix/label routing, activation policy,
and fixed inventory namespaces. Rust and Python consume the same declaration
for constructed references, watches/RBAC, ownership, exact-version clean-state
inventory, and named/observed/allocation-linked deletion evidence.
Allocation names, fixed controller infrastructure, provider-only CRDs,
break-glass allowlists, and tenant-internal resources remain domain-owned.

Tenant status and finalizer writes share one exact mutation contract. Patches
bind UID and resourceVersion after validating UID, generation, literal spec,
and deletion timestamp. General status conflicts reread and retry only the
same identity; finalizer conflicts requeue without a failure-status write.
Finalizer addition/removal are metadata merge patches, and allocation clearing
publishes explicit JSON `null`.

## Status, conditions, and exits

`just local-tenant-status <name>` returns `0` only when status and the Ready
condition observe the current generation and the phase is `Ready`. It returns
`1` for progressing, deleting, degraded, failed, ownership-invalid, stale, or
absent resources. Azure `tenant-status` retains its provider-neutral envelope.
Mutating commands and test recipes return `0` on success and `1` on validation,
ownership, admission, reconciliation, timeout, or cleanup failure.

Condition checks require the current resource generation rather than accepting
a stale `True` condition. A healthy local Tenant requires:

- a current affirmative aggregate `Available` condition on the CAPI `Cluster`;
  initial tenant access accepts its current `ControlPlaneReady` or
  `ControlPlaneAvailable` condition;
- one combined exact Machine, DevMachine, Docker container, and Node inventory,
  with containers running on the foundation network;
- available Calico, CoreDNS, and repository-owned kube-proxy workloads from
  that same observation;
- the expected static StorageClass and exact owned Docker volume.

`Tenant` infrastructure Ready is independent of catalog entry health. Its
database capability is separately observed; each entry publishes its own
Ready/Progressing/Deleting/Degraded state, storage and instance topology.
Do not use a Ready Tenant condition as proof that SQL or all databases are
ready.

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
timestamp, prints a redacted pre-mutation diagnostic record to stdout, and uses
atomic JSON Patch tests for UID, resource version, and the selected finalizer
before removing only that finalizer. Redirect stdout to an explicitly selected
path when a retained record is required. It does not delete Docker objects or
bypass host ownership checks.

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

Trunk `0.21.14` and wasm-bindgen CLI `0.2.129` are checksum-pinned cache
inputs. Only `just cache` acquires them (`just cache admin-build` is the
CI-focused subset); admin builds consume the verified binaries with locked
Cargo dependencies and Trunk offline. Generated HTML, JavaScript, Wasm, and
CSS bundles under `.runtime/` are ignored build output.

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

GitHub Actions runs Python unit/static checks and Rust generated-artifact
verification, format, Clippy, tests, and release/static-link build in
**CAPI fast checks**, independently of the destructive **CAPI end-to-end**
job. Fast checks explicitly repeat the offline Azure foundation packaging,
operator command, proof, ownership, and gate contracts. They also verify
database-controller generation, lint, tests, metrics, static build and image,
admin generation/lint/tests/metrics, and offline reproducible server/Wasm
packaging. PR E2E consumes the exact uploaded controller,
database-controller and admin artifacts and runs `just test-e2e-offline`;
scheduled/manual high-capacity CI rebuilds from the complete cache.
The final **CAPI tests** check requires fast checks and offline E2E on PRs
(including fork PRs), fast checks plus targeted/offline high-capacity and
high-capacity jobs on manual dispatch and the weekly schedule, and fast checks
alone on `main` pushes. Azure credentials are not available to GitHub Actions;
destructive Azure validation remains operator-run outside CI. Keep **CAPI
tests** as the required branch-protection check: its always-running gate rejects
failed, cancelled, or unexpectedly skipped applicable jobs.
Pushes to `main` run fast checks only, avoiding an immediate repeat of the PR's
destructive E2E. Concurrency cancels superseded runs of the same event/ref,
without a `main` push cancelling a scheduled or manually dispatched full gate.

CI installs stable Rust/Cargo with
`actions-rust-lang/setup-rust-toolchain`, including its integrated
`Swatinem/rust-cache` for Cargo's shared home and default controller target.
Cache misses are filled by `just controller-fetch`. Python wrappers leave
Cargo configuration, environment, home, target, and temporary locations at
their system defaults; enforced-offline commands use explicit `--locked
--offline`.
`Cargo.lock` checksums are the dependency integrity authority. No Go or
controller-gen tool acquisition is needed. For Docker-free local checks:

```bash
just controller-fetch
just test-unit
just test-static
just test-azure-operator-contracts
just controller-verify
just controller-lint
just controller-test
just controller-build
just cache admin-build
just admin-fetch
just admin-generate-check
just admin-lint
just admin-test
just admin-metrics
just admin-package-check
```

PRs require fast checks and one bounded online clean-to-clean E2E. Scheduled
and manually dispatched high-capacity jobs run targeted live suites and
enforced-offline E2E; pushes to `main` run fast checks. The **CAPI tests**
gate requires the checks applicable to each event.

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
exact slot Lease identity and allocation, full worker-container IDs, and exact
storage volume name.
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

The destructive Azure gate prints phase progress, keeps worker and deletion
proof identity in memory for the active run, and leaves no Tenant-specific
runtime files. A later invocation starts from the live Ready state rather than
resuming a local checkpoint.

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
- The Azure nine-disk destructive and browser/service-proxy agreement gates
  remain unmet; no managed-Azure database rollout is claimed.
- CAPZ `v1.21.3` requires the narrowly scoped external-control-plane webhook
  compatibility selector documented in
  [`docs/azure-experiment-design.md`](docs/azure-experiment-design.md).
- The `v1.21.3` patch pin replaces `v1.21.1`, which left finalizer-free
  `AzureMachinePoolMachine` children after live VMSS deletion. The patch
  release still requires a fresh destructive deletion rerun before acceptance.

See [`docs/high-level-design.md`](docs/high-level-design.md) for the shared
as-built lifecycle architecture and
[`docs/azure-experiment-design.md`](docs/azure-experiment-design.md) for the
Azure profile.
