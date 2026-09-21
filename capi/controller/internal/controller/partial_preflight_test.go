package controller

import (
	"context"
	"errors"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

type recordingTenantFactory struct {
	called bool
}

type noMatchTenantClient struct {
	client.Client
	gvk schema.GroupVersionKind
}

type failingTenantClient struct {
	client.Client
	err error
}

func (tenantClient noMatchTenantClient) Get(ctx context.Context, key client.ObjectKey, object client.Object, options ...client.GetOption) error {
	if object.GetObjectKind().GroupVersionKind() == tenantClient.gvk {
		return &meta.NoKindMatchError{
			GroupKind:        tenantClient.gvk.GroupKind(),
			SearchedVersions: []string{tenantClient.gvk.Version},
		}
	}
	return tenantClient.Client.Get(ctx, key, object, options...)
}

func (tenantClient failingTenantClient) Get(context.Context, client.ObjectKey, client.Object, ...client.GetOption) error {
	return tenantClient.err
}

func (factory *recordingTenantFactory) ClientFor([]byte, string) (client.Client, error) {
	factory.called = true
	return nil, errors.New("tenant client must not be constructed")
}

func TestTenantResourceCleanupCheckpointsBeforeCRDDeletion(t *testing.T) {
	gvk := schema.GroupVersionKind{Group: "postgresql.cnpg.io", Version: "v1", Kind: "Cluster"}
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
		Status: tenancyv1alpha1.TenantStatus{
			TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
				APIVersion: gvk.GroupVersion().String(),
				Kind:       gvk.Kind,
				Namespace:  "database",
				Name:       "capi-postgres",
				UID:        "cluster-uid",
			}},
		},
	}
	tenantClient := noMatchTenantClient{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).Build(),
		gvk:    gvk,
	}

	if _, err := deleteTenantResources(
		context.Background(),
		tenantClient,
		tenant,
		"spec-hash",
		"foundation-hash",
		false,
	); err == nil || !meta.IsNoMatchError(err) {
		t.Fatalf("expected discovery failure before the cleanup checkpoint, got %v", err)
	}

	absent, err := deleteTenantResources(
		context.Background(),
		tenantClient,
		tenant,
		"spec-hash",
		"foundation-hash",
		true,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !absent {
		t.Fatal("custom resources must not be rediscovered after the cleanup checkpoint")
	}
}

func TestTenantResourceCleanupFailsClosedOnTenantAPIErrors(t *testing.T) {
	gvk := corev1.SchemeGroupVersion.WithKind("ConfigMap")
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
		Status: tenancyv1alpha1.TenantStatus{
			TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
				APIVersion: gvk.GroupVersion().String(),
				Kind:       gvk.Kind,
				Namespace:  "default",
				Name:       "tenant-config",
				UID:        "config-uid",
			}},
		},
	}
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	transportErr := errors.New("tenant API transport unavailable")
	for name, expected := range map[string]error{
		"transport": transportErr,
		"authorization": apierrors.NewForbidden(
			schema.GroupResource{Resource: "configmaps"},
			"tenant-config",
			errors.New("denied"),
		),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := deleteTenantResources(
				context.Background(),
				failingTenantClient{Client: base, err: expected},
				tenant,
				"spec-hash",
				"foundation-hash",
				false,
			)
			if err == nil {
				t.Fatal("tenant API failure was accepted as absence")
			}
			if name == "transport" && !errors.Is(err, transportErr) {
				t.Fatalf("transport error was not preserved: %v", err)
			}
			if name == "authorization" && !apierrors.IsForbidden(err) {
				t.Fatalf("authorization error was not preserved: %v", err)
			}
		})
	}

	absent, err := deleteTenantResources(
		context.Background(),
		base,
		tenant,
		"spec-hash",
		"foundation-hash",
		false,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !absent {
		t.Fatal("authoritative NotFound was not accepted as absence")
	}
}

func TestTenantDeletePriorityHonorsControllerAndStorageDependencies(t *testing.T) {
	kinds := []string{
		"Cluster",
		"Deployment",
		"Pod",
		"PersistentVolumeClaim",
		"PersistentVolume",
		"StorageClass",
		"ClusterRole",
		"CustomResourceDefinition",
		"Namespace",
	}
	for index := 1; index < len(kinds); index++ {
		if tenantDeletePriority(kinds[index-1]) >= tenantDeletePriority(kinds[index]) {
			t.Fatalf("delete priority does not order %s before %s", kinds[index-1], kinds[index])
		}
	}
}

