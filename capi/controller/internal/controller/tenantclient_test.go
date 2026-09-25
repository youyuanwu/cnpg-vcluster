package controller

import (
	"context"
	"errors"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestBootstrapRBACRefusesPreexistingRoleBindingDrift(t *testing.T) {
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
	if err := ensureBootstrapRBAC(context.Background(), tenantClient); !errors.Is(err, errStaticResourceDrift) {
		t.Fatalf("bootstrap RoleBinding drift was not refused: %v", err)
	}
	var preserved rbacv1.RoleBinding
	if err := tenantClient.Get(context.Background(), client.ObjectKey{Namespace: "kube-system", Name: existing.Name}, &preserved); err != nil {
		t.Fatal(err)
	}
	if len(preserved.Subjects) != 1 || preserved.Subjects[0].Name != "foreign" {
		t.Fatalf("bootstrap RoleBinding drift was mutated: %#v", preserved.Subjects)
	}
}

func TestBootstrapSubjectsMatchRegardlessOfOrder(t *testing.T) {
	left := []rbacv1.Subject{
		{APIGroup: rbacv1.GroupName, Kind: "Group", Name: "system:nodes"},
		{APIGroup: rbacv1.GroupName, Kind: "Group", Name: "system:bootstrappers:kubeadm:default-node-token"},
	}
	right := []rbacv1.Subject{left[1], left[0]}
	if !bootstrapSubjectsEqual(left, right) {
		t.Fatal("equivalent bootstrap subjects were order-sensitive")
	}
	right[0].Name = "foreign"
	if bootstrapSubjectsEqual(left, right) {
		t.Fatal("foreign bootstrap subject was accepted")
	}
}
