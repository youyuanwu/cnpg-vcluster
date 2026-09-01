---
date: 2026-09-01 22:24:17 UTC
git_commit: fe4ce55
branch: feature/private-node-cnpg-tenants
repository: cnpg-vcluster
topic: "Private Node CNPG Tenants Spec Research"
tags: [research, specification]
status: complete
---

# Spec Research: Private Node CNPG Tenants

## Summary

Current vCluster documentation supports tenant control planes hosted as
containers on a central Kubernetes cluster with private worker nodes joined
directly to each tenant. Private-node mode disables all host resource
synchronization, requires tenant-side scheduling, and installs tenant-owned
Flannel, kube-proxy, and Local Path Provisioner components by default.
vCluster Platform is mandatory, but free mode is sufficient.

The documented private-node support boundary is a systemd-capable Linux
machine, with Ubuntu 22.04 or 24.04 recommended. A privileged Ubuntu systemd
container is not listed as supported worker infrastructure. It can preserve
private-node semantics for a single-host lab if it satisfies kubeadm and kernel
requirements, but must be labeled an experimental, unsupported
container-as-machine approximation. All such node containers still share the
Docker host kernel and failure domain.

## Agent Notes

The approved topology is one Docker host, one kind Kubernetes control cluster
hosting vCluster Platform/control plane, and exactly two linked vCluster tenant
clusters. Both tenants must use vCluster private nodes, with exclusive
systemd-capable worker containers or VM-containers on the same Docker host.
Each tenant owns its CNI, storage provisioner, workloads, services, volumes,
CloudNativePG operator and CRDs, and one three-instance PostgreSQL cluster.
Tenant workloads and storage resources must not be synchronized into kind.

Versions and automation must be reproducible. Current upstream requirements
must be established before implementation, and any unsupported local-container
approach must be qualified with evidence rather than replaced by shared nodes
or independent vind clusters.

## Research Findings

### Question 1: What are the current prerequisites, licensing requirements, and supported installation flow?

**Answer**: Private nodes require vCluster Platform, and Platform Free mode is
explicitly supported. Private Nodes are not part of unlicensed OSS mode; Free
mode requires Platform connectivity and interactive account/email activation,
but no credit card. Platform installation requires cluster-admin access, Helm
3.10 or later, kubectl, and egress to the Platform licensing service. The
preferred install flow is `vcluster platform start`; a pinned Helm install is
also documented. The current stable Platform release is 4.11.2 and the current
stable vCluster release is 0.36.1.

**Evidence**: vCluster “Private Nodes Quick Start” and “Compare open source and
free tiers”; Platform “Install with CLI” and “Install with Helm”; GitHub
release APIs for `loft-sh/loft` and `loft-sh/vcluster`, checked 2026-09-01.
The local Platform 4.11.2 test returned `license limits are exceeded` when the
first linked private-node tenant was created before Free activation.

**Implications**: Setup must install Platform in kind, pin Platform and
vCluster versions, verify administrative and egress prerequisites, and stop
with explicit activation guidance when Free mode has not yet been activated.
The activation itself cannot be completed non-interactively without a user's
email/account action.

### Question 2: How do private nodes join, and what node requirements apply?

**Answer**: A private-node tenant is created with private nodes enabled from
the start. A one-hour bootstrap token yields a node join script. The script
installs containerd, kubelet, and a `vcluster-vpn` systemd service, then performs
kubeadm TLS bootstrap. Supported hosts require Linux with systemd, iptables,
curl, root access, and kubeadm-compatible kernel/network behavior. Ubuntu
22.04 and 24.04 are listed as supported. Kubeadm additionally expects unique
node identity, adequate memory, full node connectivity, required ports, a CRI
runtime, and swap disabled or explicitly tolerated.

**Evidence**: vCluster “Node Requirements” and “Join Manually Provisioned
Nodes”; Kubernetes “Installing kubeadm”.

**Implications**: Each worker container must boot systemd, run nested
containerd and kubelet, have a unique hostname and MAC, expose required kernel
features, and pass preflight checks before joining. Docker does not expose the
host DMI product UUID inside these containers, so join behavior must be
established by the runtime test rather than assumed.

### Question 3: Are privileged systemd Docker containers supported private nodes?

**Answer**: Official private-node documentation specifies supported Linux
machines and operating systems, not Docker containers. It does not claim that
nested, privileged systemd containers are supported worker machines. kind
demonstrates Kubernetes nodes in Docker, but that is a separate purpose-built
node image and does not extend vCluster's private-node support matrix.

**Evidence**: vCluster “Node Requirements” supported-OS table; kind “Quick
Start” description of container nodes; no container worker option in the
vCluster manual-node documentation.

**Implications**: Repository automation may use privileged Ubuntu 24.04
systemd containers as the closest reproducible single-host approximation, but
must fail rather than use shared nodes, and must clearly document that this
worker substrate is experimental and outside the stated support matrix.

### Question 4: Which resources remain private and how is host synchronization prevented?

**Answer**: With private nodes, vCluster performs no resource synchronization
to the control-plane cluster because no control-plane-cluster worker is used.
All `sync.*` settings and sync-dependent integrations are unavailable.
Scheduling is tenant-side and cannot be disabled. Tenant nodes and workloads
are absent from the kind Kubernetes API.

**Evidence**: vCluster “Private Nodes”, especially “Other vCluster feature
limitations” and the quick start “The isolation boundary”.

**Implications**: Configuration should rely on private-node mode's enforced
no-sync behavior and verification must assert that tenant pods, services,
PVCs, PVs, CNPG CRDs, and CNPG resources are absent from kind.

