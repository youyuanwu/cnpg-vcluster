package controller

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func TestMutationReconcileAddsFinalizerBeforeEndpoint(t *testing.T) {
	scheme := testScheme(t)
	tenant := validTenant("tenant-a")
	foundation := testFoundation()
	kubernetes := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(tenant).
		WithObjects(tenant, foundationConfigMap(t, foundation)).
		Build()
	docker := &fakeDockerClient{
		container: DockerContainer{
			ID:       foundation.ManagementContainerID,
			State:    "running",
			Labels:   foundation.ManagementLabels,
			Networks: map[string]string{"kind": foundation.NetworkID},
		},
		network:    DockerNetwork{ID: foundation.NetworkID, Subnets: []string{foundation.Subnet}},
		execResult: DockerExecResult{Output: "{\"generation\":\"generation\",\"schema\":1}\n"},
	}
	reconciler := &TenantReconciler{
		Client:                  kubernetes,
		APIReader:               kubernetes,
		Docker:                  docker,
		SupportedVersion:        "1.36.4",
		MutationEnabled:         true,
		ExpectedControllerImage: foundation.ControllerImage,
	}
	request := ctrl.Request{NamespacedName: types.NamespacedName{Name: tenant.Name}}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	var afterFinalizer tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &afterFinalizer); err != nil {
		t.Fatal(err)
	}
	if !containsString(afterFinalizer.Finalizers, tenantFinalizer) {
		t.Fatal("mutation reconcile did not persist the finalizer first")
	}
	var allocations corev1.ConfigMap
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Namespace: defaultFoundationNamespace, Name: allocationConfigMapName}, &allocations); client.IgnoreNotFound(err) != nil {
		t.Fatal(err)
	} else if err == nil {
		t.Fatal("endpoint allocation preceded finalizer persistence")
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	var afterEndpoint tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &afterEndpoint); err != nil {
		t.Fatal(err)
	}
	if afterEndpoint.Status.Stage != tenancyv1alpha1.StageEndpointAllocated ||
		afterEndpoint.Status.Endpoint == "" {
		t.Fatalf("endpoint stage did not persist: %#v", afterEndpoint.Status)
	}
}

func TestDeletionAdoptsClusterApplyBeforeStatus(t *testing.T) {
	scheme := testScheme(t)
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	now := metav1.Now()
	tenant := validTenant("tenant-a")
	tenant.DeletionTimestamp = &now
	tenant.Finalizers = []string{tenantFinalizer}
	tenant.Status.Stage = tenancyv1alpha1.StageClusterCreationAuthorized
	tenant.Status.Teardown = &tenancyv1alpha1.TeardownStatus{Authority: "TenantAPINeverAuthorized"}
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
	namespace.UID = "namespace-uid"
	cluster, err := resources.Cluster(resourceContext)
	if err != nil {
		t.Fatal(err)
	}
	cluster.SetUID("cluster-uid")
	allocation := endpointConfigMap(t, foundation, tenant, "spec-hash")
	kubernetes := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(tenant).
		WithObjects(tenant, namespace, cluster, allocation).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	result, err := reconciler.finalizePartial(context.Background(), tenant, "spec-hash", foundation)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Requeue {
		t.Fatal("crash-gap adoption did not persist before deletion")
	}
	var updated tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &updated); err != nil {
		t.Fatal(err)
	}
	if findIdentity(updated.Status, clusterGVK, tenant.Name, tenant.Name) == nil ||
		findIdentity(updated.Status, corev1.SchemeGroupVersion.WithKind("Namespace"), "", tenant.Name) == nil {
		t.Fatalf("crash-gap resources were not adopted: %#v", updated.Status.ObservedResources)
	}
	var liveCluster = cluster.DeepCopy()
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Namespace: tenant.Name, Name: tenant.Name}, liveCluster); err != nil {
		t.Fatal("Cluster was deleted before adoption status persisted")
	}
}

func TestValidationOnlyModeContinuesManagedDeletion(t *testing.T) {
	scheme := testScheme(t)
	foundation := testFoundation()
	foundation.MutationEnabled = false
	now := metav1.Now()
	tenant := validTenant("tenant-a")
	tenant.DeletionTimestamp = &now
	tenant.Finalizers = []string{tenantFinalizer}
	tenant.Status.Stage = tenancyv1alpha1.StageEndpointReleased
	tenant.Status.Teardown = &tenancyv1alpha1.TeardownStatus{
		Authority: "TenantAPINeverAuthorized",
		Phase:     deletionLockReleased,
	}
	foundationObject := foundationConfigMap(t, foundation)
	foundation.Hash = foundationObject.Data["foundation.sha256"]
	objects := foundationSnapshotObjects(t, scheme)
	allocationState := newAllocationState(foundation)
	encodedAllocations, err := encodeAllocationState(allocationState)
	if err != nil {
		t.Fatal(err)
	}
	objects = append(objects,
		tenant,
		foundationObject,
		&corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Name: allocationConfigMapName, Namespace: defaultFoundationNamespace},
			Data:       map[string]string{"allocations.json": encodedAllocations},
		},
	)
	kubernetes := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(tenant).
		WithObjects(objects...).
		Build()
	docker := &fakeDockerClient{
		container: DockerContainer{
			ID:       foundation.ManagementContainerID,
			State:    "running",
			Labels:   foundation.ManagementLabels,
			Networks: map[string]string{"kind": foundation.NetworkID},
		},
		network:    DockerNetwork{ID: foundation.NetworkID, Subnets: []string{foundation.Subnet}},
		execResult: DockerExecResult{Output: "{\"generation\":\"generation\",\"schema\":1}\n"},
		volumes:    map[string]DockerVolume{},
	}
	reconciler := &TenantReconciler{
		Client:                  kubernetes,
		APIReader:               kubernetes,
		Docker:                  docker,
		SupportedVersion:        "1.36.4",
		MutationEnabled:         false,
		ExpectedControllerImage: foundation.ControllerImage,
	}
	resourceIdentities, resourceHash, err := reconciler.foundationResourceSnapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	peerHash, err := hashJSON(map[string]tenancyv1alpha1.EndpointAllocationIdentity{})
	if err != nil {
		t.Fatal(err)
	}
	var snapshotted tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &snapshotted); err != nil {
		t.Fatal(err)
	}
	snapshotted.Status.FoundationSnapshot = &tenancyv1alpha1.FoundationSnapshot{
		FoundationHash: foundation.Hash, ManagementContainerID: foundation.ManagementContainerID,
		NetworkID: foundation.NetworkID, ControllerImage: foundation.ControllerImage,
		ResourceHash: resourceHash, PeerAllocationsHash: peerHash,
		Resources: resourceIdentities, PeerAllocations: map[string]tenancyv1alpha1.EndpointAllocationIdentity{},
	}
	if err := kubernetes.Status().Update(context.Background(), &snapshotted); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Name: tenant.Name}}); err != nil {
		t.Fatal(err)
	}
	var updated tenancyv1alpha1.Tenant
	err = kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &updated)
	if client.IgnoreNotFound(err) != nil {
		t.Fatal(err)
	}
	if err == nil && containsString(updated.Finalizers, tenantFinalizer) {
		t.Fatal("validation-only mode stranded an existing managed deletion")
	}
}
