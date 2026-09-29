#![deny(unsafe_code)]

mod app;
mod error;
mod projection;
mod source;

pub use app::{AppState, router};
pub use error::{AppError, SourceError};
pub use projection::{TenantProjection, classify_tenant, project_summary};
pub use source::{DataSource, KubeDataSource, SourceFuture};
pub use tenant_admin_shared as contracts;
