use std::{
    error::Error,
    future::Future,
    io,
    pin::Pin,
    task::{Context, Poll},
    time::{Duration, Instant},
};

use futures::TryStreamExt;
use k8s_openapi::api::core::v1::Pod;
use kube::{Api, Client, api::Portforwarder};
use tenant_admin_shared::query::DatabaseQueryResult;
use tenant_controller::resources::MANAGED_DATABASE_NAMESPACE;
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    time::Instant as TokioInstant,
};
use tokio_postgres::{CancelToken, NoTls, SimpleQueryMessage, config::SslMode};

const QUERY_TIMEOUT: Duration = Duration::from_secs(30);
const CANCEL_TIMEOUT: Duration = Duration::from_secs(3);
const CANCEL_CLEANUP_TIMEOUT: Duration = Duration::from_secs(3);
const TRANSPORT_CLEANUP_TIMEOUT: Duration = Duration::from_secs(1);
const MAX_BACKEND_FRAME_BYTES: usize = 4 * 1_024 * 1_024;
const MAX_BACKEND_TOTAL_BYTES: u64 = 64 * 1_024 * 1_024;
const MAX_RESULTS: usize = 32;
const MAX_COLUMNS: usize = 128;
const MAX_ROWS: usize = 1_000;
const MAX_CELL_BYTES: usize = 16 * 1_024;
const MAX_RESPONSE_TEXT_BYTES: usize = 2 * 1_024 * 1_024;

pub(crate) type QueryFuture<'a> =
    Pin<Box<dyn Future<Output = Result<QueryExecution, QueryExecutionError>> + Send + 'a>>;

pub(crate) trait QueryExecutor: Send + Sync {
    fn execute<'a>(&'a self, request: QueryConnection<'a>) -> QueryFuture<'a>;
}

#[derive(Clone, Copy)]
pub(crate) struct PostgresQueryExecutor;

