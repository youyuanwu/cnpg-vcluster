use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use k8s_openapi::api::core::v1::{ConfigMap, Namespace};
use kube::Api;
use tenant_controller::activation;
use tenant_controller::api::SUPPORTED_KUBERNETES_VERSION;
use tenant_controller::docker::BollardDockerClient;
use tenant_controller::error::ControllerError;
use tenant_controller::foundation;
use tenant_controller::reconcile::{
    AZURE_CONFIG_NAME, Assets, AzureProvider, Config as ReconcileConfig, LocalProvider,
    ProviderLifecycle, Reconciler, run_controller,
};
use tenant_controller::runtime::{
    DEFAULT_LEADER_ELECTION_ID, DEFAULT_LEADER_ELECTION_NAMESPACE, DEFAULT_LEASE_DURATION_SECONDS,
    DEFAULT_LEASE_GRACE_SECONDS, HealthState, LeaderConfig, LeadershipContext, LeadershipGate,
    bind_health, run_leader_elected, serve_health, shutdown_signal, wait_for_shutdown,
};
use tokio::sync::watch;

const DEFAULT_HEALTH_ADDRESS: &str = "0.0.0.0:8081";
const FOUNDATION_NAMESPACE: &str = "tenant-system";
const FOUNDATION_NAME: &str = "tenant-foundation";

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum ProviderMode {
    #[default]
    Local,
    Azure,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct StartupDependencies {
    docker: bool,
    local_foundation: bool,
    local_assets: bool,
    azure_configuration: bool,
    management_watches: bool,
}

impl ProviderMode {
    const fn dependencies(self) -> StartupDependencies {
        match self {
            Self::Local => StartupDependencies {
                docker: true,
                local_foundation: true,
                local_assets: true,
                azure_configuration: false,
                management_watches: true,
            },
            Self::Azure => StartupDependencies {
                docker: false,
                local_foundation: false,
                local_assets: false,
                azure_configuration: true,
                management_watches: true,
            },
        }
    }
}

struct AbortControllerOnDrop(tokio::task::AbortHandle);

impl Drop for AbortControllerOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ManagerConfig {
    provider: ProviderMode,
    probe_in_cluster: bool,
    leader_elect: bool,
    health_address: SocketAddr,
    leader: LeaderConfig,
    supported_kubernetes_version: String,
    controller_image: String,
    activation_token: String,
}

impl Default for ManagerConfig {
    fn default() -> Self {
        let namespace = std::env::var("POD_NAMESPACE")
            .unwrap_or_else(|_| DEFAULT_LEADER_ELECTION_NAMESPACE.into());
        let base_identity = std::env::var("POD_NAME")
            .or_else(|_| std::env::var("HOSTNAME"))
            .unwrap_or_else(|_| format!("tenant-controller-{}", std::process::id()));
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock must be after the Unix epoch")
            .as_nanos();
        let identity = format!("{base_identity}-{nonce:x}");
        Self {
            provider: ProviderMode::Local,
            probe_in_cluster: false,
            leader_elect: true,
            health_address: DEFAULT_HEALTH_ADDRESS
                .parse()
                .expect("default health address is valid"),
            leader: LeaderConfig {
                lease_name: DEFAULT_LEADER_ELECTION_ID.into(),
                namespace,
                identity,
                duration_seconds: DEFAULT_LEASE_DURATION_SECONDS,
                grace_seconds: DEFAULT_LEASE_GRACE_SECONDS,
            },
            supported_kubernetes_version: SUPPORTED_KUBERNETES_VERSION.into(),
            controller_image: String::new(),
            activation_token: String::new(),
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "tenant_controller=info".into()),
        )
        .json()
        .try_init()?;
    let config = parse_args(std::env::args().skip(1))?;
    tracing::info!(
        config = %tenant_controller::sanitize::TracingSafe::new(&format!("{config:?}")),
        "starting tenant controller"
    );
    if config.probe_in_cluster {
        return probe_in_cluster().await.map_err(Into::into);
    }
    run(config).await.map_err(Into::into)
}

async fn probe_in_cluster() -> Result<(), ControllerError> {
    let config = kube::Config::incluster().map_err(|error| {
        ControllerError::Configuration(format!("in-cluster configuration is unavailable: {error}"))
    })?;
    let client = kube::Client::try_from(config)?;
    Api::<Namespace>::all(client).get("default").await?;
    Ok(())
}

async fn run(config: ManagerConfig) -> Result<(), ControllerError> {
    config.leader.validate()?;
    let client = kube::Client::try_default().await?;
    Api::<Namespace>::all(client.clone())
        .get_opt("default")
        .await?
        .ok_or_else(|| ControllerError::DependencyPending("default Namespace".into()))?;
    let dependencies = config.provider.dependencies();
    match config.provider {
        ProviderMode::Local => {
            debug_assert!(
                dependencies.docker
                    && dependencies.local_foundation
                    && dependencies.local_assets
                    && !dependencies.azure_configuration
            );
            let docker = BollardDockerClient::connect("/var/run/docker.sock")
                .map_err(|error| ControllerError::Configuration(error.to_string()))?;
            let foundation_config =
                Api::<ConfigMap>::namespaced(client.clone(), FOUNDATION_NAMESPACE)
                    .get(FOUNDATION_NAME)
                    .await?;
            let data = foundation_config.data.as_ref().ok_or_else(|| {
                ControllerError::Configuration("foundation ConfigMap data is missing".into())
            })?;
            let foundation = Arc::new(
                foundation::parse_runtime(
                    data.get("foundation.json")
                        .map(String::as_str)
                        .unwrap_or(""),
                    data.get("foundation.sha256")
                        .map(String::as_str)
                        .unwrap_or(""),
                    &config.supported_kubernetes_version,
                    &config.controller_image,
                )
                .map_err(|error| ControllerError::Configuration(error.to_string()))?,
            );
            activation::admit(
                client.clone(),
                &docker,
                &foundation.hash,
                &config.activation_token,
                foundation.creation(None).is_ok(),
            )
            .await?;
            let provider = LocalProvider::new(
                client.clone(),
                docker,
                Assets::load(std::path::Path::new("/assets"))?,
                foundation,
            );
            run_runtime(
                config,
                client,
                provider,
                dependencies.management_watches,
                tenant_controller::management::MANAGEMENT_RESOURCES,
            )
            .await
        }
        ProviderMode::Azure => {
            debug_assert!(
                !dependencies.docker
                    && !dependencies.local_foundation
                    && !dependencies.local_assets
                    && dependencies.azure_configuration
            );
            let provider_config =
                Api::<ConfigMap>::namespaced(client.clone(), FOUNDATION_NAMESPACE)
                    .get(AZURE_CONFIG_NAME)
                    .await?;
            let provider = AzureProvider::from_config_map(client.clone(), &provider_config)?;
            let configured_version = provider
                .configuration
                .values
                .supported_kubernetes_version
                .trim_start_matches('v');
            if configured_version != config.supported_kubernetes_version.trim_start_matches('v') {
                return Err(ControllerError::Configuration(
                    "Azure provider supported Kubernetes version differs from manager arguments"
                        .into(),
                ));
            }
            if provider.configuration.values.controller_image != config.controller_image {
                return Err(ControllerError::Configuration(
                    "Azure provider controller image differs from manager arguments".into(),
                ));
            }
            run_runtime(
                config,
                client,
                provider,
                dependencies.management_watches,
                tenant_controller::management::AZURE_MANAGEMENT_RESOURCES,
            )
            .await
        }
    }
}

async fn run_runtime<P: ProviderLifecycle + 'static>(
    config: ManagerConfig,
    client: kube::Client,
    provider: P,
    watch_management_resources: bool,
    management_resources: &'static [tenant_controller::management::ManagementResource],
) -> Result<(), ControllerError> {
    let reconciler = Reconciler::new(
        client.clone(),
        ReconcileConfig {
            supported_version: config.supported_kubernetes_version.clone(),
            watch_management_resources,
            management_resources,
        },
        provider,
    );

    let health = HealthState::default();
    let listener = bind_health(config.health_address).await?;
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let signal_tx = shutdown_tx.clone();
    let signal_task = tokio::spawn(async move {
        shutdown_signal().await;
        let _ = signal_tx.send(true);
    });
    let health_task = tokio::spawn(serve_health(
        listener,
        health.clone(),
        wait_for_shutdown(shutdown_rx.clone()),
    ));

    let runtime_result = if config.leader_elect {
        run_leader_elected(
            client,
            config.leader,
            health.clone(),
            shutdown_rx.clone(),
            |mut context: LeadershipContext| async move {
                let gate = context.gate.clone();
                // The leader runtime drains permits while awaiting shutdown.
                // Reconciliation must keep being polled during that drain.
                let mut controller = tokio::spawn(run_controller(reconciler, gate, async move {
                    let reason = context.stopped().await;
                    context.gate.stop_accepting();
                    tracing::info!(?reason, "controller stopping");
                }));
                let _abort_on_drop = AbortControllerOnDrop(controller.abort_handle());
                (&mut controller)
                    .await
                    .map_err(|error| ControllerError::Task(error.to_string()))?
            },
        )
        .await
    } else {
        let gate = LeadershipGate::default();
        gate.start_accepting();
        let stop_gate = gate.clone();
        health.set_ready(true);
        let result = run_controller(reconciler, gate.clone(), async move {
            wait_for_shutdown(shutdown_rx).await;
            stop_gate.stop_accepting();
        })
        .await;
        gate.stop_accepting();
        gate.drain().await;
        health.set_ready(false);
        result
    };

    let _ = shutdown_tx.send(true);
    health.set_live(false);
    signal_task.abort();
    if runtime_result.is_err() {
        health_task.abort();
        return runtime_result;
    }
    health_task
        .await
        .map_err(|error| ControllerError::Task(error.to_string()))??;
    runtime_result
}

