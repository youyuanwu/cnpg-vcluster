#![cfg(unix)]

use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Write};
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use tenant_controller::docker::{
    BollardDockerClient, DockerClient, DockerContainer, DockerError, WorkerIdentity,
    validate_volume, worker_containers,
};

struct Reply {
    status: u16,
    body: String,
}

impl Reply {
    fn json(status: u16, value: Value) -> Self {
        Self {
            status,
            body: value.to_string(),
        }
    }
}

#[derive(Debug)]
struct Request {
    method: String,
    path: String,
    body: Vec<u8>,
}

struct Daemon {
    path: PathBuf,
    requests: Arc<Mutex<Vec<Request>>>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Daemon {
    fn new(replies: Vec<Reply>) -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = PathBuf::from(format!(
            "tests/fixtures/adapters-{}-{}.sock",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        listener.set_nonblocking(true).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = requests.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let thread = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(10);
            for reply in replies {
                let mut stream = loop {
                    if stopped.load(Ordering::Acquire) {
                        return;
                    }
                    assert!(Instant::now() < deadline, "Docker request never arrived");
                    match listener.accept() {
                        Ok((stream, _)) => break stream,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(error) => panic!("{error}"),
                    }
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut bytes = Vec::new();
                let headers_end = loop {
                    let mut buffer = [0; 4096];
                    let count = stream.read(&mut buffer).unwrap();
                    assert_ne!(count, 0);
                    bytes.extend_from_slice(&buffer[..count]);
                    if let Some(index) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
                        break index + 4;
                    }
                };
                let headers = String::from_utf8(bytes[..headers_end].to_vec()).unwrap();
                let mut line = headers.lines().next().unwrap().split_whitespace();
                let method = line.next().unwrap().to_owned();
                let path = line.next().unwrap().to_owned();
                let content_length = headers
                    .lines()
                    .filter_map(|line| line.split_once(':'))
                    .find(|(key, _)| key.eq_ignore_ascii_case("content-length"))
                    .map_or(0, |(_, value)| value.trim().parse::<usize>().unwrap());
                while bytes.len() < headers_end + content_length {
                    let mut buffer = [0; 4096];
                    let count = stream.read(&mut buffer).unwrap();
                    assert_ne!(count, 0);
                    bytes.extend_from_slice(&buffer[..count]);
                }
                captured.lock().unwrap().push(Request {
                    method,
                    path,
                    body: bytes[headers_end..headers_end + content_length].to_vec(),
                });
                write!(
                    stream,
                    "HTTP/1.1 {} fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    reply.status,
                    reply.body.len(),
                    reply.body
                )
                .unwrap();
            }
        });
        Self {
            path,
            requests,
            stop,
            thread: Some(thread),
        }
    }

    fn client(&self) -> BollardDockerClient {
        BollardDockerClient::connect(self.path.to_str().unwrap()).unwrap()
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let result = self.thread.take().unwrap().join();
        std::fs::remove_file(&self.path).unwrap();
        if !thread::panicking() {
            result.unwrap();
        }
    }
}

fn volume() -> Value {
    json!({
        "Name":"lab-tenant-a-storage","Driver":"local","Scope":"local",
        "CreatedAt":"2026-09-25T12:00:00Z",
        "Mountpoint":"/var/lib/docker/volumes/lab-tenant-a-storage/_data",
        "Labels":{"tenant":"tenant-a","uid":"tenant-uid"},"Options":null
    })
}

fn labels() -> BTreeMap<String, String> {
    [
        ("tenant".into(), "tenant-a".into()),
        ("uid".into(), "tenant-uid".into()),
    ]
    .into()
}

fn container(id: &str, name: &str, tenant: &str) -> Value {
    json!({
        "Id":id,"Name":format!("/{name}"),"State":{"Status":"exited"},
        "Config":{"Labels":{"io.x-k8s.kind.cluster":tenant,"io.x-k8s.kind.role":"worker"}},
        "NetworkSettings":{"Networks":{"kind":{"NetworkID":"network-id","IPAddress":"172.18.0.4"}}}
    })
}

