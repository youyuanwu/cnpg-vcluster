# Kamaji CloudNativePG tenant lab

This directory is an independent, Linux-only local experiment for one kind
management cluster, two Kamaji hosted control planes, and tenant-owned
CloudNativePG databases. It uses the public Apache-2.0 Kamaji
**26.8.6-edge** release and requires no account, activation key, or paid
artifact. The edge channel is experimental; CLASTIX's stable artifact channel
is not freely downloadable.

The permanent compatibility gate uses one spike-only hosted control plane and
worker. Phase 4 adds the fixed two-tenant/six-worker topology and the scoped
kube-proxy remediation. The spike now passes every networking and persistence
rung; this host blocks while scaling the final topology to its second worker,
then removes all final tenant state.

## Prerequisites

- Linux with Docker Engine, cgroup v2, and permission to run privileged
  containers.
- At least 12 logical CPUs, 24 GiB of Docker memory, and 30 GiB free in
  Docker's storage filesystem.
- `curl`, `git`, `python3`, `sha256sum`, `tar`, GNU `timeout`, and the Docker
  buildx plugin.
- Host-installed `just` **1.58.0**. The lab never downloads, installs, or
  replaces `just`.

Install `just` yourself from the upstream release only after verifying the
archive. For x86_64 Linux musl:

```bash
curl -fLO \
  https://github.com/casey/just/releases/download/1.58.0/just-1.58.0-x86_64-unknown-linux-musl.tar.gz
echo '4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d  just-1.58.0-x86_64-unknown-linux-musl.tar.gz' \
  | sha256sum -c -
tar -xzf just-1.58.0-x86_64-unknown-linux-musl.tar.gz just
install -m 0755 just "$HOME/.local/bin/just"
just --version
```

The final command must print `just 1.58.0`. Remove the downloaded archive and
extracted binary after installation.

## Implemented commands

```bash
just tools
just preflight
just create-management
just spike
just destroy-spike
just create
just status
just diagnose all
just test-static
```

`just tools` installs checksum-verified kind 0.33.0, kubectl 1.36.4, and Helm
3.21.4 under `.tools/`; downloads immutable direct inputs; verifies the Kamaji
release tag/source/lock; places the exact kamaji-etcd 0.15.0 dependency beside
the source chart; and renders a digest-only transitive image inventory. It
does not install `just`.

`just preflight` validates tools, Linux Docker, cgroup v2, capacity, network
overlap, prepared checksums, remote image digests, and a short-lived
privileged-container probe. It creates no kind cluster, tenant, credential, or
retained Docker resource. `MIN_DOCKER_CPUS`, `MIN_DOCKER_MEMORY_GIB`, and
`MIN_DOCKER_STORAGE_GIB` are intentional environment inputs for operators who
want stricter admission and for deterministic rejection tests; lowering them
weakens the documented lab admission floor.

`just create-management` creates or reconciles only the management plane. It
records the exact kind node container identity before later adoption, derives
two free addresses from the actual kind Docker IPv4 subnet, installs the
pinned dependencies in order, and gates their CRDs, webhooks, controller
workloads, three `1Gi` datastore PVCs, and `DataStore/default`. A same-named
kind cluster without matching local ownership evidence is refused and is
neither adopted nor deleted.

`just spike` runs this ordered ladder:

1. upstream-equivalent privileged `kindest/node` kubeadm join;
2. Kubernetes 1.36.4, systemd kubelet, and the fixed LoadBalancer VIP;
3. Calico, CoreDNS, kube-proxy, Konnectivity, DNS, and service routing;
4. persistent `/var/lib`, default Local Path storage, a bound PVC, scheduled
   request accounting against Docker caps, and marker survival across worker
   recreation and rejoin.

The spike refuses with exit `1` before mutation if a final tenant TCP, schema
credential, kubeconfig, worker, or volume exists. Otherwise it always removes
its TCP, namespace, worker, volume, schema, etcd user/role, datastore Secret,
kubeconfig, borrowed-VIP consumer, and runtime subtree. Only the healthy
management plane and mode-`0600` result/blocker evidence remain.

Kamaji 26.8.6-edge does not create the Kubernetes 1.36.4 bootstrap permission
needed to read the named `kubeadm-config` and `kubelet-config` ConfigMaps. The
spike adds one namespaced Role with only `get` on those two resource names and
binds only `system:bootstrappers:kubeadm:default-node-token`; it does not grant
general ConfigMap reads.

