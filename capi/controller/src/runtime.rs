use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, SystemTime};

use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use k8s_openapi::api::coordination::v1::Lease;
use k8s_openapi::api::core::v1::{ConfigMap, Namespace};
use kube::api::{DeleteParams, Patch, PatchParams, Preconditions};
use kube::runtime::controller::{Config as ControllerConfig, Controller};
use kube::runtime::reflector::ObjectRef;
use kube::runtime::watcher;
use kube::{Api, Client, Resource, ResourceExt};
use kube_lease_manager::LeaseManagerBuilder;
use tokio::net::TcpListener;
use tokio::sync::{Notify, oneshot, watch};
use tokio::time::{Instant, MissedTickBehavior};

use crate::api::Tenant;
use crate::error::ControllerError;
use crate::status::{StatusUpdatePlan, plan_status_update};

pub const DEFAULT_LEADER_ELECTION_ID: &str = "tenant-controller.tenancy.cnpg-vcluster.io";
pub const DEFAULT_LEADER_ELECTION_NAMESPACE: &str = "default";
pub const DEFAULT_LEASE_DURATION_SECONDS: u64 = 30;
pub const DEFAULT_LEASE_GRACE_SECONDS: u64 = 5;
pub const DEFAULT_STATUS_CONFLICT_RETRIES: usize = 4;
pub const RECONCILE_CONCURRENCY: u16 = 1;
pub const TENANT_ANNOTATION: &str = "tenancy.cnpg-vcluster.io/tenant";

pub fn tenant_controller(client: Client) -> Controller<Tenant> {
    Controller::new(Api::<Tenant>::all(client), watcher::Config::default())
        .with_config(ControllerConfig::default().concurrency(RECONCILE_CONCURRENCY))
}

#[must_use]
pub fn map_dependent_to_tenant<K>(object: &K) -> Vec<ObjectRef<Tenant>>
where
    K: Resource + ResourceExt,
{
    object
        .annotations()
        .get(TENANT_ANNOTATION)
        .filter(|name| !name.is_empty())
        .map(|name| vec![ObjectRef::new(name)])
        .unwrap_or_default()
}

#[derive(Clone)]
pub struct DirectKubeClient {
    client: Client,
}

impl DirectKubeClient {
    #[must_use]
    pub fn new(client: Client) -> Self {
        Self { client }
    }

    #[must_use]
    pub fn client(&self) -> Client {
        self.client.clone()
    }

    pub async fn tenant(&self, name: &str) -> Result<Option<Tenant>, ControllerError> {
        Ok(Api::<Tenant>::all(self.client.clone())
            .get_opt(name)
            .await?)
    }

    pub async fn config_map(
        &self,
        namespace: &str,
        name: &str,
    ) -> Result<Option<ConfigMap>, ControllerError> {
        Ok(Api::<ConfigMap>::namespaced(self.client.clone(), namespace)
            .get_opt(name)
            .await?)
    }

    pub async fn lease(
        &self,
        namespace: &str,
        name: &str,
    ) -> Result<Option<Lease>, ControllerError> {
        Ok(Api::<Lease>::namespaced(self.client.clone(), namespace)
            .get_opt(name)
            .await?)
    }

    pub async fn namespace(&self, name: &str) -> Result<Option<Namespace>, ControllerError> {
        Ok(Api::<Namespace>::all(self.client.clone())
            .get_opt(name)
            .await?)
    }

    pub async fn delete_lease_exact(
        &self,
        namespace: &str,
        name: &str,
        uid: &str,
        resource_version: &str,
    ) -> Result<(), ControllerError> {
        let params = DeleteParams {
            preconditions: Some(Preconditions {
                uid: Some(uid.into()),
                resource_version: Some(resource_version.into()),
            }),
            ..DeleteParams::default()
        };
        Api::<Lease>::namespaced(self.client.clone(), namespace)
            .delete(name, &params)
            .await?;
        Ok(())
    }

    pub async fn update_tenant_status<F>(
        &self,
        name: &str,
        max_conflict_retries: usize,
        mutate: F,
    ) -> Result<Option<Tenant>, ControllerError>
    where
        F: Fn(&mut crate::api::TenantStatus) -> Result<(), ControllerError>,
    {
        let api = Api::<Tenant>::all(self.client.clone());
        for attempt in 0..=max_conflict_retries {
            let current = api.get(name).await?;
            match plan_status_update(&current, &mutate)? {
                StatusUpdatePlan::Noop => return Ok(None),
                plan @ StatusUpdatePlan::Replace { .. } => {
                    let patch = plan
                        .merge_patch()
                        .expect("replacement status plan has a patch");
                    match api
                        .patch_status(name, &PatchParams::default(), &Patch::Merge(&patch))
                        .await
                    {
                        Ok(updated) => return Ok(Some(updated)),
                        Err(kube::Error::Api(status))
                            if status.is_conflict() && attempt < max_conflict_retries => {}
                        Err(kube::Error::Api(status)) if status.is_conflict() => {
                            return Err(ControllerError::StatusConflict {
                                resource: name.into(),
                                attempts: attempt + 1,
                            });
                        }
                        Err(error) => return Err(error.into()),
                    }
                }
            }
        }
        unreachable!("status retry loop always returns")
    }
}

