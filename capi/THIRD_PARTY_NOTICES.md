# Third-party notices

The repository's license applies to the original lab code. The CAPI lab
downloads or deterministically transforms the projects below into ignored
local state. No downloaded binary, chart, source archive, upstream manifest,
or container image is committed to this repository.

Exact versions, source commits, URLs, checksums, image tags, and OCI digests
for the non-Rust lab inputs are recorded in
[`config/versions.env`](config/versions.env). Rust crate versions and
checksums are pinned in [`controller/Cargo.lock`](controller/Cargo.lock);
the license expressions below come from the resolved crates' Cargo metadata.
Upstream copyright, license, and NOTICE files remain authoritative.

| Project | Pinned use | Upstream license |
|---|---|---|
| kind | `v0.33.0` binary and `kindest/node:v1.36.4` | Apache-2.0 |
| Kubernetes | kubectl, kube-proxy, and Kubernetes `v1.36.4` | Apache-2.0 |
| Distribution | registry image `2.8.3` for the private offline mirror | Apache-2.0 |
| Helm | `v3.21.4` binary | Apache-2.0 |
| Cluster API | core, clusterctl, CABPK, and CAPD `v1.14.1` | Apache-2.0 |
| Kamaji CAPI provider | `v0.20.0` | Apache-2.0 |
| Kamaji | `26.8.6-edge` source | Apache-2.0 |
| kamaji-etcd | chart `0.15.0` | Apache-2.0 |
| etcd | directly pinned server `v3.5.17` and setup image `v3.5.6` | Apache-2.0 |
| cert-manager | OCI chart `v1.21.1` | Apache-2.0 |
| MetalLB | native manifest `v0.16.1` | Apache-2.0 |
| Calico | manifest and selected images `v3.32.2` | Apache-2.0 |
| CloudNativePG | operator `v1.30.0` | Apache-2.0 |
| PostgreSQL | CloudNativePG PostgreSQL `18.4` image | PostgreSQL License |
| Konnectivity | server and agent `v0.36.0` | Apache-2.0 |
| BusyBox | verification image `1.37.0` | GPL-2.0 |
| HAProxy | CAPD load-balancer image | GPL-2.0-or-later with upstream exceptions |
| just | host prerequisite `1.58.0`; not downloaded by the lab | CC0-1.0 |

The Rust manager's **direct runtime dependencies** in the locked Cargo graph
(versions shown are resolved versions, not necessarily manifest ranges):

| Crate | Version | Cargo license expression |
|---|---|---|
| axum | `0.8.9` | MIT |
| base64 | `0.22.1` | MIT OR Apache-2.0 |
| bollard | `0.21.1` | Apache-2.0 |
| chrono | `0.4.45` | MIT OR Apache-2.0 |
| futures | `0.3.34` | MIT OR Apache-2.0 |
| hex | `0.4.3` | MIT OR Apache-2.0 |
| ipnet | `2.12.2` | MIT OR Apache-2.0 |
| k8s-openapi | `0.28.0` | Apache-2.0 |
| kube | `4.2.0` | Apache-2.0 |
| kube-lease-manager | `0.12.0` | MIT |
| schemars | `1.2.2` | MIT |
| serde | `1.0.229` | MIT OR Apache-2.0 |
| serde_json | `1.0.151` | MIT OR Apache-2.0 |
| serde_yaml | `0.9.34+deprecated` | MIT OR Apache-2.0 |
| sha2 | `0.10.9` | MIT OR Apache-2.0 |
| thiserror | `2.0.21` | MIT OR Apache-2.0 |
| tokio | `1.53.1` | MIT |
| tracing | `0.1.44` | MIT |
| tracing-subscriber | `0.3.23` | MIT |
| url | `2.5.8` | MIT OR Apache-2.0 |

Direct **test-only** dependencies are `bytes 1.12.1` (MIT),
`http-body-util 0.1.5` (MIT), `tempfile 3.27.0` (MIT OR Apache-2.0), and
`tower 0.5.3` (MIT). Cargo.lock also includes transitive and platform-specific
crates; consult the resolved Cargo metadata and each upstream crate's license
and NOTICE files before redistributing a binary or its source. No crate source
is vendored into this repository.

Source and license references:

- <https://github.com/kubernetes-sigs/kind>
- <https://github.com/kubernetes/kubernetes>
- <https://github.com/distribution/distribution>
- <https://github.com/helm/helm>
- <https://github.com/kubernetes-sigs/cluster-api>
- <https://github.com/clastix/cluster-api-control-plane-provider-kamaji>
- <https://github.com/clastix/kamaji>
- <https://github.com/etcd-io/etcd>
- <https://github.com/cert-manager/cert-manager>
- <https://github.com/metallb/metallb>
- <https://github.com/projectcalico/calico>
- <https://github.com/cloudnative-pg/cloudnative-pg>
- <https://www.postgresql.org/about/licence/>
- <https://github.com/kubernetes-sigs/apiserver-network-proxy>
- <https://busybox.net/license.html>
- <https://github.com/haproxy/haproxy>
- <https://github.com/casey/just>
- <https://github.com/tokio-rs/axum>
- <https://github.com/fussybeaver/bollard>
- <https://github.com/Arnavion/k8s-openapi>
- <https://github.com/kube-rs/kube>
- <https://github.com/alex-karpenko/kube-lease-manager>
- <https://github.com/tokio-rs/tokio>
- <https://github.com/serde-rs/serde>
- <https://crates.io/>

Container images include operating-system packages and transitive components
with their own notices. Inspect the pinned image and its upstream distribution
metadata before redistribution.

## Kamaji notice

Kamaji — The Kubernetes Control Plane Manager: copyright 2022 Clastix Labs.
Licensed under the Apache License, Version 2.0:
<https://kamaji.clastix.io>.

This product includes software developed by Clastix Labs and the Kamaji
open-source community under the Apache License, Version 2.0.