pub(crate) struct QueryConnection<'a> {
    pub client: Client,
    pub pod: &'a QueryPodBinding,
    pub database: &'a str,
    pub username: &'a str,
    pub password: &'a str,
    pub sql: &'a str,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct QueryPodBinding {
    pub pod_name: String,
    pub pod_uid: String,
    pub cluster: QueryClusterBinding,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct QueryClusterBinding {
    pub api_version: String,
    pub kind: String,
    pub name: String,
    pub uid: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PodBindingError {
    InvalidIdentity,
    NotReady,
}

pub(crate) fn validate_pod_binding(
    pod: &Pod,
    requested_instance: &str,
    cluster: &QueryClusterBinding,
) -> Result<QueryPodBinding, PodBindingError> {
    let pod_uid = pod
        .metadata
        .uid
        .as_deref()
        .filter(|uid| !uid.is_empty())
        .ok_or(PodBindingError::InvalidIdentity)?;
    if pod.metadata.name.as_deref() != Some(requested_instance)
        || pod.metadata.namespace.as_deref() != Some(MANAGED_DATABASE_NAMESPACE)
        || !has_canonical_controlling_owner(pod, cluster)
    {
        return Err(PodBindingError::InvalidIdentity);
    }
    if !pod_is_ready(pod) {
        return Err(PodBindingError::NotReady);
    }
    Ok(QueryPodBinding {
        pod_name: requested_instance.to_owned(),
        pod_uid: pod_uid.to_owned(),
        cluster: cluster.clone(),
    })
}

fn validate_rebound_pod(pod: &Pod, binding: &QueryPodBinding) -> Result<(), PodBindingError> {
    let current = validate_pod_binding(pod, &binding.pod_name, &binding.cluster)?;
    if current.pod_uid != binding.pod_uid {
        return Err(PodBindingError::InvalidIdentity);
    }
    Ok(())
}

fn has_canonical_controlling_owner(pod: &Pod, cluster: &QueryClusterBinding) -> bool {
    pod.metadata
        .owner_references
        .as_ref()
        .is_some_and(|owners| {
            owners.iter().any(|owner| {
                owner.api_version == cluster.api_version
                    && owner.kind == cluster.kind
                    && owner.name == cluster.name
                    && owner.uid == cluster.uid
                    && owner.controller == Some(true)
                    && owner.block_owner_deletion == Some(true)
            })
        })
}

fn pod_is_ready(pod: &Pod) -> bool {
    pod.metadata.deletion_timestamp.is_none()
        && pod
            .status
            .as_ref()
            .and_then(|status| status.phase.as_deref())
            == Some("Running")
        && pod
            .status
            .as_ref()
            .and_then(|status| status.conditions.as_ref())
            .is_some_and(|conditions| {
                conditions
                    .iter()
                    .any(|condition| condition.type_ == "Ready" && condition.status == "True")
            })
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct QueryExecution {
    pub duration_ms: u64,
    pub truncated: bool,
    pub results: Vec<DatabaseQueryResult>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum QueryExecutionError {
    DatabaseUnavailable,
    ResponseLimitExceeded,
    QueryFailed {
        sqlstate: Option<String>,
        message: String,
    },
    TimedOut,
    OutcomeUnknown,
}

impl QueryExecutor for PostgresQueryExecutor {
    fn execute<'a>(&'a self, request: QueryConnection<'a>) -> QueryFuture<'a> {
        Box::pin(execute_query(request))
    }
}

async fn execute_query(
    request: QueryConnection<'_>,
) -> Result<QueryExecution, QueryExecutionError> {
    let started = Instant::now();
    let deadline = TokioInstant::now() + QUERY_TIMEOUT;
    let setup = async {
        let mut forwarder = open_validated_portforward(request.client.clone(), request.pod).await?;
        let stream = forwarder
            .take_stream()
            .ok_or(QueryExecutionError::DatabaseUnavailable)?;
        let mut config = tokio_postgres::Config::new();
        config
            .user(request.username)
            .password(request.password)
            .dbname(request.database)
            .ssl_mode(SslMode::Disable);
        let (client, connection) = config
            .connect_raw(BoundedBackendStream::new(stream), NoTls)
            .await
            .map_err(connection_error)?;
        Ok::<_, QueryExecutionError>((client, connection, forwarder))
    };
    let (client, connection, forwarder) = tokio::time::timeout_at(deadline, setup)
        .await
        .map_err(|_| QueryExecutionError::DatabaseUnavailable)??;
    let cancel_token = client.cancel_token();
    let connection_task = AbortOnDrop(Some(tokio::spawn(async move {
        let _ = connection.await;
    })));
    let cancel_client = request.client.clone();
    let cancel_binding = request.pod.clone();
    let result = run_query_with_timeout(
        collect_results(&client, request.sql, started),
        deadline,
        move || cancel_query(cancel_client, cancel_binding, cancel_token),
        QueryTimings::production(),
    )
    .await;
    connection_task.abort_and_wait().await;
    forwarder.abort_and_wait().await;
    result
}

trait AsyncIo: AsyncRead + AsyncWrite + Unpin + Send {}

impl<T> AsyncIo for T where T: AsyncRead + AsyncWrite + Unpin + Send {}

type BoxedIo = Box<dyn AsyncIo>;

struct PortForwardGuard {
    forwarder: Option<Portforwarder>,
    stream: Option<BoxedIo>,
}

impl PortForwardGuard {
    fn take_stream(&mut self) -> Option<BoxedIo> {
        self.stream.take()
    }

    async fn abort_and_wait(mut self) {
        let Some(forwarder) = self.forwarder.take() else {
            return;
        };
        forwarder.abort();
        let _ = tokio::time::timeout(TRANSPORT_CLEANUP_TIMEOUT, forwarder.join()).await;
    }
}

impl Drop for PortForwardGuard {
    fn drop(&mut self) {
        if let Some(forwarder) = &self.forwarder {
            forwarder.abort();
        }
    }
}

async fn open_validated_portforward(
    client: Client,
    binding: &QueryPodBinding,
) -> Result<PortForwardGuard, QueryExecutionError> {
    let pods = Api::<Pod>::namespaced(client, MANAGED_DATABASE_NAMESPACE);
    let mut forwarder = pods
        .portforward(&binding.pod_name, &[5432])
        .await
        .map_err(|_| QueryExecutionError::DatabaseUnavailable)?;
    let pod = pods
        .get_opt(&binding.pod_name)
        .await
        .map_err(|_| QueryExecutionError::DatabaseUnavailable)?
        .ok_or(QueryExecutionError::DatabaseUnavailable)?;
    validate_rebound_pod(&pod, binding).map_err(|_| QueryExecutionError::DatabaseUnavailable)?;
    let stream = forwarder
        .take_stream(5432)
        .map(|stream| Box::new(stream) as BoxedIo)
        .ok_or(QueryExecutionError::DatabaseUnavailable)?;
    Ok(PortForwardGuard {
        forwarder: Some(forwarder),
        stream: Some(stream),
    })
}

async fn cancel_query(
    client: Client,
    binding: QueryPodBinding,
    cancel_token: CancelToken,
) -> Result<PortForwardGuard, QueryExecutionError> {
    let mut forwarder = open_validated_portforward(client, &binding).await?;
    let stream = forwarder
        .take_stream()
        .ok_or(QueryExecutionError::DatabaseUnavailable)?;
    cancel_token
        .cancel_query_raw(stream, NoTls)
        .await
        .map_err(|_| QueryExecutionError::DatabaseUnavailable)?;
    Ok(forwarder)
}

#[derive(Clone, Copy)]
struct QueryTimings {
    cancel_timeout: Duration,
    cleanup_timeout: Duration,
}

impl QueryTimings {
    const fn production() -> Self {
        Self {
            cancel_timeout: CANCEL_TIMEOUT,
            cleanup_timeout: CANCEL_CLEANUP_TIMEOUT,
        }
    }
}

async fn run_query_with_timeout<Q, C, CF, G>(
    query: Q,
    deadline: TokioInstant,
    cancel: C,
    timings: QueryTimings,
) -> Result<QueryExecution, QueryExecutionError>
where
    Q: Future<Output = Result<QueryExecution, QueryExecutionError>>,
    C: FnOnce() -> CF,
    CF: Future<Output = Result<G, QueryExecutionError>>,
{
    tokio::pin!(query);
    tokio::select! {
        biased;
        result = &mut query => result,
        () = tokio::time::sleep_until(deadline) => {
            let cancel_guard =
                match tokio::time::timeout(timings.cancel_timeout, cancel()).await {
                    Ok(Ok(guard)) => Some(guard),
                    Ok(Err(_)) | Err(_) => None,
                };
            let cancel_confirmed = cancel_guard.is_some();
            let outcome = match tokio::time::timeout(timings.cleanup_timeout, &mut query).await {
                Ok(Ok(execution)) => Ok(execution),
                Ok(Err(QueryExecutionError::QueryFailed { sqlstate: Some(code), .. }))
                    if cancel_confirmed && code == "57014" =>
                {
                    Err(QueryExecutionError::TimedOut)
                }
                Ok(Err(error @ QueryExecutionError::QueryFailed { .. }))
                | Ok(Err(error @ QueryExecutionError::ResponseLimitExceeded)) => Err(error),
                Ok(Err(_)) | Err(_) => Err(QueryExecutionError::OutcomeUnknown),
            };
            drop(cancel_guard);
            outcome
        }
    }
}

struct AbortOnDrop(Option<tokio::task::JoinHandle<()>>);

impl AbortOnDrop {
    async fn abort_and_wait(mut self) {
        let Some(mut task) = self.0.take() else {
            return;
        };
        task.abort();
        let _ = tokio::time::timeout(TRANSPORT_CLEANUP_TIMEOUT, &mut task).await;
    }
}

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        if let Some(task) = &self.0 {
            task.abort();
        }
    }
}

struct BoundedBackendStream<S> {
    inner: S,
    header: [u8; 5],
    header_filled: usize,
    header_sent: usize,
    body_remaining: usize,
    forwarded_total: u64,
}

impl<S> BoundedBackendStream<S> {
    const fn new(inner: S) -> Self {
        Self {
            inner,
            header: [0; 5],
            header_filled: 0,
            header_sent: 0,
            body_remaining: 0,
            forwarded_total: 0,
        }
    }

    fn reset_frame(&mut self) {
        self.header_filled = 0;
        self.header_sent = 0;
        self.body_remaining = 0;
    }

    fn validate_header(&mut self) -> io::Result<()> {
        let declared = u32::from_be_bytes([
            self.header[1],
            self.header[2],
            self.header[3],
            self.header[4],
        ]);
        if declared < 4 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid PostgreSQL backend frame",
            ));
        }
        let frame_size = usize::try_from(declared)
            .unwrap_or(usize::MAX)
            .saturating_add(1);
        if frame_size > MAX_BACKEND_FRAME_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                BackendStreamError::FrameLimit,
            ));
        }
        let next_total = self
            .forwarded_total
            .checked_add(u64::try_from(frame_size).unwrap_or(u64::MAX))
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, BackendStreamError::TotalLimit)
            })?;
        if next_total > MAX_BACKEND_TOTAL_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                BackendStreamError::TotalLimit,
            ));
        }
        self.forwarded_total = next_total;
        self.body_remaining = frame_size - 5;
        Ok(())
    }
}

