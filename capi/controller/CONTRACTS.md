# Rust Tenant contracts

`tenancy.cnpg-vcluster.io/v1alpha2` is the only installed Tenant API.
Rust `src/bin/generate.rs` produces the checked-in
`config/crd/bases/tenancy.cnpg-vcluster.io_tenants.yaml` and
`config/rbac/role.yaml`. `just controller-verify` compares those artifacts
against generation without rewriting them. The v1alpha1 CRD, Go manager and
admission webhook are not installation inputs; unsupported legacy state blocks
the current installer and is not migrated or deleted. See
[`API_COMPATIBILITY.md`](API_COMPATIBILITY.md) for the public contract.
The repository-root Cargo workspace owns the shared dependency versions,
release profile, and lockfile; this crate inherits its dependencies from that
workspace.

The root `rust-toolchain.toml` selects Rust/Cargo 1.98.1 with the
minimal profile, Clippy, and rustfmt; the crate retains an MSRV of 1.89. The
Python wrappers leave Cargo home, target, temporary, compiler, flag, wrapper,
profile, and configuration selection to Cargo's defaults. CI consumes the
same toolchain file through `actions-rust-lang/setup-rust-toolchain`, including
its integrated `Swatinem/rust-cache`. From `capi/`, fetch the locked dependency
graph online before offline checks:

```sh
just controller-fetch
just controller-verify
just controller-lint
just controller-test
just controller-metrics
just controller-build
```

For direct Cargo usage from `capi/controller/`, run `cargo fetch --locked` and
`cargo run --locked --offline --bin generate -- --check`. Check mode
compares the authoritative CRD/RBAC paths without writing. `cargo fmt
--all --check`, `cargo clippy --locked --offline --all-targets
--all-features -- -D warnings`, and `cargo test --locked --offline
--all-targets --all-features` are the equivalent direct validation commands.

`CAPI_OFFLINE_ENFORCED=1` makes the fetch itself offline. The release wrapper
builds with Cargo's selected target and explicit `--locked --offline`.
Static CRT flags apply to the final manager binary, not proc-macro
dependencies; Cargo's JSON artifact output identifies the executable, and
packaging rejects dynamic ELF dependencies before staging it with verified
Calico/CNPG assets in a scratch image. An empty Cargo home cannot satisfy the
offline build.

Cargo discovers four integration targets: `controller`, `adapters`,
`allocation`, and `finalization`. They share the Kubernetes API simulator
under `tests/support/`. `controller-metrics` reports production Rust source
before test-only modules and rejects growth above the 12,000-line workflow ceiling.

Installation uses one `Recreate` replica, a separate leader-election Lease,
and HTTP `/healthz` and `/readyz`, without admission ports or TLS mounts.
Local mode additionally uses Docker, the schema-3 local foundation, and staged
assets. Azure mode uses the schema-1 Azure provider ConfigMap and no Docker
socket or Azure credentials. The same binary accepts `--provider=local|azure`;
one deployment installs exactly one lifecycle implementation.

The generated management-resource JSON is the cross-language local operator
contract. Entries declare exact served API identity and scope,
Tenant/worker/kubeconfig/observed/allocation naming, whether the resource is
watched, watch-name suffix or cluster-label routing, inventory policy and
namespace, evidence participation, and checked narrow exemptions. Rust and
Python reject malformed catalogs, unserved declared versions, malformed list
envelopes/items/owner references, and ambiguous identity.
Python inventories exact versioned raw collection paths: the list envelope
must declare the catalog API version and `<Kind>List`, while items must have
valid name, UID, and scope. Raw-list items may omit redundant `apiVersion` and
`kind`; if present, these must match. Rust likewise accepts absent dynamic
item type metadata but rejects mismatched supplied types.
Allocation names, fixed controller infrastructure, provider-only CRDs,
break-glass allowlists, test fixtures, and tenant-internal resources remain
domain-owned rather than duplicating catalog semantics.

Tenant status and finalizer writes share exact UID/generation/spec/deletion
validation. Every patch includes UID and resourceVersion and validates the
returned UID. General status mutation retries four conflicts through direct
rereads; finalizer mutation does not retry internally and conflicts requeue
before failure-status reporting. Allocation removal publishes explicit null
before finalizer removal.

PR fast checks upload the verified static manager and PR E2E consumes that
same-revision artifact from `.tools/artifacts`. Scheduled and manually
dispatched high-capacity validation do not use the artifact and retain a clean
enforced-offline release build.

Azure lifecycle authority is Rust-only. Python may provision and inspect the
shared foundation, submit/observe/delete the Tenant CR, externally prove Azure
absence, and run the explicit VMSS replacement gate. Static checks require the
old rendering/lifecycle/deletion modules and filesystem Tenant runtime to
remain absent.
