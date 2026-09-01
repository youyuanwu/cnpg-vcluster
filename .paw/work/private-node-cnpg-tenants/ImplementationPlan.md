# Private Node CNPG Tenants Implementation Plan

## Overview

Implement a repository-owned local lab that creates one kind control cluster,
installs vCluster Platform, creates two Platform-linked private-node tenant
control planes, and joins three exclusive systemd-capable Docker worker
containers to each tenant. Each tenant will use vCluster's private-node
Flannel, kube-proxy, and Local Path Provisioner defaults and will independently
run CloudNativePG plus one three-instance PostgreSQL cluster.

The automation will use explicit kubeconfig files and repository-scoped runtime
state. A privileged Ubuntu worker image will provide the closest reproducible
single-host approximation of a supported Linux private-node machine. Setup will
fail rather than substitute shared nodes if that approximation cannot pass
vCluster's node join.

## Current State Analysis

The repository contains only `docs/high-level-design.md`. It describes one
shared-node tenant, host-synchronized workloads, host storage, and translated
resource diagnostics (`CodeResearch.md`, “Existing Experiment Definition”).
No executable setup, configuration, manifests, tests, Makefile, or README
exists.

Research establishes that vCluster 0.36.1 private-node mode disables all host
resource synchronization and enables tenant-side scheduling. It installs
tenant-owned Flannel, kube-proxy, and Local Path Provisioner by default.
CloudNativePG 1.30.0 supports the selected Kubernetes 1.36.0 tenant version and
a three-instance PostgreSQL cluster. vCluster's documented private-node support
matrix covers systemd-capable Linux machines, not nested Docker containers, so
the worker substrate must remain explicitly experimental.

## Desired End State

- `make create` bootstraps pinned local tools when needed, creates one named kind
  cluster, starts pinned vCluster Platform, creates two private-node tenant
  control planes, starts six tenant-exclusive workers, joins three to each
  tenant, installs CNPG independently, and deploys two three-instance databases.
- `make verify` proves topology, Platform linkage, no-sync isolation, CNI/DNS,
  tenant storage ownership, CNPG ownership/readiness, distinct SQL data paths,
  and primary failover.
- `make status` is read-only and presents focused health/diagnostics for every
  layer.
- `make destroy` is idempotent, resets/removes joined workers, deletes tenants
  and Platform, deletes only the named kind cluster, and removes generated
  runtime state.
- Static validation and an end-to-end test exercise the same public entry
  points.
- Documentation describes the as-built private-node topology and its unsupported
  container-as-machine and shared-kernel limitations.

## What We're NOT Doing

- Shared-node vClusters or synchronized tenant workloads/storage.
- vind, k3d, minikube, or separate tenant Kubernetes control clusters.
- Production hardening, backups, object storage, monitoring stacks, external
  load balancers, autoscaling, managed storage, or performance benchmarking.
- A reduced tenant count, worker count, or PostgreSQL instance count.
- Custom CNI or CSI components when vCluster's private-node defaults are
  available.
- Persistence after full environment teardown or resilience to Docker-host
  failure.

## Phase Status

- [x] **Phase 1: Repository Foundation and Reproducible Inputs** - Add pinned versions, declarative topology/database inputs, tool bootstrap, and static validation.
- [ ] **Phase 2: Central Platform and Private Worker Bootstrap** - Create kind and Platform, create two linked private-node tenants, and join three exclusive systemd workers to each.
- [ ] **Phase 3: Tenant Databases and Lifecycle Operations** - Install tenant-owned CNPG stacks and implement verification, status, failover, and scoped teardown.
- [ ] **Phase 4: Documentation and End-to-End Validation** - Record the as-built system, update the high-level design, and run the complete local lifecycle.

## Phase Candidates

No unresolved phase candidates.

---

## Phase 1: Repository Foundation and Reproducible Inputs

### Changes Required

- **`Makefile`**: Define public `tools`, `create`, `verify`, `status`,
  `diagnose`, `destroy`, `test-static`, and `test-e2e` entry points.
- **`.gitignore`**: Ignore downloaded tools, caches, generated kubeconfigs,
  credentials, join material, logs, and runtime state while preserving PAW
  artifact lifecycle behavior until finalization.
