# Kamaji CloudNativePG tenant lab

This directory is an independent, Linux-only local experiment for one kind
management cluster, two Kamaji hosted control planes, and tenant-owned
CloudNativePG databases. It uses the public Apache-2.0 Kamaji
**26.8.6-edge** release and requires no account, activation key, or paid
artifact. The edge channel is experimental; CLASTIX's stable artifact channel
is not freely downloadable.

The permanent compatibility gate uses one spike-only hosted control plane and
worker. Phase 4 adds the fixed two-tenant/six-worker topology, the scoped
kube-proxy remediation, and explicit host inotify admission. The previously
observed worker exit `255` was host `fs.inotify.max_user_instances=128`
exhaustion during systemd startup, not evidence that the worker substrate or
host capacity was unsupported.

## Prerequisites

- Linux with Docker Engine, cgroup v2, and permission to run privileged
  containers.
- At least 12 logical CPUs, 24 GiB of Docker memory, and 30 GiB free in
  Docker's storage filesystem.
- Host inotify limits of at least `fs.inotify.max_user_instances=1024` and
  `fs.inotify.max_user_watches=524288`. The instance floor is proven for the
  management node plus six workers; the watch floor is the standard
  multi-node kind recommendation and avoids the paired watch bottleneck.
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
just prepare-host
just preflight
just create-management
just spike
just destroy-spike
just create
just verify
just status
just diagnose all
just test-static
just test-inotify-negative
just test-kube-proxy-restart tenant-a
```

`just tools` installs checksum-verified kind 0.33.0, kubectl 1.36.4, and Helm
3.21.4 under `.tools/`; downloads immutable direct inputs; verifies the Kamaji
release tag/source/lock; places the exact kamaji-etcd 0.15.0 dependency beside
the source chart; and renders a digest-only transitive image inventory. It
does not install `just`.

`just prepare-host` records the current two inotify values once in ignored,
mode-`0600` runtime state, raises only values below the required floors with
non-interactive `sudo sysctl -w`, and verifies the result. In a non-interactive
lab session without cached sudo credentials, membership in the Docker group
allows the same runtime-only write through a short-lived privileged pinned
worker container; the recipe reports that fallback explicitly. It is
idempotent and does not create persistent `/etc/sysctl.d` configuration.
Phase 6 full teardown restores the recorded originals only after all nested
workers are gone. Creation never changes host sysctls implicitly.

`just preflight` validates tools, Linux Docker, cgroup v2, CPU/memory/storage
capacity, both inotify floors, network
overlap, prepared checksums, remote image digests, and a short-lived
privileged-container probe. It creates no kind cluster, tenant, credential, or
retained Docker resource. `MIN_DOCKER_CPUS`, `MIN_DOCKER_MEMORY_GIB`, and
`MIN_DOCKER_STORAGE_GIB`, `MIN_INOTIFY_INSTANCES`, and
`MIN_INOTIFY_WATCHES` are intentional environment inputs for operators who
want stricter admission and for deterministic rejection tests; lowering them
weakens the documented lab admission floor. `just test-inotify-negative`
proves both inotify failures occur before any retained mutation.

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
directly expose this setting. The lab therefore lets Kamaji initially
reconcile each control plane and its managed add-on objects, then pauses that
TCP, patches only the generated kube-proxy ConfigMap through the explicit
tenant kubeconfig, verifies the value immediately and after ten seconds, and
performs all worker joins afterward. An unpaused probe reverted the value
within two seconds, so the successful steady state intentionally retains the
pause. This pauses only future Kamaji reconciliation: the hosted API,
control-plane pods, tenant workers, kube-proxy, CoreDNS, Konnectivity, Calico,
and tenant workloads keep running. This is an experimental container-worker
workaround, not normal full Kamaji reconciliation. kube-proxy stays enabled
and Calico is unchanged.

`just create` reuses only that current passing compatibility revision, removes
and verifies every spike residual, checks that borrowed VIPs and datastore
identities are free, then reconciles `tenant-a` and `tenant-b`, three exclusive
workers each, independently rendered Calico and Local Path resources, DNS,
Service routing, endpoint reachability, one bound smoke PVC per tenant, and
independent CloudNativePG 1.30.0 operators with three PostgreSQL 18.4
instances per tenant. Set `SKIP_CNPG=1` only when intentionally reconciling
the worker/add-on layers without installing or changing database resources.
Workers and volumes have exact ownership labels, persistent `/var/lib`,
same-name refusal, stopped/stale handling, partial rejoin, and short-lived
token cleanup.

Validated Phase 4 runs leave exactly two Ready, intentionally paused TCPs,
three disjoint Ready workers per tenant, healthy
CoreDNS/kube-proxy/Konnectivity/Calico, one default Local Path class, and a
Bound smoke PVC per tenant. Repeating
`just create` first requires the pause annotation, remediation revision, and
`maxPerCore: 0` to remain intact and fails instead of silently repairing a
drifted TCP. It replaces no healthy worker. Removing one owned worker and
rerunning create restores only that worker while retaining its owned volume.

The former `tenant-a-workers` exit `255` was reproduced by exhausting the
host-root inotify instance pool and eliminated by raising
`max_user_instances` to `1024`. Failed worker starts now preserve sanitized
mode-`0600` Docker inspect state and log tails before cleanup; runtime/systemd
probes run only for a live container. The bounded retry reuses the owned
`/var/lib` volume rather than recreating it.

`just test-kube-proxy-restart tenant-a` deletes one tenant kube-proxy pod,
waits for its replacement to become Ready, rejects the original
`nf_conntrack_max` permission crash, and proves the ConfigMap remains
`maxPerCore: 0` while the TCP stays paused.

`just verify` checks exact management/TCP/schema/worker ownership, confirms no
tenant CNPG or database storage resource appears through the management API,
and validates one operator, one Cluster, three distinct PostgreSQL placements,
three claims, and three volumes per tenant. It writes distinct SQL markers,
proves both Kubernetes and PostgreSQL credentials are rejected by the opposite
tenant without mistaking connectivity failure for authentication rejection,
then exercises replica replacement with PVC/PV reuse and primary failover with
retained data.

`status` and `diagnose` are read-only. They report tools, Docker, ownership
evidence, selected VIPs, cert-manager, MetalLB, Kamaji, datastore state, and
tenant-control-plane/spike layers without exporting credentials or reconciling
resources. Final views include both TCP identities, pause/remediation state,
endpoints, kube-proxy configuration, worker counts, Ready replica counts for
CoreDNS, kube-proxy, Konnectivity, Calico, and Local Path, storage classes,
smoke PVCs, and blocked residual checks. They exit unhealthy when an expected
replica, pause, patch, or storage gate is absent.
Healthy tenant views also report CNPG CRDs, webhooks, RBAC, operator image and
readiness, PostgreSQL services and primary, instance placements, PVCs, PVs,
and recent database events.

## Security and state

Generated content is confined to ignored `.tools/` and `.runtime/`
directories. Shell entry points use `umask 077`; state directories use mode
`0700`; kubeconfigs and credentials use mode `0600`; and Kubernetes wrappers
always select an explicit kubeconfig. Waits, including `systemctl is-system-running --wait`, and network operations are finite
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