impl<S> AsyncRead for BoundedBackendStream<S>
where
    S: AsyncRead + Unpin,
{
    fn poll_read(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        output: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if output.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        if this.header_filled < this.header.len() {
            let mut header_output = ReadBuf::new(&mut this.header[this.header_filled..]);
            match Pin::new(&mut this.inner).poll_read(context, &mut header_output) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Err(error)) => return Poll::Ready(Err(error)),
                Poll::Ready(Ok(())) => {
                    let read = header_output.filled().len();
                    if read == 0 {
                        return if this.header_filled == 0 {
                            Poll::Ready(Ok(()))
                        } else {
                            Poll::Ready(Err(io::Error::new(
                                io::ErrorKind::UnexpectedEof,
                                "truncated PostgreSQL backend frame header",
                            )))
                        };
                    }
                    this.header_filled += read;
                    if this.header_filled < this.header.len() {
                        context.waker().wake_by_ref();
                        return Poll::Pending;
                    }
                    if let Err(error) = this.validate_header() {
                        return Poll::Ready(Err(error));
                    }
                }
            }
        }
        if this.header_sent < this.header.len() {
            let count = (this.header.len() - this.header_sent).min(output.remaining());
            output.put_slice(&this.header[this.header_sent..this.header_sent + count]);
            this.header_sent += count;
            if this.header_sent == this.header.len() && this.body_remaining == 0 {
                this.reset_frame();
            }
            return Poll::Ready(Ok(()));
        }

        let count = this.body_remaining.min(output.remaining());
        let target = output.initialize_unfilled_to(count);
        let mut body_output = ReadBuf::new(target);
        match Pin::new(&mut this.inner).poll_read(context, &mut body_output) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Err(error)) => Poll::Ready(Err(error)),
            Poll::Ready(Ok(())) => {
                let read = body_output.filled().len();
                if read == 0 {
                    return Poll::Ready(Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "truncated PostgreSQL backend frame",
                    )));
                }
                output.advance(read);
                this.body_remaining -= read;
                if this.body_remaining == 0 {
                    this.reset_frame();
                }
                Poll::Ready(Ok(()))
            }
        }
    }
}

