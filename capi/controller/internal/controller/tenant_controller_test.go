package controller

import (
	"context"
	"fmt"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := tenancyv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return scheme
}

func TestValidTenantStaysMutationDisabledWithoutFinalizer(t *testing.T) {
	scheme := testScheme(t)
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", Generation: 1},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
	}

	client := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(tenant).WithObjects(tenant).Build()
	reconciler := &TenantReconciler{Client: client, SupportedVersion: "1.36.4"}
	if _, err := reconciler.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Name: tenant.Name}}); err != nil {
		t.Fatal(err)
	}
	var updated tenancyv1alpha1.Tenant
	if err := client.Get(context.Background(), types.NamespacedName{Name: tenant.Name}, &updated); err != nil {
		t.Fatal(err)
	}
	if len(updated.Finalizers) != 0 {
		t.Fatalf("validation-only reconcile added finalizer: %v", updated.Finalizers)
	}
	if updated.Status.Phase != tenancyv1alpha1.PhaseProgressing {
		t.Fatalf("unexpected phase %q", updated.Status.Phase)
	}
}

func TestReconcileMetadataPreservesDegradedReadinessPhase(t *testing.T) {
	tenant := validTenant("tenant-a")
	tenant.Generation = 4
	status := tenancyv1alpha1.TenantStatus{
		Phase: tenancyv1alpha1.PhaseDegraded,
	}
	initializeReconcileStatus(&status, tenant)
	if status.Phase != tenancyv1alpha1.PhaseDegraded {
		t.Fatalf("readiness degradation was overwritten: %q", status.Phase)
	}
	if status.ObservedGeneration != tenant.Generation {
		t.Fatalf("observed generation was not refreshed: %d", status.ObservedGeneration)
	}
}

func TestInvalidTenantFailsWithoutFinalizer(t *testing.T) {
	scheme := testScheme(t)
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", Generation: 1},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           4,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
	}

	client := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(tenant).WithObjects(tenant).Build()
	reconciler := &TenantReconciler{Client: client, SupportedVersion: "1.36.4"}
	if _, err := reconciler.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Name: tenant.Name}}); err != nil {
		t.Fatal(err)
	}
	var updated tenancyv1alpha1.Tenant
	if err := client.Get(context.Background(), types.NamespacedName{Name: tenant.Name}, &updated); err != nil {
		t.Fatal(err)
	}
	if updated.Status.Phase != tenancyv1alpha1.PhaseFailed || len(updated.Finalizers) != 0 {
		t.Fatalf("unexpected invalid Tenant state: %#v", updated)
	}
}

func TestDeletionFailureReasonsExposeRecoveryClass(t *testing.T) {
	phase, reason := classifyDeletionFailure(fmt.Errorf("Docker inspection unavailable"))
	if phase != tenancyv1alpha1.PhaseDeleting || reason != "DeletionBlocked" {
		t.Fatalf("unexpected deletion classification: %s %s", phase, reason)
	}

	phase, reason = classifyDeletionFailure(fmt.Errorf("ownership mismatch"))
	if phase != tenancyv1alpha1.PhaseOwnershipInvalid || reason != "OwnershipInvalid" {
		t.Fatalf("unexpected ownership classification: %s %s", phase, reason)
	}
	phase, reason = classifyFoundationFailure(true, errFoundationMismatch)
	if phase != tenancyv1alpha1.PhaseDeleting || reason != "FoundationMismatch" {
		t.Fatalf("unexpected foundation classification: %s %s", phase, reason)
	}
}

func TestImmutableDriftPublishesBoundedDegradedCondition(t *testing.T) {
	tenant := validTenant("tenant-a")
	tenant.Generation = 3
	client := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant).
		Build()
	reconciler := &TenantReconciler{Client: client, APIReader: client}
	if err := reconciler.degraded(context.Background(), tenant, "ImmutableDrift", errImmutableDrift); err != nil {
		t.Fatal(err)
	}
	var updated tenancyv1alpha1.Tenant
	if err := client.Get(context.Background(), types.NamespacedName{Name: tenant.Name}, &updated); err != nil {
		t.Fatal(err)
	}
	if updated.Status.Phase != tenancyv1alpha1.PhaseDegraded {
		t.Fatalf("unexpected phase: %s", updated.Status.Phase)
	}
	condition := meta.FindStatusCondition(updated.Status.Conditions, "Ready")
	if condition == nil || condition.Reason != "ImmutableDrift" ||
		condition.ObservedGeneration != tenant.Generation {
		t.Fatalf("unexpected immutable drift condition: %#v", condition)
	}
}

func TestDeletionFailurePublishesRecoveryCondition(t *testing.T) {
	tenant := validTenant("tenant-a")
	tenant.Generation = 2
	client := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant).
		Build()
	reconciler := &TenantReconciler{Client: client, APIReader: client}
	if err := reconciler.failure(
		context.Background(),
		tenant,
		"spec-hash",
		tenancyv1alpha1.PhaseDeleting,
		"DeletionBlocked",
		fmt.Errorf("Docker inspection unavailable"),
	); err == nil {
		t.Fatal("failure helper unexpectedly returned nil")
	}
	var updated tenancyv1alpha1.Tenant
	if err := client.Get(context.Background(), types.NamespacedName{Name: tenant.Name}, &updated); err != nil {
		t.Fatal(err)
	}
	condition := meta.FindStatusCondition(updated.Status.Conditions, "Ready")
	if condition == nil || condition.Reason != "DeletionBlocked" ||
		condition.ObservedGeneration != tenant.Generation {
		t.Fatalf("unexpected deletion condition: %#v", condition)
	}
}
