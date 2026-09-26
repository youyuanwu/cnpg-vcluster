mod support;

use std::collections::BTreeMap;

use k8s_openapi::api::coordination::v1::{Lease, LeaseSpec};
use kube::Client;
use serde_json::{Value, json};
use support::Server;
use tenant_controller::{
    allocation::{
        AllocationError, ClaimContext, ClaimDecision, ReleaseDecision, allocate, decide_claim,
        decide_release, lease_name, new_lease, recover_allocation, release,
    },
    foundation::AllocationSlot,
};

fn slots() -> Vec<AllocationSlot> {
    vec![
        AllocationSlot {
            slot_id: "slot-a".into(),
            vip_ordinal: 0,
            endpoint: "172.18.255.223".into(),
            pod_cidr: "10.73.0.0/16".into(),
            service_cidr: "10.143.0.0/16".into(),
        },
        AllocationSlot {
            slot_id: "slot-b".into(),
            vip_ordinal: 1,
            endpoint: "172.18.255.224".into(),
            pod_cidr: "10.74.0.0/16".into(),
            service_cidr: "10.144.0.0/16".into(),
        },
    ]
}

fn context<'a>(uid: &'a str, slots: &'a [AllocationSlot]) -> ClaimContext<'a> {
    ClaimContext {
        namespace: "management",
        ownership_label: "example.io/owned",
        lab_prefix: "example",
        tenant_name: "tenant-a",
        tenant_uid: uid,
        spec_hash: "spec-hash",
        foundation_hash: "foundation-hash",
        slots,
    }
}

fn owned(context: &ClaimContext<'_>, slot: &AllocationSlot, uid: &str, rv: &str) -> Lease {
    let mut lease = new_lease(context, slot);
    lease.metadata.uid = Some(uid.into());
    lease.metadata.resource_version = Some(rv.into());
    lease
}

#[test]
fn deterministic_names_exact_markers_and_status() {
    let slots = slots();
    let context = context("uid-a", &slots);
    let name = lease_name(&slots[0].slot_id);
    assert_eq!(name, lease_name("slot-a"));
    assert!(
        name.len() <= 63
            && name
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
    );
    assert_ne!(name, lease_name("slot-b"));
    assert_ne!(name, lease_name("SLOT-A"));
    let long_name = lease_name(&"s".repeat(63));
    assert_eq!(long_name.len(), 63);
    assert!(
        long_name
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
    );
    let lease = new_lease(&context, &slots[0]);
    assert_eq!(lease.metadata.namespace.as_deref(), Some("management"));
    assert_eq!(lease.metadata.name.as_deref(), Some(name.as_str()));
    assert_eq!(
        lease.metadata.labels.as_ref().unwrap(),
        &BTreeMap::from([
            ("example.io/owned".into(), "example".into()),
            ("tenancy.cnpg-vcluster.io/slot-id".into(), "slot-a".into()),
            ("tenancy.cnpg-vcluster.io/tenant".into(), "tenant-a".into()),
        ])
    );
    assert_eq!(
        lease.metadata.annotations.as_ref().unwrap(),
        &BTreeMap::from([
            ("tenancy.cnpg-vcluster.io/tenant".into(), "tenant-a".into()),
            ("tenancy.cnpg-vcluster.io/tenant-uid".into(), "uid-a".into()),
            (
                "tenancy.cnpg-vcluster.io/spec-hash".into(),
                "spec-hash".into()
            ),
            (
                "tenancy.cnpg-vcluster.io/foundation-hash".into(),
                "foundation-hash".into()
            ),
            (
                "tenancy.cnpg-vcluster.io/resource".into(),
                "allocation-lease".into()
            ),
            ("tenancy.cnpg-vcluster.io/slot-id".into(), "slot-a".into()),
            (
                "tenancy.cnpg-vcluster.io/endpoint".into(),
                slots[0].endpoint.clone()
            ),
            (
                "tenancy.cnpg-vcluster.io/pod-cidr".into(),
                slots[0].pod_cidr.clone()
            ),
            (
                "tenancy.cnpg-vcluster.io/service-cidr".into(),
                slots[0].service_cidr.clone()
            ),
        ])
    );
    let claim = match decide_claim(
        &context,
        &[owned(&context, &slots[0], "lease-a", "1")],
        None,
    )
    .unwrap()
    {
        ClaimDecision::Existing(claim) => claim,
        _ => panic!("did not recover existing claim"),
    };
    assert_eq!(claim.status().slot_id, "slot-a");
    assert_eq!(claim.status().endpoint, slots[0].endpoint);
    assert_eq!(claim.status().pod_cidr, slots[0].pod_cidr);
    assert_eq!(claim.status().service_cidr, slots[0].service_cidr);
}

