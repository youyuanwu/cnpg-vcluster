# Rust Tenant contracts

`tenancy.cnpg-vcluster.io/v1alpha2` is the only installed local Tenant API.
Rust `src/bin/generate.rs` produces the checked-in
`config/crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml` and
`config/rbac/role.yaml`. `just controller-verify` compares those artifacts
against generation without rewriting them. The v1alpha1 CRD, Go manager and
admission webhook are not installation inputs; cutover rejects existing
Go-managed state instead of migrating it. See
[`API_COMPATIBILITY.md`](API_COMPATIBILITY.md) for the public contract.

Use the system-installed Rust/Cargo >= 1.89; the Python wrappers keep Cargo
home, target and temporary build files under private `../.tools/`, disable
rustup auto-install, and never acquire a compiler. From `capi/`, fetch the
locked dependency graph online before offline checks:

```sh
just controller-fetch
just controller-verify
just controller-lint
just controller-test
just controller-build
```

For direct Cargo usage from `capi/controller/`, set `CARGO_HOME` to
`../.tools/cargo-home` and `CARGO_TARGET_DIR` to
`../.tools/cargo-target`, then run `cargo fetch --locked` and
`cargo run --locked --offline --bin generate -- --check`. Check mode
compares the authoritative CRD/RBAC paths without writing. `cargo fmt
--all --check`, `cargo clippy --locked --offline --all-targets
--all-features -- -D warnings`, and `cargo test --locked --offline
--all-targets --all-features` are the equivalent direct validation commands.

`CAPI_OFFLINE_ENFORCED=1` makes the fetch itself offline. The release wrapper
then builds from a fresh private target with `--locked --offline` and
`CARGO_NET_OFFLINE=true`. Static CRT flags apply to the final manager binary,
not proc-macro dependencies; packaging rejects dynamic ELF dependencies and
stages the static manager with verified Calico/CNPG assets in a scratch image.
An empty Cargo home cannot satisfy the offline build.

Installation uses one `Recreate` replica, a separate leader-election Lease,
Docker socket access, and HTTP `/healthz` and `/readyz`, without admission
ports or TLS mounts. A disposable in-cluster Job probes Kubernetes DNS and
the `default` Namespace with mounted credentials. The Python-produced
schema-3 foundation resolves an ordered slot catalog; non-expiring per-slot
allocation Leases and status bind each Tenant's assigned endpoint and CIDRs.
Only after clean cutover, CEL/fieldValidation/status checks, image checks and
foundation publication does the installer enable creation mutation.
