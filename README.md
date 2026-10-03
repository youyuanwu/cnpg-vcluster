# CNPG multi-tenant lab

This repository contains one Cluster API and CloudNativePG experiment:

- [`capi/`](capi/) uses Cluster API, CABPK, CAPD development resources,
  CAPZ, Kamaji, and the Kamaji control-plane provider. Its interface is
  `just` from inside that directory.

## Cluster API lab

The local profile creates one kind management cluster and accepts explicit
tenant specifications for Kamaji hosted control planes, CAPD Docker workers,
and isolated Docker-backed storage. A Tenant owns an explicit database
catalog; operators can add and delete up to three independent CloudNativePG
clusters, each with one to three instances, without changing the Tenant.
Each management cluster also runs a Leptos/Axum Tenant Admin UI. Its
schema-v5 detail view shows entry-scoped status/topology and an
unsafe PostgreSQL superuser console for exact Ready instances on local or Azure
Tenants. The overview can create either provider's Tenants, and detail pages
delete the exact displayed Tenant UID:

```sh
cd capi
just cache
just tools
just prepare-host
just preflight
just create-management
just local-tenant-apply config/tenants/examples/local.yaml
just local-tenant-status tenant-example
just admin-port-forward
just local-tenant-delete tenant-example
just destroy
```

Local tenants are declarative `tenancy.cnpg-vcluster.io/v1alpha4` resources
reconciled by a Rust/kube-rs operator. Their immutable specs contain common
`kubernetesVersion` and `workers` fields plus a tagged provider; local
manifests use `provider.type: local`, with no database count. The controller
assigns the endpoint and Pod/Service CIDRs under `status.provider.allocation`.
Change a tenant by deleting and reapplying its manifest. Ready reflects current
Kubernetes infrastructure conditions, not database-cluster health. Ordinary
DELETE closes and drains the database catalog before provider finalization;
unknown create/storage outcomes retain finalizers. Apply is asynchronous;
repeat `local-tenant-status` until it exits zero.

This clean-install-only breaking cutover replaces experimental v1alpha3
`provider.databases` and implicit `capi-postgres`; retained Tenants require
ordinary deletion and independent workload/storage cleanup before the
create-locked CRD transition. The abandoned draft `TenantDatabase` CRD is
not migrated or automatically removed: its presence blocks installation
until all served versions, in-flight creates, and legacy data have been
independently retired. The same Rust Tenant manager runs in explicit
`local` or `azure` mode; a separate database-controller reconciles catalog
entries. Azure commands accept the retained JSON client input, submit a
Tenant, and use its generation-aware status and ordinary finalization.
Shared Azure foundation provisioning remains external.
Ordinary Tenant commands do not persist a second lifecycle journal; guarded
management installation retains an owner-only bootstrap CREATE probe and
activation identity until exact terminal proof. Local validation may cache
an owner-only Tenant kubeconfig on demand; deletion removes it, and explicit
cache-clear commands remove one or all cached kubeconfigs.
CAPD and the shared-host storage profile remain local development mechanisms;
neither profile is a production hostile-tenant isolation boundary.

See [`capi/README.md`](capi/README.md) and
[`capi/docs/high-level-design.md`](capi/docs/high-level-design.md). The
admin architecture, management read RBAC, unsafe SQL access, APIs, packaging,
and port-forward workflow are documented in
[`capi/docs/admin-ui-design.md`](capi/docs/admin-ui-design.md). The credentialed
Azure three-by-three destructive/nine-disk absence and real
browser-versus-service-proxy agreement passed manually outside CI on
2026-10-03 (see [`capi/README.md`](capi/README.md)); this is experimental
acceptance rather than a production storage guarantee.
The local
operator uses the Rust 1.98.1 toolchain declared in
[`rust-toolchain.toml`](rust-toolchain.toml); no Go tool downloads are needed.
