pub mod azure_runtime;
pub mod catalog_runtime;
pub mod management;
pub mod readiness;
pub mod sanitize;
pub use catalog_runtime::{TENANT_UID, database_namespace, storage_namespace};
