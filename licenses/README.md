# License references

The lab does not commit third-party binaries, charts, source archives,
manifests, images, or vendored Rust crates. Non-Rust inputs are downloaded
into ignored local state from the pinned upstream locations in
`../config/versions.env`; Cargo resolves the Rust crates in
[`Cargo.lock`](../../Cargo.lock) through the system
shared Cargo home. The controller and Tenant Admin images include statically
linked binaries built from that locked crate graph. The admin image also
includes generated JavaScript, WebAssembly, and CSS produced by the pinned
Trunk and wasm-bindgen CLI inputs; those browser bundles are ignored build
output and are not committed.

The authoritative license texts are maintained by the upstream projects:

- Apache License 2.0: <https://www.apache.org/licenses/LICENSE-2.0>
- MIT License: <https://opensource.org/license/mit>
- PostgreSQL License: <https://www.postgresql.org/about/licence/>
- GNU General Public License version 2:
  <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>
- CC0 1.0: <https://creativecommons.org/publicdomain/zero/1.0/legalcode>

Trunk, wasm-bindgen CLI, Leptos, and the new browser/server Rust dependencies
are mapped to their pinned versions and upstream license expressions in
`THIRD_PARTY_NOTICES.md`. Trunk and wasm-bindgen archives are downloaded only
by the explicit cache flow and retain their upstream licensing requirements.

See [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) for the mapping
between each pinned project and its license. Before redistributing a downloaded
artifact or container image, retain its upstream copyright, license, NOTICE,
and operating-system package metadata. For a Rust binary, inspect the full
resolved Cargo dependency graph and each crate's upstream license/NOTICE
files, including transitive dependencies; the direct-crate expressions in the
notices are not a complete binary attribution inventory.
