#![deny(unsafe_code)]

pub mod error;
pub mod format;
pub mod route;
pub mod topology;

#[cfg(target_arch = "wasm32")]
mod api;
#[cfg(target_arch = "wasm32")]
mod app;

#[cfg(target_arch = "wasm32")]
pub fn mount() {
    leptos::mount::mount_to_body(app::App);
}
