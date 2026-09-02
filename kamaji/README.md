# Kamaji CloudNativePG tenant lab

This directory is an independent, Linux-only local experiment for one kind
management cluster, two Kamaji hosted control planes, and tenant-owned
CloudNativePG databases. It uses the public Apache-2.0 Kamaji
**26.8.6-edge** release and requires no account, activation key, or paid
artifact. The edge channel is experimental; CLASTIX's stable artifact channel
is not freely downloadable.

Phase 1 establishes deterministic inputs, secure local state, host preflight,
read-only inspection, and static tests. It does not create clusters.

## Prerequisites

- Linux with Docker Engine, cgroup v2, and permission to run privileged
  containers.
- At least 12 logical CPUs, 24 GiB of Docker memory, and 30 GiB free in
  Docker's storage filesystem.
- `curl`, `git`, `python3`, `sha256sum`, `tar`, and GNU `timeout`.
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

## Phase 1 commands

```bash
just tools
just preflight
just status
just diagnose
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
retained Docker resource.

`status` and `diagnose` are read-only. Before management creation, they still
report tools, Docker, owned objects, runtime state, blocker state, and the
absence of management resources.

## Security and state

Generated content is confined to ignored `.tools/` and `.runtime/`
directories. Shell entry points use `umask 077`; state directories use mode
`0700`; kubeconfigs and credentials use mode `0600`; and Kubernetes wrappers
always select an explicit kubeconfig. Waits and network operations are finite
and configurable through `config/settings.env`.

Exit status `0` means success, `1` means an ordinary error or unhealthy state,
and Exit status `2` is reserved for a recognized compatibility blocker with a
machine-readable blocker record. Phase 1 has no normal path that returns `2`.

## Licensing, telemetry, and support boundary

Kamaji telemetry is enabled by upstream defaults. The management phase will
install it with `telemetry.disabled: true`; no account or license credential is
used.

Kamaji documents regular Linux virtual machines and bare metal as workers. The
planned privileged `kindest/node` workers are an unsupported
container-as-machine compatibility experiment. They share one host kernel,
Docker daemon, storage, networking, power, and failure domain. They do not
provide hostile-tenant, kernel, hardware, or production isolation. If standard
bootstrap cannot converge, later phases return the recognized blocked result
instead of claiming tenant or database verification succeeded.

See [docs/high-level-design.md](docs/high-level-design.md) for the design and
complete deterministic input chain.
