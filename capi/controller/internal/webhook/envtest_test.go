package webhook

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	runtimewebhook "sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func TestWebhookRejectsUnknownFieldsAndSemanticUpdates(t *testing.T) {
	configRoot := filepath.Join("..", "..", "config")
	environment := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join(configRoot, "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		WebhookInstallOptions: envtest.WebhookInstallOptions{
			Paths: []string{filepath.Join(configRoot, "webhook", "validating-webhook.yaml")},
		},
	}
	restConfig, err := environment.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := environment.Stop(); err != nil {
			t.Errorf("stop envtest: %v", err)
		}
	})

	scheme := runtime.NewScheme()
	if err := tenancyv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	options := environment.WebhookInstallOptions
	manager, err := ctrl.NewManager(restConfig, ctrl.Options{
		Scheme: scheme,
		WebhookServer: runtimewebhook.NewServer(runtimewebhook.Options{
			Host:    options.LocalServingHost,
			Port:    options.LocalServingPort,
			CertDir: options.LocalServingCertDir,
		}),
		Metrics: metricsserver.Options{BindAddress: "0"},
	})
	if err != nil {
		t.Fatal(err)
	}
	manager.GetWebhookServer().Register(
		"/validate-tenancy-cnpg-vcluster-io-v1alpha1-tenant",
		&admission.Webhook{Handler: &TenantValidator{SupportedVersion: "1.36.4"}},
	)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() {
		if err := manager.Start(ctx); err != nil {
			t.Errorf("start webhook manager: %v", err)
		}
	}()

	client, err := dynamic.NewForConfig(restConfig)
	if err != nil {
		t.Fatal(err)
	}
	tenants := client.Resource(schema.GroupVersionResource{
		Group: "tenancy.cnpg-vcluster.io", Version: "v1alpha1", Resource: "tenants",
	})
	valid := tenantObject("tenant-a", "v1.36.4")
	deadline := time.Now().Add(10 * time.Second)
	for {
		_, err = tenants.Create(ctx, valid, metav1.CreateOptions{})
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("create valid Tenant: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}

	unknown := tenantObject("tenant-unknown", "1.36.4")
	unknown.Object["spec"].(map[string]any)["unknown"] = true
	if _, err := tenants.Create(ctx, unknown, metav1.CreateOptions{}); err == nil {
		t.Fatal("unknown field was accepted by live webhook")
	}

	current, err := tenants.Get(ctx, "tenant-a", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	current.Object["spec"].(map[string]any)["kubernetesVersion"] = "1.36.4"
	if _, err := tenants.Update(ctx, current, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("canonical-equivalent update was rejected: %v", err)
	}
	current, err = tenants.Get(ctx, "tenant-a", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	current.Object["spec"].(map[string]any)["workers"] = int64(2)
	if _, err := tenants.Update(ctx, current, metav1.UpdateOptions{}); err == nil {
		t.Fatal("semantic update was accepted by live webhook")
	}

	current, err = tenants.Get(ctx, "tenant-a", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := unstructured.SetNestedField(current.Object, "Progressing", "status", "phase"); err != nil {
		t.Fatal(err)
	}
	updatedStatus, err := tenants.UpdateStatus(ctx, current, metav1.UpdateOptions{})
	if err != nil {
		t.Fatalf("status subresource update failed: %v", err)
	}
	version, _, err := unstructured.NestedString(updatedStatus.Object, "spec", "kubernetesVersion")
	if err != nil || version != "1.36.4" {
		t.Fatalf("status update changed spec: version=%q err=%v", version, err)
	}

	rawConfig := rest.CopyConfig(restConfig)
	rawConfig.GroupVersion = &schema.GroupVersion{
		Group: "tenancy.cnpg-vcluster.io", Version: "v1alpha1",
	}
	rawConfig.APIPath = "/apis"
	rawConfig.NegotiatedSerializer = serializer.NewCodecFactory(scheme).WithoutConversion()
	rawClient, err := rest.RESTClientFor(rawConfig)
	if err != nil {
		t.Fatal(err)
	}
	duplicate := []byte(`{"apiVersion":"tenancy.cnpg-vcluster.io/v1alpha1","kind":"Tenant","metadata":{"name":"tenant-duplicate"},"spec":{"kubernetesVersion":"1.36.4","workers":1,"workers":2,"databaseCount":1,"podCIDR":"10.30.0.0/16","serviceCIDR":"10.31.0.0/16"}}`)
	if err := rawClient.Post().
		Resource("tenants").
		Param("fieldValidation", "Strict").
		Body(duplicate).
		Do(ctx).
		Error(); err == nil {
		t.Fatal("duplicate JSON field was accepted by strict API request")
	}
	if err := rawClient.Post().
		Resource("tenants").
		Param("fieldValidation", "Strict").
		Body([]byte(`{"apiVersion":`)).
		Do(ctx).
		Error(); err == nil {
		t.Fatal("malformed JSON was accepted by strict API request")
	}
}

func tenantObject(name, version string) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "tenancy.cnpg-vcluster.io/v1alpha1",
		"kind":       "Tenant",
		"metadata": map[string]any{
			"name": name,
		},
		"spec": map[string]any{
			"kubernetesVersion": version,
			"workers":           int64(1),
			"databaseCount":     int64(1),
			"podCIDR":           "10.20.0.0/16",
			"serviceCIDR":       "10.21.0.0/16",
		},
	}}
}
