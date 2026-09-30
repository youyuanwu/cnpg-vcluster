use std::{env, fs::File, io::BufReader};

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, State},
    routing::{get, post},
};
use hyper_util::{
    rt::{TokioExecutor, TokioIo},
    server::conn::auto::Builder,
    service::TowerToHyperService,
};
use tenant_database_controller::admission::{self, AdmissionAnswer, AdmissionReview};
use tokio::net::TcpListener;
use tokio_rustls::{TlsAcceptor, rustls::ServerConfig};

async fn mutate(
    State(client): State<kube::Client>,
    Json(review): Json<AdmissionReview>,
) -> Json<AdmissionAnswer> {
    Json(admission::review(client, review, false).await)
}

async fn validate(
    State(client): State<kube::Client>,
    Json(review): Json<AdmissionReview>,
) -> Json<AdmissionAnswer> {
    Json(admission::review(client, review, true).await)
}

fn tls_config() -> Result<ServerConfig, Box<dyn std::error::Error>> {
    let certificate_path = env::var("ADMISSION_CERT_FILE")?;
    let key_path = env::var("ADMISSION_KEY_FILE")?;
    let certs = rustls_pemfile::certs(&mut BufReader::new(File::open(certificate_path)?))
        .collect::<Result<Vec<_>, _>>()?;
    let key = rustls_pemfile::private_key(&mut BufReader::new(File::open(key_path)?))?
        .ok_or("admission TLS private key is missing")?;
    let mut config = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(config)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = kube::Client::try_default().await?;
    let tls = TlsAcceptor::from(std::sync::Arc::new(tls_config()?));
    let listener = TcpListener::bind("0.0.0.0:9443").await?;
    let app = Router::new()
        .route("/healthz", get(|| async { "ok" }))
        .route("/mutate", post(mutate))
        .route("/validate", post(validate))
        .layer(DefaultBodyLimit::max(128 * 1024))
        .with_state(client);
    loop {
        let (socket, _) = listener.accept().await?;
        let tls = tls.clone();
        let app = app.clone();
        tokio::spawn(async move {
            let Ok(stream) = tls.accept(socket).await else {
                return;
            };
            let _ = Builder::new(TokioExecutor::new())
                .serve_connection(TokioIo::new(stream), TowerToHyperService::new(app))
                .await;
        });
    }
}
