//! Go parity: ownership.go, postcni_test.go topology chains,
//! tenantresources_test.go marker refusal, and finalize_simplified_test.go
//! replacement/dangling-owner/kubeconfig refusal cases.

use k8s_openapi::{
    ByteString,
    api::core::v1::Secret,
    apimachinery::pkg::apis::meta::v1::{ObjectMeta, OwnerReference},
};
use kube::core::DynamicObject;
use serde_json::json;
use tenant_controller::{
    api::{Tenant, TenantSpec, TenantStatus},
    ownership::*,
};

fn identity() -> Identity<'static> {
    Identity {
        tenant_name: "tenant-a",
        tenant_uid: "tenant-uid",
        spec_hash: "spec-hash",
        foundation_hash: "foundation-hash",
        ownership_label: "example.io/owned",
        lab_prefix: "example",
    }
}

fn object(kind: &str, name: &str, uid: &str) -> DynamicObject {
    let api = match kind {
        "Namespace" => "v1",
        "DevCluster" | "DevMachineTemplate" | "DevMachine" => {
            "infrastructure.cluster.x-k8s.io/v1beta2"
        }
        "KamajiControlPlane" => CONTROL_PLANE_API_VERSION,
        "KubeadmConfigTemplate" | "KubeadmConfig" => "bootstrap.cluster.x-k8s.io/v1beta2",
        _ => CLUSTER_API_VERSION,
    };
    serde_json::from_value(json!({
        "apiVersion":api,"kind":kind,
        "metadata":{"name":name,"namespace":"tenant-a","uid":uid},
        "spec":{}
    }))
    .unwrap()
}

fn owner(object: &DynamicObject) -> OwnerReference {
    OwnerReference {
        api_version: object.types.as_ref().unwrap().api_version.clone(),
        kind: object.types.as_ref().unwrap().kind.clone(),
        name: object.metadata.name.clone().unwrap(),
        uid: object.metadata.uid.clone().unwrap(),
        ..Default::default()
    }
}

fn tenant() -> Tenant {
    let mut tenant = Tenant::new(
        "tenant-a",
        TenantSpec {
            kubernetes_version: "1.36.4".into(),
            workers: 1,
            databases: 1,
        },
    );
    tenant.metadata.uid = Some("tenant-uid".into());
    tenant.status = Some(TenantStatus {
        cluster_uid: Some("cluster-uid".into()),
        ..Default::default()
    });
    tenant
}

fn marked_meta() -> ObjectMeta {
    ObjectMeta {
        name: Some("tenant-a".into()),
        uid: Some("cluster-uid".into()),
        labels: Some(identity().labels()),
        annotations: Some(identity().annotations("cluster")),
        ..Default::default()
    }
}

#[test]
fn root_markers_require_uid_and_every_identity_field() {
    let metadata = marked_meta();
    assert_eq!(
        validate_root_ownership(&metadata, identity(), "cluster"),
        Ok(())
    );
    for key in [
        TENANT_ANNOTATION,
        TENANT_UID_ANNOTATION,
        SPEC_HASH_ANNOTATION,
        FOUNDATION_ANNOTATION,
        RESOURCE_ANNOTATION,
    ] {
        for removed in [true, false] {
            let mut changed = metadata.clone();
            if removed {
                changed.annotations.as_mut().unwrap().remove(key);
            } else {
                changed
                    .annotations
                    .as_mut()
                    .unwrap()
                    .insert(key.into(), "foreign".into());
            }
            assert!(
                validate_root_ownership(&changed, identity(), "cluster").is_err(),
                "{key}"
            );
            assert!(
                validate_tenant_object_ownership(&changed, identity(), "cluster").is_err(),
                "{key}"
            );
        }
    }
    for uid in [None, Some("".into())] {
        let mut changed = metadata.clone();
        changed.uid = uid;
        assert!(matches!(
            validate_root_ownership(&changed, identity(), "cluster"),
            Err(OwnershipError::MissingUid(_))
        ));
    }
    for labels in [
        None,
        Some([("example.io/owned".into(), "foreign".into())].into()),
    ] {
        let mut changed = metadata.clone();
        changed.labels = labels;
        assert!(validate_root_ownership(&changed, identity(), "cluster").is_err());
        assert_eq!(
            validate_tenant_object_ownership(&changed, identity(), "cluster"),
            Ok(())
        );
    }
}

