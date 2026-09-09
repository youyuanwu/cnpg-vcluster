# Third-party notices

The repository's license applies to the original lab code. The CAPI lab
downloads or deterministically transforms the projects below into ignored
local state. No downloaded binary, chart, source archive, upstream manifest,
or container image is committed to this repository.

Exact versions, source commits, URLs, checksums, image tags, and OCI digests
are recorded in [`config/versions.env`](config/versions.env). Upstream
copyright, license, and NOTICE files remain authoritative.

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

Container images include operating-system packages and transitive components
with their own notices. Inspect the pinned image and its upstream distribution
metadata before redistribution.

## Kamaji notice

Kamaji — The Kubernetes Control Plane Manager: copyright 2022 Clastix Labs.
Licensed under the Apache License, Version 2.0:
<https://kamaji.clastix.io>.

This product includes software developed by Clastix Labs and the Kamaji
open-source community under the Apache License, Version 2.0.
