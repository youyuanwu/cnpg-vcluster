package controller

import (
	"context"
	"errors"
	"fmt"
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

type createRaceClient struct {
	client.Client
	foreign client.Object
}

func (value *createRaceClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if err := value.Client.Create(ctx, value.foreign); err != nil {
		return err
	}
	return apierrors.NewAlreadyExists(
		schema.GroupResource{Group: object.GetObjectKind().GroupVersionKind().Group, Resource: "objects"},
		object.GetName(),
	)
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

func TestEnsureTenantObjectCreatesMissingChild(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).Build(),
	}
	changed, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
	)
	if err != nil {
		t.Fatal(err)
	}
	if !changed || !tenantClient.created {
		t.Fatal("missing child was not created")
	}
}

func TestEnsureStaticTenantObjectDoesNotMutateExistingDuringProgress(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	current := markedTenantConfigMap(gvk, tenant, "drifted")
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
	}
	changed, err := ensureStaticTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
		false,
	)
	if err != nil || changed {
		t.Fatalf("unexpected static progress result: changed=%t err=%v", changed, err)
	}
	if tenantClient.patched {
		t.Fatal("existing static resource was patched during progress")
	}
}

func TestEnsureStaticTenantObjectRecreatesMissingWhileReady(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).Build(),
	}
	changed, err := ensureStaticTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	)
	if err != nil || !changed || !tenantClient.created {
		t.Fatalf("missing Ready static resource was not recreated: changed=%t err=%v", changed, err)
	}
}

func TestEnsureStaticTenantObjectAcceptsMatchingReadyResource(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	desired := markedTenantConfigMap(gvk, tenant, "desired")
	current := desired.DeepCopy()
	current.SetUID("live-uid")
	current.SetResourceVersion("7")
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
	}
	changed, err := ensureStaticTenantObject(
		context.Background(),
		tenantClient,
		desired,
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	)
	if err != nil || changed {
		t.Fatalf("unexpected matching static result: changed=%t err=%v", changed, err)
	}
	if !tenantClient.patched {
		t.Fatal("Ready static resource was not dry-run validated")
	}
}

func TestEnsureStaticTenantObjectReportsDriftWithoutMutation(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	current := markedTenantConfigMap(gvk, tenant, "drifted")
	current.SetUID("live-uid")
	current.SetResourceVersion("7")
	desired := markedTenantConfigMap(gvk, tenant, "desired")
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build()
	tenantClient := &recordingPatchClient{Client: base}
	changed, err := ensureStaticTenantObject(
		context.Background(),
		tenantClient,
		desired,
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	)
	if changed || !errors.Is(err, errStaticResourceDrift) {
		t.Fatalf("static drift was not reported: changed=%t err=%v", changed, err)
	}
	var preserved unstructured.Unstructured
	preserved.SetGroupVersionKind(gvk)
	if err := base.Get(context.Background(), client.ObjectKeyFromObject(current), &preserved); err != nil {
		t.Fatal(err)
	}
	data, _, _ := unstructured.NestedStringMap(preserved.Object, "data")
	if data["value"] != "drifted" {
		t.Fatalf("dry-run validation mutated the live resource: %v", data)
	}
}

func TestEnsureTenantObjectRefusesForeignSameName(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	foreign := markedTenantConfigMap(gvk, tenant, "foreign")
	foreign.SetAnnotations(map[string]string{resources.TenantAnnotation: "other"})
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(foreign).Build()
	tenantClient := &recordingPatchClient{Client: base}
	_, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
	)
	if err == nil {
		t.Fatal("foreign same-name child was accepted")
	}

	if tenantClient.patched {
		t.Fatal("foreign same-name child was mutated")
	}
}

func TestEnsureTenantObjectRefusesForeignCreateRace(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	foreign := markedTenantConfigMap(gvk, tenant, "foreign")
	foreign.SetAnnotations(map[string]string{resources.TenantAnnotation: "other"})
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	tenantClient := &createRaceClient{Client: base, foreign: foreign}
	if _, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
	); err == nil {
		t.Fatal("foreign create race was accepted")
	}
	var current unstructured.Unstructured
	current.SetGroupVersionKind(gvk)
	if err := base.Get(context.Background(), client.ObjectKeyFromObject(foreign), &current); err != nil {
		t.Fatal(err)
	}
	if current.GetAnnotations()[resources.TenantAnnotation] != "other" {
		t.Fatal("foreign create-race object was modified")
	}
}

func TestEnsureManagementObjectRefusesForeignCreateRace(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	desired := markedTenantConfigMap(gvk, tenant, "desired")
	desired.SetAnnotations(map[string]string{
		resources.TenantAnnotation:     tenant.Name,
		resources.TenantUIDAnnotation:  string(tenant.UID),
		resources.SpecHashAnnotation:   "spec-hash",
		resources.FoundationAnnotation: foundation.Hash,
		resources.ResourceAnnotation:   "management-test",
	})
	foreign := desired.DeepCopy()
	foreign.SetAnnotations(map[string]string{resources.TenantAnnotation: "other"})
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	race := &createRaceClient{Client: base, foreign: foreign}
	reconciler := &TenantReconciler{Client: race, APIReader: race}
	if _, _, err := reconciler.ensureManagementObject(
		context.Background(),
		desired,
		tenant,
		"spec-hash",
		foundation,
		"management-test",
	); err == nil {
		t.Fatal("foreign management create race was accepted")
	}
}

func TestEnsureTenantObjectBindsDriftRepairToLiveIdentity(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	current := markedTenantConfigMap(gvk, tenant, "drifted")
	current.SetUID("live-uid")
	current.SetResourceVersion("7")
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
	}
	changed, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
	)
	if err != nil {
		t.Fatal(err)
	}
	if changed || !tenantClient.patched {
		t.Fatal("owned drift was not repaired")
	}
	if tenantClient.patchUID != "live-uid" || tenantClient.patchResourceRV != "7" {
		t.Fatalf("drift repair was not bound to the live identity: uid=%s rv=%s", tenantClient.patchUID, tenantClient.patchResourceRV)
	}
}

func TestEnsureTenantObjectReturnsRetryableApplyConflict(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	current := markedTenantConfigMap(gvk, tenant, "drifted")
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
		patchErr: apierrors.NewConflict(
			schema.GroupResource{Resource: "configmaps"},
			current.GetName(),
			fmt.Errorf("changed"),
		),
	}

	_, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
	)
	if !errors.Is(err, errStableApplyConflict) {
		t.Fatalf("apply conflict was not marked retryable: %v", err)
	}
}

func TestEnsureTenantObjectClassifiesImmutableApplyFailure(t *testing.T) {
	gvk := schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}
	tenant := testTenant()
	current := markedTenantConfigMap(gvk, tenant, "drifted")
	tenantClient := &recordingPatchClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
		patchErr: apierrors.NewInvalid(
			schema.GroupKind{Group: gvk.Group, Kind: gvk.Kind},
			current.GetName(),
			nil,
		),
	}
	_, err := ensureTenantObject(
		context.Background(),
		tenantClient,
		markedTenantConfigMap(gvk, tenant, "desired"),
		tenant,
		"spec-hash",
		"foundation-hash",
	)
	if !errors.Is(err, errImmutableDrift) {
		t.Fatalf("invalid apply was not classified as immutable drift: %v", err)
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

func testTenant() *tenancyv1alpha1.Tenant {
	return &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
	}
}

func markedTenantConfigMap(gvk schema.GroupVersionKind, tenant *tenancyv1alpha1.Tenant, value string) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
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
}
