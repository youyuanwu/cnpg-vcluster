#![deny(unsafe_code)]

#[cfg(target_arch = "wasm32")]
fn main() {
    tenant_admin_web::mount();
}

#[cfg(not(target_arch = "wasm32"))]
fn main() {}
