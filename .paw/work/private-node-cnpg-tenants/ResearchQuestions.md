# Research Questions: Private Node CNPG Tenants

**Target Branch**: `feature/private-node-cnpg-tenants`
**Issue URL**: none

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

## External Research Questions

1. What are the current official prerequisites, licensing requirements, and
   supported installation flow for vCluster Platform plus linked vClusters
   using private nodes?
2. How do vCluster private nodes join a tenant, and what operating-system,
   systemd, kernel, container-runtime, VPN, networking, and Kubernetes
   requirements apply to each private worker?
3. Can private nodes be implemented reproducibly as privileged,
   systemd-capable Docker containers on one Linux Docker host? Which
   container/VM-container approaches are supported, documented, or known to
   work, and what limitations must be stated?
4. Which resources stay entirely inside a private-node vCluster, which
   components still run on the central host cluster, and what vCluster
   configuration prevents pods, services, PVCs, PVs, and CNPG custom resources
   from being synchronized to the kind control cluster?
5. What CNI and storage components are required within each private-node
   tenant, and which choices are compatible with the selected Kubernetes and
   container runtime in a systemd-capable node container?
6. What kind topology, ports, host networking, routes, and Docker capabilities
   are needed for the Platform, VPN, tenant control planes, and private worker
   containers to communicate on one Docker host?
7. What current, mutually compatible versions of kind/Kubernetes, vCluster
   Platform/vCluster CLI/chart, private-node components, CNI, storage
   provisioner, CloudNativePG, and PostgreSQL should be pinned?
8. Does current CloudNativePG support one three-instance `Cluster` per tenant
   on these private nodes, and what storage, scheduling, webhook, and
   Kubernetes-version constraints must the manifests and verification account
   for?
9. Which official commands and status surfaces can verify tenant linkage,
   private-node readiness, resource isolation, CNI, storage, CNPG readiness,
   PostgreSQL replication, data access, and primary failover?
10. What teardown order safely removes both CNPG clusters, tenant-owned storage,
    private workers, linked vClusters, Platform components, and the named kind
    cluster without affecting unrelated Docker or Kubernetes resources?
