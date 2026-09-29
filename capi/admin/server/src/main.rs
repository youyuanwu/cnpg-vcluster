#![deny(unsafe_code)]

use std::{env, error::Error, net::SocketAddr, path::PathBuf, sync::Arc};

use kube::Client;
use tenant_admin_server::{AppState, KubeDataSource, contracts::query::ProviderMode, router};
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

const DEFAULT_ADDRESS: &str = "0.0.0.0:8080";
const DEFAULT_WEB_DIRECTORY: &str = "/web";

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(match EnvFilter::try_from_default_env() {
            Ok(filter) => filter,
            Err(_) => EnvFilter::new("info"),
        })
        .json()
        .init();
    let provider = provider_mode()?;
    let web_directory = PathBuf::from(match env::var("TENANT_ADMIN_WEB_DIR") {
        Ok(directory) => directory,
        Err(_) => DEFAULT_WEB_DIRECTORY.into(),
    });
    let client = Client::try_default().await?;
    let state = AppState::new(Arc::new(KubeDataSource::new(client)), provider);
    let address: SocketAddr = DEFAULT_ADDRESS.parse()?;
    let listener = TcpListener::bind(address).await?;
    tracing::info!(%address, ?provider, web_directory = %web_directory.display(), "tenant admin server listening");
    axum::serve(listener, router(state, web_directory))
        .with_graceful_shutdown(shutdown())
        .await?;
    Ok(())
}

fn provider_mode() -> Result<ProviderMode, Box<dyn Error>> {
    let value = env::var("TENANT_ADMIN_PROVIDER");
    parse_provider_mode(value.as_deref().ok()).map_err(Into::into)
}

fn parse_provider_mode(value: Option<&str>) -> Result<ProviderMode, String> {
    match value {
        Some("local") => Ok(ProviderMode::Local),
        Some("azure") => Ok(ProviderMode::Azure),
        Some(value) => Err(format!(
            "TENANT_ADMIN_PROVIDER must be local or azure, got {value}"
        )),
        None => Err("TENANT_ADMIN_PROVIDER must be set to local or azure".into()),
    }
}

async fn shutdown() {
    if let Err(error) = tokio::signal::ctrl_c().await {
        tracing::error!(%error, "failed to install shutdown signal handler");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_mode_is_strict() {
        assert_eq!(
            parse_provider_mode(Some("local")).expect("local"),
            ProviderMode::Local
        );
        assert_eq!(
            parse_provider_mode(Some("azure")).expect("azure"),
            ProviderMode::Azure
        );
        assert!(parse_provider_mode(Some("invalid")).is_err());
        assert!(parse_provider_mode(None).is_err());
    }
}