#[test]
fn root_rejects_tenant_owner_but_provider_owners_are_separately_checked() {
    let mut metadata = marked_meta();
    metadata.owner_references = Some(vec![OwnerReference {
        api_version: "tenancy.cnpg-vcluster.io/v1alpha2".into(),
        kind: "Tenant".into(),
        name: "tenant-a".into(),
        uid: "tenant-uid".into(),
        ..Default::default()
    }]);
    assert!(matches!(
        validate_root_ownership(&metadata, identity(), "cluster"),
        Err(OwnershipError::TenantOwner(_))
    ));
    metadata.owner_references = Some(vec![owner(&object("Cluster", "tenant-a", "cluster-uid"))]);
    assert_eq!(
        validate_root_ownership(&metadata, identity(), "cluster"),
        Ok(())
    );
}

#[test]
fn static_tenant_annotations_do_not_require_content_or_management_labels() {
    let mut object = object("ConfigMap", "static", "static-uid");
    object.metadata.annotations = Some(identity().annotations("network-endpoint"));
    object.data = json!({"data":{"drift":"is-not-generically-repaired"}});
    assert_eq!(
        validate_tenant_object_ownership(&object.metadata, identity(), "network-endpoint"),
        Ok(())
    );
    object.metadata.annotations.as_mut().unwrap().insert(
        TENANT_UID_ANNOTATION.into(),
        "same-name-new-tenant-uid".into(),
    );
    assert!(
        validate_tenant_object_ownership(&object.metadata, identity(), "network-endpoint").is_err()
    );
}

#[test]
fn recorded_cluster_identity_rejects_replacement_but_allows_initial_binding() {
    let mut tenant = tenant();
    assert_eq!(validate_cluster_uid(&tenant, &marked_meta()), Ok(()));
    let mut changed = marked_meta();
    changed.uid = Some("replacement-cluster".into());
    assert!(matches!(
        validate_cluster_uid(&tenant, &changed),
        Err(OwnershipError::ClusterUid { .. })
    ));
    changed.uid = None;
    assert!(validate_cluster_uid(&tenant, &changed).is_err());
    tenant.status.as_mut().unwrap().cluster_uid = None;
    assert_eq!(validate_cluster_uid(&tenant, &changed), Ok(()));
}

fn topology() -> (DynamicObject, Vec<DynamicObject>) {
    let deployment = object("MachineDeployment", "tenant-a-worker", "deployment-uid");
    let mut set = object("MachineSet", "tenant-a-set", "set-uid");
    set.metadata.owner_references = Some(vec![owner(&deployment)]);
    let mut machine = object("Machine", "worker-a", "machine-uid");
    machine.metadata.owner_references = Some(vec![owner(&set)]);
    let mut dev = object("DevMachine", "worker-a", "devmachine-uid");
    dev.metadata.owner_references = Some(vec![owner(&machine)]);
    (dev, vec![deployment, set, machine])
}

#[test]
fn provider_owner_chain_walks_every_exact_uid_without_label_filtering() {
    let (dev, inventory) = topology();
    assert_eq!(
        validate_owner_chain(&dev, "deployment-uid", &inventory),
        Ok(())
    );
    assert_eq!(
        validate_owner_chain(&dev, "machine-uid", &inventory),
        Ok(())
    );
    assert_eq!(
        validate_owner_chain(&inventory[0], "deployment-uid", &[]),
        Ok(())
    );
    assert!(validate_owner_chain(&dev, "", &inventory).is_err());
    let mut foreign = dev.clone();
    foreign.metadata.name = Some("extra-foreign".into());
    foreign.metadata.owner_references.as_mut().unwrap()[0].uid = "foreign-uid".into();
    // All inventory entries must pass before count-based pending/readiness decisions.
    assert!(
        [dev, foreign]
            .iter()
            .try_for_each(|object| validate_owner_chain(object, "deployment-uid", &inventory))
            .is_err()
    );
}