fn parse_args<I, S>(arguments: I) -> Result<ManagerConfig, ControllerError>
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    let mut config = ManagerConfig::default();
    let arguments: Vec<String> = arguments.into_iter().map(Into::into).collect();
    let mut index = 0;
    while index < arguments.len() {
        let argument = &arguments[index];
        if argument == "--probe-in-cluster" {
            config.probe_in_cluster = true;
            index += 1;
            continue;
        }
        let (flag, inline_value) = argument
            .split_once('=')
            .map_or((argument.as_str(), None), |(flag, value)| {
                (flag, Some(value))
            });
        let value = match inline_value {
            Some(value) => value,
            None => {
                index += 1;
                arguments.get(index).ok_or_else(|| {
                    ControllerError::Configuration(format!("{flag} requires a value"))
                })?
            }
        };
        match flag {
            "--provider" => {
                config.provider = match value {
                    "local" => ProviderMode::Local,
                    "azure" => ProviderMode::Azure,
                    _ => {
                        return Err(ControllerError::Configuration(
                            "--provider must be local or azure".into(),
                        ));
                    }
                };
            }
            "--leader-elect" => config.leader_elect = parse_bool(flag, value)?,
            "--health-probe-bind-address" => {
                config.health_address = normalize_address(value).parse().map_err(|error| {
                    ControllerError::Configuration(format!(
                        "invalid --health-probe-bind-address: {error}"
                    ))
                })?;
            }
            "--leader-election-id" => config.leader.lease_name = value.into(),
            "--leader-election-namespace" => config.leader.namespace = value.into(),
            "--leader-election-identity" => config.leader.identity = value.into(),
            "--leader-lease-duration-seconds" => {
                config.leader.duration_seconds = parse_u64(flag, value)?;
            }
            "--leader-renew-grace-seconds" => {
                config.leader.grace_seconds = parse_u64(flag, value)?;
            }
            "--supported-kubernetes-version" => {
                config.supported_kubernetes_version = value.into();
            }
            "--controller-image" => config.controller_image = value.into(),
            "--activation-token" => config.activation_token = value.into(),
            "--metrics-bind-address" => {}
            _ => {
                return Err(ControllerError::Configuration(format!(
                    "unknown argument {flag}"
                )));
            }
        }
        index += 1;
    }
    config.leader.validate()?;
    Ok(config)
}