fn route(request: &Request) -> &str {
    if let Some(path) = request.path.strip_prefix("/v")
        && path.starts_with(|c: char| c.is_ascii_digit())
    {
        return &path[path.find('/').unwrap()..];
    }
    &request.path
}

#[tokio::test]
async fn volume_requests_use_exact_names_labels_and_nonforced_delete() {
    let daemon = Daemon::new(vec![
        Reply::json(200, volume()),
        Reply::json(201, volume()),
        Reply {
            status: 204,
            body: String::new(),
        },
    ]);
    let docker = daemon.client();
    let observed = docker
        .inspect_volume("lab-tenant-a-storage")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(observed.created_at, "2026-09-25T12:00:00Z");
    assert_eq!(observed.labels, labels());
    validate_volume(&observed, "lab-tenant-a-storage", &labels()).unwrap();
    assert_eq!(
        docker
            .create_volume("lab-tenant-a-storage", &labels())
            .await
            .unwrap(),
        observed
    );
    docker.remove_volume("lab-tenant-a-storage").await.unwrap();
    let requests = daemon.requests.lock().unwrap();
    assert_eq!(requests.len(), 3);
    assert_eq!(requests[0].method, "GET");
    assert_eq!(route(&requests[0]), "/volumes/lab-tenant-a-storage");
    assert_eq!(requests[1].method, "POST");
    assert_eq!(route(&requests[1]), "/volumes/create");
    assert_eq!(
        serde_json::from_slice::<Value>(&requests[1].body).unwrap(),
        json!({"Name":"lab-tenant-a-storage","Labels":labels()})
    );
    assert_eq!(requests[2].method, "DELETE");
    assert_eq!(route(&requests[2]), "/volumes/lab-tenant-a-storage");
    assert!(requests[2].body.is_empty());
}

#[tokio::test]
async fn lists_all_containers_without_server_filters_and_inspects_exact_ids() {
    let daemon = Daemon::new(vec![
        Reply::json(
            200,
            json!([
                {"Id":"unrelated","Names":["/misleading-worker"],"Labels":{}},
                {"Id":"gone","Names":["/worker-replacement"]},
                {"Id":"worker","Names":["/wrong-list-name"],"Labels":{},"State":"exited"}
            ]),
        ),
        Reply::json(200, container("unrelated", "other-worker", "other")),
        Reply::json(404, json!({"message":"No such container"})),
        Reply::json(200, container("worker", "tenant-a-worker-abc", "tenant-a")),
    ]);
    let docker = daemon.client();
    let machine_names = BTreeSet::from(["tenant-a-worker-abc".into()]);
    let workers = docker
        .list_worker_containers(WorkerIdentity {
            tenant_name: "tenant-a",
            network_id: "network-id",
            machine_names: &machine_names,
        })
        .await
        .unwrap();
    assert_eq!(workers.len(), 1);
    assert_eq!(workers[0].id, "worker");
    assert_eq!(workers[0].name, "tenant-a-worker-abc");
    assert_eq!(workers[0].state, "exited");
    assert_eq!(workers[0].networks["kind"], "network-id");
    assert_eq!(workers[0].network_addresses["network-id"], "172.18.0.4");
    let requests = daemon.requests.lock().unwrap();
    assert_eq!(requests.len(), 4);
    let (path, query) = route(&requests[0]).split_once('?').unwrap();
    assert_eq!(path, "/containers/json");
    let query: BTreeMap<_, _> = url::form_urlencoded::parse(query.as_bytes()).collect();
    assert_eq!(query.get("all").map(|v| v.as_ref()), Some("true"));
    assert!(query.get("filters").is_none_or(|value| value == "{}"));
    assert!(query.get("limit").is_none_or(|value| value == "0"));
    for (index, id) in ["unrelated", "gone", "worker"].iter().enumerate() {
        assert_eq!(requests[index + 1].method, "GET");
        assert_eq!(
            route(&requests[index + 1]),
            format!("/containers/{id}/json")
        );
    }
}