#[test]
fn recovery_duplicates_collisions_and_exhaustion() {
    let slots = slots();
    let context = context("uid-a", &slots);
    let first = owned(&context, &slots[0], "lease-a", "1");
    let second = owned(&context, &slots[1], "lease-b", "2");
    assert!(matches!(
        decide_claim(&context, std::slice::from_ref(&first), None),
        Ok(ClaimDecision::Existing(_))
    ));
    assert!(matches!(
        decide_claim(&context, &[first.clone(), second], None),
        Err(AllocationError::Duplicate)
    ));
    let mut other = first.clone();
    other.metadata.annotations.as_mut().unwrap().insert(
        "tenancy.cnpg-vcluster.io/tenant-uid".into(),
        "uid-other".into(),
    );
    assert!(matches!(
        decide_claim(&context, &[other.clone()], None),
        Err(AllocationError::NameHeld)
    ));
    other
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .insert("tenancy.cnpg-vcluster.io/tenant".into(), "other".into());
    assert_eq!(
        decide_claim(&context, &[other.clone()], None).unwrap(),
        ClaimDecision::Create(slots[1].clone())
    );
    let mut second_other = other.clone();
    second_other.metadata.name = Some(lease_name(&slots[1].slot_id));
    assert!(matches!(
        decide_claim(&context, &[other, second_other], None),
        Err(AllocationError::Exhausted)
    ));
    for key in [
        "tenancy.cnpg-vcluster.io/spec-hash",
        "tenancy.cnpg-vcluster.io/foundation-hash",
        "tenancy.cnpg-vcluster.io/endpoint",
        "tenancy.cnpg-vcluster.io/pod-cidr",
        "tenancy.cnpg-vcluster.io/service-cidr",
        "tenancy.cnpg-vcluster.io/slot-id",
    ] {
        let mut invalid = first.clone();
        invalid
            .metadata
            .annotations
            .as_mut()
            .unwrap()
            .insert(key.into(), "changed".into());
        assert!(
            matches!(
                decide_claim(&context, &[invalid], None),
                Err(AllocationError::Claim(_))
            ),
            "{key}"
        );
    }
    let mut invalid = first;
    invalid
        .metadata
        .labels
        .as_mut()
        .unwrap()
        .remove("tenancy.cnpg-vcluster.io/slot-id");
    assert!(matches!(
        decide_claim(&context, &[invalid], None),
        Err(AllocationError::Claim(_))
    ));
}

#[test]
fn api_defaulted_empty_spec_is_durable_but_election_fields_are_rejected() {
    let slots = slots();
    let context = context("uid-a", &slots);
    let mut lease = owned(&context, &slots[0], "lease-a", "1");
    lease.spec = Some(LeaseSpec::default());
    assert!(matches!(
        decide_claim(&context, std::slice::from_ref(&lease), None),
        Ok(ClaimDecision::Existing(_))
    ));
    assert!(matches!(
        decide_release(&context, std::slice::from_ref(&lease), None, true),
        Ok(ReleaseDecision::Delete(_))
    ));
    for spec in [
        LeaseSpec {
            holder_identity: Some("foreign".into()),
            ..Default::default()
        },
        LeaseSpec {
            lease_duration_seconds: Some(30),
            ..Default::default()
        },
        LeaseSpec {
            lease_transitions: Some(0),
            ..Default::default()
        },
    ] {
        lease.spec = Some(spec);
        assert!(matches!(
            decide_claim(&context, std::slice::from_ref(&lease), None),
            Err(AllocationError::Claim(_))
        ));
        assert!(matches!(
            decide_release(&context, std::slice::from_ref(&lease), None, true),
            Err(AllocationError::Claim(_))
        ));
    }
}