#[derive(Clone, Debug)]
pub struct HealthState {
    inner: Arc<HealthInner>,
}

#[derive(Debug)]
struct HealthInner {
    live: AtomicBool,
    ready: AtomicBool,
}

impl Default for HealthState {
    fn default() -> Self {
        Self {
            inner: Arc::new(HealthInner {
                live: AtomicBool::new(true),
                ready: AtomicBool::new(false),
            }),
        }
    }
}

impl HealthState {
    #[must_use]
    pub fn is_live(&self) -> bool {
        self.inner.live.load(Ordering::Acquire)
    }

    #[must_use]
    pub fn is_ready(&self) -> bool {
        self.inner.ready.load(Ordering::Acquire)
    }

    pub fn set_live(&self, live: bool) {
        self.inner.live.store(live, Ordering::Release);
        if !live {
            self.set_ready(false);
        }
    }

    pub fn set_ready(&self, ready: bool) {
        self.inner.ready.store(ready, Ordering::Release);
    }
}

pub fn health_router(state: HealthState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .with_state(state)
}

pub async fn serve_health(
    listener: TcpListener,
    state: HealthState,
    shutdown: impl Future<Output = ()> + Send + 'static,
) -> Result<(), ControllerError> {
    axum::serve(listener, health_router(state))
        .with_graceful_shutdown(shutdown)
        .await?;
    Ok(())
}

pub async fn bind_health(address: SocketAddr) -> Result<TcpListener, ControllerError> {
    Ok(TcpListener::bind(address).await?)
}

