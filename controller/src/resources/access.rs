use k8s_openapi::{
    api::rbac::v1::{PolicyRule, Role, RoleBinding, RoleRef, Subject},
    apimachinery::pkg::apis::meta::v1::ObjectMeta,
};

#[derive(Clone, Debug)]
pub struct BootstrapRole {
    pub role: Role,
    pub binding: RoleBinding,
}

pub fn bootstrap_rbac() -> Vec<BootstrapRole> {
    let subjects = [
        "system:bootstrappers:kubeadm:default-node-token",
        "system:nodes",
    ]
    .map(|name| Subject {
        api_group: Some("rbac.authorization.k8s.io".into()),
        kind: "Group".into(),
        name: name.into(),
        ..Default::default()
    })
    .to_vec();
    [
        ("kubeadm:nodes-kubeadm-config", "kubeadm-config"),
        ("kubeadm:kubelet-config", "kubelet-config"),
    ]
    .into_iter()
    .map(|(name, resource)| {
        let metadata = ObjectMeta {
            name: Some(name.into()),
            namespace: Some("kube-system".into()),
            ..Default::default()
        };
        BootstrapRole {
            role: Role {
                metadata: metadata.clone(),
                rules: Some(vec![PolicyRule {
                    api_groups: Some(vec!["".into()]),
                    resources: Some(vec!["configmaps".into()]),
                    resource_names: Some(vec![resource.into()]),
                    verbs: vec!["get".into()],
                    ..Default::default()
                }]),
            },
            binding: RoleBinding {
                metadata,
                role_ref: RoleRef {
                    api_group: Some("rbac.authorization.k8s.io".into()),
                    kind: "Role".into(),
                    name: name.into(),
                },
                subjects: Some(subjects.clone()),
            },
        }
    })
    .collect()
}

pub fn bootstrap_subjects_match(actual: &[Subject], expected: &[Subject]) -> bool {
    let key = |subject: &Subject| {
        (
            subject.api_group.clone().unwrap_or_default(),
            subject.kind.clone(),
            subject.name.clone(),
            subject.namespace.clone().unwrap_or_default(),
        )
    };
    let mut actual: Vec<_> = actual.iter().map(key).collect();
    let mut expected: Vec<_> = expected.iter().map(key).collect();
    actual.sort();
    expected.sort();
    actual == expected
}