#[test]
fn status_bound_claim_and_terminal_proof_are_distinct() {
    let slots = slots();
    let context = context("uid-a", &slots);
    let first = owned(&context, &slots[0], "lease-a", "12");
    let bound = match decide_claim(&context, std::slice::from_ref(&first), None).unwrap() {
        ClaimDecision::Existing(claim) => claim.status(),
        _ => unreachable!(),
    };
    assert!(matches!(
        decide_claim(&context, &[], Some(&bound)),
        Err(AllocationError::Missing)
    ));
    let mut wrong = bound.clone();
    wrong.endpoint = "172.18.255.225".into();
    assert!(matches!(
        decide_claim(&context, std::slice::from_ref(&first), Some(&wrong)),
        Err(AllocationError::StatusMismatch)
    ));
    assert!(matches!(
        decide_release(&context, &[], Some(&bound), false),
        Err(AllocationError::Missing)
    ));
    assert_eq!(
        decide_release(&context, std::slice::from_ref(&first), Some(&bound), false).unwrap(),
        ReleaseDecision::Pending
    );
    assert_eq!(
        decide_release(&context, &[], Some(&bound), true).unwrap(),
        ReleaseDecision::Complete
    );
    let mut successor = first.clone();
    successor.metadata.uid = Some("lease-new".into());
    successor.metadata.annotations.as_mut().unwrap().insert(
        "tenancy.cnpg-vcluster.io/tenant-uid".into(),
        "new-tenant-uid".into(),
    );
    assert!(matches!(
        decide_release(&context, &[successor.clone()], Some(&bound), false),
        Err(AllocationError::NameHeld)
    ));
    assert_eq!(
        decide_release(&context, &[successor], Some(&bound), true).unwrap(),
        ReleaseDecision::Complete
    );
    let mut malformed = first.clone();
    malformed
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .remove("tenancy.cnpg-vcluster.io/tenant-uid");
    assert!(matches!(
        decide_release(&context, &[malformed], Some(&bound), true),
        Err(AllocationError::Claim(_))
    ));
    let intent = match decide_release(&context, &[first], Some(&bound), true).unwrap() {
        ReleaseDecision::Delete(intent) => intent,
        _ => panic!("old claim must still be deleted"),
    };
    assert_eq!(
        (intent.uid.as_str(), intent.resource_version.as_str()),
        ("lease-a", "12")
    );
    assert_eq!(
        decide_release(&context, &[], None, false).unwrap(),
        ReleaseDecision::Pending
    );
}

#[test]
fn deletion_recovers_exact_claims_without_a_creation_slot_catalog() {
    let slots = slots();
    let creation = context("uid-a", &slots);
    let deletion = context("uid-a", &[]);
    let lease = owned(&creation, &slots[0], "lease-a", "12");
    let bound = (&slots[0]).into();
    assert_eq!(
        recover_allocation(&deletion, std::slice::from_ref(&lease)).unwrap(),
        Some(bound)
    );
    let bound = (&slots[0]).into();
    for bound in [None, Some(&bound)] {
        assert_eq!(
            decide_release(&deletion, std::slice::from_ref(&lease), bound, false).unwrap(),
            ReleaseDecision::Pending
        );
        assert!(matches!(
            decide_release(&deletion, std::slice::from_ref(&lease), bound, true),
            Ok(ReleaseDecision::Delete(_))
        ));
    }
    assert_eq!(
        decide_release(&deletion, &[], None, true).unwrap(),
        ReleaseDecision::Complete
    );
    assert!(matches!(
        decide_claim(&deletion, &[], None),
        Err(AllocationError::InvalidPool)
    ));
    let mut changed: tenant_controller::api::AllocationStatus = (&slots[0]).into();
    changed.endpoint = "172.18.255.225".into();
    assert!(matches!(
        decide_release(
            &deletion,
            std::slice::from_ref(&lease),
            Some(&changed),
            true
        ),
        Err(AllocationError::StatusMismatch)
    ));
    assert!(matches!(
        recover_allocation(
            &deletion,
            &[lease, owned(&creation, &slots[1], "duplicate", "13")]
        ),
        Err(AllocationError::Duplicate)
    ));
}

