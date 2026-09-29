#![deny(unsafe_code)]

mod api;
pub mod query;
pub mod routes;

pub use api::{ApiEnvelope, ApiError, ApiErrorCode, ApiErrorEnvelope};

pub const API_SCHEMA_NAME: &str = "tenant-admin";
pub const API_SCHEMA_VERSION: u16 = 1;

pub const ADMIN_RESOURCE_NAME: &str = "tenant-admin";
pub const ADMIN_NAMESPACE: &str = "tenant-system";
pub const ADMIN_CONTAINER_PORT: u16 = 8080;
pub const ADMIN_SERVICE_PORT: u16 = 80;
