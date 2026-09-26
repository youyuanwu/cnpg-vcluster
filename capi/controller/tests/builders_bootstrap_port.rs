//! Go parity: phase2_test.go bootstrap cases and
//! reconcile_workers_test.go ordinal preparation and order independence.

use tenant_controller::resources::*;

fn archives() -> Vec<WorkerArchive> {
    REQUIRED_WORKER_IMAGE_KEYS
        .into_iter()
        .map(|key| WorkerArchive {
            key: key.into(),
            path: format!("images/{}.tar", key.to_lowercase()),
            sha256: "b".repeat(64),
            reference: format!(
                "example/{}:v1@sha256:{}",
                key.to_lowercase(),
                "c".repeat(64)
            ),
            tagged: format!("docker.io/example/{}:v1", key.to_lowercase()),
            worker: true,
        })
        .collect()
}

fn inputs(archives: &[WorkerArchive]) -> BootstrapInputs<'_> {
    BootstrapInputs {
        archives,
        generation: "generation",
        cache_container_path: "/var/lib/capi-image-cache",
        storage_container_path: "/var/lib/storage",
        offline_enforced: false,
        registry: None,
        allowed_subnets: &[],
    }
}

#[test]
fn sorted_archive_import_and_three_reference_forms_are_exact() {
    let archives = archives();
    let commands = worker_bootstrap_commands(inputs(&archives), 1).unwrap();
    assert_eq!(commands.len(), 36);
    let first_path =
        "'/var/lib/capi-image-cache/generations/generation/images/calico_cni_image.tar'";
    assert_eq!(
        &commands[..5],
        &[
            format!(
                "printf '%s  %s\\n' '{}' {first_path} | sha256sum -c -",
                "b".repeat(64)
            ),
            format!("ctr --namespace k8s.io images import --digests {first_path}"),
            format!(
                "ctr --namespace k8s.io images tag --force 'docker.io/example/calico_cni_image:v1' 'example/calico_cni_image:v1@sha256:{}'",
                "c".repeat(64)
            ),
            format!(
                "ctr --namespace k8s.io images tag --force 'docker.io/example/calico_cni_image:v1' 'docker.io/example/calico_cni_image:v1@sha256:{}'",
                "c".repeat(64)
            ),
            format!(
                "ctr --namespace k8s.io images tag --force 'docker.io/example/calico_cni_image:v1' 'docker.io/example/calico_cni_image@sha256:{}'",
                "c".repeat(64)
            ),
        ]
    );
    let mut sorted = archives.clone();
    sorted.sort_by(|left, right| left.key.cmp(&right.key));
    let (chunks, remainder) = commands[..35].as_chunks::<5>();
    assert!(remainder.is_empty());
    for (chunk, archive) in chunks.iter().zip(sorted) {
        assert!(chunk[0].contains(&archive.path));
        assert!(chunk[1].contains(&archive.path));
        assert!(chunk[2].ends_with(&shell_quote(&archive.reference)));
        assert!(chunk[3].ends_with(&shell_quote(&canonical_exact_reference(&archive).unwrap())));
        assert!(chunk[4].ends_with(&shell_quote(&runtime_digest_reference(&archive).unwrap())));
    }
    assert!(!commands.join("\n").contains("hosts.toml"));
    assert!(!commands.join("\n").contains("CAPI_OFFLINE"));
}

