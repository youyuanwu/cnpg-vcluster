# Private Node CNPG Tenants

## Overview

The implementation supplies a complete repository-owned local lab for two
CloudNativePG tenants. One kind cluster hosts vCluster Platform and both tenant
control planes. Each tenant is configured for private nodes and receives three
exclusive privileged Ubuntu systemd containers that run containerd, kubelet,
and vCluster VPN.

Each tenant independently owns Flannel, kube-proxy, CoreDNS, Local Path
Provisioner, CloudNativePG 1.30.0, and one three-instance PostgreSQL 18.4
cluster. Private-node mode disables vCluster resource synchronization, so
tenant workloads and volumes are absent from the kind API.

## Architecture and Design

### High-Level Architecture

The central kind cluster is a management and control-plane host only.
`tenant-a` and `tenant-b` each have separate APIs, non-overlapping pod/Service
CIDRs, separate private workers, and separate node-local storage. The tenant
control planes connect to workers through node-to-control-plane VPN. Worker
containers share a Docker bridge, so node-to-node VPN is intentionally
disabled and direct cross-node connectivity is verified.

The worker image uses Ubuntu 24.04 and systemd. Each worker has a fixed unique
hostname, a private cgroup namespace, a 3 GiB memory ceiling, a 2 CPU burst
ceiling, and a dedicated Docker volume mounted at `/var/lib` for nested
containerd, kubelet, and local-path data. Docker assigns a distinct MAC for the
container lifetime; recreating a container can assign a different MAC.

### Design Decisions

- **Fail closed on architecture**: tenant creation always uses the Helm driver
  with private nodes and Platform linkage. No shared-node, vind, unlinked, or
  Docker-driver fallback exists.
- **Supported tenant defaults**: Flannel, kube-proxy, CoreDNS, and Local Path
  Provisioner remain under vCluster management.
- **Three workers per tenant**: required hostname anti-affinity places the
  three PostgreSQL instances on distinct local worker containers.
- **Repository-local tools and state**: downloaded CLIs stay in `.tools/`.
  Kubeconfigs, credentials, join scripts, blockers, and logs stay in
  `.runtime/` with `0700` directories and `0600` sensitive files.
- **Two-tier pinning**: direct inputs are checksum/digest pinned. Components
  delivered by vCluster are fixed by vCluster 0.36.1 and inventoried at runtime.

### Integration Points

`scripts/create.sh` invokes kind, vCluster Platform, vCluster tenant creation,
the private-node join script, kubectl, and the vendored CNPG manifest.
`scripts/verify.sh` uses tenant kubeconfigs exclusively for tenant resources and
the host kubeconfig exclusively for central absence checks.

Platform's Loft Router URL is the default endpoint for private-node VPN.
`PLATFORM_HOST` can select a separately managed reachable HTTPS endpoint.

## User Guide

### Prerequisites

- Linux Docker Engine with cgroup v2 and privileged containers.
- 12 CPUs, 24 GiB Docker memory, and 30 GiB free Docker storage.
- Network access to upstream downloads, registries, Platform licensing, and
  the Platform HTTPS endpoint.
- Browser/email access to activate vCluster Platform Free.

Direct versions are kind 0.33.0, kind Kubernetes 1.36.4, tenant Kubernetes
1.36.0, kubectl 1.36.4, Helm 3.19.0, vCluster 0.36.1, Platform 4.11.2,
CloudNativePG 1.30.0, PostgreSQL 18.4 system-trixie, BusyBox 1.37.0 for
verification, and Ubuntu 24.04.

### Basic Usage

Run `make create`. The command performs preflight, creates kind and Platform,
creates and links both tenants, builds six workers, joins them, installs CNPG
twice, and creates both databases.

The first current-Platform run may stop with
`platform-free-tier-activation-required`. This is an upstream product
requirement: Private Nodes are in the Free tier, which requires Platform
connection and an account/email activation step. The automation records the
blocker in `.runtime/blocker`. Activate through the displayed Platform URL and
rerun the same command.