func TestPartialFinalizationExactDeletesRecordedClusterResourceSet(t *testing.T) {
	scheme := testScheme(t)
	gvk := schema.GroupVersionKind{Group: "addons.cluster.x-k8s.io", Version: "v1beta2", Kind: "ClusterResourceSet"}
	scheme.AddKnownTypeWithName(gvk, &unstructured.Unstructured{})
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	now := metav1.Now()
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{
			Name:              "tenant-a",
			UID:               "tenant-uid",
			Finalizers:        []string{tenantFinalizer},
			DeletionTimestamp: &now,
		},
		Status: tenancyv1alpha1.TenantStatus{
			Stage: tenancyv1alpha1.StageReady,
			Teardown: &tenancyv1alpha1.TeardownStatus{
				Authority: "LiveBootstrapRBACCleanupComplete",
				Phase:     "LiveBootstrapRBACCleanupComplete",
			},
		},
	}
	resourceSet := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": gvk.GroupVersion().String(),
		"kind":       gvk.Kind,
		"metadata": map[string]any{
			"name":      "tenant-a-network",
			"namespace": "tenant-a",
			"uid":       "resource-set-uid",
			"labels": map[string]any{
				foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
			},
			"annotations": map[string]any{
				resources.TenantAnnotation:     tenant.Name,
				resources.TenantUIDAnnotation:  string(tenant.UID),
				resources.SpecHashAnnotation:   "spec-hash",
				resources.FoundationAnnotation: foundation.Hash,
				resources.ResourceAnnotation:   "network-resource-set",
			},
		},
		"spec": map[string]any{"strategy": "ApplyOnce"},
	}}
	tenant.Status.ObservedResources = []tenancyv1alpha1.ObservedResourceIdentity{identityFor(resourceSet)}
	kubernetes := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(tenant).
		WithObjects(tenant, resourceSet).
		Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}

	if _, err := reconciler.finalizePartial(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var current unstructured.Unstructured
	current.SetGroupVersionKind(gvk)
	err := kubernetes.Get(context.Background(), client.ObjectKey{Namespace: "tenant-a", Name: "tenant-a-network"}, &current)
	if !apierrors.IsNotFound(err) {
		t.Fatalf("recorded ClusterResourceSet was not exact-deleted: %v", err)
	}
}

func TestDeletionPreflightRejectsForeignVolumeBeforeTenantAPIMutation(t *testing.T) {
	scheme := testScheme(t)
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	now := metav1.Now()
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{
			Name:              "tenant-a",
			UID:               "tenant-uid",
			Generation:        2,
			Finalizers:        []string{tenantFinalizer},
			DeletionTimestamp: &now,
		},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
		Status: tenancyv1alpha1.TenantStatus{
			Stage: tenancyv1alpha1.StageTenantAPICleanupRequired,
			Teardown: &tenancyv1alpha1.TeardownStatus{
				Authority:  "TenantAPICleanupRequired",
				ClusterUID: "cluster-uid",
			},
		},
	}
	resourceContext := resources.Context{
		Tenant: tenant,
		Spec: validation.CanonicalSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
		SpecHash:       "spec-hash",
		FoundationHash: foundation.Hash,
		Endpoint:       "172.18.255.1:6443",
		Inputs:         foundation.ResourceInputs(),
	}
	namespace := resources.Namespace(resourceContext)
	namespace.UID = types.UID("namespace-uid")
	cluster, err := resources.Cluster(resourceContext)
	if err != nil {
		t.Fatal(err)
	}
	cluster.SetUID(types.UID("cluster-uid"))
	devCluster, err := resources.DevCluster(resourceContext)
	if err != nil {
		t.Fatal(err)
	}
	devCluster.SetUID(types.UID("dev-cluster-uid"))
	devCluster.SetOwnerReferences([]metav1.OwnerReference{providerOwner(cluster)})
	controlPlane, err := resources.KamajiControlPlane(resourceContext)
	if err != nil {
		t.Fatal(err)
	}
	controlPlane.SetUID(types.UID("control-plane-uid"))
	controlPlane.SetOwnerReferences([]metav1.OwnerReference{providerOwner(cluster)})
	secret := &corev1.Secret{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      tenant.Name + "-kubeconfig",
			Namespace: tenant.Name,
			UID:       "secret-uid",
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: controlPlaneGVK.GroupVersion().String(),
				Kind:       controlPlaneGVK.Kind,
				Name:       tenant.Name,
				UID:        controlPlane.GetUID(),
			}},
		},
		Type: corev1.SecretType("cluster.x-k8s.io/secret"),
		Data: map[string][]byte{"value": []byte("kubeconfig")},
	}
	for _, object := range []client.Object{namespace, cluster, devCluster, controlPlane, secret} {
		if value, ok := object.(*corev1.Secret); ok {
			tenant.Status.ObservedResources = append(tenant.Status.ObservedResources, kubeconfigSecretIdentity(value))
		} else {
			tenant.Status.ObservedResources = append(tenant.Status.ObservedResources, identityFor(object))
		}
	}
	allocation := endpointConfigMap(t, foundation, tenant, "spec-hash")
	kubernetes := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(tenant).
		WithObjects(tenant, namespace, cluster, devCluster, controlPlane, secret, allocation).
		Build()
	volumeName := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
	docker := &fakeDockerClient{volumes: map[string]DockerVolume{
		volumeName: {
			Name:       volumeName,
			CreatedAt:  "now",
			Mountpoint: "/foreign",
			Labels:     map[string]string{"foreign": "true"},
		},
	}}
	factory := &recordingTenantFactory{}
	reconciler := &TenantReconciler{
		Client:        kubernetes,
		APIReader:     kubernetes,
		Docker:        docker,
		TenantClients: factory,
	}
	_, err = reconciler.finalizePartial(context.Background(), tenant, "spec-hash", foundation)
	if err == nil || !strings.Contains(err.Error(), "Docker volume ownership") {
		t.Fatalf("unexpected preflight result: %v", err)
	}
	if factory.called {
		t.Fatal("tenant API client was constructed before ownership preflight completed")
	}
}

func providerOwner(object client.Object) metav1.OwnerReference {
	gvk := object.GetObjectKind().GroupVersionKind()
	return metav1.OwnerReference{
		APIVersion: gvk.GroupVersion().String(),
		Kind:       gvk.Kind,
		Name:       object.GetName(),
		UID:        object.GetUID(),
	}
}