#[test]
fn ordinal_directories_are_prepared_before_offline_setup_and_independent_of_archive_order() {
    let mut archives = archives();
    let registry = OfflineRegistry {
        address: "172.18.0.10".into(),
        port: 5000,
    };
    for offline in [false, true] {
        for count in [1, 2, 3] {
            let mut input = inputs(&archives);
            input.offline_enforced = offline;
            let input = BootstrapInputs {
                registry: Some(&registry),
                ..input
            };
            let baseline = worker_bootstrap_commands(input, 0).unwrap();
            let commands = worker_bootstrap_commands(input, count).unwrap();
            assert_eq!(commands.len(), baseline.len() + count as usize);
            for ordinal in 1..=count {
                let directory = format!("'/var/lib/storage/volumes/cnpg/{ordinal}'");
                assert_eq!(
                    commands[35 + ordinal as usize - 1],
                    format!(
                        "mkdir -p {directory} && chown 26:26 {directory} && chmod 0700 {directory}"
                    )
                );
            }
            let mut unchanged = commands[..35].to_vec();
            unchanged.extend_from_slice(&commands[35 + count as usize..]);
            assert_eq!(unchanged, baseline);
            assert_eq!(worker_bootstrap_commands(input, count).unwrap(), commands);
            archives.reverse();
            let mut reversed = inputs(&archives);
            reversed.offline_enforced = offline;
            reversed.registry = Some(&registry);
            assert_eq!(
                worker_bootstrap_commands(reversed, count).unwrap(),
                commands
            );
        }
    }
}

#[test]
fn offline_mirrors_include_every_worker_registry_and_egress_rule_order_is_exact() {
    let mut archives = archives();
    archives[0].tagged = "registry.k8s.io/calico/cni:v1".into();
    archives[1].tagged = "quay.io/calico/controllers:v1".into();
    archives.push(WorkerArchive {
        key: "VERIFY_IMAGE".into(),
        tagged: "localhost:5001/verify:v1".into(),
        ..archives[2].clone()
    });
    archives.push(WorkerArchive {
        key: "MANAGEMENT_ONLY".into(),
        tagged: "unused.example/image:v1".into(),
        worker: false,
        ..archives[2].clone()
    });
    let registry = OfflineRegistry {
        address: "172.18.0.10".into(),
        port: 5000,
    };
    let subnets = ["10.0.0.0/16", "127.0.0.0/8", "172.18.0.0/16"].map(String::from);
    let mut input = inputs(&archives);
    input.offline_enforced = true;
    input.registry = Some(&registry);
    input.allowed_subnets = &subnets;
    let commands = worker_bootstrap_commands(input, 1).unwrap();
    assert_eq!(commands.len(), 35 + 1 + 4 + 8);
    for (command, (registry, server)) in commands[36..40].iter().zip([
        ("docker.io", "https://registry-1.docker.io"),
        ("localhost:5001", "https://localhost:5001"),
        ("quay.io", "https://quay.io"),
        ("registry.k8s.io", "https://registry.k8s.io"),
    ]) {
        assert_eq!(
            command,
            &format!(
                "umask 077; mkdir -p '/etc/containerd/certs.d/{registry}'; printf %s 'server = \"{server}\"\n\n[host.\"http://172.18.0.10:5000\"]\n  capabilities = [\"pull\", \"resolve\"]\n' > '/etc/containerd/certs.d/{registry}/hosts.toml'"
            )
        );
    }
    assert_eq!(
        &commands[40..],
        &[
            "iptables -N CAPI_OFFLINE 2>/dev/null || true",
            "iptables -F CAPI_OFFLINE",
            "iptables -A CAPI_OFFLINE -d '10.0.0.0/16' -j RETURN",
            "iptables -A CAPI_OFFLINE -d '127.0.0.0/8' -j RETURN",
            "iptables -A CAPI_OFFLINE -d '172.18.0.0/16' -j RETURN",
            "iptables -A CAPI_OFFLINE -p tcp -m multiport --dports 80,443 -j REJECT",
            "iptables -A CAPI_OFFLINE -j RETURN",
            "iptables -C OUTPUT -j CAPI_OFFLINE 2>/dev/null || iptables -I OUTPUT 1 -j CAPI_OFFLINE",
        ]
    );
    assert!(!commands.join("\n").contains("unused.example"));
    assert_eq!(
        commands
            .iter()
            .filter(|command| command.contains("images import"))
            .count(),
        7
    );
}