impl<S> AsyncWrite for BoundedBackendStream<S>
where
    S: AsyncWrite + Unpin,
{
    fn poll_write(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<Result<usize, io::Error>> {
        Pin::new(&mut self.get_mut().inner).poll_write(context, buffer)
    }

    fn poll_flush(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Result<(), io::Error>> {
        Pin::new(&mut self.get_mut().inner).poll_flush(context)
    }

    fn poll_shutdown(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Result<(), io::Error>> {
        Pin::new(&mut self.get_mut().inner).poll_shutdown(context)
    }
}

#[derive(Debug)]
enum BackendStreamError {
    FrameLimit,
    TotalLimit,
}

impl std::fmt::Display for BackendStreamError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("PostgreSQL response exceeded the service limit")
    }
}

impl Error for BackendStreamError {}

async fn collect_results(
    client: &tokio_postgres::Client,
    sql: &str,
    started: Instant,
) -> Result<QueryExecution, QueryExecutionError> {
    let stream = client.simple_query_raw(sql).await.map_err(postgres_error)?;
    futures::pin_mut!(stream);
    let mut results = Vec::new();
    let mut active_result = None;
    let mut result_count = 0usize;
    let mut retained_rows = 0usize;
    let mut retained_text = 0usize;
    let mut globally_truncated = false;

    while let Some(message) = stream.try_next().await.map_err(postgres_error)? {
        match message {
            SimpleQueryMessage::RowDescription(columns) => {
                result_count = result_count.saturating_add(1);
                if results.len() >= MAX_RESULTS {
                    globally_truncated = true;
                    active_result = None;
                    continue;
                }
                let mut result_truncated = columns.len() > MAX_COLUMNS;
                let columns = columns
                    .iter()
                    .take(MAX_COLUMNS)
                    .map(|column| {
                        bounded_text(
                            column.name(),
                            MAX_CELL_BYTES,
                            &mut retained_text,
                            &mut result_truncated,
                        )
                    })
                    .collect();
                globally_truncated |= result_truncated;
                results.push(DatabaseQueryResult {
                    columns,
                    rows: Vec::new(),
                    affected_rows: 0,
                    truncated: result_truncated,
                });
                active_result = Some(results.len() - 1);
            }
            SimpleQueryMessage::Row(row) => {
                let Some(index) = active_result else {
                    globally_truncated = true;
                    continue;
                };
                if retained_rows >= MAX_ROWS {
                    results[index].truncated = true;
                    globally_truncated = true;
                    continue;
                }
                let column_count = results[index].columns.len();
                let mut row_truncated = row.len() > column_count;
                let values = (0..column_count)
                    .map(|column| {
                        row.get(column).map(|value| {
                            bounded_text(
                                value,
                                MAX_CELL_BYTES,
                                &mut retained_text,
                                &mut row_truncated,
                            )
                        })
                    })
                    .collect();
                retained_rows = retained_rows.saturating_add(1);
                results[index].rows.push(values);
                results[index].truncated |= row_truncated;
                globally_truncated |= row_truncated;
            }
            SimpleQueryMessage::CommandComplete(affected_rows) => {
                if let Some(index) = active_result.take() {
                    results[index].affected_rows = affected_rows;
                    continue;
                }
                result_count = result_count.saturating_add(1);
                if results.len() >= MAX_RESULTS {
                    globally_truncated = true;
                    continue;
                }
                results.push(DatabaseQueryResult {
                    columns: Vec::new(),
                    rows: Vec::new(),
                    affected_rows,
                    truncated: false,
                });
            }
            _ => {}
        }
    }
    if result_count > MAX_RESULTS {
        globally_truncated = true;
    }
    Ok(QueryExecution {
        duration_ms: u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
        truncated: globally_truncated,
        results,
    })
}

fn bounded_text(
    value: &str,
    per_value_limit: usize,
    retained_text: &mut usize,
    truncated: &mut bool,
) -> String {
    let available = MAX_RESPONSE_TEXT_BYTES.saturating_sub(*retained_text);
    let limit = per_value_limit.min(available);
    let end = floor_char_boundary(value, limit);
    if end < value.len() {
        *truncated = true;
    }
    *retained_text = retained_text.saturating_add(end);
    value[..end].to_owned()
}

fn floor_char_boundary(value: &str, limit: usize) -> usize {
    let mut end = value.len().min(limit);
    while end > 0 && !value.is_char_boundary(end) {
        end -= 1;
    }
    end
}

fn postgres_error(error: tokio_postgres::Error) -> QueryExecutionError {
    if has_backend_response_limit(&error) {
        return QueryExecutionError::ResponseLimitExceeded;
    }
    let Some(database_error) = error.as_db_error() else {
        return QueryExecutionError::DatabaseUnavailable;
    };
    QueryExecutionError::QueryFailed {
        sqlstate: Some(database_error.code().code().to_owned()),
        message: tenant_controller::sanitize::text(database_error.message())
            .chars()
            .take(512)
            .collect(),
    }
}

fn connection_error(error: tokio_postgres::Error) -> QueryExecutionError {
    if has_backend_response_limit(&error) {
        QueryExecutionError::ResponseLimitExceeded
    } else {
        QueryExecutionError::DatabaseUnavailable
    }
}

fn has_backend_response_limit(error: &tokio_postgres::Error) -> bool {
    let mut source: Option<&(dyn Error + 'static)> = Some(error);
    while let Some(current) = source {
        if current.downcast_ref::<BackendStreamError>().is_some()
            || current
                .downcast_ref::<io::Error>()
                .and_then(io::Error::get_ref)
                .is_some_and(|cause| cause.downcast_ref::<BackendStreamError>().is_some())
        {
            return true;
        }
        source = current.source();
    }
    false
}

#[cfg(test)]
mod tests {
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    };

    use serde_json::json;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        sync::Notify,
    };

    use super::*;

    #[test]
    fn bounded_text_preserves_utf8_boundaries_and_sets_truncation() {
        let mut retained = 0;
        let mut truncated = false;
        let value = bounded_text("aéx", 2, &mut retained, &mut truncated);
        assert_eq!(value, "a");
        assert_eq!(retained, 1);
        assert!(truncated);
    }

    #[test]
    fn bounded_text_enforces_global_response_budget() {
        let mut retained = MAX_RESPONSE_TEXT_BYTES - 2;
        let mut truncated = false;
        let value = bounded_text("abcd", MAX_CELL_BYTES, &mut retained, &mut truncated);
        assert_eq!(value, "ab");
        assert_eq!(retained, MAX_RESPONSE_TEXT_BYTES);
        assert!(truncated);
    }

    #[tokio::test]
    async fn backend_frame_limit_rejects_oversized_declared_frame_before_header_exposure() {
        let (mut peer, stream) = tokio::io::duplex(64);
        let declared = u32::try_from(MAX_BACKEND_FRAME_BYTES).expect("frame limit");
        peer.write_all(&[
            b'D',
            declared.to_be_bytes()[0],
            declared.to_be_bytes()[1],
            declared.to_be_bytes()[2],
            declared.to_be_bytes()[3],
        ])
        .await
        .expect("write header");
        let mut bounded = BoundedBackendStream::new(stream);
        let mut output = [0_u8; 5];
        let error = bounded
            .read_exact(&mut output)
            .await
            .expect_err("oversized frame must fail");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert_eq!(output, [0; 5]);
        assert!(error.to_string().contains("service limit"));
    }

    #[tokio::test]
    async fn backend_frame_limit_forwards_normal_fragmented_frames() {
        let (mut peer, stream) = tokio::io::duplex(64);
        let writer = tokio::spawn(async move {
            peer.write_all(b"Z").await.expect("tag");
            tokio::task::yield_now().await;
            peer.write_all(&[0, 0]).await.expect("partial length");
            tokio::task::yield_now().await;
            peer.write_all(&[0, 5, b'I']).await.expect("frame tail");
            peer.shutdown().await.expect("shutdown");
        });
        let mut bounded = BoundedBackendStream::new(stream);
        let mut output = Vec::new();
        bounded
            .read_to_end(&mut output)
            .await
            .expect("bounded read");
        writer.await.expect("writer task");
        assert_eq!(output, [b'Z', 0, 0, 0, 5, b'I']);
    }

    #[tokio::test]
    async fn postgres_connect_maps_backend_frame_limit_to_response_limit_error() {
        let (stream, mut peer) = tokio::io::duplex(1_024);
        let declared = u32::try_from(MAX_BACKEND_FRAME_BYTES).expect("frame limit");
        let writer = tokio::spawn(async move {
            let mut startup_length = [0_u8; 4];
            peer.read_exact(&mut startup_length)
                .await
                .expect("startup length");
            let startup_length =
                usize::try_from(u32::from_be_bytes(startup_length)).expect("startup size");
            let mut startup_body = vec![0_u8; startup_length.saturating_sub(4)];
            peer.read_exact(&mut startup_body)
                .await
                .expect("startup body");
            peer.write_all(&[
                b'R',
                declared.to_be_bytes()[0],
                declared.to_be_bytes()[1],
                declared.to_be_bytes()[2],
                declared.to_be_bytes()[3],
            ])
            .await
            .expect("write oversized authentication frame");
            tokio::time::sleep(Duration::from_millis(20)).await;
        });
        let mut config = tokio_postgres::Config::new();
        config.user("postgres").ssl_mode(SslMode::Disable);
        let error = match config
            .connect_raw(BoundedBackendStream::new(stream), NoTls)
            .await
        {
            Ok(_) => panic!("oversized backend frame must fail"),
            Err(error) => error,
        };
        writer.await.expect("writer task");
        assert_eq!(
            connection_error(error),
            QueryExecutionError::ResponseLimitExceeded
        );
    }

    #[test]
    fn pod_uid_replacement_and_noncanonical_controller_are_rejected_before_connect() {
        let original: Pod =
            serde_json::from_value(test_pod("pod-uid", true, true)).expect("original Pod");
        let binding = validate_pod_binding(
            &original,
            "capi-postgres-1",
            &QueryClusterBinding {
                api_version: "postgresql.cnpg.io/v1".into(),
                kind: "Cluster".into(),
                name: "capi-postgres".into(),
                uid: "database-uid".into(),
            },
        )
        .expect("binding");

        let replacement: Pod = serde_json::from_value(test_pod("replacement-uid", true, true))
            .expect("replacement Pod");
        assert_eq!(
            validate_rebound_pod(&replacement, &binding),
            Err(PodBindingError::InvalidIdentity)
        );
        for (controller, block_owner_deletion) in [(false, true), (true, false)] {
            let pod: Pod =
                serde_json::from_value(test_pod("pod-uid", controller, block_owner_deletion))
                    .expect("Pod");
            assert_eq!(
                validate_rebound_pod(&pod, &binding),
                Err(PodBindingError::InvalidIdentity)
            );
        }
    }

    #[tokio::test]
    async fn timeout_is_reported_only_after_confirmed_cancellation() {
        let notification = Arc::new(Notify::new());
        let query_notification = notification.clone();
        let query = async move {
            query_notification.notified().await;
            Err(QueryExecutionError::QueryFailed {
                sqlstate: Some("57014".into()),
                message: "canceling statement due to user request".into(),
            })
        };
        let cancel_notification = notification.clone();
        let result = run_query_with_timeout(
            query,
            TokioInstant::now() + Duration::from_millis(1),
            move || async move {
                cancel_notification.notify_one();
                Ok(())
            },
            QueryTimings {
                cancel_timeout: Duration::from_millis(20),
                cleanup_timeout: Duration::from_millis(20),
            },
        )
        .await;
        assert_eq!(result, Err(QueryExecutionError::TimedOut));
    }

    #[tokio::test]
    async fn cancellation_transport_stays_alive_until_query_termination_is_observed() {
        struct LiveCancellationGuard(Arc<AtomicBool>);

        impl Drop for LiveCancellationGuard {
            fn drop(&mut self) {
                self.0.store(false, Ordering::SeqCst);
            }
        }

        let live = Arc::new(AtomicBool::new(false));
        let notification = Arc::new(Notify::new());
        let query_live = live.clone();
        let query_notification = notification.clone();
        let query = async move {
            query_notification.notified().await;
            tokio::task::yield_now().await;
            if query_live.load(Ordering::SeqCst) {
                Err(QueryExecutionError::QueryFailed {
                    sqlstate: Some("57014".into()),
                    message: "canceling statement due to user request".into(),
                })
            } else {
                Err(QueryExecutionError::DatabaseUnavailable)
            }
        };
        let cancel_live = live.clone();
        let cancel_notification = notification.clone();
        let result = run_query_with_timeout(
            query,
            TokioInstant::now() + Duration::from_millis(1),
            move || async move {
                cancel_live.store(true, Ordering::SeqCst);
                cancel_notification.notify_one();
                Ok(LiveCancellationGuard(cancel_live))
            },
            QueryTimings {
                cancel_timeout: Duration::from_millis(20),
                cleanup_timeout: Duration::from_millis(20),
            },
        )
        .await;
        assert_eq!(result, Err(QueryExecutionError::TimedOut));
        assert!(!live.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn timeout_with_unconfirmed_termination_reports_unknown_outcome() {
        for cancel_result in [Ok(()), Err(QueryExecutionError::DatabaseUnavailable)] {
            let result = run_query_with_timeout(
                std::future::pending(),
                TokioInstant::now() + Duration::from_millis(1),
                move || async move { cancel_result },
                QueryTimings {
                    cancel_timeout: Duration::from_millis(5),
                    cleanup_timeout: Duration::from_millis(5),
                },
            )
            .await;
            assert_eq!(result, Err(QueryExecutionError::OutcomeUnknown));
        }
    }

    #[tokio::test]
    async fn connection_task_is_aborted_and_reaped_before_return() {
        struct DropMarker(Arc<AtomicBool>);

        impl Drop for DropMarker {
            fn drop(&mut self) {
                self.0.store(true, Ordering::SeqCst);
            }
        }

        let dropped = Arc::new(AtomicBool::new(false));
        let task_dropped = dropped.clone();
        let task = tokio::spawn(async move {
            let _marker = DropMarker(task_dropped);
            std::future::pending::<()>().await;
        });
        tokio::task::yield_now().await;
        AbortOnDrop(Some(task)).abort_and_wait().await;
        assert!(dropped.load(Ordering::SeqCst));
    }

    fn test_pod(uid: &str, controller: bool, block_owner_deletion: bool) -> serde_json::Value {
        json!({
            "apiVersion":"v1",
            "kind":"Pod",
            "metadata":{
                "name":"capi-postgres-1",
                "namespace":"database",
                "uid":uid,
                "ownerReferences":[{
                    "apiVersion":"postgresql.cnpg.io/v1",
                    "kind":"Cluster",
                    "name":"capi-postgres",
                    "uid":"database-uid",
                    "controller":controller,
                    "blockOwnerDeletion":block_owner_deletion
                }]
            },
            "spec":{"containers":[{"name":"postgres","image":"postgres:18"}]},
            "status":{
                "phase":"Running",
                "conditions":[{"type":"Ready","status":"True"}]
            }
        })
    }
}