Additional container-only bootstrap adjustments are exact:
Konnectivity tolerates the worker's temporary `not-ready:NoSchedule` taint,
Calico's installer receives only the fixed API VIP/port before Service routing
exists. The original live target blocked when kube-proxy tried to write
`/proc/sys/net/netfilter/nf_conntrack_max`. On this host, the Linux kernel
exposes `net.netfilter.nf_conntrack_max` read-only in a separate network
namespace even to a privileged container; nesting, Docker/runc masking, and
missing privilege are not the cause. The implemented remediation configures
kube-proxy with `conntrack.maxPerCore: 0` before it starts, matching kind's
container-node behavior. Kamaji's
`spec.addons.kubeProxy` fields select the image repository and tag but do not
directly expose this setting. The lab therefore pauses TCP reconciliation,
patches only the generated kube-proxy ConfigMap through the explicit tenant
kubeconfig, verifies the value immediately and after ten seconds, and performs
all worker joins afterward. An unpaused probe reverted the value within two
seconds; the paused value remained throughout the passing spike. kube-proxy
stays enabled and Calico is unchanged.

`just create` reuses only that current passing compatibility revision, removes
and verifies every spike residual, checks that borrowed VIPs and datastore
identities are free, then reconciles `tenant-a` and `tenant-b`, three exclusive
workers each, independently rendered Calico and Local Path resources, DNS,
Service routing, endpoint reachability, and one bound smoke PVC per tenant.
Workers and volumes have exact ownership labels, persistent `/var/lib`,
same-name refusal, stopped/stale handling, partial rejoin, and short-lived
token cleanup.

On the current host, repeated final runs reach the exact first failing
prerequisite `tenant-a-workers`: with both TCPs present and worker 1 joined,
worker 2 exits during systemd startup with
`running=false,exit=255,oom=false`. One bounded recreate/retry produces the
same result. Exit `2` cleanup removes both TCPs/namespaces/schemas/credentials,
all kubeconfigs, workers, tenant volumes, and tenant runtime subtrees. Only the
healthy management plane and blocker/result evidence remain.

`status` and `diagnose` are read-only. They report tools, Docker, ownership
evidence, selected VIPs, cert-manager, MetalLB, Kamaji, datastore state, and
tenant-control-plane/spike layers without exporting credentials or reconciling
resources. Final views include both TCP identities, endpoints, kube-proxy
remediation, worker counts, add-ons, storage classes, smoke PVCs, and blocked
residual checks.

## Security and state

Generated content is confined to ignored `.tools/` and `.runtime/`
directories. Shell entry points use `umask 077`; state directories use mode
`0700`; kubeconfigs and credentials use mode `0600`; and Kubernetes wrappers
always select an explicit kubeconfig. Waits and network operations are finite
and configurable through `config/settings.env`.

Exit status `0` means success, `1` means an ordinary error or unhealthy state,
and exit status `2` is reserved for a recognized compatibility blocker with a
machine-readable blocker record. Management creation never returns `2`;
Compatibility gates may do so only through the dedicated
blocker path.

`KUBEADM_IGNORE_PREFLIGHT_ERRORS` in `config/settings.env` is the single
allowlist source used by the initial no-ignore attempt and any retry. Its current
exact value is empty (`KUBEADM_IGNORE_PREFLIGHT_ERRORS=""`): no preflight
exception is ignored unless the target 1.36.4 join first reports the exact
container-specific error set. The join token TTL is `10m`; tokens and mode
`0600` join files are deleted after every attempt and are never printed by
normal commands.

## Licensing, telemetry, and support boundary

Kamaji telemetry is enabled by upstream defaults. This lab installs it with
`telemetry.disabled: true`, rendered as `--disable-telemetry`; no account or
license credential is used.

Kamaji documents regular Linux virtual machines and bare metal as workers. The
privileged `kindest/node` workers are an unsupported
container-as-machine compatibility experiment. They share one host kernel,
Docker daemon, storage, networking, power, and failure domain. They do not
provide hostile-tenant, kernel, hardware, or production isolation. If standard
bootstrap cannot converge, `just spike` returns the recognized blocked result
with its first failing rung instead of claiming tenant or database verification
succeeded.

See [docs/high-level-design.md](docs/high-level-design.md) for the design and
complete deterministic input chain.