fn malformed_successors(valid: &Lease) -> Vec<(&'static str, Lease)> {
    let mut cases = Vec::new();
    for key in [
        "tenancy.cnpg-vcluster.io/tenant",
        "tenancy.cnpg-vcluster.io/tenant-uid",
        "tenancy.cnpg-vcluster.io/spec-hash",
        "tenancy.cnpg-vcluster.io/foundation-hash",
        "tenancy.cnpg-vcluster.io/resource",
        "tenancy.cnpg-vcluster.io/slot-id",
        "tenancy.cnpg-vcluster.io/endpoint",
        "tenancy.cnpg-vcluster.io/pod-cidr",
        "tenancy.cnpg-vcluster.io/service-cidr",
    ] {
        let mut lease = valid.clone();
        lease.metadata.annotations.as_mut().unwrap().remove(key);
        cases.push((key, lease));
    }
    for mutation in [
        "labels",
        "extra-label",
        "extra-annotation",
        "namespace",
        "uid",
        "resource-version",
        "owner",
        "deleting",
        "election-spec",
        "empty-spec-hash",
        "foreign-foundation",
        "invalid-tenant-name",
        "changed-slot",
        "changed-endpoint",
        "changed-pod-cidr",
        "changed-service-cidr",
    ] {
        let mut lease = valid.clone();
        match mutation {
            "labels" => lease.metadata.labels = None,
            "extra-label" => {
                lease
                    .metadata
                    .labels
                    .as_mut()
                    .unwrap()
                    .insert("extra".into(), "value".into());
            }
            "extra-annotation" => {
                lease
                    .metadata
                    .annotations
                    .as_mut()
                    .unwrap()
                    .insert("extra".into(), "value".into());
            }
            "namespace" => lease.metadata.namespace = Some("foreign".into()),
            "uid" => lease.metadata.uid = None,
            "resource-version" => lease.metadata.resource_version = Some(String::new()),
            "owner" => lease.metadata.owner_references = Some(vec![Default::default()]),
            "deleting" => {
                lease.metadata.deletion_timestamp =
                    Some(serde_json::from_value(json!("2026-09-25T00:00:00Z")).unwrap())
            }
            "election-spec" => {
                lease.spec = Some(LeaseSpec {
                    holder_identity: Some("election".into()),
                    ..Default::default()
                })
            }
            mutation => {
                let (key, value) = match mutation {
                    "empty-spec-hash" => ("spec-hash", ""),
                    "foreign-foundation" => ("foundation-hash", "foreign"),
                    "invalid-tenant-name" => ("tenant", "INVALID"),
                    "changed-slot" => ("slot-id", "other-slot"),
                    "changed-endpoint" => ("endpoint", "172.18.255.225"),
                    "changed-pod-cidr" => ("pod-cidr", "10.99.0.0/16"),
                    "changed-service-cidr" => ("service-cidr", "10.199.0.0/16"),
                    _ => unreachable!(),
                };
                lease
                    .metadata
                    .annotations
                    .as_mut()
                    .unwrap()
                    .insert(format!("tenancy.cnpg-vcluster.io/{key}"), value.into());
            }
        }
        cases.push((mutation, lease));
    }
    cases
}