- **`config/versions.env`**: Pin kind, Kubernetes/node image, kubectl, Helm,
  vCluster, Platform, CloudNativePG, PostgreSQL, and Ubuntu worker versions plus
  integrity values available from upstream. Direct repository inputs are pinned
  explicitly; vCluster-bundled and join-installed transitive component versions
  are captured by the pinned vCluster release and inventoried at runtime.
- **`config/settings.env`**: Define finite overridable waits for kind, Platform,
  tenant APIs, nodes, add-ons, claims, CNPG readiness, pod restart, and failover;
  fixed resource names; a minimum 12-CPU/24-GiB/30-GiB-free-disk host budget;
  and per-worker 2-CPU/3-GiB burst ceilings rather than reserved capacity.
- **`config/kind.yaml`**: Define the single-control-plane kind cluster and
  Docker-network-compatible API exposure required by Platform and tenant
  control planes.
- **`config/platform.yaml`**: Define deterministic Platform resource sizing and
  local-lab settings without committed credentials.
- **`config/tenants/tenant-a.yaml`**, **`config/tenants/tenant-b.yaml`**: Enable
  private nodes and node-to-control-plane VPN, pin Kubernetes, retain
  tenant-owned Flannel, kube-proxy, and Local Path Provisioner, retain the
  tenant control plane's CoreDNS component, assign non-overlapping pod/service
  CIDRs, and persist tenant control-plane state.
  Node-to-node VPN remains disabled because all workers have direct connectivity
  on one Docker bridge, matching upstream guidance to avoid VPN where direct
  connectivity exists.
- **`config/worker/Dockerfile`**: Build the pinned Ubuntu 24.04
  systemd-capable worker image with vCluster/kubeadm prerequisites.
- **`manifests/cnpg/operator.yaml`**: Vendor or reference the pinned upstream
  CNPG 1.30.0 installation manifest with provenance.
- **`manifests/cnpg/cluster-tenant-a.yaml`**,
  **`manifests/cnpg/cluster-tenant-b.yaml`**: Define one distinct
  three-instance PostgreSQL cluster per tenant, pinned operand image, small
  claims, and required hostname spreading.
- **`scripts/lib/common.sh`**: Centralize repository paths, fixed names, tenant
  metadata, command execution, context isolation, retries, logging, and
  diagnostics. Apply `umask 077`, create runtime directories as `0700`, write
  credentials/kubeconfigs/join material as `0600`, and consume every configured
  timeout so no wait is unbounded.
- **`scripts/tools.sh`**: Download pinned CLI tools into `.tools/`, validate
  checksums, and avoid modifying the host's global toolchain.
- **`scripts/test-static.sh`**: Validate shell syntax, required files, version
  pins, tenant count/CIDR uniqueness, private-node flags, CNPG instance counts,
  and forbidden shared-node/sync configuration.

### Success Criteria

#### Automated Verification

- [ ] Shell parsing passes: `bash -n scripts/*.sh scripts/lib/*.sh`.
- [ ] Static checks pass: `make test-static`.
- [ ] Tool bootstrap reports the pinned versions: `make tools`.
- [ ] Whitespace validation passes: `git diff --check`.

#### Manual Verification

- [ ] Declarative inputs define exactly two tenants and three workers per
  tenant.
- [ ] No configuration permits tenant resource sync to kind or shared-node
  scheduling.
- [ ] Runtime secrets and generated kubeconfigs are covered by ignore rules.
- [ ] Pinning policy distinguishes explicit direct pins from transitive
  vCluster-bundled assets and requires runtime version/image inventory.

---

## Phase 2: Central Platform and Private Worker Bootstrap

### Changes Required

- **`scripts/create.sh`**: Orchestrate prerequisite checks, tool bootstrap,
  Docker capacity checks, kind creation, Platform installation/login, tenant
  creation/linkage, kubeconfig capture, worker image build, six exclusive worker
  containers, token generation, node join, and readiness waits. Preserve
  per-tenant diagnostics and make completed steps safely retryable. Support a
  documented `SKIP_CNPG=1` bootstrap-only mode used by Phase 2 validation.