#[tokio::test]
async fn only_inspection_404_can_mean_absence() {
    for status in [400, 403, 404, 409, 429, 500, 503] {
        let daemon = Daemon::new(vec![
            Reply::json(status, json!({"message":"private daemon diagnostic"})),
            Reply::json(status, json!({"message":"private daemon diagnostic"})),
            Reply::json(status, json!({"message":"private daemon diagnostic"})),
            Reply::json(status, json!({"message":"private daemon diagnostic"})),
            Reply::json(status, json!({"message":"private daemon diagnostic"})),
        ]);
        let docker = daemon.client();
        let inspected = docker.inspect_volume("volume").await;
        let container = docker.inspect_container("worker").await;
        if status == 404 {
            assert_eq!(inspected.unwrap(), None);
            assert_eq!(container.unwrap(), None);
        } else {
            assert!(
                matches!(inspected, Err(DockerError::Daemon { status: actual, .. }) if actual == status)
            );
            assert!(
                matches!(container, Err(DockerError::Daemon { status: actual, .. }) if actual == status)
            );
        }
        for error in [
            docker.remove_volume("volume").await.unwrap_err(),
            docker.create_volume("volume", &labels()).await.unwrap_err(),
            docker.list_containers().await.unwrap_err(),
        ] {
            assert!(
                matches!(error, DockerError::Daemon { status: actual, .. } if actual == status)
            );
            assert!(!format!("{error:?} {error}").contains("private daemon diagnostic"));
        }
    }
}

#[tokio::test]
async fn any_non404_inspection_error_aborts_inventory_including_unrelated_containers() {
    for status in [403, 409, 500, 503] {
        let daemon = Daemon::new(vec![
            Reply::json(200, json!([{"Id":"unrelated"}])),
            Reply::json(status, json!({"message":"unavailable"})),
        ]);
        assert!(matches!(
            daemon.client().list_containers().await,
            Err(DockerError::Daemon { status: actual, .. }) if actual == status
        ));
    }
}

#[tokio::test]
async fn transport_decode_missing_id_and_replacement_are_not_absence() {
    let daemon = Daemon::new(vec![Reply::json(200, json!([{"Names":["/worker"]}]))]);
    assert!(matches!(
        daemon.client().list_containers().await,
        Err(DockerError::Identity(_))
    ));
    let daemon = Daemon::new(vec![Reply::json(
        200,
        container("replacement", "worker", "tenant-a"),
    )]);
    assert!(matches!(
        daemon.client().inspect_container("old-id").await,
        Err(DockerError::Identity(_))
    ));
    let daemon = Daemon::new(vec![Reply {
        status: 200,
        body: "{broken".into(),
    }]);
    assert!(matches!(
        daemon.client().inspect_volume("volume").await,
        Err(DockerError::Transport { .. })
    ));
    let daemon = Daemon::new(Vec::new());
    let docker = daemon.client();
    drop(daemon);
    assert!(matches!(
        docker.inspect_volume("volume").await,
        Err(DockerError::Transport { .. })
    ));
}

#[tokio::test]
async fn network_identity_and_subnets_are_retained_and_errors_surface() {
    let daemon = Daemon::new(vec![
        Reply::json(
            200,
            json!({"Id":"network-id","IPAM":{"Config":[{"Subnet":"172.18.0.0/16"},{"Subnet":""},{}]}}),
        ),
        Reply::json(200, json!({"Id":"replacement"})),
        Reply::json(404, json!({"message":"missing"})),
    ]);
    let docker = daemon.client();
    let network = docker.inspect_network("network-id").await.unwrap();
    assert_eq!(network.id, "network-id");
    assert_eq!(network.subnets, ["172.18.0.0/16"]);
    assert!(matches!(
        docker.inspect_network("network-id").await,
        Err(DockerError::Identity(_))
    ));
    assert!(matches!(
        docker.inspect_network("network-id").await,
        Err(DockerError::Daemon { status: 404, .. })
    ));
    assert_eq!(
        route(&daemon.requests.lock().unwrap()[0]),
        "/networks/network-id"
    );
}

