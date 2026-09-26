//! Tenant credentials are read only from the exact provider-owned Secret.
//! Errors deliberately retain neither Secret data nor API response bodies.

use std::collections::BTreeSet;
use std::time::Duration;

use base64::{Engine, engine::general_purpose::STANDARD};
use k8s_openapi::api::{
    core::v1::Secret,
    rbac::v1::{Role, RoleBinding, Subject},
};
use kube::api::PostParams;
use kube::config::{KubeConfigOptions, Kubeconfig};
use kube::core::DynamicObject;
use kube::{Api, Client, Config};

use crate::ownership::{
    CONTROL_PLANE_API_VERSION, validate_kubeconfig_secret, validate_kubeconfig_secret_for_deletion,
};
use crate::resources::{bootstrap_rbac, bootstrap_subjects_match};

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum TenantClientError {
    #[error("Tenant kubeconfig Secret is pending")]
    SecretPending,
    #[error("Tenant kubeconfig Secret contract is invalid")]
    SecretContract,
    #[error("Tenant kubeconfig Secret ownership cannot be proven")]
    SecretOwnership,
    #[error("Tenant kubeconfig is invalid: {0}")]
    Kubeconfig(&'static str),
    #[error("Tenant TLS client construction failed")]
    ClientConfiguration,
    #[error("Tenant administrative access is pending")]
    AdministrativeAccessPending,
    #[error("Tenant bootstrap {0} differs from the supported content")]
    BootstrapMismatch(&'static str),
    #[error("Tenant {operation} API request returned HTTP {status}")]
    Api {
        operation: &'static str,
        status: u16,
    },
    #[error("Tenant {operation} API transport or response decoding failed")]
    Transport { operation: &'static str },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TenantApiErrorClass {
    Pending,
    Conflict,
    Retryable,
    Terminal,
}

pub fn classify_tenant_api_error(error: &kube::Error) -> TenantApiErrorClass {
    match error {
        kube::Error::Api(status) if matches!(status.code, 403 | 404) => {
            TenantApiErrorClass::Pending
        }
        kube::Error::Api(status) if status.code == 409 => TenantApiErrorClass::Conflict,
        kube::Error::Api(status)
            if status.code == 408 || status.code == 429 || status.code >= 500 =>
        {
            TenantApiErrorClass::Retryable
        }
        kube::Error::Api(_) => TenantApiErrorClass::Terminal,
        _ => TenantApiErrorClass::Retryable,
    }
}

impl TenantClientError {
    fn request(operation: &'static str, error: kube::Error, bootstrap: bool) -> Self {
        match error {
            kube::Error::Api(status) if bootstrap && status.code == 403 => {
                Self::AdministrativeAccessPending
            }
            kube::Error::Api(status) => Self::Api {
                operation,
                status: status.code,
            },
            _ => Self::Transport { operation },
        }
    }

    pub fn class(&self) -> TenantApiErrorClass {
        match self {
            Self::SecretPending
            | Self::AdministrativeAccessPending
            | Self::Api { status: 404, .. } => TenantApiErrorClass::Pending,
            Self::Api { status: 409, .. } => TenantApiErrorClass::Conflict,
            Self::Api { status, .. } if *status == 408 || *status == 429 || *status >= 500 => {
                TenantApiErrorClass::Retryable
            }
            Self::Transport { .. } => TenantApiErrorClass::Retryable,
            _ => TenantApiErrorClass::Terminal,
        }
    }
}

fn validate_endpoint(server: &str, expected: &str) -> Result<(), TenantClientError> {
    let invalid = || TenantClientError::Kubeconfig("endpoint does not match the allocation");
    let authority: axum::http::uri::Authority = expected.parse().map_err(|_| invalid())?;
    let url = url::Url::parse(server).map_err(|_| invalid())?;
    if authority.port_u16().is_none_or(|port| port == 0)
        || server != format!("https://{expected}")
        || url.scheme() != "https"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(invalid());
    }
    Ok(())
}

fn required_data(value: Option<&str>, field: &'static str) -> Result<(), TenantClientError> {
    let value = value.ok_or(TenantClientError::Kubeconfig(field))?;
    if STANDARD
        .decode(value)
        .map_err(|_| TenantClientError::Kubeconfig(field))?
        .is_empty()
    {
        return Err(TenantClientError::Kubeconfig(field));
    }
    Ok(())
}

fn unique_names<'a>(names: impl Iterator<Item = &'a str>) -> Result<(), TenantClientError> {
    let mut seen = BTreeSet::new();
    if names
        .into_iter()
        .any(|name| name.is_empty() || !seen.insert(name))
    {
        return Err(TenantClientError::Kubeconfig("ambiguous named entries"));
    }
    Ok(())
}

/// Pure validation runs before kube-rs can load any authentication mechanism.
pub fn parse_owned_kubeconfig(
    secret: &Secret,
    control_plane: &DynamicObject,
    namespace: &str,
    tenant_name: &str,
    endpoint: &str,
) -> Result<Kubeconfig, TenantClientError> {
    if namespace.is_empty()
        || tenant_name.is_empty()
        || secret.metadata.name.as_deref() != Some(&format!("{tenant_name}-kubeconfig"))
        || secret.metadata.namespace.as_deref() != Some(namespace)
        || secret.metadata.uid.as_deref().is_none_or(str::is_empty)
    {
        return Err(TenantClientError::SecretContract);
    }
    if control_plane.metadata.name.as_deref() != Some(tenant_name)
        || control_plane.metadata.namespace.as_deref() != Some(namespace)
        || !control_plane.types.as_ref().is_some_and(|types| {
            types.kind == "KamajiControlPlane" && types.api_version == CONTROL_PLANE_API_VERSION
        })
    {
        return Err(TenantClientError::SecretOwnership);
    }
    validate_kubeconfig_secret_for_deletion(secret, Some(control_plane))
        .map_err(|_| TenantClientError::SecretOwnership)?;
    validate_kubeconfig_secret(secret, Some(control_plane))
        .map_err(|_| TenantClientError::SecretContract)?;
    let bytes = &secret
        .data
        .as_ref()
        .and_then(|data| data.get("value"))
        .ok_or(TenantClientError::SecretContract)?
        .0;
    let text =
        std::str::from_utf8(bytes).map_err(|_| TenantClientError::Kubeconfig("invalid UTF-8"))?;
    let mut configuration: Kubeconfig =
        serde_yaml::from_str(text).map_err(|_| TenantClientError::Kubeconfig("invalid YAML"))?;
    unique_names(
        configuration
            .contexts
            .iter()
            .map(|entry| entry.name.as_str()),
    )?;
    unique_names(
        configuration
            .clusters
            .iter()
            .map(|entry| entry.name.as_str()),
    )?;
    unique_names(
        configuration
            .auth_infos
            .iter()
            .map(|entry| entry.name.as_str()),
    )?;
    let current = configuration
        .current_context
        .as_deref()
        .filter(|name| !name.is_empty())
        .ok_or(TenantClientError::Kubeconfig("current context is missing"))?;
    let context = configuration
        .contexts
        .iter()
        .find(|entry| entry.name == current)
        .and_then(|entry| entry.context.as_ref())
        .ok_or(TenantClientError::Kubeconfig(
            "current context is incomplete",
        ))?;
    let user = context
        .user
        .as_deref()
        .filter(|name| !name.is_empty())
        .ok_or(TenantClientError::Kubeconfig("current context has no user"))?;
    let cluster = configuration
        .clusters
        .iter()
        .find(|entry| entry.name == context.cluster)
        .and_then(|entry| entry.cluster.as_ref())
        .ok_or(TenantClientError::Kubeconfig("cluster is missing"))?;
    let auth = configuration
        .auth_infos
        .iter()
        .find(|entry| entry.name == user)
        .and_then(|entry| entry.auth_info.as_ref())
        .ok_or(TenantClientError::Kubeconfig("user is missing"))?;
    validate_endpoint(cluster.server.as_deref().unwrap_or(""), endpoint)?;
    if cluster.insecure_skip_tls_verify == Some(true)
        || cluster.certificate_authority.is_some()
        || cluster.proxy_url.is_some()
        || cluster.tls_server_name.is_some()
        || auth.client_certificate.is_some()
        || auth.client_key.is_some()
        || auth.token.is_some()
        || auth.token_file.is_some()
        || auth.username.is_some()
        || auth.password.is_some()
        || auth.exec.is_some()
        || auth.auth_provider.is_some()
        || auth.impersonate.is_some()
        || auth.impersonate_uid.is_some()
        || auth.impersonate_groups.is_some()
        || auth.impersonate_user_extra.is_some()
        || !auth.other.is_empty()
    {
        return Err(TenantClientError::Kubeconfig(
            "only embedded CA and client certificate authentication is supported",
        ));
    }
    required_data(
        cluster.certificate_authority_data.as_deref(),
        "CA data is incomplete",
    )?;
    required_data(
        auth.client_certificate_data.as_deref(),
        "client certificate data is incomplete",
    )?;
    // Serializing just this field avoids adding a second secrecy dependency;
    // the value never becomes part of an error or a diagnostic.
    let credentials = serde_json::to_value(auth)
        .map_err(|_| TenantClientError::Kubeconfig("client key data is incomplete"))?;
    required_data(
        credentials
            .get("client-key-data")
            .and_then(serde_json::Value::as_str),
        "client key data is incomplete",
    )?;
    let cluster_name = context.cluster.clone();
    let user_name = user.to_owned();
    let current = current.to_owned();
    configuration.contexts.retain(|entry| entry.name == current);
    configuration
        .clusters
        .retain(|entry| entry.name == cluster_name);
    configuration
        .auth_infos
        .retain(|entry| entry.name == user_name);
    Ok(configuration)
}

pub async fn validated_tenant_config(
    secret: &Secret,
    control_plane: &DynamicObject,
    namespace: &str,
    tenant_name: &str,
    endpoint: &str,
) -> Result<Config, TenantClientError> {
    let configuration =
        parse_owned_kubeconfig(secret, control_plane, namespace, tenant_name, endpoint)?;
    let mut config = Config::from_custom_kubeconfig(configuration, &KubeConfigOptions::default())
        .await
        .map_err(|_| TenantClientError::ClientConfiguration)?;
    if config.root_cert.as_ref().is_none_or(Vec::is_empty) {
        return Err(TenantClientError::ClientConfiguration);
    }
    config.proxy_url = None;
    config.connect_timeout = Some(Duration::from_secs(30));
    config.read_timeout = Some(Duration::from_secs(30));
    config.write_timeout = Some(Duration::from_secs(30));
    config.default_retry = false;
    Ok(config)
}

pub async fn tenant_client_from_secret(
    secret: &Secret,
    control_plane: &DynamicObject,
    namespace: &str,
    tenant_name: &str,
    endpoint: &str,
) -> Result<Client, TenantClientError> {
    Client::try_from(
        validated_tenant_config(secret, control_plane, namespace, tenant_name, endpoint).await?,
    )
    .map_err(|_| TenantClientError::ClientConfiguration)
}

pub async fn load_tenant_client(
    management: Client,
    control_plane: &DynamicObject,
    namespace: &str,
    tenant_name: &str,
    endpoint: &str,
) -> Result<(Client, Secret), TenantClientError> {
    let secret = Api::<Secret>::namespaced(management, namespace)
        .get_opt(&format!("{tenant_name}-kubeconfig"))
        .await
        .map_err(|error| TenantClientError::request("read kubeconfig Secret", error, false))?
        .ok_or(TenantClientError::SecretPending)?;
    let client =
        tenant_client_from_secret(&secret, control_plane, namespace, tenant_name, endpoint).await?;
    Ok((client, secret))
}

pub fn bootstrap_subjects_equal(actual: &[Subject], expected: &[Subject]) -> bool {
    bootstrap_subjects_match(actual, expected)
}

pub fn validate_bootstrap_role(actual: &Role, expected: &Role) -> Result<(), TenantClientError> {
    if actual.metadata.name != expected.metadata.name
        || actual.metadata.namespace != expected.metadata.namespace
        || actual.rules != expected.rules
    {
        return Err(TenantClientError::BootstrapMismatch("Role"));
    }
    Ok(())
}

pub fn validate_bootstrap_binding(
    actual: &RoleBinding,
    expected: &RoleBinding,
) -> Result<(), TenantClientError> {
    if actual.metadata.name != expected.metadata.name
        || actual.metadata.namespace != expected.metadata.namespace
        || actual.role_ref != expected.role_ref
        || !bootstrap_subjects_equal(
            actual.subjects.as_deref().unwrap_or_default(),
            expected.subjects.as_deref().unwrap_or_default(),
        )
    {
        return Err(TenantClientError::BootstrapMismatch("RoleBinding"));
    }
    Ok(())
}

pub async fn ensure_bootstrap_rbac(client: Client) -> Result<(), TenantClientError> {
    let roles: Api<Role> = Api::namespaced(client.clone(), "kube-system");
    let bindings: Api<RoleBinding> = Api::namespaced(client, "kube-system");
    for desired in bootstrap_rbac() {
        let name = desired
            .role
            .metadata
            .name
            .as_deref()
            .expect("builder supplies name");
        let current = roles
            .get_opt(name)
            .await
            .map_err(|error| TenantClientError::request("read bootstrap Role", error, true))?;
        if let Some(current) = current {
            validate_bootstrap_role(&current, &desired.role)?;
        } else {
            match roles.create(&PostParams::default(), &desired.role).await {
                Ok(created) => validate_bootstrap_role(&created, &desired.role)?,
                Err(kube::Error::Api(status)) if status.code == 409 => {
                    let current = roles.get(name).await.map_err(|error| {
                        TenantClientError::request("reread bootstrap Role", error, true)
                    })?;
                    validate_bootstrap_role(&current, &desired.role)?;
                }
                Err(error) => {
                    return Err(TenantClientError::request(
                        "create bootstrap Role",
                        error,
                        true,
                    ));
                }
            }
        }
        let current = bindings.get_opt(name).await.map_err(|error| {
            TenantClientError::request("read bootstrap RoleBinding", error, true)
        })?;
        if let Some(current) = current {
            validate_bootstrap_binding(&current, &desired.binding)?;
        } else {
            match bindings
                .create(&PostParams::default(), &desired.binding)
                .await
            {
                Ok(created) => validate_bootstrap_binding(&created, &desired.binding)?,
                Err(kube::Error::Api(status)) if status.code == 409 => {
                    let current = bindings.get(name).await.map_err(|error| {
                        TenantClientError::request("reread bootstrap RoleBinding", error, true)
                    })?;
                    validate_bootstrap_binding(&current, &desired.binding)?;
                }
                Err(error) => {
                    return Err(TenantClientError::request(
                        "create bootstrap RoleBinding",
                        error,
                        true,
                    ));
                }
            }
        }
    }
    Ok(())
}