#[tokio::test]
async fn malformed_terminal_successors_never_complete_or_receive_mutations() {
    let slots = slots();
    let old = context("uid-a", &[]);
    let successor = ClaimContext {
        tenant_uid: "uid-next",
        spec_hash: "new-spec",
        ..old
    };
    let valid = owned(&successor, &slots[0], "successor-lease", "20");
    let bound = (&slots[0]).into();
    for (case, malformed) in malformed_successors(&valid) {
        for reread in [false, true] {
            let mock = Mock::default();
            if reread {
                mock.insert(owned(&old, &slots[0], "old-lease", "10"));
                mock.replace_on_get(malformed.clone());
            } else {
                mock.insert(malformed.clone());
            }
            assert!(
                release(mock.client(), &old, Some(&bound), true)
                    .await
                    .is_err(),
                "{case}, reread={reread}"
            );
            assert_eq!(mock.leases(), vec![malformed.clone()]);
            assert!(
                mock.requests().iter().all(|(method, _, _)| method == "GET"),
                "{case}"
            );
        }
    }
    for name in ["tenant-a", "tenant-b"] {
        let successor = ClaimContext {
            tenant_name: name,
            ..successor
        };
        let valid = owned(&successor, &slots[0], "successor-lease", "20");
        assert_eq!(
            decide_release(&old, std::slice::from_ref(&valid), Some(&bound), true).unwrap(),
            ReleaseDecision::Complete
        );
        assert!(decide_release(&old, std::slice::from_ref(&valid), Some(&bound), false).is_err());
        let duplicate = owned(&successor, &slots[1], "duplicate", "21");
        assert!(matches!(
            decide_release(&old, &[valid, duplicate], Some(&bound), true),
            Err(AllocationError::Duplicate)
        ));
    }
}

#[derive(Clone)]
struct Mock(Server);

impl Default for Mock {
    fn default() -> Self {
        let server = Server::default();
        server.allow_list(LEASES);
        Self(server)
    }
}

impl Mock {
    fn client(&self) -> Client {
        self.0.client()
    }

    fn insert(&self, lease: Lease) {
        let name = lease.metadata.name.clone().unwrap();
        self.0.insert(&lease_path(&name), lease);
    }

    fn requests(&self) -> Vec<(String, String, Value)> {
        self.0
            .calls()
            .into_iter()
            .map(|call| (call.method, call.path, call.body))
            .collect()
    }

    fn clear(&self) {
        self.0.clear();
    }

    fn remove(&self, name: &str) {
        self.0.remove(&lease_path(name));
    }

    fn leases(&self) -> Vec<Lease> {
        self.0
            .values(&format!("{LEASES}/"))
            .into_iter()
            .map(|value| serde_json::from_value(value).unwrap())
            .collect()
    }

    fn lease(&self, name: &str) -> Lease {
        serde_json::from_value(self.0.get(&lease_path(name))).unwrap()
    }

    fn create_collision(&self, lease: Lease) {
        let name = lease.metadata.name.clone().unwrap();
        self.0.mutate_on(
            "POST",
            LEASES,
            &lease_path(&name),
            Some(serde_json::to_value(lease).unwrap()),
        );
        self.0.respond(
            "POST",
            LEASES,
            409,
            support::kube::status(409, "AlreadyExists"),
        );
    }

    fn replace_on_get(&self, lease: Lease) {
        let path = lease_path(lease.metadata.name.as_deref().unwrap());
        self.0
            .replace_on("GET", &path, Some(serde_json::to_value(lease).unwrap()));
    }

    fn replace_on_delete(&self, lease: Lease) {
        let path = lease_path(lease.metadata.name.as_deref().unwrap());
        self.0
            .replace_on("DELETE", &path, Some(serde_json::to_value(lease).unwrap()));
    }
}

const LEASES: &str = "/apis/coordination.k8s.io/v1/namespaces/management/leases";

fn lease_path(name: &str) -> String {
    format!("{LEASES}/{name}")
}