async fn healthz(State(state): State<HealthState>) -> StatusCode {
    if state.is_live() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

async fn readyz(State(state): State<HealthState>) -> StatusCode {
    if state.is_ready() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

#[derive(Clone, Debug)]
pub struct LeadershipGate {
    inner: Arc<LeadershipGateInner>,
}

#[derive(Debug)]
struct LeadershipGateInner {
    accepting: AtomicBool,
    active: AtomicUsize,
    drained: Notify,
}

impl Default for LeadershipGate {
    fn default() -> Self {
        Self {
            inner: Arc::new(LeadershipGateInner {
                accepting: AtomicBool::new(false),
                active: AtomicUsize::new(0),
                drained: Notify::new(),
            }),
        }
    }
}

impl LeadershipGate {
    pub fn start_accepting(&self) {
        self.inner.accepting.store(true, Ordering::Release);
    }

    pub fn stop_accepting(&self) {
        self.inner.accepting.store(false, Ordering::Release);
        if self.inner.active.load(Ordering::Acquire) == 0 {
            self.notify_drained();
        }
    }

    #[must_use]
    pub fn is_accepting(&self) -> bool {
        self.inner.accepting.load(Ordering::Acquire)
    }

    #[must_use]
    pub fn try_enter(&self) -> Option<WorkPermit> {
        if !self.is_accepting() {
            return None;
        }
        self.inner.active.fetch_add(1, Ordering::AcqRel);
        if !self.is_accepting() {
            self.leave();
            return None;
        }
        Some(WorkPermit { gate: self.clone() })
    }

    pub async fn drain(&self) {
        while self.inner.active.load(Ordering::Acquire) != 0 {
            self.inner.drained.notified().await;
        }
    }

    fn leave(&self) {
        if self.inner.active.fetch_sub(1, Ordering::AcqRel) == 1 && !self.is_accepting() {
            self.notify_drained();
        }
    }

    fn notify_drained(&self) {
        self.inner.drained.notify_waiters();
        self.inner.drained.notify_one();
    }
}

pub struct WorkPermit {
    gate: LeadershipGate,
}

impl Drop for WorkPermit {
    fn drop(&mut self) {
        self.gate.leave();
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeaderConfig {
    pub lease_name: String,
    pub namespace: String,
    pub identity: String,
    pub duration_seconds: u64,
    pub grace_seconds: u64,
}

impl LeaderConfig {
    pub fn validate(&self) -> Result<(), ControllerError> {
        if self.lease_name.trim().is_empty() {
            return Err(ControllerError::Configuration(
                "leader election lease name must not be empty".into(),
            ));
        }
        if self.namespace.trim().is_empty() {
            return Err(ControllerError::Configuration(
                "leader election namespace must not be empty".into(),
            ));
        }
        if self.identity.trim().is_empty() {
            return Err(ControllerError::Configuration(
                "leader election identity must not be empty".into(),
            ));
        }
        if self.grace_seconds == 0
            || self.duration_seconds <= self.grace_seconds
            || self.duration_seconds > i32::MAX as u64
        {
            return Err(ControllerError::Configuration(
                "leader lease duration must fit the Lease API and exceed a non-zero grace period"
                    .into(),
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StopReason {
    LeadershipLost,
    Shutdown,
}

struct LeaseConfirmation {
    api: Api<Lease>,
    identity: String,
    duration: Duration,
    grace: Duration,
    uid: Option<String>,
    transitions: Option<i32>,
    last_renewal: Option<SystemTime>,
    deadline: Instant,
}

impl LeaseConfirmation {
    fn new(client: Client, config: &LeaderConfig) -> Self {
        let duration = Duration::from_secs(config.duration_seconds);
        let grace = Duration::from_secs(config.grace_seconds);
        // The manager renews at expiry minus grace; split that grace between
        // observing the renewal, retiring work, and exiting before expiry.
        Self {
            api: Api::namespaced(client, &config.namespace),
            identity: config.identity.clone(),
            duration,
            grace,
            uid: None,
            transitions: None,
            last_renewal: None,
            deadline: Instant::now() + duration - grace / 2,
        }
    }

    async fn observe(&mut self, name: &str) -> Result<(), ControllerError> {
        let lease = self.api.get(name).await?;
        if Instant::now() >= self.deadline {
            return Err(ControllerError::LeadershipLost);
        }
        let spec = lease.spec.as_ref().ok_or(ControllerError::LeadershipLost)?;
        let renew = spec
            .renew_time
            .as_ref()
            .map(|time| SystemTime::from(time.0))
            .ok_or(ControllerError::LeadershipLost)?;
        let uid = lease
            .metadata
            .uid
            .as_deref()
            .ok_or(ControllerError::LeadershipLost)?;
        if spec.holder_identity.as_deref() != Some(self.identity.as_str())
            || spec.lease_duration_seconds != Some(self.duration.as_secs() as i32)
            || self.uid.as_deref().is_some_and(|previous| previous != uid)
            || self
                .transitions
                .is_some_and(|previous| spec.lease_transitions != Some(previous))
            || self.last_renewal.is_some_and(|previous| renew < previous)
        {
            return Err(ControllerError::LeadershipLost);
        }
        let expires_safely = renew
            .checked_add(self.duration)
            .and_then(|expiry| expiry.checked_sub(self.grace / 2))
            .ok_or(ControllerError::LeadershipLost)?;
        let remaining = expires_safely
            .duration_since(SystemTime::now())
            .map_err(|_| ControllerError::LeadershipLost)?;
        if self.last_renewal != Some(renew) {
            // Re-reading the same Lease is not a successful renewal.
            self.deadline = Instant::now() + remaining.min(self.duration - self.grace / 2);
            self.last_renewal = Some(renew);
            self.uid = Some(uid.to_owned());
            self.transitions = spec.lease_transitions;
        }
        if Instant::now() >= self.deadline {
            return Err(ControllerError::LeadershipLost);
        }
        Ok(())
    }

    async fn monitor(
        &mut self,
        name: &str,
        confirmed: oneshot::Sender<()>,
        deadline_tx: watch::Sender<Instant>,
    ) -> Result<(), ControllerError> {
        let mut confirmed = Some(confirmed);
        let mut interval = tokio::time::interval((self.grace / 2).min(Duration::from_secs(1)));
        interval.set_missed_tick_behavior(MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                biased;
                () = tokio::time::sleep_until(self.deadline) => return Err(ControllerError::LeadershipLost),
                _ = interval.tick() => {
                    let observed = tokio::select! {
                        biased;
                        () = tokio::time::sleep_until(self.deadline) => return Err(ControllerError::LeadershipLost),
                        result = self.observe(name) => result,
                    };
                    match observed {
                        Ok(()) => {
                            deadline_tx.send_replace(self.deadline);
                            if let Some(sender) = confirmed.take() {
                                let _ = sender.send(());
                            }
                        }
                        Err(ControllerError::Kube(error)) => {
                            tracing::warn!(%error, "unable to confirm leader lease renewal");
                        }
                        Err(error) => return Err(error),
                    }
                }
            }
        }
    }
}

pub struct LeadershipContext {
    pub gate: LeadershipGate,
    leadership: watch::Receiver<bool>,
    shutdown: watch::Receiver<bool>,
    stop: watch::Receiver<bool>,
}

impl LeadershipContext {
    pub async fn stopped(&mut self) -> StopReason {
        loop {
            if *self.shutdown.borrow() {
                return StopReason::Shutdown;
            }
            if !*self.leadership.borrow() || *self.stop.borrow() {
                return StopReason::LeadershipLost;
            }
            tokio::select! {
                changed = self.stop.changed() => {
                    if changed.is_err() || *self.stop.borrow() {
                        return StopReason::LeadershipLost;
                    }
                }
                changed = self.shutdown.changed() => {
                    if changed.is_err() || *self.shutdown.borrow() {
                        return StopReason::Shutdown;
                    }
                }
                changed = self.leadership.changed() => {
                    if changed.is_err() || !*self.leadership.borrow() {
                        return StopReason::LeadershipLost;
                    }
                }
            }
        }
    }
}

pub async fn run_leader_elected<F, Fut>(
    client: Client,
    config: LeaderConfig,
    health: HealthState,
    mut shutdown: watch::Receiver<bool>,
    workload: F,
) -> Result<(), ControllerError>
where
    F: FnOnce(LeadershipContext) -> Fut,
    Fut: Future<Output = Result<(), ControllerError>>,
{
    config.validate()?;
    let manager = LeaseManagerBuilder::new(client.clone(), &config.lease_name)
        .with_namespace(&config.namespace)
        .with_identity(&config.identity)
        .with_duration(config.duration_seconds)
        .with_grace(config.grace_seconds)
        .build()
        .await?;
    let (mut leadership, manager_task) = manager.watch().await;

    loop {
        if *shutdown.borrow() {
            drop(leadership);
            manager_task
                .await
                .map_err(|error| ControllerError::Task(error.to_string()))??;
            return Ok(());
        }
        if *leadership.borrow() {
            break;
        }
        tokio::select! {
            changed = shutdown.changed() => {
                if changed.is_err() || *shutdown.borrow() {
                    drop(leadership);
                    manager_task
                        .await
                        .map_err(|error| ControllerError::Task(error.to_string()))??;
                    return Ok(());
                }
            }
            changed = leadership.changed() => {
                if changed.is_err() {
                    return Err(ControllerError::LeadershipLost);
                }
            }
        }
    }

    let mut confirmation = LeaseConfirmation::new(client.clone(), &config);
    let (deadline_tx, deadline_rx) = watch::channel(confirmation.deadline);
    let (confirmed_tx, mut confirmed_rx) = oneshot::channel();
    let mut monitor =
        std::pin::pin!(confirmation.monitor(&config.lease_name, confirmed_tx, deadline_tx));
    loop {
        tokio::select! {
            biased;
            result = &mut monitor => {
                drop(leadership);
                manager_task.abort();
                return Err(result.err().unwrap_or(ControllerError::LeadershipLost));
            }
            confirmed = &mut confirmed_rx => {
                confirmed.map_err(|_| ControllerError::LeadershipLost)?;
                break;
            }
            changed = shutdown.changed() => {
                if changed.is_err() || *shutdown.borrow() {
                    drop(leadership);
                    manager_task
                        .await
                        .map_err(|error| ControllerError::Task(error.to_string()))??;
                    return Ok(());
                }
            }
            changed = leadership.changed() => {
                if changed.is_err() || !*leadership.borrow() {
                    return Err(ControllerError::LeadershipLost);
                }
            }
        }
    }

    let gate = LeadershipGate::default();
    let (stop_tx, stop_rx) = watch::channel(false);
    gate.start_accepting();
    health.set_ready(true);
    let context = LeadershipContext {
        gate: gate.clone(),
        leadership: leadership.clone(),
        shutdown: shutdown.clone(),
        stop: stop_rx,
    };
    let mut workload = Box::pin(workload(context));

    let (stop_reason, finished) = tokio::select! {
        biased;
        result = &mut monitor => {
            tracing::warn!(?result, "leader lease confirmation stopped");
            (Some(StopReason::LeadershipLost), None)
        }
        result = &mut workload => (None, Some(result)),
        changed = shutdown.changed() => {
            if changed.is_err() || *shutdown.borrow() {
                (Some(StopReason::Shutdown), None)
            } else {
                unreachable!("shutdown watch changed without becoming true")
            }
        }
        changed = leadership.changed() => {
            if changed.is_err() || !*leadership.borrow() {
                (Some(StopReason::LeadershipLost), None)
            } else {
                unreachable!("leader watch changed without losing leadership")
            }
        }
    };

    health.set_ready(false);
    gate.stop_accepting();
    let _ = stop_tx.send(true);
    let retire_by = *deadline_rx.borrow() + Duration::from_secs(config.grace_seconds) / 4;
    let mut manager_task = manager_task;
    let retirement = async {
        gate.drain().await;
        let result = match finished {
            Some(result) => result,
            None => (&mut workload).await,
        };
        drop(workload);
        drop(leadership);
        if stop_reason == Some(StopReason::LeadershipLost) {
            manager_task.abort();
            return result;
        }
        (&mut manager_task)
            .await
            .map_err(|error| ControllerError::Task(error.to_string()))??;
        result
    };
    match tokio::time::timeout_at(retire_by, retirement).await {
        Ok(result) => {
            result?;
            match stop_reason {
                None | Some(StopReason::Shutdown) => Ok(()),
                Some(StopReason::LeadershipLost) => Err(ControllerError::LeadershipLost),
            }
        }
        Err(_) => {
            manager_task.abort();
            Err(ControllerError::Task(
                "controller did not retire before leader lease expiry".into(),
            ))
        }
    }
}

pub async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("SIGTERM handler can be installed");
        tokio::select! {
            result = tokio::signal::ctrl_c() => {
                result.expect("Ctrl-C handler can be installed");
            }
            _ = terminate.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c()
            .await
            .expect("Ctrl-C handler can be installed");
    }
}

pub async fn wait_for_shutdown(mut shutdown: watch::Receiver<bool>) {
    while !*shutdown.borrow() {
        if shutdown.changed().await.is_err() {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::convert::Infallible;
    use std::sync::Mutex;

    use axum::body::Body as AxumBody;
    use http_body_util::BodyExt;
    use kube::ResourceExt;
    use kube::client::Body;
    use serde_json::{Value, json};
    use tower::{ServiceExt, service_fn};

    use crate::api::{AllocationStatus, TenantPhase, TenantSpec, TenantStatus};

    use super::*;

    #[derive(Clone)]
    struct MockKube {
        requests: Arc<Mutex<Vec<RecordedRequest>>>,
        responses: Arc<Mutex<VecDeque<(StatusCode, Value)>>>,
    }

    #[derive(Clone, Debug)]
    struct RecordedRequest {
        method: String,
        path: String,
        content_type: Option<String>,
        body: Value,
    }

    impl MockKube {
        fn client(responses: Vec<(StatusCode, Value)>) -> (Client, Self) {
            let mock = Self {
                requests: Arc::new(Mutex::new(Vec::new())),
                responses: Arc::new(Mutex::new(responses.into())),
            };
            let service = mock.clone();
            let client = Client::new(
                service_fn(move |request: axum::http::Request<Body>| {
                    let service = service.clone();
                    async move {
                        let method = request.method().to_string();
                        let path = request
                            .uri()
                            .path_and_query()
                            .map_or_else(String::new, ToString::to_string);
                        let content_type = request
                            .headers()
                            .get(axum::http::header::CONTENT_TYPE)
                            .and_then(|value| value.to_str().ok())
                            .map(str::to_owned);
                        let bytes = request.into_body().collect().await.unwrap().to_bytes();
                        let body = if bytes.is_empty() {
                            Value::Null
                        } else {
                            serde_json::from_slice(&bytes).unwrap()
                        };
                        service.requests.lock().unwrap().push(RecordedRequest {
                            method,
                            path,
                            content_type,
                            body,
                        });
                        let (status, body) = service.responses.lock().unwrap().pop_front().unwrap();
                        Ok::<_, Infallible>(
                            axum::http::Response::builder()
                                .status(status)
                                .header("content-type", "application/json")
                                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                                .unwrap(),
                        )
                    }
                }),
                "default",
            );
            (client, mock)
        }

        fn requests(&self) -> Vec<RecordedRequest> {
            self.requests.lock().unwrap().clone()
        }
    }

    #[derive(Clone)]
    struct MockLease {
        lease: Arc<Mutex<Value>>,
        api_errors: Arc<AtomicBool>,
        patch_errors: Arc<AtomicBool>,
        acquired_at: Arc<Mutex<Option<Instant>>>,
        renewals: Arc<AtomicUsize>,
    }

    impl MockLease {
        fn client() -> (Client, Self) {
            let mock = Self {
                lease: Arc::new(Mutex::new(json!({
                    "apiVersion": "coordination.k8s.io/v1",
                    "kind": "Lease",
                    "metadata": {"name": "leader", "namespace": "system", "uid": "lease-uid", "resourceVersion": "1"},
                    "spec": {}
                }))),
                api_errors: Arc::new(AtomicBool::new(false)),
                patch_errors: Arc::new(AtomicBool::new(false)),
                acquired_at: Arc::new(Mutex::new(None)),
                renewals: Arc::new(AtomicUsize::new(0)),
            };
            let service = mock.clone();
            let client = Client::new(
                service_fn(move |request: axum::http::Request<Body>| {
                    let service = service.clone();
                    async move {
                        assert_eq!(
                            request.uri().path(),
                            "/apis/coordination.k8s.io/v1/namespaces/system/leases/leader"
                        );
                        let method = request.method().clone();
                        let bytes = request.into_body().collect().await.unwrap().to_bytes();
                        let (status, response) = if service.api_errors.load(Ordering::Acquire)
                            || (method == axum::http::Method::PATCH
                                && service.patch_errors.load(Ordering::Acquire))
                        {
                            (
                                StatusCode::SERVICE_UNAVAILABLE,
                                api_error(503, "ServiceUnavailable"),
                            )
                        } else {
                            let mut lease = service.lease.lock().unwrap();
                            if method == axum::http::Method::PATCH {
                                let patch: Value = serde_json::from_slice(&bytes).unwrap();
                                for (key, value) in patch["spec"].as_object().unwrap() {
                                    lease["spec"][key] = value.clone();
                                }
                                if lease["spec"]["holderIdentity"] == "pod-a"
                                    && patch["spec"].get("renewTime").is_some()
                                {
                                    service.renewals.fetch_add(1, Ordering::AcqRel);
                                    let mut acquired = service.acquired_at.lock().unwrap();
                                    if acquired.is_none() {
                                        *acquired = Some(Instant::now());
                                    }
                                }
                            } else {
                                assert_eq!(method, axum::http::Method::GET);
                            }
                            (StatusCode::OK, lease.clone())
                        };
                        Ok::<_, Infallible>(
                            axum::http::Response::builder()
                                .status(status)
                                .header("content-type", "application/json")
                                .body(Body::from(serde_json::to_vec(&response).unwrap()))
                                .unwrap(),
                        )
                    }
                }),
                "default",
            );
            (client, mock)
        }
    }

    fn leader_config() -> LeaderConfig {
        LeaderConfig {
            lease_name: "leader".into(),
            namespace: "system".into(),
            identity: "pod-a".into(),
            duration_seconds: 4,
            grace_seconds: 2,
        }
    }

    async fn wait_for_not_ready(health: &HealthState) {
        tokio::time::timeout(Duration::from_secs(4), async {
            while health.is_ready() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("leader must become unready before lease expiration");
    }

    fn tenant(resource_version: &str, endpoint: &str) -> Tenant {
        let mut tenant = Tenant::new(
            "tenant-a",
            TenantSpec {
                kubernetes_version: "1.36.4".into(),
                workers: 1,
                databases: 1,
            },
        );
        tenant.metadata.uid = Some("tenant-uid".into());
        tenant.metadata.resource_version = Some(resource_version.into());
        tenant.metadata.generation = Some(3);
        tenant.status = Some(TenantStatus {
            observed_generation: Some(3),
            phase: Some(TenantPhase::Progressing),
            allocation: Some(AllocationStatus {
                slot_id: "slot-a".into(),
                endpoint: endpoint.into(),
                pod_cidr: "10.73.0.0/16".into(),
                service_cidr: "10.143.0.0/16".into(),
            }),
            ..Default::default()
        });
        tenant
    }

    fn api_error(code: u16, reason: &str) -> Value {
        json!({
            "apiVersion":"v1",
            "kind":"Status",
            "status":"Failure",
            "message":"request failed",
            "reason":reason,
            "code":code
        })
    }

    #[tokio::test]
    async fn direct_reads_use_exact_uncached_paths() {
        let tenant = tenant("1", "10.0.0.8");
        let (client, mock) = MockKube::client(vec![
            (StatusCode::OK, serde_json::to_value(&tenant).unwrap()),
            (
                StatusCode::OK,
                json!({"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"foundation","namespace":"system"}}),
            ),
            (StatusCode::NOT_FOUND, api_error(404, "NotFound")),
        ]);
        let direct = DirectKubeClient::new(client);
        assert_eq!(
            direct.tenant("tenant-a").await.unwrap().unwrap().name_any(),
            "tenant-a"
        );
        assert!(
            direct
                .config_map("system", "foundation")
                .await
                .unwrap()
                .is_some()
        );
        assert!(direct.lease("system", "slot-a").await.unwrap().is_none());
        let requests = mock.requests();
        assert_eq!(
            requests
                .iter()
                .map(|request| (request.method.as_str(), request.path.as_str()))
                .collect::<Vec<_>>(),
            vec![
                (
                    "GET",
                    "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants/tenant-a"
                ),
                ("GET", "/api/v1/namespaces/system/configmaps/foundation"),
                (
                    "GET",
                    "/apis/coordination.k8s.io/v1/namespaces/system/leases/slot-a"
                ),
            ]
        );
        assert!(requests.iter().all(|request| request.body.is_null()));
    }

    #[tokio::test]
    async fn exact_delete_sends_uid_and_resource_version_preconditions() {
        let (client, mock) = MockKube::client(vec![(
            StatusCode::OK,
            json!({"apiVersion":"v1","kind":"Status","status":"Success"}),
        )]);
        DirectKubeClient::new(client)
            .delete_lease_exact("system", "slot-a", "lease-uid", "17")
            .await
            .unwrap();
        let requests = mock.requests();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].method, "DELETE");
        assert_eq!(
            requests[0].path,
            "/apis/coordination.k8s.io/v1/namespaces/system/leases/slot-a?"
        );
        assert_eq!(
            requests[0].content_type.as_deref(),
            Some("application/json")
        );
        assert_eq!(
            requests[0].body,
            json!({"preconditions":{"resourceVersion":"17","uid":"lease-uid"}})
        );
    }

    #[tokio::test]
    async fn status_conflict_rereads_and_preserves_concurrent_fields() {
        let first = tenant("10", "10.0.0.8");
        let concurrent = tenant("11", "10.0.0.9");
        let mut updated = concurrent.clone();
        updated.status.as_mut().unwrap().phase = Some(TenantPhase::Ready);
        let (client, mock) = MockKube::client(vec![
            (StatusCode::OK, serde_json::to_value(&first).unwrap()),
            (StatusCode::CONFLICT, api_error(409, "Conflict")),
            (StatusCode::OK, serde_json::to_value(&concurrent).unwrap()),
            (StatusCode::OK, serde_json::to_value(&updated).unwrap()),
        ]);
        let result = DirectKubeClient::new(client)
            .update_tenant_status("tenant-a", 2, |status| {
                status.phase = Some(TenantPhase::Ready);
                Ok(())
            })
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            result.status.unwrap().allocation.unwrap().endpoint,
            "10.0.0.9"
        );
        let requests = mock.requests();
        assert_eq!(requests.len(), 4);
        assert_eq!(requests[1].method, "PATCH");
        assert_eq!(
            requests[1].path,
            "/apis/tenancy.cnpg-vcluster.io/v1alpha2/tenants/tenant-a/status?"
        );
        assert_eq!(
            requests[1].content_type.as_deref(),
            Some("application/merge-patch+json")
        );
        assert_eq!(
            requests[1]
                .body
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            vec!["metadata", "status"]
        );
        assert_eq!(requests[1].body["metadata"]["resourceVersion"], "10");
        assert_eq!(requests[3].body["metadata"]["resourceVersion"], "11");
        assert_eq!(
            requests[3].body["status"]["allocation"]["endpoint"],
            "10.0.0.9"
        );
        assert_eq!(requests[3].body["status"]["phase"], "Ready");
    }

    #[tokio::test]
    async fn status_noop_performs_no_write() {
        let current = tenant("10", "10.0.0.8");
        let (client, mock) = MockKube::client(vec![(
            StatusCode::OK,
            serde_json::to_value(&current).unwrap(),
        )]);
        let result = DirectKubeClient::new(client)
            .update_tenant_status("tenant-a", 2, |_| Ok(()))
            .await
            .unwrap();
        assert!(result.is_none());
        let requests = mock.requests();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].method, "GET");
    }

    #[tokio::test]
    async fn status_conflict_exhaustion_is_typed() {
        let current = tenant("10", "10.0.0.8");
        let (client, mock) = MockKube::client(vec![
            (StatusCode::OK, serde_json::to_value(&current).unwrap()),
            (StatusCode::CONFLICT, api_error(409, "Conflict")),
            (StatusCode::OK, serde_json::to_value(&current).unwrap()),
            (StatusCode::CONFLICT, api_error(409, "Conflict")),
        ]);
        let error = DirectKubeClient::new(client)
            .update_tenant_status("tenant-a", 1, |status| {
                status.phase = Some(TenantPhase::Ready);
                Ok(())
            })
            .await
            .unwrap_err();
        assert!(matches!(
            error,
            ControllerError::StatusConflict {
                ref resource,
                attempts: 2
            } if resource == "tenant-a"
        ));
        assert_eq!(mock.requests().len(), 4);
    }

    #[tokio::test]
    async fn health_and_readiness_reflect_runtime_state() {
        let health = HealthState::default();
        let app = health_router(health.clone());
        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .uri("/healthz")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .uri("/readyz")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        health.set_ready(true);
        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .uri("/readyz")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        health.set_live(false);
        let response = app
            .oneshot(
                axum::http::Request::builder()
                    .uri("/healthz")
                    .body(AxumBody::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert!(!health.is_ready());
    }

    #[tokio::test]
    async fn leadership_loss_stops_new_work_and_drains_active_work() {
        let gate = LeadershipGate::default();
        gate.start_accepting();
        let permit = gate.try_enter().expect("leader accepts work");
        gate.stop_accepting();
        assert!(gate.try_enter().is_none());
        let draining = {
            let gate = gate.clone();
            tokio::spawn(async move {
                gate.drain().await;
            })
        };
        tokio::task::yield_now().await;
        assert!(!draining.is_finished());
        drop(permit);
        draining.await.unwrap();
    }

    #[tokio::test]
    async fn renewal_errors_close_admission_and_drain_before_lease_expiry() {
        let (client, mock) = MockLease::client();
        let health = HealthState::default();
        let (_shutdown_tx, shutdown_rx) = watch::channel(false);
        let (gate_tx, gate_rx) = oneshot::channel();
        let runtime = tokio::spawn(run_leader_elected(
            client,
            leader_config(),
            health.clone(),
            shutdown_rx,
            move |mut context| async move {
                gate_tx.send(context.gate.clone()).unwrap();
                assert_eq!(context.stopped().await, StopReason::LeadershipLost);
                Ok(())
            },
        ));
        let gate = tokio::time::timeout(Duration::from_secs(2), gate_rx)
            .await
            .unwrap()
            .unwrap();
        assert!(health.is_ready());
        let permit = gate.try_enter().expect("confirmed leader accepts work");
        let acquired_at = mock.acquired_at.lock().unwrap().unwrap();
        mock.api_errors.store(true, Ordering::Release);
        wait_for_not_ready(&health).await;
        assert!(!gate.is_accepting());
        assert!(gate.try_enter().is_none());
        assert!(!runtime.is_finished(), "in-flight work must drain");
        drop(permit);
        let result = tokio::time::timeout(Duration::from_secs(2), runtime)
            .await
            .expect("election must retire before the lease expires")
            .unwrap();
        assert!(
            matches!(
                result,
                Err(ControllerError::LeadershipLost | ControllerError::LeaderElection(_))
            ),
            "{result:?}"
        );
        assert!(Instant::now() < acquired_at + Duration::from_secs(4));
    }

    #[tokio::test]
    async fn stuck_work_forces_election_exit_before_lease_expiry() {
        let (client, mock) = MockLease::client();
        let health = HealthState::default();
        let (_shutdown_tx, shutdown_rx) = watch::channel(false);
        let (gate_tx, gate_rx) = oneshot::channel();
        let runtime = tokio::spawn(run_leader_elected(
            client,
            leader_config(),
            health.clone(),
            shutdown_rx,
            move |mut context| async move {
                gate_tx.send(context.gate.clone()).unwrap();
                context.stopped().await;
                Ok(())
            },
        ));
        let gate = tokio::time::timeout(Duration::from_secs(2), gate_rx)
            .await
            .unwrap()
            .unwrap();
        let permit = gate.try_enter().unwrap();
        let acquired_at = mock.acquired_at.lock().unwrap().unwrap();
        mock.api_errors.store(true, Ordering::Release);
        let result = tokio::time::timeout(Duration::from_secs(4), runtime)
            .await
            .expect("retirement cannot wait indefinitely for in-flight work")
            .unwrap();
        assert!(matches!(result, Err(ControllerError::Task(_))));
        assert!(!health.is_ready());
        assert!(gate.try_enter().is_none());
        assert!(Instant::now() < acquired_at + Duration::from_secs(4));
        drop(permit);
    }

    #[tokio::test]
    async fn repeated_reads_of_unrenewed_lease_do_not_extend_admission() {
        let (client, mock) = MockLease::client();
        let health = HealthState::default();
        let (_shutdown_tx, shutdown_rx) = watch::channel(false);
        let (gate_tx, gate_rx) = oneshot::channel();
        let runtime = tokio::spawn(run_leader_elected(
            client,
            leader_config(),
            health.clone(),
            shutdown_rx,
            move |mut context| async move {
                gate_tx.send(context.gate.clone()).unwrap();
                context.stopped().await;
                Ok(())
            },
        ));
        let gate = tokio::time::timeout(Duration::from_secs(2), gate_rx)
            .await
            .unwrap()
            .unwrap();
        let acquired_at = mock.acquired_at.lock().unwrap().unwrap();
        mock.patch_errors.store(true, Ordering::Release);
        wait_for_not_ready(&health).await;
        assert_eq!(mock.renewals.load(Ordering::Acquire), 1);
        assert!(gate.try_enter().is_none());
        let result = tokio::time::timeout(Duration::from_secs(2), runtime)
            .await
            .unwrap()
            .unwrap();
        assert!(result.is_err());
        assert!(Instant::now() < acquired_at + Duration::from_secs(4));
    }

    #[tokio::test]
    async fn observed_holder_change_stops_controller_without_watch_notification() {
        let (client, mock) = MockLease::client();
        let health = HealthState::default();
        let (_shutdown_tx, shutdown_rx) = watch::channel(false);
        let (gate_tx, gate_rx) = oneshot::channel();
        let runtime = tokio::spawn(run_leader_elected(
            client,
            leader_config(),
            health.clone(),
            shutdown_rx,
            move |mut context| async move {
                gate_tx.send(context.gate.clone()).unwrap();
                assert_eq!(context.stopped().await, StopReason::LeadershipLost);
                Ok(())
            },
        ));
        let gate = tokio::time::timeout(Duration::from_secs(2), gate_rx)
            .await
            .unwrap()
            .unwrap();
        mock.lease.lock().unwrap()["spec"]["holderIdentity"] = json!("pod-b");
        let result = tokio::time::timeout(Duration::from_secs(2), runtime)
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(result, Err(ControllerError::LeadershipLost)));
        assert!(!health.is_ready());
        assert!(gate.try_enter().is_none());
    }

    #[tokio::test]
    async fn transient_renewal_errors_recover_before_deadline() {
        let (client, mock) = MockLease::client();
        let health = HealthState::default();
        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let (gate_tx, gate_rx) = oneshot::channel();
        let runtime = tokio::spawn(run_leader_elected(
            client,
            leader_config(),
            health.clone(),
            shutdown_rx,
            move |mut context| async move {
                gate_tx.send(context.gate.clone()).unwrap();
                assert_eq!(context.stopped().await, StopReason::Shutdown);
                Ok(())
            },
        ));
        let gate = tokio::time::timeout(Duration::from_secs(2), gate_rx)
            .await
            .unwrap()
            .unwrap();
        mock.api_errors.store(true, Ordering::Release);
        tokio::time::sleep(Duration::from_millis(400)).await;
        mock.api_errors.store(false, Ordering::Release);
        tokio::time::timeout(Duration::from_secs(4), async {
            while mock.renewals.load(Ordering::Acquire) < 2 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("lease manager must resume successful renewals");
        assert!(health.is_ready());
        assert!(gate.try_enter().is_some());
        shutdown_tx.send(true).unwrap();
        assert!(
            tokio::time::timeout(Duration::from_secs(2), runtime)
                .await
                .unwrap()
                .unwrap()
                .is_ok()
        );
        assert!(!health.is_ready());
    }

    #[test]
    fn leader_config_rejects_unsafe_timings_and_empty_identity() {
        let valid = LeaderConfig {
            lease_name: DEFAULT_LEADER_ELECTION_ID.into(),
            namespace: "system".into(),
            identity: "pod-a".into(),
            duration_seconds: 30,
            grace_seconds: 5,
        };
        valid.validate().unwrap();
        let mut invalid = valid.clone();
        invalid.grace_seconds = 30;
        assert!(invalid.validate().is_err());
        invalid = valid;
        invalid.identity.clear();
        assert!(invalid.validate().is_err());
        invalid.identity = "pod-a".into();
        invalid.duration_seconds = i32::MAX as u64 + 1;
        assert!(invalid.validate().is_err());
    }

    #[test]
    fn dependent_watch_mapping_uses_only_the_exact_tenant_annotation() {
        let mut object = ConfigMap::default();
        object.metadata.annotations = Some(
            [
                (TENANT_ANNOTATION.into(), "tenant-a".into()),
                ("unrelated".into(), "tenant-b".into()),
            ]
            .into(),
        );
        assert_eq!(
            map_dependent_to_tenant(&object),
            vec![ObjectRef::new("tenant-a")]
        );
        object
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .remove(TENANT_ANNOTATION);
        assert!(map_dependent_to_tenant(&object).is_empty());
        assert_eq!(RECONCILE_CONCURRENCY, 1);
    }
}
