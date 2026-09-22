package controller

import (
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

type recordingPatchClient struct {
	client.Client
	patched bool
}

func (tenantClient *recordingPatchClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	tenantClient.patched = true
	return tenantClient.Client.Patch(ctx, object, patch, options...)
}

func TestEnsureTenantObjectRejectsRecordedReplacementBeforePatch(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
		Status: tenancyv1alpha1.TenantStatus{
			TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
				APIVersion: gvk.GroupVersion().String(),
				Kind:       gvk.Kind,
				Namespace:  "default",
				Name:       "network-config",
				UID:        "recorded-uid",
			}},
		},
	}
	desired := markedTenantConfigMap(gvk, tenant, "desired")
	current := markedTenantConfigMap(gvk, tenant, "foreign")
	current.SetUID(types.UID("replacement-uid"))
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build()
	tenantClient := &recordingPatchClient{Client: base}

	_, _, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		desired,
		tenant,
		"spec-hash",
		"foundation-hash",
	)
	if err == nil || !strings.Contains(err.Error(), "identity changed") {
		t.Fatalf("recorded replacement was not rejected: %v", err)
	}
	if tenantClient.patched {
		t.Fatal("recorded replacement was mutated before UID validation")
	}
}

func TestCompletedProbeIdentitiesAreRemovedFromReadyState(t *testing.T) {
	status := tenancyv1alpha1.TenantStatus{
		TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{
			{APIVersion: "v1", Kind: "Pod", Namespace: "default", Name: "tenant-a-network-verify", UID: "network"},
			{APIVersion: "v1", Kind: "Pod", Namespace: "default", Name: "tenant-a-storage-verify", UID: "storage"},
			{APIVersion: "v1", Kind: "Pod", Namespace: "database", Name: "tenant-a-sql-verify", UID: "sql"},
			{APIVersion: "v1", Kind: "Node", Name: "worker-a", UID: "node"},
		},
	}
	if !removeTenantProbeIdentities(&status, "tenant-a") {
		t.Fatal("completed probe identities were not removed")
	}
	if len(status.TenantResources) != 1 || status.TenantResources[0].Kind != "Node" {
		t.Fatalf("unexpected retained identities: %#v", status.TenantResources)
	}
	if removeTenantProbeIdentities(&status, "tenant-a") {
		t.Fatal("probe identity removal was not idempotent")
	}
}

func markedTenantConfigMap(gvk schema.GroupVersionKind, tenant *tenancyv1alpha1.Tenant, value string) *unstructured.Unstructured {
	object := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": gvk.GroupVersion().String(),
		"kind":       gvk.Kind,
		"metadata": map[string]any{
			"name":      "network-config",
			"namespace": "default",
			"annotations": map[string]any{
				resources.TenantAnnotation:     tenant.Name,
				resources.TenantUIDAnnotation:  string(tenant.UID),
				resources.SpecHashAnnotation:   "spec-hash",
				resources.FoundationAnnotation: "foundation-hash",
				resources.ResourceAnnotation:   "network-workload",
			},
		},
		"data": map[string]any{"value": value},
	}}
	return object
}
