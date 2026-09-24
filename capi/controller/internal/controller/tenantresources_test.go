package controller

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
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
	patched         bool
	created         bool
	patchUID        types.UID
	patchResourceRV string
	patchErr        error
}

func (tenantClient *recordingPatchClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	tenantClient.patched = true
	tenantClient.patchUID = object.GetUID()
	tenantClient.patchResourceRV = object.GetResourceVersion()
	if tenantClient.patchErr != nil {
		return tenantClient.patchErr
	}
	return tenantClient.Client.Patch(ctx, object, patch, options...)
}

func (tenantClient *recordingPatchClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	tenantClient.created = true
	return tenantClient.Client.Create(ctx, object, options...)
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

func TestEnsureTenantObjectRejectsMissingRecordedIdentityBeforeCreate(t *testing.T) {
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
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).Build(),
	}
	_, _, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
	)
	if err == nil || !strings.Contains(err.Error(), "is absent") {
		t.Fatalf("missing recorded resource was recreated: %v", err)
	}
	if tenantClient.created {
		t.Fatal("missing recorded resource was created before identity validation")
	}
}

func TestEnsureTenantObjectBindsDriftRepairToObservedIdentity(t *testing.T) {
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
	current := markedTenantConfigMap(gvk, tenant, "drifted")
	current.SetUID("recorded-uid")
	current.SetResourceVersion("7")
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
	}
	_, changed, err := ensureTenantObjectWithPatchResult(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !changed || !tenantClient.patched {
		t.Fatal("same-identity drift was not repaired")
	}
	if tenantClient.patchUID != "recorded-uid" || tenantClient.patchResourceRV != "7" {
		t.Fatalf(
			"drift repair was not bound to the observed identity: uid=%s rv=%s",
			tenantClient.patchUID,
			tenantClient.patchResourceRV,
		)
	}
}

func TestEnsureTenantObjectReturnsRetryableApplyConflict(t *testing.T) {
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
	current := markedTenantConfigMap(gvk, tenant, "drifted")
	current.SetUID("recorded-uid")
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
		patchErr: apierrors.NewConflict(
			schema.GroupResource{Resource: "configmaps"},
			current.GetName(),
			fmt.Errorf("changed"),
		),
	}
	_, _, err := ensureTenantObjectWithPatchResult(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	)
	if !errors.Is(err, errStableApplyConflict) {
		t.Fatalf("apply conflict was not marked retryable: %v", err)
	}
}

func TestReadinessRejectsStaleObservedGenerations(t *testing.T) {
	deployment := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata":   map[string]any{"name": "example", "generation": int64(2)},
		"spec":       map[string]any{"replicas": int64(1)},
		"status": map[string]any{
			"observedGeneration": int64(1),
			"availableReplicas":  int64(1),
		},
	}}
	if workloadAvailable(deployment) {
		t.Fatal("stale workload generation was accepted")
	}
	machine := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "cluster.x-k8s.io/v1beta2",
		"kind":       "Machine",
		"metadata":   map[string]any{"name": "example", "generation": int64(2)},
		"status": map[string]any{"conditions": []any{map[string]any{
			"type":               "Ready",
			"status":             "True",
			"observedGeneration": int64(1),
		}}},
	}}
	if tenantObjectReady(machine) {
		t.Fatal("stale Ready condition generation was accepted")
	}
}

func TestDesiredMatchIgnoresServerAddedDefaultsButDetectsDrift(t *testing.T) {
	desired := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata": map[string]any{
			"name": "example",
		},
		"spec": map[string]any{
			"template": map[string]any{
				"spec": map[string]any{
					"containers": []any{map[string]any{
						"name":  "main",
						"image": "example@sha256:exact",
					}},
				},
			},
		},
	}}
	current := desired.DeepCopy()
	desired.Object["preserveUnknownFields"] = false
	desired.Object["maximum"] = int64(4294967295)
	desired.Object["status"] = map[string]any{"acceptedNames": "source-only"}
	current.Object["maximum"] = float64(4294967295)
	containers, _, _ := unstructured.NestedSlice(
		current.Object,
		"spec",
		"template",
		"spec",
		"containers",
	)
	containers[0].(map[string]any)["imagePullPolicy"] = "IfNotPresent"
	if err := unstructured.SetNestedSlice(
		current.Object,
		containers,
		"spec",
		"template",
		"spec",
		"containers",
	); err != nil {
		t.Fatal(err)
	}
	if !desiredMatchesCurrent(desired, current) {
		t.Fatal("server-added default was treated as desired-field drift")
	}
	containers[0].(map[string]any)["image"] = "example:drifted"
	if err := unstructured.SetNestedSlice(
		current.Object,
		containers,
		"spec",
		"template",
		"spec",
		"containers",
	); err != nil {
		t.Fatal(err)
	}
	if desiredMatchesCurrent(desired, current) {
		t.Fatal("desired image drift was ignored")
	}
}

func TestValidateTenantResourceOwnershipRejectsReplacement(t *testing.T) {
	gvk := schema.GroupVersionKind{Group: "storage.k8s.io", Version: "v1", Kind: "StorageClass"}
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
		Status: tenancyv1alpha1.TenantStatus{
			TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
				APIVersion: gvk.GroupVersion().String(),
				Kind:       gvk.Kind,
				Name:       tenantStorageClass,
				UID:        "recorded-uid",
			}},
		},
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(gvk)
	current.SetName(tenantStorageClass)
	current.SetUID("replacement-uid")
	current.SetAnnotations(map[string]string{
		resources.TenantAnnotation:     tenant.Name,
		resources.TenantUIDAnnotation:  string(tenant.UID),
		resources.SpecHashAnnotation:   "spec-hash",
		resources.FoundationAnnotation: "foundation-hash",
	})
	kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build()
	if err := validateTenantResourceOwnership(
		context.Background(),
		kubernetes,
		tenant,
		"spec-hash",
		"foundation-hash",
	); err == nil || !strings.Contains(err.Error(), "ownership changed") {
		t.Fatalf("same-name Tenant resource replacement was accepted: %v", err)
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