#[test]
fn owner_chain_rejects_missing_multiple_empty_foreign_and_cross_namespace_owners() {
    let (dev, inventory) = topology();
    let reference = dev.metadata.owner_references.as_ref().unwrap()[0].clone();
    for references in [
        None,
        Some(vec![]),
        Some(vec![reference.clone(), reference.clone()]),
    ] {
        let mut changed = dev.clone();
        changed.metadata.owner_references = references;
        assert!(matches!(
            validate_owner_chain(&changed, "deployment-uid", &inventory),
            Err(OwnershipError::OwnerChain(_))
        ));
    }
    for field in ["uid", "name", "kind", "apiVersion"] {
        let mut changed = dev.clone();
        let reference = &mut changed.metadata.owner_references.as_mut().unwrap()[0];
        match field {
            "uid" => reference.uid = "foreign".into(),
            "name" => reference.name = "foreign".into(),
            "kind" => reference.kind = "Foreign".into(),
            _ => reference.api_version = "foreign.example/v1".into(),
        }
        assert!(
            validate_owner_chain(&changed, "deployment-uid", &inventory).is_err(),
            "{field}"
        );
    }
    let mut changed = dev.clone();
    changed.metadata.owner_references.as_mut().unwrap()[0]
        .uid
        .clear();
    assert!(validate_owner_chain(&changed, "deployment-uid", &inventory).is_err());
    changed = dev.clone();
    changed.metadata.uid = None;
    assert!(validate_owner_chain(&changed, "deployment-uid", &inventory).is_err());
    changed = dev.clone();
    changed.metadata.namespace = Some("foreign".into());
    assert!(matches!(
        validate_owner_chain(&changed, "deployment-uid", &inventory),
        Err(OwnershipError::MissingOwner(_))
    ));
    for api in ["", "group/version/extra", "/v1", "group/"] {
        changed = dev.clone();
        changed.metadata.owner_references.as_mut().unwrap()[0].api_version = api.into();
        assert_eq!(
            validate_owner_chain(&changed, "deployment-uid", &inventory),
            Err(OwnershipError::ApiVersion)
        );
    }
    assert!(matches!(
        validate_owner_chain(&dev, "deployment-uid", &[]),
        Err(OwnershipError::MissingOwner(_))
    ));
}

#[test]
fn owner_chain_rejects_mid_chain_replacement_cycles_and_ambiguous_inventory() {
    let (dev, mut inventory) = topology();
    inventory[1].metadata.uid = Some("replacement-set".into());
    assert_eq!(
        validate_owner_chain(&dev, "deployment-uid", &inventory),
        Err(OwnershipError::OwnerUid)
    );
    let (dev, mut inventory) = topology();
    inventory[1].metadata.owner_references = Some(vec![owner(&inventory[2])]);
    assert_eq!(
        validate_owner_chain(&dev, "deployment-uid", &inventory),
        Err(OwnershipError::Cycle)
    );
    let (dev, mut inventory) = topology();
    inventory.push(inventory[2].clone());
    assert!(matches!(
        validate_owner_chain(&dev, "deployment-uid", &inventory),
        Err(OwnershipError::DuplicateOwner(_))
    ));
}

#[test]
fn management_roots_never_have_provider_owners() {
    for kind in ["Namespace", "Cluster"] {
        let mut root = object(kind, "tenant-a", "root");
        assert_eq!(
            validate_provider_owner(&root, "tenant-a", true, &[]),
            Ok(())
        );
        root.metadata.owner_references =
            Some(vec![owner(&object("Cluster", "tenant-a", "cluster-uid"))]);
        assert!(validate_provider_owner(&root, "tenant-a", false, &[]).is_err());
        assert!(validate_provider_owner_for_deletion(&root, &tenant(), &[]).is_err());
    }
}