- **`scripts/lib/platform.sh`**: Encapsulate pinned non-interactive Platform
  startup with generated ignored admin credentials, Loft Router endpoint
  discovery, reachability checks from kind and worker containers, current
  Platform session handling, and verification that both tenants are linked.
  Permit an explicit `PLATFORM_HOST` override for a user-provided reachable
  HTTPS endpoint; if neither endpoint works, fail with diagnostics rather than
  changing tenancy mode.
- **`scripts/lib/tenant.sh`**: Encapsulate private-node tenant creation,
  explicit kubeconfig handling, bootstrap-token generation, join command
  execution without token logging, expected-node checks, and tenant add-on
  readiness.
- **`scripts/lib/workers.sh`**: Build/start fixed-name privileged systemd worker
  containers on the kind Docker network with unique identities, persistent
  node-local data volumes, required cgroup/kernel mounts, resource limits, and
  ownership labels.
- **`scripts/diagnose.sh`**: Capture kind, Platform, tenant control-plane,
  worker-systemd, containerd, kubelet, and VPN diagnostics for a named tenant or
  all layers.
- **`scripts/test-static.sh`**: Add assertions for fixed prefixes, ownership
  labels, unique workers, explicit kubeconfigs, token redaction, and no fallback
  driver. Assert bounded waits and restrictive runtime file modes.

### Success Criteria

#### Automated Verification

- [ ] Updated static checks pass: `make test-static`.
- [ ] Bootstrap-only lifecycle succeeds through ready tenant nodes:
  `SKIP_CNPG=1 make create`.
- [ ] Central cluster shows no tenant worker nodes.
- [ ] Tenant A and Tenant B each report exactly three Ready workers, with
  disjoint names.
- [ ] Platform status lists both linked tenant clusters.
- [ ] Preflight checks Docker/API availability, pinned tool versions including
  Helm 3.10+, cluster-admin capability, cgroup/kernel/network prerequisites,
  host capacity, download/licensing egress, and Platform reachability, and a
  forced preflight failure creates no resources.
- [ ] The selected Platform URL is stable and reachable from kind and all six
  worker containers.
- [ ] A second `SKIP_CNPG=1 make create` and a seeded partial topology converge
  to exactly two tenants and six workers without duplicates or healthy-tenant
  loss.

#### Manual Verification

- [ ] Worker containers boot systemd and run containerd, kubelet, and
  `vcluster-vpn`.
- [ ] Each worker's labels and names unambiguously identify its tenant.
- [ ] A join failure identifies the unsupported container substrate and does
  not attempt shared nodes, Docker driver tenants, or vind.
- [ ] If bounded remediation cannot produce Ready private workers, diagnostics
  record the unsupported-substrate blocker for Phase 4 documentation and mark
  dependent runtime criteria blocked rather than passed.
- [ ] If the Platform endpoint is unavailable after bounded retries and no
  explicit reachable `PLATFORM_HOST` is supplied, diagnostics record an
  external-endpoint blocker with the same fail-closed, no-fallback contract.

---

## Phase 3: Tenant Databases and Lifecycle Operations

### Changes Required

- **`scripts/create.sh`**: Continue after node readiness by installing the
  pinned CNPG manifest independently in each tenant, waiting for operator and
  webhook readiness, applying each tenant's single database manifest, and
  waiting for three ready instances.
- **`scripts/verify.sh`**: Verify fixed topology, private-node isolation,
  Platform linkage, CNI/kube-proxy/CoreDNS, one default tenant StorageClass,
  three bound database claims and tenant PVs, tenant-only CNPG CRDs/resources,
  pod-to-node placement across three distinct node names, independent SQL
  markers, cross-tenant disjoint node/database/claim/volume identities, replica
  restart with PVC reuse and data reread, primary disruption/promotion to a
  different primary, and post-failover data reads. Explicitly assert central
  absence of tenant Services and tenant CNPG CRDs, workloads, admission
  resources, and scoped RBAC. Emit the required StorageClass/provisioner/node/
  claim/volume or database/event diagnostic bundle on timeout and fail on any
  unmet assertion.
- **`scripts/status.sh`**: Provide read-only summaries for Docker, kind,
  Platform, both tenant APIs, worker services, node/add-on health, CNPG
  operators, database clusters, pods, services, claims, and volumes.