fn parse_bool(flag: &str, value: &str) -> Result<bool, ControllerError> {
    value.parse().map_err(|_| {
        ControllerError::Configuration(format!("{flag} must be true or false, got {value:?}"))
    })
}

fn parse_u64(flag: &str, value: &str) -> Result<u64, ControllerError> {
    value
        .parse()
        .map_err(|_| ControllerError::Configuration(format!("{flag} must be an unsigned integer")))
}

fn normalize_address(value: &str) -> String {
    value
        .strip_prefix(':')
        .map_or_else(|| value.to_owned(), |port| format!("0.0.0.0:{port}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_safe_and_non_reconciling() {
        let config = parse_args(Vec::<String>::new()).unwrap();
        assert!(config.leader_elect);
        assert_eq!(config.provider, ProviderMode::Local);
        assert!(config.activation_token.is_empty());
        assert_eq!(
            config.health_address,
            DEFAULT_HEALTH_ADDRESS.parse().unwrap()
        );
        assert_eq!(
            config.supported_kubernetes_version,
            SUPPORTED_KUBERNETES_VERSION
        );
        assert_eq!(config.leader.lease_name, DEFAULT_LEADER_ELECTION_ID);
    }

    #[test]
    fn parses_inline_and_separate_runtime_flags() {
        let config = parse_args([
            "--probe-in-cluster",
            "--provider=azure",
            "--leader-elect=false",
            "--health-probe-bind-address",
            ":9090",
            "--leader-election-id=custom",
            "--leader-election-namespace",
            "system",
            "--leader-election-identity=pod-a",
            "--leader-lease-duration-seconds=20",
            "--leader-renew-grace-seconds=4",
            "--supported-kubernetes-version=1.36.5",
            "--controller-image=controller:test",
            "--activation-token=token-a",
            "--metrics-bind-address=0",
        ])
        .unwrap();
        assert!(config.probe_in_cluster);
        assert_eq!(config.provider, ProviderMode::Azure);
        assert!(!config.leader_elect);
        assert_eq!(config.health_address, "0.0.0.0:9090".parse().unwrap());
        assert_eq!(config.leader.lease_name, "custom");
        assert_eq!(config.leader.namespace, "system");
        assert_eq!(config.leader.identity, "pod-a");
        assert_eq!(config.leader.duration_seconds, 20);
        assert_eq!(config.leader.grace_seconds, 4);
        assert_eq!(config.controller_image, "controller:test");
        assert_eq!(config.activation_token, "token-a");
    }

    #[test]
    fn rejects_unknown_missing_and_unsafe_values() {
        assert!(parse_args(["--unknown=true"]).is_err());
        assert!(parse_args(["--provider=other"]).is_err());
        assert!(parse_args(["--leader-elect"]).is_err());
        assert!(parse_args(["--leader-elect=maybe"]).is_err());
        assert!(
            parse_args([
                "--leader-lease-duration-seconds=5",
                "--leader-renew-grace-seconds=5"
            ])
            .is_err()
        );
    }

    #[test]
    fn provider_modes_select_disjoint_startup_dependencies() {
        assert_eq!(
            ProviderMode::Local.dependencies(),
            StartupDependencies {
                docker: true,
                local_foundation: true,
                local_assets: true,
                azure_configuration: false,
                management_watches: true,
            }
        );
        assert_eq!(
            ProviderMode::Azure.dependencies(),
            StartupDependencies {
                docker: false,
                local_foundation: false,
                local_assets: false,
                azure_configuration: true,
                management_watches: true,
            }
        );
    }
}
