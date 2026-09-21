package controller

import (
	"context"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestBootstrapRBACConvergesPreexistingRoleBinding(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := rbacv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	existing := &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "kubeadm:kubelet-config", Namespace: "kube-system"},
		RoleRef: rbacv1.RoleRef{
			APIGroup: rbacv1.GroupName,
			Kind:     "Role",
			Name:     "kubeadm:kubelet-config",
		},
		Subjects: []rbacv1.Subject{{APIGroup: rbacv1.GroupName, Kind: "Group", Name: "foreign"}},
	}
	tenantClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existing).Build()
	if err := applyBootstrapRBAC(context.Background(), tenantClient); err != nil {
		t.Fatal(err)
	}
	var updated rbacv1.RoleBinding
	if err := tenantClient.Get(context.Background(), client.ObjectKey{Namespace: "kube-system", Name: existing.Name}, &updated); err != nil {
		t.Fatal(err)
	}
	if len(updated.Subjects) != 2 ||
		updated.Subjects[0].Name != "system:bootstrappers:kubeadm:default-node-token" ||
		updated.Subjects[1].Name != "system:nodes" {
		t.Fatalf("bootstrap RoleBinding did not converge: %#v", updated.Subjects)
	}
}
