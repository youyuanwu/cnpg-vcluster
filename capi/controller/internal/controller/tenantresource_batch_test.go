package controller

import (
	"context"
	"fmt"
	"slices"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

type batchRecordingClient struct {
	client.Client
	creates  []string
	patches  []string
	races    map[string]*unstructured.Unstructured
	patchErr error
}

func (value *batchRecordingClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	value.creates = append(value.creates, object.GetName())
	if raced := value.races[object.GetName()]; raced != nil {
		if err := value.Client.Create(ctx, raced); err != nil {
			return err
		}
		return apierrors.NewAlreadyExists(schema.GroupResource{Resource: "objects"}, object.GetName())
	}
	return value.Client.Create(ctx, object, options...)
}

func (value *batchRecordingClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	value.patches = append(value.patches, object.GetName())
	if value.patchErr != nil {
		return value.patchErr
	}
	return value.Client.Patch(ctx, object, patch, options...)
}

func batchObject(apiVersion, kind, namespace, name string) *unstructured.Unstructured {
	value := markedTenantConfigMap(schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"}, testTenant(), "desired")
	value.SetAPIVersion(apiVersion)
	value.SetKind(kind)
	value.SetName(name)
	value.SetNamespace(namespace)
	delete(value.Object, "data")
	return value
}

func TestEnsureTenantObjectsCreatesAndValidatesWholeBatch(t *testing.T) {
	const count = 43
	objects := make([]*unstructured.Unstructured, count)
	for index := range objects {
		objects[index] = batchObject("v1", "ConfigMap", "default", fmt.Sprintf("config-%02d", index))
	}
	kubernetes := &batchRecordingClient{Client: fake.NewClientBuilder().WithScheme(testScheme(t)).Build()}
	result, err := ensureTenantObjects(context.Background(), kubernetes, objects, testTenant(), "spec-hash", "foundation-hash")
	if err != nil || !result.Created || result.Pending {
		t.Fatalf("unexpected first batch result: %+v, %v", result, err)
	}
	if len(kubernetes.creates) != count || len(kubernetes.patches) != 0 {
		t.Fatalf("batch did not create every object exactly once: creates=%d patches=%d", len(kubernetes.creates), len(kubernetes.patches))
	}
	result, err = ensureTenantObjects(context.Background(), kubernetes, objects, testTenant(), "spec-hash", "foundation-hash")
	if err != nil || result.Created || result.Pending {
		t.Fatalf("unexpected existing batch result: %+v, %v", result, err)
	}
	if len(kubernetes.creates) != count || len(kubernetes.patches) != 0 {
		t.Fatalf("existing static objects were mutated: creates=%d patches=%d", len(kubernetes.creates), len(kubernetes.patches))
	}
}

func TestEnsureTenantObjectsChecksOwnershipAfterCreation(t *testing.T) {
	for _, annotation := range []string{
		resources.TenantAnnotation, resources.TenantUIDAnnotation, resources.SpecHashAnnotation,
		resources.FoundationAnnotation, resources.ResourceAnnotation,
	} {
		t.Run(annotation, func(t *testing.T) {
			first := batchObject("v1", "ConfigMap", "default", "first")
			second := batchObject("v1", "ConfigMap", "default", "second")
			last := batchObject("v1", "ConfigMap", "default", "last")
			foreign := second.DeepCopy()
			annotations := foreign.GetAnnotations()
			annotations[annotation] = "foreign"
			foreign.SetAnnotations(annotations)
			kubernetes := &batchRecordingClient{
				Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(foreign).Build(),
			}
			result, err := ensureTenantObjects(context.Background(), kubernetes, []*unstructured.Unstructured{first, second, last}, testTenant(), "spec-hash", "foundation-hash")
			if err == nil || !result.Created || !isOwnershipError(err) {
				t.Fatalf("batch skipped ownership validation after a creation: %+v, %v", result, err)
			}
			if len(kubernetes.patches) != 0 || !slices.Equal(kubernetes.creates, []string{"first"}) {
				t.Fatalf("batch mutated foreign or subsequent objects: creates=%v patches=%v", kubernetes.creates, kubernetes.patches)
			}
		})
	}
}

func TestEnsureTenantObjectsHandlesCreateRaces(t *testing.T) {
	for _, owned := range []bool{true, false} {
		t.Run(fmt.Sprintf("owned=%t", owned), func(t *testing.T) {
			first := batchObject("v1", "ConfigMap", "default", "first")
			raced := batchObject("v1", "ConfigMap", "default", "raced")
			last := batchObject("v1", "ConfigMap", "default", "last")
			current := raced.DeepCopy()
			if !owned {
				annotations := current.GetAnnotations()
				annotations[resources.TenantUIDAnnotation] = "replacement"
				current.SetAnnotations(annotations)
			}
			kubernetes := &batchRecordingClient{
				Client: fake.NewClientBuilder().WithScheme(testScheme(t)).Build(),
				races:  map[string]*unstructured.Unstructured{"raced": current},
			}
			result, err := ensureTenantObjects(context.Background(), kubernetes, []*unstructured.Unstructured{first, raced, last}, testTenant(), "spec-hash", "foundation-hash")
			if !result.Created {
				t.Fatal("batch lost the earlier creation")
			}
			if owned {
				if err != nil || !slices.Equal(kubernetes.creates, []string{"first", "raced", "last"}) ||
					len(kubernetes.patches) != 0 {
					t.Fatalf("owned create race did not continue: %+v, %v, creates=%v patches=%v", result, err, kubernetes.creates, kubernetes.patches)
				}
			} else if err == nil || !isOwnershipError(err) || len(kubernetes.patches) != 0 || len(kubernetes.creates) != 2 {
				t.Fatalf("foreign create race was not refused: %+v, %v", result, err)
			}
		})
	}
}

func batchCRD(name, kind string) *unstructured.Unstructured {
	crd := batchObject("apiextensions.k8s.io/v1", "CustomResourceDefinition", "", name+".example.io")
	crd.Object["spec"] = map[string]any{
		"group": "example.io",
		"names": map[string]any{"kind": kind, "plural": name},
		"scope": "Namespaced",
		"versions": []any{
			map[string]any{"name": "v1", "served": true, "storage": true},
			map[string]any{"name": "v2", "served": true, "storage": false},
		},
	}
	return crd
}

func TestEnsureTenantObjectsWaitsForCRDEstablishmentAndDiscovery(t *testing.T) {
	ctx := context.Background()
	namespace := batchObject("v1", "Namespace", "", "workload")
	widgetCRD := batchCRD("widgets", "Widget")
	gadgetCRD := batchCRD("gadgets", "Gadget")
	widget := batchObject("example.io/v1", "Widget", "workload", "widget")
	config := batchObject("v1", "ConfigMap", "workload", "config")
	account := batchObject("v1", "ServiceAccount", "workload", "account")
	deployment := batchObject("apps/v1", "Deployment", "workload", "deployment")
	objects := []*unstructured.Unstructured{widget, deployment, config, account, namespace, widgetCRD, gadgetCRD}
	mapper := meta.NewDefaultRESTMapper(nil)
	kubernetes := &batchRecordingClient{Client: fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithRESTMapper(mapper).
		WithStatusSubresource(widgetCRD, gadgetCRD).
		Build()}
	apply := func() tenantApplyResult {
		t.Helper()
		result, err := ensureTenantObjects(ctx, kubernetes, objects, testTenant(), "spec-hash", "foundation-hash")
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	result := apply()
	if !result.Created || !result.Pending || !slices.Equal(kubernetes.creates, []string{"workload", "widgets.example.io", "gadgets.example.io"}) {
		t.Fatalf("CRD prerequisites were not created together: %+v, %v", result, kubernetes.creates)
	}
	for _, crd := range []*unstructured.Unstructured{widgetCRD, gadgetCRD} {
		current := crd.DeepCopy()
		if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(crd), current); err != nil {
			t.Fatal(err)
		}
		current.Object["status"] = map[string]any{"conditions": []any{
			map[string]any{"type": "Established", "status": "True"},
		}}
		if err := kubernetes.Status().Update(ctx, current); err != nil {
			t.Fatal(err)
		}
	}
	result = apply()
	if result.Created || !result.Pending || len(kubernetes.creates) != 3 {
		t.Fatalf("establishment without discovery released dependents: %+v", result)
	}
	for _, kind := range []string{"Widget", "Gadget"} {
		mapper.Add(schema.GroupVersionKind{Group: "example.io", Version: "v1", Kind: kind}, meta.RESTScopeNamespace)
	}
	result = apply()
	if !result.Pending || len(kubernetes.creates) != 3 {
		t.Fatalf("missing served version released dependents: %+v", result)
	}
	for _, kind := range []string{"Widget", "Gadget"} {
		mapper.Add(schema.GroupVersionKind{Group: "example.io", Version: "v2", Kind: kind}, meta.RESTScopeNamespace)
	}
	result = apply()
	if !result.Created || result.Pending {
		t.Fatalf("ready CRDs did not release dependents: %+v", result)
	}
	expected := []string{"workload", "widgets.example.io", "gadgets.example.io", "config", "account", "widget", "deployment"}
	if !slices.Equal(kubernetes.creates, expected) {
		t.Fatalf("dependency ordering changed: got %v, want %v", kubernetes.creates, expected)
	}
}

func TestEnsureTenantObjectsRejectsForeignCRDBeforeDependents(t *testing.T) {
	crd := batchCRD("widgets", "Widget")
	foreign := crd.DeepCopy()
	foreign.SetAnnotations(nil)
	kubernetes := &batchRecordingClient{Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(foreign).Build()}
	_, err := ensureTenantObjects(context.Background(), kubernetes, []*unstructured.Unstructured{
		batchObject("example.io/v1", "Widget", "default", "widget"), crd,
	}, testTenant(), "spec-hash", "foundation-hash")
	if err == nil || !isOwnershipError(err) || len(kubernetes.creates) != 0 || len(kubernetes.patches) != 0 {
		t.Fatalf("foreign CRD was not rejected before dependent mutation: %v", err)
	}
}
