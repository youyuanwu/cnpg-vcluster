#![deny(unsafe_code)]

pub mod database_console;
pub mod error;
pub mod format;
pub mod route;
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
