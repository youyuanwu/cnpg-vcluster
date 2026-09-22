package webhook

import (
	"bytes"
	"context"
	"encoding/json"
	"testing"
	"time"

	admissionv1 "k8s.io/api/admission/v1"
	authenticationv1 "k8s.io/api/authentication/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	tenantcontroller "github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/controller"
)

func tenantJSON(version string, extra string) []byte {
	document := `{"apiVersion":"tenancy.cnpg-vcluster.io/v1alpha1","kind":"Tenant","metadata":{"name":"tenant-a"},"spec":{"kubernetesVersion":"` + version + `","workers":1,"databaseCount":1,"podCIDR":"10.20.0.0/16","serviceCIDR":"10.21.0.0/16"` + extra + `}}`
	return []byte(document)
}

func TestDeleteRequiresExactLiveReservation(t *testing.T) {
	now := time.Unix(2_000_000_000, 0).UTC()
	document := tenantJSON("1.36.4", "")
	document = bytes.Replace(document, []byte(`"name":"tenant-a"`), []byte(`"name":"tenant-a","uid":"tenant-uid"`), 1)
	reservation, err := json.Marshal(tenantcontroller.DeletionReservation{
		Schema: 1, TenantName: "tenant-a", TenantUID: "tenant-uid",
		Requester: "test-user", Nonce: "nonce", ExpiresAt: float64(now.Add(time.Minute).Unix()),
	})
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := coordinationv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := tenancyv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	reservationConfigMap := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: tenantcontroller.DeletionReservationName, Namespace: "tenant-system"},
		Data:       map[string]string{"reservation.json": string(reservation)},
	}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(reservationConfigMap).Build()
	handler := &TenantValidator{
		SupportedVersion: "1.36.4",
		Reader:           kubernetes,
		Client:           kubernetes,
		Namespace:        "tenant-system",
		Now:              func() time.Time { return now },
	}
	request := admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
		Operation: admissionv1.Delete,
		OldObject: runtimeRaw(document),
		UserInfo:  authenticationv1.UserInfo{Username: "test-user"},
	}}
	if response := handler.Handle(context.Background(), request); !response.Allowed {
		t.Fatalf("reserved delete was denied: %s", response.Result.Message)
	}
	request.UserInfo.Username = "other-user"
	if response := handler.Handle(context.Background(), request); response.Allowed {
		t.Fatal("requester mismatch was allowed")
	}
	deletingAt := metav1.Now()
	active := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{
			Name: "tenant-b", UID: "tenant-b-uid",
			DeletionTimestamp: &deletingAt, Finalizers: []string{"test"},
		},
	}
	handler.Reader = fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(reservationConfigMap.DeepCopy(), active).
		Build()
	handler.Client = handler.Reader.(client.Client)
	request.UserInfo.Username = "test-user"
	if response := handler.Handle(context.Background(), request); response.Allowed {
		t.Fatal("second overlapping deletion was allowed")
	}
	handler.Reader = kubernetes
	handler.Client = kubernetes
	handler.Now = func() time.Time { return now.Add(2 * time.Minute) }
	if response := handler.Handle(context.Background(), request); response.Allowed {
		t.Fatal("expired reservation was allowed")
	}
}

func TestDeleteWithoutReservationIsDenied(t *testing.T) {
	document := tenantJSON("1.36.4", "")
	document = bytes.Replace(document, []byte(`"name":"tenant-a"`), []byte(`"name":"tenant-a","uid":"tenant-uid"`), 1)
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := coordinationv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := tenancyv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	handler := &TenantValidator{
		SupportedVersion: "1.36.4",
		Reader:           fake.NewClientBuilder().WithScheme(scheme).Build(),
	}
	response := handler.Handle(context.Background(), admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
		Operation: admissionv1.Delete,
		OldObject: runtimeRaw(document),
		UserInfo:  authenticationv1.UserInfo{Username: "test-user"},
	}})
	if response.Allowed {
		t.Fatal("unreserved delete was allowed")
	}
}

func runtimeRaw(data []byte) runtime.RawExtension {
	return runtime.RawExtension{Raw: data}
}

func TestCreateRejectsUnknownField(t *testing.T) {
	handler := &TenantValidator{SupportedVersion: "1.36.4"}
	response := handler.Handle(context.Background(), admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
		Operation: admissionv1.Create,
		Object:    runtimeRaw(tenantJSON("1.36.4", `,"unknown":true`)),
	}})
	if response.Allowed {
		t.Fatal("unknown field was allowed")
	}
}

func TestCreateRejectsBooleanAndMissingFields(t *testing.T) {
	handler := &TenantValidator{SupportedVersion: "1.36.4"}
	for name, document := range map[string][]byte{
		"boolean": bytes.Replace(
			tenantJSON("1.36.4", ""),
			[]byte(`"workers":1`),
			[]byte(`"workers":true`),
			1,
		),
		"missing": bytes.Replace(
			tenantJSON("1.36.4", ""),
			[]byte(`,"databaseCount":1`),
			nil,
			1,
		),
	} {
		t.Run(name, func(t *testing.T) {
			response := handler.Handle(context.Background(), admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
				Operation: admissionv1.Create,
				Object:    runtimeRaw(document),
			}})
			if response.Allowed {
				t.Fatal("invalid field shape was allowed")
			}
		})
	}
}

func TestUpdateAllowsCanonicalEquivalentVersion(t *testing.T) {
	handler := &TenantValidator{SupportedVersion: "1.36.4"}
	response := handler.Handle(context.Background(), admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
		Operation: admissionv1.Update,
		Object:    runtimeRaw(tenantJSON("1.36.4", "")),
		OldObject: runtimeRaw(tenantJSON("v1.36.4", "")),
	}})
	if !response.Allowed {
		t.Fatalf("equivalent update denied: %s", response.Result.Message)
	}
}

func TestUpdateRejectsSemanticChange(t *testing.T) {
	oldObject := tenantJSON("1.36.4", "")
	var document map[string]any
	if err := json.Unmarshal(oldObject, &document); err != nil {
		t.Fatal(err)
	}
	document["spec"].(map[string]any)["workers"] = float64(2)
	newObject, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	handler := &TenantValidator{SupportedVersion: "1.36.4"}
	response := handler.Handle(context.Background(), admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
		Operation: admissionv1.Update,
		Object:    runtimeRaw(newObject),
		OldObject: runtimeRaw(oldObject),
	}})
	if response.Allowed {
		t.Fatal("semantic update was allowed")
	}
}
