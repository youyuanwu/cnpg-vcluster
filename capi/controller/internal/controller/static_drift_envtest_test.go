package controller

import (
	"context"
	"errors"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

func TestStaticTenantObjectDryRunAgainstAPIServer(t *testing.T) {
	environment := &envtest.Environment{}
	configuration, err := environment.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := environment.Stop(); err != nil {
			t.Error(err)
		}
	})
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubernetes, err := client.New(configuration, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	namespace := &corev1.Namespace{}
	namespace.Name = "static-drift"
	if err := kubernetes.Create(ctx, namespace); err != nil {
		t.Fatal(err)
	}

	tenant := testTenant()
	desired := markedTenantConfigMap(
		schema.GroupVersionKind{Version: "v1", Kind: "ConfigMap"},
		tenant,
		"desired",
	)
	desired.SetNamespace(namespace.Name)
	created, err := ensureStaticTenantObject(
		ctx,
		kubernetes,
		desired,
		tenant,
		"spec-hash",
		"foundation-hash",
		false,
	)
	if err != nil || !created {
		t.Fatalf("create static ConfigMap: created=%t err=%v", created, err)
	}
	if created, err := ensureStaticTenantObject(
		ctx,
		kubernetes,
		desired,
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	); err != nil || created {
		t.Fatalf("matching real static ConfigMap failed dry-run audit: created=%t err=%v", created, err)
	}
	service := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "Service",
		"metadata": map[string]any{
			"name":      "defaulted",
			"namespace": namespace.Name,
		},
		"spec": map[string]any{
			"selector": map[string]any{"app": "defaulted"},
			"ports":    []any{map[string]any{"port": int64(80)}},
		},
	}}
	service.SetLabels(desired.GetLabels())
	service.SetAnnotations(desired.GetAnnotations())
	if created, err := ensureStaticTenantObject(
		ctx,
		kubernetes,
		service,
		tenant,
		"spec-hash",
		"foundation-hash",
		false,
	); err != nil || !created {
		t.Fatalf("create defaulted static Service: created=%t err=%v", created, err)
	}
	if created, err := ensureStaticTenantObject(
		ctx,
		kubernetes,
		service,
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	); err != nil || created {
		t.Fatalf("defaulted real Service failed dry-run audit: created=%t err=%v", created, err)
	}

	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(desired), current); err != nil {
		t.Fatal(err)
	}
	current.Object["data"] = map[string]any{"value": "drifted"}
	if err := kubernetes.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := ensureStaticTenantObject(
		ctx,
		kubernetes,
		desired,
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	); !errors.Is(err, errStaticResourceDrift) {
		t.Fatalf("real static drift was not detected: %v", err)
	}
	preserved := &unstructured.Unstructured{}
	preserved.SetGroupVersionKind(desired.GroupVersionKind())
	if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(desired), preserved); err != nil {
		t.Fatal(err)
	}
	data, _, _ := unstructured.NestedStringMap(preserved.Object, "data")
	if data["value"] != "drifted" {
		t.Fatalf("real dry-run audit mutated live content: %v", data)
	}
}
