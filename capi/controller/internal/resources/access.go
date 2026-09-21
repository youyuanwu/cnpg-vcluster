package resources

import (
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

func BootstrapRBAC() []runtime.Object {
	subjects := []rbacv1.Subject{
		{APIGroup: rbacv1.GroupName, Kind: "Group", Name: "system:bootstrappers:kubeadm:default-node-token"},
		{APIGroup: rbacv1.GroupName, Kind: "Group", Name: "system:nodes"},
	}
	result := make([]runtime.Object, 0, 4)
	for _, item := range []struct {
		name     string
		resource string
	}{
		{name: "kubeadm:nodes-kubeadm-config", resource: "kubeadm-config"},
		{name: "kubeadm:kubelet-config", resource: "kubelet-config"},
	} {
		result = append(result,
			&rbacv1.Role{
				TypeMeta:   metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "Role"},
				ObjectMeta: metav1.ObjectMeta{Name: item.name, Namespace: "kube-system"},
				Rules: []rbacv1.PolicyRule{{
					APIGroups:     []string{""},
					Resources:     []string{"configmaps"},
					ResourceNames: []string{item.resource},
					Verbs:         []string{"get"},
				}},
			},
			&rbacv1.RoleBinding{
				TypeMeta:   metav1.TypeMeta{APIVersion: rbacv1.SchemeGroupVersion.String(), Kind: "RoleBinding"},
				ObjectMeta: metav1.ObjectMeta{Name: item.name, Namespace: "kube-system"},
				RoleRef: rbacv1.RoleRef{
					APIGroup: rbacv1.GroupName,
					Kind:     "Role",
					Name:     item.name,
				},
				Subjects: subjects,
			},
		)
	}
	return result
}
