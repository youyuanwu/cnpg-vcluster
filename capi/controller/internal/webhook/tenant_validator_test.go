package webhook

import (
	"context"
	"encoding/json"
	"testing"

	admissionv1 "k8s.io/api/admission/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

func tenantJSON(version string, extra string) []byte {
	document := `{"apiVersion":"tenancy.cnpg-vcluster.io/v1alpha1","kind":"Tenant","metadata":{"name":"tenant-a"},"spec":{"kubernetesVersion":"` + version + `","workers":1,"databaseCount":1,"podCIDR":"10.20.0.0/16","serviceCIDR":"10.21.0.0/16"` + extra + `}}`
	return []byte(document)
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
