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

func TestEmptyFoundationDeletionRefusesOrphanWorkerContainer(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker: &fakeDockerClient{
			volumes: map[string]DockerVolume{},
			workers: []DockerContainer{{
				Name: "tenant-a-worker-orphan",
				ID:   "container-uid",
			}},
		},
	}
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err == nil {
		t.Fatal("orphan worker container did not block empty-foundation deletion")
	}
	var current tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &current); err != nil {
		t.Fatal(err)
	}
	if !containsString(current.Finalizers, tenantFinalizer) {
		t.Fatal("orphan worker container allowed finalizer removal")
	}
}

func TestEmptyFoundationDeletionRecordsOwnedMachineResidue(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	machine := markedManagementObject(
		machineGVK,
		tenant,
		foundation,
		"machine",
		"tenant-a-worker-one",
		"machine-uid",
	)
	machine.SetLabels(map[string]string{
		foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
		"cluster.x-k8s.io/cluster-name":  tenant.Name,
	})
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, machine).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var current tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &current); err != nil {
		t.Fatal(err)
	}
	if current.Status.FoundationHash != foundation.Hash ||
		!containsString(current.Finalizers, tenantFinalizer) {
		t.Fatalf("owned Machine residue was not retained for cleanup: %#v", current.Status)
	}
}

func TestEmptyFoundationDeletionRefusesOrphanDevMachine(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	devMachine := markedManagementObject(
		postCNIDevMachineGVK,
		tenant,
		foundation,
		"machine",
		"tenant-a-worker-one",
		"devmachine-uid",
	)
	devMachine.SetLabels(map[string]string{
		foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
		"cluster.x-k8s.io/cluster-name":  tenant.Name,
	})
	devMachine.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion: machineGVK.GroupVersion().String(),
		Kind:       machineGVK.Kind,
		Name:       devMachine.GetName(),
		UID:        "missing-machine",
	}})
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, devMachine).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err == nil {
		t.Fatal("orphan DevMachine did not block empty-foundation deletion")
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

func TestKubeconfigOwnershipMismatchBlocksTenantCleanup(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	tenant.Status.TenantAPICreationAuthorized = true
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
				UID:        "foreign-control-plane",
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
		Client:        kubernetes,
		APIReader:     kubernetes,
		Docker:        &fakeDockerClient{volumes: map[string]DockerVolume{}},
		TenantClients: staticClientFactory{client: kubernetes},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err == nil {
		t.Fatal("foreign kubeconfig Secret owner was accepted")
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(clusterGVK)
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(cluster), current); err != nil {
		t.Fatalf("Cluster was deleted before kubeconfig ownership validation: %v", err)
	}
}

func TestManagementChildDeletionRefusesWrongProviderOwner(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	controlPlane := markedManagementObject(
		controlPlaneGVK,
		tenant,
		foundation,
		"kamaji-control-plane",
		tenant.Name,
		"control-plane-uid",
	)
	controlPlane.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion: clusterGVK.GroupVersion().String(),
		Kind:       clusterGVK.Kind,
		Name:       tenant.Name,
		UID:        "foreign-cluster",
	}})
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithObjects(controlPlane).
		Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}
	if _, err := reconciler.deleteExactUnstructured(
		context.Background(),
		tenant,
		"spec-hash",
		foundation,
		controlPlaneGVK,
		tenant.Name,
		tenant.Name,
		"kamaji-control-plane",
	); err == nil {
		t.Fatal("wrong-owner management child was deleted")
	}

	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(controlPlaneGVK)
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(controlPlane), current); err != nil {
		t.Fatalf("wrong-owner management child was not preserved: %v", err)
	}
}

func TestManagementChildDeletionAcceptsRecordedDanglingClusterOwner(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	controlPlane := markedManagementObject(
		controlPlaneGVK,
		tenant,
		foundation,
		"kamaji-control-plane",
		tenant.Name,
		"control-plane-uid",
	)
	controlPlane.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion: clusterGVK.GroupVersion().String(),
		Kind:       clusterGVK.Kind,
		Name:       tenant.Name,
		UID:        types.UID(tenant.Status.ClusterUID),
	}})
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithObjects(controlPlane).
		Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}
	absent, err := reconciler.deleteExactUnstructured(
		context.Background(),
		tenant,
		"spec-hash",
		foundation,
		controlPlaneGVK,
		tenant.Name,
		tenant.Name,
		"kamaji-control-plane",
	)
	if err != nil || absent {
		t.Fatalf("recorded dangling owner was not accepted: absent=%v err=%v", absent, err)
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(controlPlaneGVK)
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(controlPlane), current); !apierrors.IsNotFound(err) {
		t.Fatalf("management child remained after validated deletion: %v", err)
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
		machineGVK,
		postCNIDevMachineGVK,
	} {
		scheme.AddKnownTypeWithName(gvk, &unstructured.Unstructured{})
		scheme.AddKnownTypeWithName(
			gvk.GroupVersion().WithKind(gvk.Kind+"List"),
			&unstructured.UnstructuredList{},
		)
	}
	return scheme
}

type staticClientFactory struct {
	client client.Client
}

func (factory staticClientFactory) ClientFor([]byte, string) (client.Client, error) {
	return factory.client, nil
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