Run `make status` for a read-only summary and `make verify` for destructive
restart/failover checks. Run `make destroy` to remove the lab.

### Advanced Usage

- `SKIP_CNPG=1 make create`: bootstrap only through private-node add-ons.
- `PLATFORM_HOST=https://... make create`: use an existing reachable Platform
  hostname instead of the generated Loft Router endpoint.
- `PLATFORM_CA_FILE=path/to/ca.pem`: trust a custom CA for the Platform and join
  endpoint without disabling TLS verification.
- `make diagnose TENANT=tenant-a`: focus Kubernetes events and worker service
  logs on one tenant.
- Override finite wait values from `config/settings.env` in the environment
  when a slower host needs additional time.

## API Reference

### Key Components

- `scripts/lib/common.sh`: names, paths, kubeconfig wrappers, permissions,
  duration parsing, bounded retries, checksum checks, and blocker recording.
- `scripts/lib/platform.sh`: Platform credentials, installation, login,
  endpoint discovery, reachability, and linked-tenant lookup.
- `scripts/lib/tenant.sh`: tenant control-plane lifecycle, kubeconfig capture,
  join-script handling, node labeling, and add-on readiness.
- `scripts/lib/workers.sh`: systemd worker image/container/volume lifecycle.
- `scripts/lib/cnpg.sh`: per-tenant CNPG installation, readiness, credentials,
  and ephemeral PostgreSQL client.

### Configuration Options

- `config/versions.env`: direct version/checksum/digest pins.
- `config/settings.env`: names, resource thresholds, worker ceilings, and
  timeouts.
- `config/kind.yaml`: the single central kind node and non-overlapping network.
- `config/platform.yaml`: Platform resource sizing and Loft Router setting.
- `config/tenants/*.yaml`: private nodes, VPN, Kubernetes version, CIDRs,
  tenant add-ons, and control-plane persistence.
- `manifests/cnpg/*.yaml`: vendored operator and per-tenant database clusters.

## Testing

### How to Test

`make test-static` validates shell parsing, exact topology, no-sync/no-fallback
configuration, checksums/digests, bounded waits, security modes, database
instance counts, and documentation.

`make test-e2e` creates an unrelated sentinel, proves clean-state status and
forced preflight are fail-closed, attempts the full lifecycle, repeats create,
runs status and verification, destroys twice, and proves the sentinel remains.

### Observed Result

On 2026-09-01 the host passed tool, Docker capacity, kind, Platform, endpoint,
static, read-only status, and idempotent scoped teardown tests. A separate
repository worker-image probe booted the pinned image with systemd and a
writable private cgroup v2 mount. The main lifecycle then reached Platform
4.11.2, which rejected the first linked private-node tenant because Free mode
had not been activated:

`request is blocked because license limits are exceeded`

The repository reports `platform-free-tier-activation-required`, preserves
evidence with restrictive permissions during the run, and exits without
creating shared-node or unlinked substitutes. Worker join, tenant CNPG, SQL,
restart, and failover criteria remain blocked until the required upstream
activation is completed.

### Edge Cases

- Missing capacity, cgroup v2, Docker access, licensing egress, or tool
  integrity fails before cluster creation.
- Missing Platform activation or endpoint writes a recognized blocker.
- Worker join failure identifies the unsupported container substrate.
- Claim/failover timeouts collect tenant and worker diagnostics.
- Repeated teardown succeeds when resources are partial or absent.

## Limitations and Future Work

The container workers are not listed in vCluster's supported private-node
machine matrix. They are privileged and share the Docker host's Linux kernel.
All six PostgreSQL instances, tenant workers, control planes, Platform, and kind
therefore share a single failure and security domain.

Completing Platform Free activation is the remaining external prerequisite for
the full runtime compatibility test. No production hardening, backups,
monitoring stack, autoscaling, external load balancing, or cross-host storage
is included.