#[test]
fn bootstrap_refuses_each_missing_duplicate_or_unprepared_required_image() {
    for key in REQUIRED_WORKER_IMAGE_KEYS {
        let mut missing = archives();
        missing.retain(|archive| archive.key != key);
        assert!(
            matches!(worker_bootstrap_commands(inputs(&missing),1),Err(BuildError::WorkerImage(value)) if value == key)
        );
        let mut duplicate = archives();
        duplicate.push(
            duplicate
                .iter()
                .find(|archive| archive.key == key)
                .unwrap()
                .clone(),
        );
        assert!(worker_bootstrap_commands(inputs(&duplicate), 1).is_err());
        let mut unprepared = archives();
        unprepared
            .iter_mut()
            .find(|archive| archive.key == key)
            .unwrap()
            .worker = false;
        assert!(worker_bootstrap_commands(inputs(&unprepared), 1).is_err());
    }
}

#[test]
fn malformed_bootstrap_inputs_fail_instead_of_emitting_partial_commands() {
    let mut archives = archives();
    let valid = archives[0].clone();
    for path in [
        "",
        "../outside.tar",
        "/absolute.tar",
        "images/../outside.tar",
        "images//archive.tar",
    ] {
        archives[0].path = path.into();
        assert!(worker_bootstrap_commands(inputs(&archives), 1).is_err());
    }
    archives[0] = valid.clone();
    archives[0].sha256 = "not-a-sha256".into();
    assert!(worker_bootstrap_commands(inputs(&archives), 1).is_err());
    archives[0] = valid.clone();
    archives[0].reference = "image:no-digest".into();
    assert!(worker_bootstrap_commands(inputs(&archives), 1).is_err());
    archives[0] = valid;
    for count in [-1, 4] {
        assert!(worker_bootstrap_commands(inputs(&archives), count).is_err());
    }
    let mut input = inputs(&archives);
    input.offline_enforced = true;
    assert!(matches!(
        worker_bootstrap_commands(input, 1),
        Err(BuildError::Registry)
    ));
    for (address, port) in [("", 5000), ("127.0.0.1", 0)] {
        let registry = OfflineRegistry {
            address: address.into(),
            port,
        };
        let input = BootstrapInputs {
            registry: Some(&registry),
            ..input
        };
        assert!(matches!(
            worker_bootstrap_commands(input, 1),
            Err(BuildError::Registry)
        ));
    }
    for generation in ["", "..", "../outside", "a/b"] {
        let mut input = inputs(&archives);
        input.generation = generation;
        assert!(worker_bootstrap_commands(input, 1).is_err());
    }
}

#[test]
fn registry_and_reference_parsing_preserve_ports_and_docker_canonical_names() {
    for (image, registry) in [
        ("postgres:17", "docker.io"),
        ("library/postgres:17", "docker.io"),
        ("example/pg:v1", "docker.io"),
        ("docker.io/example/pg:v1", "docker.io"),
        ("localhost/pg:v1", "localhost"),
        ("localhost:5000/pg:v1", "localhost:5000"),
        ("quay.io/org/image:v1@sha256:abc", "quay.io"),
    ] {
        assert_eq!(image_registry(image), registry);
    }
    let mut archive = archives().remove(0);
    archive.tagged = "localhost:5000/image:v1".into();
    archive.reference = "image:v1@sha256:abc".into();
    assert_eq!(
        canonical_exact_reference(&archive).unwrap(),
        "localhost:5000/image:v1@sha256:abc"
    );
    assert_eq!(
        runtime_digest_reference(&archive).unwrap(),
        "localhost:5000/image@sha256:abc"
    );
    archive.tagged = "localhost:5000/image".into();
    assert_eq!(
        runtime_digest_reference(&archive).unwrap(),
        "localhost:5000/image@sha256:abc"
    );
    assert_eq!(shell_quote("one'two"), "'one'\"'\"'two'");
    assert_eq!(shell_quote(""), "''");
}

#[test]
fn bootstrap_quotes_every_host_controlled_path_and_argument() {
    let mut archives = archives();
    archives[0].path = "images/a'b.tar".into();
    let mut input = inputs(&archives);
    input.cache_container_path = "/cache's";
    input.storage_container_path = "/storage's";
    let commands = worker_bootstrap_commands(input, 1).unwrap();
    assert!(commands[0].contains("'/cache'\"'\"'s/generations/generation/images/a'\"'\"'b.tar'"));
    assert!(commands[35].contains("'/storage'\"'\"'s/volumes/cnpg/1'"));
}