fn worker() -> DockerContainer {
    DockerContainer {
        id: "worker-id".into(),
        name: "tenant-a-worker-abc".into(),
        labels: [
            ("io.x-k8s.kind.cluster".into(), "tenant-a".into()),
            ("io.x-k8s.kind.role".into(), "worker".into()),
        ]
        .into(),
        networks: [("kind".into(), "network-id".into())].into(),
        network_addresses: [("network-id".into(), "172.18.0.4".into())].into(),
        state: "running".into(),
    }
}

#[test]
fn worker_validation_checks_all_related_identities_before_counts() {
    let machine_names = BTreeSet::from(["tenant-a-worker-abc".into()]);
    let identity = WorkerIdentity {
        tenant_name: "tenant-a",
        network_id: "network-id",
        machine_names: &machine_names,
    };
    assert_eq!(
        worker_containers(vec![worker()], identity).unwrap(),
        [worker()]
    );
    for edit in 0..6 {
        let mut invalid = worker();
        match edit {
            0 => invalid.id.clear(),
            1 => invalid.name.push_str("-foreign"),
            2 => {
                invalid
                    .labels
                    .insert("io.x-k8s.kind.cluster".into(), "foreign".into());
            }
            3 => {
                invalid.labels.remove("io.x-k8s.kind.role");
            }
            4 => {
                invalid.networks.insert("kind".into(), "foreign-id".into());
            }
            _ => {
                invalid
                    .labels
                    .insert("io.x-k8s.kind.role".into(), "control-plane".into());
            }
        }
        assert!(
            worker_containers(vec![invalid], identity).is_err(),
            "edit {edit}"
        );
    }
    assert!(worker_containers(vec![worker(), worker()], identity).is_err());
}

#[test]
fn exact_capd_load_balancer_is_validated_but_not_counted_as_a_worker() {
    let machine_names = BTreeSet::from(["tenant-a-worker-abc".into()]);
    let identity = WorkerIdentity {
        tenant_name: "tenant-a",
        network_id: "network-id",
        machine_names: &machine_names,
    };
    let mut load_balancer = worker();
    load_balancer.id = "load-balancer-id".into();
    load_balancer.name = "tenant-a-lb".into();
    load_balancer
        .labels
        .insert("io.x-k8s.kind.role".into(), "external-load-balancer".into());
    assert_eq!(
        worker_containers(vec![load_balancer.clone(), worker()], identity).unwrap(),
        [worker()]
    );
    for edit in 0..4 {
        let mut foreign = load_balancer.clone();
        match edit {
            0 => {
                foreign
                    .labels
                    .insert("io.x-k8s.kind.cluster".into(), "other".into());
            }
            1 => {
                foreign.name.push_str("-foreign");
            }
            2 => {
                foreign.networks.insert("kind".into(), "other".into());
            }
            _ => {
                foreign
                    .labels
                    .insert("io.x-k8s.kind.role".into(), "worker".into());
            }
        }
        assert!(worker_containers(vec![foreign], identity).is_err());
    }
}

#[tokio::test]
async fn volume_collision_or_malformed_identity_never_becomes_created_owned_volume() {
    for edit in 0..4 {
        let mut existing = volume();
        match edit {
            0 => existing["Name"] = json!("replacement"),
            1 => existing["Labels"] = json!({"foreign":"true"}),
            2 => existing["CreatedAt"] = json!(""),
            _ => existing["Mountpoint"] = json!("relative/path"),
        }
        let daemon = Daemon::new(vec![Reply::json(201, existing)]);
        assert!(
            daemon
                .client()
                .create_volume("lab-tenant-a-storage", &labels())
                .await
                .is_err()
        );
    }
}

#[tokio::test]
async fn unsafe_path_identifiers_are_rejected_before_transport() {
    let daemon = Daemon::new(Vec::new());
    for name in [
        "",
        "../other",
        "volume?force=true",
        "worker/name",
        ".",
        "..",
    ] {
        assert!(matches!(
            daemon.client().inspect_volume(name).await,
            Err(DockerError::Identity(_))
        ));
        assert!(matches!(
            daemon.client().remove_volume(name).await,
            Err(DockerError::Identity(_))
        ));
    }
    assert!(daemon.requests.lock().unwrap().is_empty());
}
