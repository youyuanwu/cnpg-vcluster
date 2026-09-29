#![deny(unsafe_code)]

#[cfg(target_arch = "wasm32")]
fn main() {
    console_error_panic_hook::set_once();
    tenant_admin_web::mount();
}

#[cfg(not(target_arch = "wasm32"))]
fn main() {}