#[tokio::test]
async fn create_restart_before_status_and_bound_get() {
    let mock = Mock::default();
    let slots = slots();
    let context = context("uid-a", &slots);
    let first = allocate(mock.client(), &context, None).await.unwrap();
    assert_eq!(first.slot, slots[0]);
    let recovered = allocate(mock.client(), &context, None).await.unwrap();
    assert_eq!(first, recovered);
    assert_eq!(
        allocate(mock.client(), &context, Some(&first.status()))
            .await
            .unwrap(),
        first
    );
    let calls = mock.requests();
    assert_eq!(
        calls
            .iter()
            .filter(|(method, _, _)| method == "POST")
            .count(),
        1
    );
    assert!(
        calls
            .iter()
            .filter(|(method, _, _)| method == "GET")
            .count()
            >= 2
    );
    assert!(
        calls
            .iter()
            .filter(|(method, _, _)| method == "GET")
            .all(|(_, path, _)| path
                .starts_with("/apis/coordination.k8s.io/v1/namespaces/management/leases"))
    );
    mock.clear();
    assert!(matches!(
        allocate(mock.client(), &context, Some(&first.status())).await,
        Err(AllocationError::Missing)
    ));
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|(method, _, _)| method == "POST")
            .count(),
        1
    );
}

#[tokio::test]
async fn already_exists_reread_foreign_and_same_owner() {
    let slots = slots();
    let context = context("uid-a", &slots);
    let mock = Mock::default();
    mock.create_collision(owned(&context, &slots[0], "winning-lease", "8"));
    let winner = allocate(mock.client(), &context, None).await.unwrap();
    assert_eq!(winner.lease_uid, "winning-lease");
    assert!(mock.requests().iter().any(|(method, _, _)| method == "GET"));

    let mock = Mock::default();
    let mut foreign = owned(&context, &slots[0], "foreign-lease", "8");
    foreign.metadata.annotations.as_mut().unwrap().insert(
        "tenancy.cnpg-vcluster.io/tenant-uid".into(),
        "other-uid".into(),
    );
    foreign
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .insert("tenancy.cnpg-vcluster.io/tenant".into(), "other".into());
    mock.create_collision(foreign);
    let allocated = allocate(mock.client(), &context, None).await.unwrap();
    assert_eq!(allocated.slot, slots[1]);
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|(method, _, _)| method == "POST")
            .count(),
        2
    );
}

#[tokio::test]
async fn live_duplicate_and_changed_status_bound_claim_refuse_mutation() {
    let mock = Mock::default();
    let slots = slots();
    let context = context("uid-a", &slots);
    let first = owned(&context, &slots[0], "lease-a", "1");
    let second = owned(&context, &slots[1], "lease-b", "2");
    let bound = match decide_claim(&context, std::slice::from_ref(&first), None).unwrap() {
        ClaimDecision::Existing(claim) => claim.status(),
        _ => unreachable!(),
    };
    mock.insert(first.clone());
    mock.insert(second);
    assert!(matches!(
        allocate(mock.client(), &context, None).await,
        Err(AllocationError::Duplicate)
    ));
    assert!(matches!(
        release(mock.client(), &context, Some(&bound), true).await,
        Err(AllocationError::Duplicate)
    ));
    assert!(
        !mock
            .requests()
            .iter()
            .any(|(method, _, _)| method == "POST" || method == "DELETE")
    );

    mock.remove(&lease_name("slot-b"));
    let mut changed = first;
    changed
        .metadata
        .labels
        .as_mut()
        .unwrap()
        .insert("tenancy.cnpg-vcluster.io/slot-id".into(), "changed".into());
    mock.replace_on_get(changed);
    assert!(matches!(
        allocate(mock.client(), &context, Some(&bound)).await,
        Err(AllocationError::Claim(_))
    ));
    assert!(
        !mock
            .requests()
            .iter()
            .any(|(method, _, _)| method == "POST" || method == "DELETE")
    );
}

#[tokio::test]
async fn concurrent_claims_exhaustion_and_same_name_new_uid() {
    let mock = Mock::default();
    let slots = slots();
    let a = context("uid-a", &slots);
    let b = context("uid-b", &slots);
    let (first, second) = tokio::join!(
        allocate(mock.client(), &a, None),
        allocate(mock.client(), &b, None)
    );
    assert!(first.is_ok());
    assert!(matches!(second, Err(AllocationError::NameHeld)));
    let mut c = context("uid-c", &slots);
    c.tenant_name = "tenant-c";
    assert_eq!(
        allocate(mock.client(), &c, None).await.unwrap().slot,
        slots[1]
    );
    let mut d = context("uid-d", &slots);
    d.tenant_name = "tenant-d";
    assert!(matches!(
        allocate(mock.client(), &d, None).await,
        Err(AllocationError::Exhausted)
    ));
}