- **`scripts/destroy.sh`**: Delete database resources, remove tenant clusters,
  reset joined node services when token/reset material is usable, remove only
  ownership-labeled worker containers/volumes, uninstall Platform, delete the
  named kind cluster, and clear generated state. Remain safe when steps are
  absent or partially completed.
- **`scripts/test-static.sh`**: Assert that verification checks both tenants,
  performs failover and data validation, and checks host absence rather than
  translated resources.

### Success Criteria

#### Automated Verification

- [ ] Runtime criteria below are required when Platform and private workers are
  available; if Phase 2 records an endpoint/substrate blocker, Phase 3 instead
  verifies fail-closed status/diagnostics and successful scoped teardown without
  claiming database success.
- [ ] Static checks pass: `make test-static`.
- [ ] `make status` performs no mutations and returns non-zero for a required
  unhealthy layer.
- [ ] `make verify` confirms each CNPG operator and exactly one three-instance
  cluster per tenant.
- [ ] `make verify` confirms six bound claims/volumes total and no tenant
  workloads, CNPG APIs, claims, volumes, or private nodes in kind.
- [ ] `make verify` writes/reads distinct tenant markers and completes primary
  failover to a different primary with the original marker still readable in
  both tenants.
- [ ] `make verify` restarts one replica per tenant, observes the expected PVC
  reuse and instance rejoin, and rereads the original marker.
- [ ] `make verify` proves tenant A and tenant B have disjoint worker,
  database, claim, volume, and SQL marker identities.
- [ ] `make status` returns non-zero before creation and zero after a healthy
  create, exercising its unhealthy-layer contract.
- [ ] `make destroy` can be run twice and preserves an unrelated sentinel Docker
  container.

#### Manual Verification

- [ ] Status output is concise enough to locate the failing layer without
  exposing credentials or bootstrap tokens.
- [ ] Teardown diagnostics distinguish inactive node components from fully
  removed worker containers.

---

## Phase 4: Documentation and End-to-End Validation

### Changes Required

- **`.paw/work/private-node-cnpg-tenants/Docs.md`**: Record pinned versions,
  repository layout, command contracts, runtime state, component ownership,
  verification semantics, diagnostics, teardown, and observed compatibility
  results. Load `paw-docs-guidance` before writing.
- **`docs/high-level-design.md`**: Replace the shared-node, single-tenant,
  host-synchronized design with the final one-kind/two-private-tenant topology,
  per-tenant CNI/storage/CNPG ownership, Platform/VPN flows, and the explicit
  unsupported container-worker and shared Docker host/kernel limitation.
- **`README.md`**: Add concise prerequisites, quick start, status, verification,
  teardown, resource expectations, and links to the design.
- **`scripts/test-e2e.sh`**: Exercise clean teardown, create, status, full
  verification including restart and failover, repeated create, a controlled
  partial-state convergence case, teardown, repeated teardown, and unrelated
  sentinel preservation using the public Make targets.
- **`scripts/test-static.sh`**: Validate documentation mentions both private
  tenants, no-sync ownership, six workers, two three-instance databases, and
  shared-kernel limitations. Assert that documentation enumerates every direct
  version pin and explains the transitive pinning policy.

### Success Criteria

#### Automated Verification

- [ ] Documentation/static validation passes: `make test-static`.
- [ ] Complete lifecycle passes: `make test-e2e`; if Phase 2 records a verified
  endpoint/substrate blocker after bounded remediation, the test records a
  bootstrap-only blocked result, validates fail-closed status/diagnostics and
  teardown, and does not report runtime criteria as passed.
- [ ] Final clean-state check finds no repository-named kind cluster, worker
  container, worker volume, generated credential, or runtime file.
- [ ] `git diff --check` passes.

#### Manual Verification

- [ ] The high-level diagram and ownership table match observed runtime state.
- [ ] Documentation clearly differentiates vCluster private-node semantics from
  host-kernel isolation and production support.
- [ ] Every user-facing command has prerequisites, expected outcome, and
  failure/diagnostic guidance.

---

## References

- Issue: none
- Spec: `.paw/work/private-node-cnpg-tenants/Spec.md`
- Research: `.paw/work/private-node-cnpg-tenants/SpecResearch.md`,
  `.paw/work/private-node-cnpg-tenants/CodeResearch.md`
