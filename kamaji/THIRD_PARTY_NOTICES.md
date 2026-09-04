# Third-party notices

The repository's MIT license applies to the original lab code. The lab also
redistributes, downloads, or deterministically transforms the following
Apache-2.0 projects. The complete Apache License 2.0 text is included at
[`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt).

| Project | Pinned source used by this lab | Treatment |
|---|---|---|
| Kamaji | `26.8.6-edge`, commit `80f32baafe34cba9d739c41208c21090dbe1827d` | The verified source chart is copied into ignored local state and deterministically transformed to digest-pinned images. |
| cert-manager | `v1.21.1` OCI chart | The verified chart is downloaded locally and installed with supported per-workload digest values. |
| MetalLB | `v0.16.1` native manifest | The verified manifest is transformed immediately before use to replace the controller and speaker tags with recorded digests. |
| Calico | `v3.32.2` manifest | The checksum-verified manifest is downloaded into ignored `.tools/inputs/` and deterministically rendered under `.runtime/` with the tenant CIDR and recorded image digests. |
| Local Path Provisioner | `v0.0.37` manifest | The checksum-verified manifest is downloaded into ignored `.tools/inputs/` and deterministically rendered under `.runtime/` with the tenant storage path, default-class setting, and recorded image digests. |
| CloudNativePG | `v1.30.0` release manifest | The checksum-verified release is downloaded into ignored `.tools/inputs/` and rendered under `.runtime/` with the approved controller digest before tenant installation. |

Exact source URLs, checksums, digests, and image provenance are recorded in
[`config/versions.env`](config/versions.env). Upstream copyright, license, and
NOTICE files remain authoritative:

- Kamaji: <https://github.com/clastix/kamaji/tree/80f32baafe34cba9d739c41208c21090dbe1827d>
- cert-manager: <https://github.com/cert-manager/cert-manager/tree/v1.21.1>
- MetalLB: <https://github.com/metallb/metallb/tree/v0.16.1>
- Calico: <https://github.com/projectcalico/calico/tree/v3.32.2>
- Local Path Provisioner: <https://github.com/rancher/local-path-provisioner/tree/v0.0.37>
- CloudNativePG: <https://github.com/cloudnative-pg/cloudnative-pg/tree/v1.30.0>

## Kamaji NOTICE

Kamaji — The Kubernetes Control Plane Manager: copyright 2022 Clastix Labs.
Licensed under the Apache License, Version 2.0:
<https://kamaji.clastix.io>.

This product includes software developed by Clastix Labs and the Kamaji
open-source community under the Apache License, Version 2.0.
