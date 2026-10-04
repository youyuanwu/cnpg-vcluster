#![deny(unsafe_code)]

pub mod catalog_state;
pub mod database_console;
pub mod error;
pub mod explorer;
pub mod format;
pub mod lifecycle;
pub mod mutation_ui_state;
pub mod route;
pub mod tenant_ui;
pub mod topology;

#[cfg(any(target_arch = "wasm32", test))]
pub(crate) const fn unsafe_request_header() -> (&'static str, &'static str) {
    (
        tenant_admin_shared::routes::TENANT_ADMIN_UNSAFE_REQUEST_HEADER,
        tenant_admin_shared::routes::TENANT_ADMIN_UNSAFE_REQUEST_VALUE,
    )
}

#[cfg(target_arch = "wasm32")]
mod api;
#[cfg(target_arch = "wasm32")]
mod app;
#[cfg(target_arch = "wasm32")]
mod catalog_ui;
#[cfg(target_arch = "wasm32")]
mod resource_explorer_ui;

#[cfg(target_arch = "wasm32")]
pub fn mount() {
    leptos::mount::mount_to_body(app::App);
}

#[cfg(test)]
mod tests {
    use super::unsafe_request_header;

    #[test]
    fn post_requests_use_the_shared_unsafe_request_header() {
        assert_eq!(
            unsafe_request_header(),
            ("x-tenant-admin-unsafe-request", "1")
        );
    }
}