#[tokio::test]
async fn release_preconditions_crash_windows_and_successor_reuse() {
    let mock = Mock::default();
    let slots = slots();
    let context = context("uid-a", &slots);
    let old = owned(&context, &slots[0], "old-lease", "14");
    mock.insert(old.clone());
    let bound = decide_claim(&context, std::slice::from_ref(&old), None).unwrap();
    let ClaimDecision::Existing(bound) = bound else {
        panic!("missing claim")
    };
    assert_eq!(
        release(mock.client(), &context, Some(&bound.status()), false)
            .await
            .unwrap(),
        ReleaseDecision::Pending
    );
    assert!(
        !mock
            .requests()
            .iter()
            .any(|(method, _, _)| method == "DELETE")
    );
    assert_eq!(
        release(mock.client(), &context, Some(&bound.status()), true)
            .await
            .unwrap(),
        ReleaseDecision::Pending
    );
    let delete = mock
        .requests()
        .into_iter()
        .find(|(method, _, _)| method == "DELETE")
        .unwrap();
    assert_eq!(
        delete.2["preconditions"],
        json!({"uid":"old-lease","resourceVersion":"14"})
    );
    assert_eq!(
        release(mock.client(), &context, Some(&bound.status()), true)
            .await
            .unwrap(),
        ReleaseDecision::Complete
    );
    let successor = ClaimContext {
        tenant_uid: "new-uid",
        ..context
    };
    let next = allocate(mock.client(), &successor, None).await.unwrap();
    assert_eq!(next.slot, slots[0]);
    assert!(matches!(
        release(mock.client(), &context, Some(&bound.status()), false).await,
        Err(AllocationError::NameHeld)
    ));
    assert_eq!(
        release(mock.client(), &context, Some(&bound.status()), true)
            .await
            .unwrap(),
        ReleaseDecision::Complete
    );
    assert_eq!(
        mock.lease(&lease_name("slot-a")).metadata.uid.as_deref(),
        Some(next.lease_uid.as_str())
    );
    assert_eq!(
        mock.requests()
            .iter()
            .filter(|(method, _, _)| method == "DELETE")
            .count(),
        1
    );
}

#[tokio::test]
async fn changed_bound_claim_and_delete_race_never_mutate_successor() {
    let mock = Mock::default();
    let slots = slots();
    let context = context("uid-a", &slots);
    let old = owned(&context, &slots[0], "old-lease", "14");
    let bound = match decide_claim(&context, std::slice::from_ref(&old), None).unwrap() {
        ClaimDecision::Existing(claim) => claim.status(),
        _ => unreachable!(),
    };
    mock.insert(old.clone());
    let mut changed = old.clone();
    changed
        .metadata
        .annotations
        .as_mut()
        .unwrap()
        .insert("tenancy.cnpg-vcluster.io/endpoint".into(), "changed".into());
    mock.replace_on_get(changed);
    assert!(matches!(
        release(mock.client(), &context, Some(&bound), true).await,
        Err(AllocationError::Claim(_))
    ));
    assert!(
        !mock
            .requests()
            .iter()
            .any(|(method, _, _)| method == "DELETE")
    );

    mock.insert(old.clone());
    let mut successor = old;
    successor.metadata.uid = Some("successor".into());
    successor.metadata.resource_version = Some("15".into());
    mock.replace_on_delete(successor);
    assert!(matches!(
        release(mock.client(), &context, Some(&bound), true).await,
        Err(AllocationError::Api(_))
    ));
    assert_eq!(
        mock.lease(&lease_name("slot-a")).metadata.uid.as_deref(),
        Some("successor")
    );
}
