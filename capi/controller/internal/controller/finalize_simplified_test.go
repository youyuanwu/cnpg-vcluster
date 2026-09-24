package controller

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

func TestFinalizerOnlyDeletionCompletesWithoutFoundationStatus(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var current tenancyv1alpha1.Tenant
	err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &current)
	if err == nil && containsString(current.Finalizers, tenantFinalizer) {
		t.Fatal("finalizer-only Tenant remained blocked")
	}
	if client.IgnoreNotFound(err) != nil {
		t.Fatal(err)
	}
}

func TestTenantAPIFailureBlocksClusterDeletionBeforeCleanup(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	tenant.Status.TenantAPICreationAuthorized = true
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid")
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, cluster).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err == nil {
		t.Fatal("tenant API failure did not block finalization")
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(clusterGVK)
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(cluster), current); err != nil {
		t.Fatalf("Cluster was deleted before tenant cleanup: %v", err)
	}
}

func TestCleanupCheckpointPermitsClusterDeletionWithoutTenantAPI(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	tenant.Status.TenantAPICreationAuthorized = true
	tenant.Status.TenantCleanupClusterUID = tenant.Status.ClusterUID
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid")
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, cluster).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(clusterGVK)
	err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(cluster), current)
	if !apierrors.IsNotFound(err) {
		t.Fatalf("Cluster remained after successful cleanup checkpoint: %v", err)
	}
}

func TestClusterOnlyPartialCreationIsDeletedWithoutTenantAPI(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid")
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, cluster).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var refreshed tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &refreshed); err != nil {
		t.Fatal(err)
	}
	if refreshed.Status.ClusterUID != "cluster-uid" {
		t.Fatalf("Cluster UID was not recovered: %q", refreshed.Status.ClusterUID)
	}
	if _, err := reconciler.finalizeTenant(context.Background(), &refreshed, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(clusterGVK)
	err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(cluster), current)
	if !apierrors.IsNotFound(err) {
		t.Fatalf("Cluster-only partial creation remained: %v", err)
	}
}

func TestEndpointReleaseCrashWindowCompletes(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.Endpoint = "172.18.255.1:6443"
	tenant.Status.ClusterUID = "cluster-uid"
	tenant.Status.TenantAPICreationAuthorized = true
	tenant.Status.TenantCleanupClusterUID = tenant.Status.ClusterUID
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	state := newAllocationState(foundation)
	encoded, err := encodeAllocationState(state)
	if err != nil {
		t.Fatal(err)
	}
	allocations := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      allocationConfigMapName,
			Namespace: defaultFoundationNamespace,
		},
		Data: map[string]string{"allocations.json": encoded},
	}
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, allocations).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var refreshed tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &refreshed); err != nil {
		t.Fatal(err)
	}
	if refreshed.Status.Endpoint != "" {
		t.Fatalf("released endpoint was not cleared: %q", refreshed.Status.Endpoint)
	}
	if _, err := reconciler.finalizeTenant(context.Background(), &refreshed, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	err = kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &refreshed)
	if err == nil && containsString(refreshed.Finalizers, tenantFinalizer) {
		t.Fatal("endpoint crash-window Tenant remained blocked")
	}
	if client.IgnoreNotFound(err) != nil {
		t.Fatal(err)
	}
}

func TestEndpointReleaseCrashWindowCompletesWithoutAllocationConfigMap(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.Endpoint = "172.18.255.1:6443"
	tenant.Status.ClusterUID = "cluster-uid"
	tenant.Status.TenantAPICreationAuthorized = true
	tenant.Status.TenantCleanupClusterUID = tenant.Status.ClusterUID
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var refreshed tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &refreshed); err != nil {
		t.Fatal(err)
	}
	if refreshed.Status.Endpoint != "" {
		t.Fatalf("released endpoint was not cleared: %q", refreshed.Status.Endpoint)
	}
}

func TestKubeconfigIsDeletedBeforeManagementRoots(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	tenant.Status.TenantAPICreationAuthorized = true
	tenant.Status.TenantCleanupClusterUID = tenant.Status.ClusterUID
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid")
	controlPlane := markedManagementObject(controlPlaneGVK, tenant, foundation, "kamaji-control-plane", tenant.Name, "control-plane-uid")
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      tenant.Name + "-kubeconfig",
			Namespace: tenant.Name,
			UID:       "secret-uid",
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: controlPlaneGVK.GroupVersion().String(),
				Kind:       controlPlaneGVK.Kind,
				Name:       controlPlane.GetName(),
				UID:        controlPlane.GetUID(),
			}},
		},
		Type: corev1.SecretType("cluster.x-k8s.io/secret"),
		Data: map[string][]byte{"value": []byte("kubeconfig")},
	}
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, cluster, controlPlane, secret).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var currentSecret corev1.Secret
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(secret), &currentSecret); !apierrors.IsNotFound(err) {
		t.Fatalf("kubeconfig Secret remained: %v", err)
	}
	for _, object := range []*unstructured.Unstructured{cluster, controlPlane} {
		current := &unstructured.Unstructured{}
		current.SetGroupVersionKind(object.GroupVersionKind())
		if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(object), current); err != nil {
			t.Fatalf("%s was deleted before kubeconfig Secret: %v", object.GetKind(), err)
		}
	}
}

func deletingTenant(name string) *tenancyv1alpha1.Tenant {
	now := metav1.Now()
	tenant := validTenant(name)
	tenant.UID = "tenant-uid"
	tenant.Finalizers = []string{tenantFinalizer}
	tenant.DeletionTimestamp = &now
	return tenant
}

func simplifiedFinalizerScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := testScheme(t)
	for _, gvk := range []schema.GroupVersionKind{
		clusterGVK,
		devClusterGVK,
		controlPlaneGVK,
		kubeadmTemplateGVK,
		devMachineTemplateGVK,
		machineDeploymentGVK,
	} {
		scheme.AddKnownTypeWithName(gvk, &unstructured.Unstructured{})
	}
	return scheme
}

func markedManagementObject(gvk schema.GroupVersionKind, tenant *tenancyv1alpha1.Tenant, foundation Foundation, resource, name, uid string) *unstructured.Unstructured {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(gvk)
	object.SetNamespace(tenant.Name)
	object.SetName(name)
	object.SetUID(types.UID(uid))
	object.SetLabels(map[string]string{
		foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
	})
	object.SetAnnotations(map[string]string{
		resources.TenantAnnotation:     tenant.Name,
		resources.TenantUIDAnnotation:  string(tenant.UID),
		resources.SpecHashAnnotation:   "spec-hash",
		resources.FoundationAnnotation: foundation.Hash,
		resources.ResourceAnnotation:   resource,
	})
	return object
}