#[test]
fn management_provider_owners_are_optional_until_required_and_exact_afterward() {
    let cluster = object("Cluster", "tenant-a", "cluster-uid");
    let deployment = object("MachineDeployment", "tenant-a-worker", "deployment-uid");
    let inventory = vec![cluster.clone(), deployment.clone()];
    for kind in [
        "DevCluster",
        "KamajiControlPlane",
        "MachineDeployment",
        "KubeadmConfigTemplate",
        "DevMachineTemplate",
    ] {
        let mut child = object(kind, "child", "child-uid");
        assert_eq!(
            validate_provider_owner(&child, "tenant-a", false, &inventory),
            Ok(())
        );
        assert_eq!(
            validate_provider_owner(&child, "tenant-a", true, &inventory),
            Err(OwnershipError::ProviderOwnerPending)
        );
        child.metadata.owner_references = Some(vec![owner(&cluster)]);
        assert_eq!(
            validate_provider_owner(&child, "tenant-a", true, &inventory),
            Ok(())
        );
        child.metadata.owner_references.as_mut().unwrap()[0].controller = Some(true);
        child.metadata.owner_references.as_mut().unwrap()[0].block_owner_deletion = Some(true);
        assert_eq!(
            validate_provider_owner(&child, "tenant-a", true, &inventory),
            Ok(())
        );
        assert!(validate_provider_owner(&child, "tenant-a", true, &[]).is_err());
        child.metadata.owner_references = Some(vec![owner(&deployment)]);
        assert_eq!(
            validate_provider_owner(&child, "tenant-a", true, &inventory).is_ok(),
            kind.ends_with("Template")
        );
        for field in ["uid", "name", "kind", "apiVersion"] {
            let mut reference = owner(&cluster);
            match field {
                "uid" => reference.uid = "foreign".into(),
                "name" => reference.name = "foreign".into(),
                "kind" => reference.kind = "Foreign".into(),
                _ => reference.api_version = "foreign/v1".into(),
            }
            child.metadata.owner_references = Some(vec![reference]);
            assert!(
                validate_provider_owner(&child, "tenant-a", false, &inventory).is_err(),
                "{kind}/{field}"
            );
        }
        child.metadata.owner_references = Some(vec![owner(&cluster), owner(&deployment)]);
        assert!(validate_provider_owner(&child, "tenant-a", false, &inventory).is_err());
    }
}

#[test]
fn deletion_accepts_only_recorded_dangling_cluster_or_live_template_deployment() {
    let tenant = tenant();
    let cluster = object("Cluster", "tenant-a", "cluster-uid");
    let deployment = object("MachineDeployment", "tenant-a-worker", "deployment-uid");
    for kind in [
        "DevCluster",
        "KamajiControlPlane",
        "MachineDeployment",
        "KubeadmConfigTemplate",
        "DevMachineTemplate",
    ] {
        let mut child = object(kind, "child", "child-uid");
        assert_eq!(
            validate_provider_owner_for_deletion(&child, &tenant, &[]),
            Ok(())
        );
        child.metadata.owner_references = Some(vec![owner(&cluster)]);
        assert_eq!(
            validate_provider_owner_for_deletion(&child, &tenant, &[]),
            Ok(())
        );
        child.metadata.owner_references.as_mut().unwrap()[0].uid = "foreign-cluster".into();
        assert!(validate_provider_owner_for_deletion(&child, &tenant, &[]).is_err());
        child.metadata.owner_references = Some(vec![owner(&deployment)]);
        assert_eq!(
            validate_provider_owner_for_deletion(
                &child,
                &tenant,
                std::slice::from_ref(&deployment)
            )
            .is_ok(),
            kind.ends_with("Template")
        );
        assert!(validate_provider_owner_for_deletion(&child, &tenant, &[]).is_err());
        child.metadata.owner_references = Some(vec![owner(&cluster), owner(&deployment)]);
        assert!(
            validate_provider_owner_for_deletion(
                &child,
                &tenant,
                std::slice::from_ref(&deployment)
            )
            .is_err()
        );
    }
    let mut child = object("DevCluster", "tenant-a", "child");
    child.metadata.owner_references = Some(vec![owner(&cluster)]);
    let mut unbound = tenant;
    unbound.status.as_mut().unwrap().cluster_uid = None;
    assert!(validate_provider_owner_for_deletion(&child, &unbound, &[]).is_err());
}

