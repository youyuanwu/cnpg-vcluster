package controller

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func TestDeletionReservationBecomesRenewableLeaseAndReleasesLast(t *testing.T) {
	now := time.Now().UTC()
	tenant := validTenant("tenant-a")
	tenant.UID = "tenant-uid"
	reservation, err := json.Marshal(DeletionReservation{
		Schema: 1, TenantName: tenant.Name, TenantUID: string(tenant.UID),
		Requester: "test-user", Nonce: "nonce", ExpiresAt: float64(now.Add(time.Hour).Unix()),
	})
	if err != nil {
		t.Fatal(err)
	}

	configMap := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name: DeletionReservationName, Namespace: defaultFoundationNamespace, UID: "reservation-uid",
		},
		Data: map[string]string{deletionReservationKey: string(reservation)},
	}
	scheme := testScheme(t)
	kubernetes := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(tenant).
		WithObjects(tenant, configMap).
		Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}

	locked, err := reconciler.ensureDeletionLock(context.Background(), tenant)
	if err != nil || locked {
		t.Fatalf("reservation status persistence failed: locked=%v err=%v", locked, err)
	}
	var current tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &current); err != nil {
		t.Fatal(err)
	}
	if current.Status.Teardown == nil || current.Status.Teardown.Reservation != "nonce" {
		t.Fatalf("reservation nonce was not persisted: %#v", current.Status.Teardown)
	}
	locked, err = reconciler.ensureDeletionLock(context.Background(), &current)
	if err != nil || locked {
		t.Fatalf("Lease creation failed: locked=%v err=%v", locked, err)
	}
	locked, err = reconciler.ensureDeletionLock(context.Background(), &current)
	if err != nil || !locked {
		t.Fatalf("Lease renewal failed: locked=%v err=%v", locked, err)
	}

	for attempt := 0; attempt < 3; attempt++ {
		released, err := reconciler.releaseDeletionLock(context.Background(), &current)
		if err != nil {
			t.Fatal(err)
		}
		if attempt < 2 && released {
			t.Fatal("deletion control state released before both objects were absent")
		}
		if attempt == 2 && !released {
			t.Fatal("deletion control state remained after exact release")
		}
	}
}

func TestAdmissionLeaseSerializesOverlappingReservations(t *testing.T) {
	kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	now := time.Unix(2_000_000_000, 0)
	first := DeletionReservation{
		Schema: 1, TenantName: "tenant-a", TenantUID: "uid-a",
		Requester: "user-a", Nonce: "nonce-a", ExpiresAt: float64(now.Add(time.Minute).Unix()),
	}
	second := DeletionReservation{
		Schema: 1, TenantName: "tenant-b", TenantUID: "uid-b",
		Requester: "user-b", Nonce: "nonce-b", ExpiresAt: float64(now.Add(time.Minute).Unix()),
	}
	if err := AcquireDeletionAdmissionLease(context.Background(), kubernetes, defaultFoundationNamespace, first, "request-a", now); err != nil {
		t.Fatal(err)
	}
	if err := AcquireDeletionAdmissionLease(context.Background(), kubernetes, defaultFoundationNamespace, second, "request-b", now); err == nil {
		t.Fatal("overlapping reservation acquired the admission Lease")
	}
	if err := AcquireDeletionAdmissionLease(context.Background(), kubernetes, defaultFoundationNamespace, second, "request-b", now.Add(61*time.Second)); err != nil {
		t.Fatalf("expired admission Lease was not recoverable: %v", err)
	}
}