### Question 5: What CNI and storage components are required?

**Answer**: vCluster installs Flannel, kube-proxy, and Rancher Local Path
Provisioner in each private-node tenant by default. The Local Path Provisioner
provides the default StorageClass. CoreDNS is a tenant control-plane component,
configured separately under `controlPlane.coredns`, rather than a private-node
deploy add-on. Alternatives can be installed, but become user-managed and
outside vCluster support.

**Evidence**: vCluster “Deploy vCluster AddOns”, including its “Control plane
components” section. Current upstream releases checked 2026-09-01 are Flannel
0.28.9 and Local Path Provisioner 0.0.37, while vCluster's bundled image
defaults are controlled by the pinned vCluster chart.

**Implications**: Retain and verify the supported bundled defaults rather than
introducing independent CNI/CSI complexity. Storage remains node-local and
tenant-owned.

### Question 6: What networking is required on one Docker host?

**Answer**: vCluster VPN requires both tenant control planes and worker nodes
to reach the Platform URL. It uses WireGuard/Tailscale technology and can relay
through Platform when direct connectivity fails. Node-to-node VPN is optional;
when enabled, default Flannel uses `tailscale0`. Platform also documents ports
8443, 9443, 9444, and 9090 between Kubernetes control-plane components and
Platform services. Kubeadm and CNI ports must remain reachable among each
tenant's workers.

**Evidence**: vCluster “vCluster VPN”; Platform install prerequisites;
Kubernetes kubeadm networking requirements.

**Implications**: Use a dedicated Docker bridge shared by kind and worker
containers, make the Platform URL resolvable/reachable there, enable
node-to-control-plane VPN, and assign non-overlapping tenant pod/service CIDRs.
Keep node-to-node VPN disabled while direct worker connectivity is verified;
upstream recommends it only where nodes cannot directly reach each other or
traffic encryption is required.

### Question 7: Which versions are mutually compatible?

**Answer**: Current stable releases are kind 0.33.0, vCluster 0.36.1, Platform
4.11.2, and CloudNativePG 1.30.0. kind 0.33.0 publishes a Kubernetes 1.36.4
node image with a required digest. vCluster 0.36 defaults to Kubernetes 1.36.0.
CloudNativePG 1.30 supports Kubernetes 1.34 through 1.36 and PostgreSQL 14
through 18. Local Path Provisioner 0.0.37 and Flannel 0.28.9 are current, but
private-node defaults are delivered by the pinned vCluster release.

**Evidence**: Official GitHub release APIs checked 2026-09-01; vCluster
private-node quick start; CloudNativePG “Supported releases”.

**Implications**: Pin kind's central node image to Kubernetes 1.36.4, tenant
Kubernetes to 1.36.0, vCluster 0.36.1, Platform 4.11.2, CloudNativePG 1.30.0,
and a specific PostgreSQL 18.4 operand image.

### Question 8: Does CloudNativePG support the required cluster shape?

**Answer**: CloudNativePG's local quick start explicitly deploys a three-node
PostgreSQL `Cluster`. Version 1.30 supports vanilla Kubernetes 1.36 and
PostgreSQL 14–18. Each instance receives persistent storage from the tenant's
default StorageClass. The operator and CRDs are ordinary Kubernetes resources
and can be installed separately in each tenant API.

**Evidence**: CloudNativePG “Quickstart” and “Supported releases”; CloudNativePG
1.30.0 release.

**Implications**: One three-instance CNPG `Cluster` per tenant is valid. Tests
must account for node-local PVs and use replication/failover rather than volume
relocation as the recovery mechanism.

### Question 9: What status surfaces verify the environment?

**Answer**: Official flows use `kubectl get nodes`, Platform/vCluster CLI
cluster status, `systemctl`/`journalctl` for `vcluster-vpn`, tenant Kubernetes
resources, and CNPG `Cluster` status. CNPG labels resources with
`cnpg.io/cluster`; its read/write service and generated application secret
provide a direct data-path test.

**Evidence**: vCluster private-node quick start, join, VPN troubleshooting;
CloudNativePG quick start and operational documentation.

**Implications**: Status and verification automation should cover Platform,
tenant control planes, exclusive nodes, VPN, CNI, StorageClass/PVs, per-tenant
CNPG ownership, SQL write/read, and primary transition.

### Question 10: What is the safe teardown order?

**Answer**: Delete tenant workloads first, then tenant clusters; reset joined
nodes with the join script's `--reset-only` mode; remove worker machines; then
uninstall Platform and delete the named control-plane cluster. Deleting a
vCluster invalidates its token and leaves node components installed but
inactive until reset.

**Evidence**: vCluster private-node quick start cleanup and join-script flags;
kind quick start deletion behavior.

**Implications**: Teardown must retain per-node reset material long enough to
reset workers, target only repository-named resources, and verify cleanup at
each boundary.

## Open Unknowns

- Whether vCluster's join preflight accepts the chosen privileged Ubuntu
  systemd container without adjustments cannot be known from documentation;
  it requires the repository's end-to-end runtime test.
- Whether Loft Router can provide a stable enough Platform endpoint from this
  non-interactive local environment depends on external service availability
  during execution.
- Private-worker join and database validation remain blocked in the current
  environment until the self-hosted Platform Free tier is activated through
  the user/account flow required by upstream.

## User-Provided External Knowledge

- Docker 29.7.2 with overlayfs, 16 CPUs, and approximately 31 GiB RAM is
  available. Local inspection confirmed 33.6 GB visible to Docker.
- The topology, tenant count, private-node requirement, per-tenant CNPG
  ownership, and three-instance cluster shape are user-approved constraints.