fn secret(control_plane: &DynamicObject) -> Secret {
    Secret {
        metadata: ObjectMeta {
            name: Some("tenant-a-kubeconfig".into()),
            namespace: Some("tenant-a".into()),
            owner_references: Some(vec![owner(control_plane)]),
            ..Default::default()
        },
        type_: Some("cluster.x-k8s.io/secret".into()),
        data: Some([("value".into(), ByteString(b"kubeconfig".to_vec()))].into()),
        ..Default::default()
    }
}

#[test]
fn kubeconfig_creation_validates_content_type_and_current_control_plane_uid() {
    let cp = object("KamajiControlPlane", "tenant-a", "cp-uid");
    let valid = secret(&cp);
    assert_eq!(validate_kubeconfig_secret(&valid, Some(&cp)), Ok(()));
    assert_eq!(
        validate_kubeconfig_secret(&valid, None),
        Err(OwnershipError::SecretOwner)
    );
    let mut changed = valid.clone();
    changed.type_ = Some("Opaque".into());
    assert_eq!(
        validate_kubeconfig_secret(&changed, Some(&cp)),
        Err(OwnershipError::SecretContract)
    );
    changed = valid.clone();
    changed.data = None;
    assert_eq!(
        validate_kubeconfig_secret(&changed, Some(&cp)),
        Err(OwnershipError::SecretContract)
    );
    changed = valid.clone();
    changed
        .data
        .as_mut()
        .unwrap()
        .get_mut("value")
        .unwrap()
        .0
        .clear();
    assert_eq!(
        validate_kubeconfig_secret(&changed, Some(&cp)),
        Err(OwnershipError::SecretContract)
    );
    changed = valid.clone();
    changed.metadata.owner_references.as_mut().unwrap()[0].uid = "foreign".into();
    assert_eq!(
        validate_kubeconfig_secret(&changed, Some(&cp)),
        Err(OwnershipError::SecretOwner)
    );
    changed = valid;
    changed
        .metadata
        .owner_references
        .as_mut()
        .unwrap()
        .push(owner(&object("Cluster", "tenant-a", "cluster-uid")));
    assert_eq!(validate_kubeconfig_secret(&changed, Some(&cp)), Ok(()));
    assert!(!has_owner_uid(&[], ""));
}

#[test]
fn kubeconfig_deletion_requires_one_exact_control_plane_owner_not_secret_content() {
    let cp = object("KamajiControlPlane", "tenant-a", "cp-uid");
    let mut valid = secret(&cp);
    valid.type_ = None;
    valid.data = None;
    assert_eq!(
        validate_kubeconfig_secret_for_deletion(&valid, Some(&cp)),
        Ok(())
    );
    assert_eq!(
        validate_kubeconfig_secret_for_deletion(&valid, None),
        Err(OwnershipError::SecretOwner)
    );
    for field in ["uid", "name", "kind", "apiVersion"] {
        let mut changed = valid.clone();
        let reference = &mut changed.metadata.owner_references.as_mut().unwrap()[0];
        match field {
            "uid" => reference.uid = "foreign".into(),
            "name" => reference.name = "foreign".into(),
            "kind" => reference.kind = "Foreign".into(),
            _ => reference.api_version = "foreign/v1".into(),
        }
        assert_eq!(
            validate_kubeconfig_secret_for_deletion(&changed, Some(&cp)),
            Err(OwnershipError::SecretOwner)
        );
    }
    let reference = owner(&cp);
    for references in [None, Some(vec![]), Some(vec![reference.clone(), reference])] {
        let mut changed = valid.clone();
        changed.metadata.owner_references = references;
        assert_eq!(
            validate_kubeconfig_secret_for_deletion(&changed, Some(&cp)),
            Err(OwnershipError::SecretOwner)
        );
    }
}
