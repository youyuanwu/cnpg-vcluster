package controller

import (
	"context"
	"fmt"
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
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

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

func TestEmptyFoundationDeletionRefusesUnprovenMachineResidue(t *testing.T) {
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
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err == nil {
		t.Fatal("Machine with no exact provider owner chain was accepted")
	}
	var current tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &current); err != nil {
		t.Fatal(err)
	}
	if !containsString(current.Finalizers, tenantFinalizer) {
		t.Fatalf("unproven Machine residue allowed finalizer removal: %#v", current.Status)
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

func TestTenantAPIFailureDoesNotBlockClusterDeletion(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid")
	controlPlane := markedManagementObject(controlPlaneGVK, tenant, foundation, "kamaji-control-plane", tenant.Name, "control-plane-uid")
	controlPlane.SetOwnerReferences([]metav1.OwnerReference{topologyOwner(cluster)})
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Name: tenant.Name + "-kubeconfig", Namespace: tenant.Name,
		OwnerReferences: []metav1.OwnerReference{topologyOwner(controlPlane)},
	}}
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, cluster, controlPlane, secret).
		Build()
	reconciler := &TenantReconciler{
		Client:        kubernetes,
		APIReader:     kubernetes,
		Docker:        &fakeDockerClient{volumes: map[string]DockerVolume{}},
		TenantClients: forbiddenTenantClientFactory{t: t},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(clusterGVK)
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(cluster), current); !apierrors.IsNotFound(err) {
		t.Fatalf("Cluster was not deleted without tenant API access: %v", err)
	}
}

func TestClusterReplacementBlocksDeletion(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "replacement-uid")
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
		t.Fatal("replacement Cluster UID was accepted")
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(clusterGVK)
	err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(cluster), current)
	if err != nil {
		t.Fatalf("replacement Cluster was deleted: %v", err)
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

func TestKubeconfigOwnershipMismatchBlocksClusterDeletion(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
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
		WithStatusSubresource(tenant).
		WithObjects(tenant, controlPlane).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err == nil {
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
		WithStatusSubresource(tenant).
		WithObjects(tenant, controlPlane).
		Build()
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatalf("recorded dangling owner was not accepted: %v", err)
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(controlPlaneGVK)
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(controlPlane), current); !apierrors.IsNotFound(err) {
		t.Fatalf("management child remained after validated deletion: %v", err)
	}
}

func TestPartialCreationDeletionCompletesAcrossRestarts(t *testing.T) {
	for stage := 0; stage <= 8; stage++ {
		t.Run(fmt.Sprintf("creation-stage-%d", stage), func(t *testing.T) {
			ctx := context.Background()
			tenant := deletingTenant("tenant-a")
			foundation := testFoundation()
			foundation.Hash = "foundation-hash"
			roots := []client.Object{
				ownedDeletionNamespace(tenant, foundation),
				markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid"),
				markedManagementObject(devClusterGVK, tenant, foundation, "dev-cluster", tenant.Name, "dev-cluster-uid"),
				markedManagementObject(controlPlaneGVK, tenant, foundation, "kamaji-control-plane", tenant.Name, "control-plane-uid"),
				markedManagementObject(kubeadmTemplateGVK, tenant, foundation, "kubeadm-config-template", tenant.Name+"-worker", "bootstrap-template-uid"),
				markedManagementObject(devMachineTemplateGVK, tenant, foundation, "dev-machine-template", tenant.Name+"-worker", "infrastructure-template-uid"),
				markedManagementObject(machineDeploymentGVK, tenant, foundation, "machine-deployment", tenant.Name+"-worker", "deployment-uid"),
			}
			objects := []client.Object{tenant}
			if stage > 0 {
				objects = append(objects, roots[:stage-1]...)
			}
			kubernetes := fake.NewClientBuilder().
				WithScheme(simplifiedFinalizerScheme(t)).
				WithStatusSubresource(tenant).
				WithObjects(objects...).
				Build()
			docker := &fakeDockerClient{volumes: map[string]DockerVolume{
				"peer-storage": {Name: "peer-storage"},
			}}
			if stage >= 5 {
				volume := ownedDeletionVolume(tenant, foundation)
				docker.volumes[volume.Name] = volume
			}
			peer := validTenant("tenant-b")
			peer.UID = "peer-uid"
			peerEndpoint, err := allocateEndpoint(ctx, kubernetes, kubernetes, defaultFoundationNamespace, foundation, peer, "peer-spec-hash")
			if err != nil {
				t.Fatal(err)
			}
			if stage > 0 {
				// Cover the crash after allocation but before endpoint/foundation
				// status has been saved.
				if _, err := allocateEndpoint(ctx, kubernetes, kubernetes, defaultFoundationNamespace, foundation, tenant, "spec-hash"); err != nil {
					t.Fatal(err)
				}
			}
			complete := false
			for attempt := 0; attempt < 24; attempt++ {
				var current tenancyv1alpha1.Tenant
				if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(tenant), &current); apierrors.IsNotFound(err) {
					complete = true
					break
				} else if err != nil {
					t.Fatal(err)
				}
				reconciler := &TenantReconciler{
					Client: kubernetes, APIReader: kubernetes, Docker: docker,
					TenantClients: forbiddenTenantClientFactory{t: t},
				}
				if _, err := reconciler.finalizeTenant(ctx, &current, "spec-hash", foundation); err != nil {
					t.Fatal(err)
				}
			}
			if !complete {
				t.Fatal("partial creation did not finalize across fresh reconciler instances")
			}
			if len(docker.volumes) != 1 || docker.volumes["peer-storage"].Name != "peer-storage" {
				t.Fatalf("target volume remains or peer volume was removed: %#v", docker.volumes)
			}
			observed, present, err := observeEndpoint(ctx, kubernetes, defaultFoundationNamespace, foundation, peer, "peer-spec-hash")
			if err != nil || !present || observed != peerEndpoint {
				t.Fatalf("peer endpoint changed: %q %v %v", observed, present, err)
			}
			if _, present, err := observeEndpoint(ctx, kubernetes, defaultFoundationNamespace, foundation, tenant, "spec-hash"); err != nil || present {
				t.Fatalf("target endpoint remains: %v %v", present, err)
			}
			for _, root := range objects[1:] {
				if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(root), root.DeepCopyObject().(client.Object)); !apierrors.IsNotFound(err) {
					t.Fatalf("partial management root %T remains: %v", root, err)
				}
			}
		})
	}
}

func TestDeletionWaitsForAllProviderDescendantsBeforeHostCleanup(t *testing.T) {
	for _, gvk := range deletionDescendantGVKs {
		t.Run(gvk.Kind, func(t *testing.T) {
			tenant := deletingTenant("tenant-a")
			tenant.Status.FoundationHash = "foundation-hash"
			tenant.Status.ClusterUID = "cluster-uid"
			foundation := testFoundation()
			foundation.Hash = tenant.Status.FoundationHash
			descendant := &unstructured.Unstructured{}
			descendant.SetGroupVersionKind(gvk)
			descendant.SetNamespace(tenant.Name)
			descendant.SetName("remaining-provider-object")
			descendant.SetUID("descendant-uid")
			namespace := ownedDeletionNamespace(tenant, foundation)
			volume := ownedDeletionVolume(tenant, foundation)
			docker := &fakeDockerClient{volumes: map[string]DockerVolume{volume.Name: volume}}
			kubernetes := fake.NewClientBuilder().
				WithScheme(simplifiedFinalizerScheme(t)).
				WithStatusSubresource(tenant).
				WithObjects(tenant, namespace, descendant).
				Build()
			reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes, Docker: docker}
			result, err := reconciler.finalizeTenant(context.Background(), tenant, "spec-hash", foundation)
			if err != nil || result.RequeueAfter == 0 {
				t.Fatalf("did not wait for provider descendant: %#v %v", result, err)
			}
			assertDeletionStateRetained(t, kubernetes, docker, tenant, namespace, volume.Name)
		})
	}
}

func TestFinalizationPreservesStateOnIdentityOrInspectionFailure(t *testing.T) {
	for _, failure := range []string{"volume-label", "volume-name", "namespace", "namespace-owner", "cluster", "cluster-owner", "foundation", "endpoint", "missing-endpoint", "docker", "management-list"} {
		t.Run(failure, func(t *testing.T) {
			ctx := context.Background()
			tenant := deletingTenant("tenant-a")
			tenant.Status.FoundationHash = "foundation-hash"
			tenant.Status.ClusterUID = "cluster-uid"
			foundation := testFoundation()
			foundation.Hash = tenant.Status.FoundationHash
			namespace := ownedDeletionNamespace(tenant, foundation)
			cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid")
			volume := ownedDeletionVolume(tenant, foundation)
			volumeName := volume.Name
			docker := &fakeDockerClient{volumes: map[string]DockerVolume{}}
			switch failure {
			case "volume-label":
				volume.Labels["tenancy.cnpg-vcluster.io/tenant-uid"] = "foreign-tenant"
			case "volume-name":
				volume.Name = "foreign-volume"
			case "namespace":
				namespace.Annotations[resources.TenantUIDAnnotation] = "foreign-tenant"
			case "namespace-owner":
				namespace.OwnerReferences = []metav1.OwnerReference{topologyOwner(cluster)}
			case "cluster":
				cluster.SetUID("replacement-cluster")
			case "cluster-owner":
				cluster.SetOwnerReferences([]metav1.OwnerReference{{
					APIVersion: "example.io/v1", Kind: "ForeignOwner", Name: "foreign", UID: "foreign",
				}})
			case "foundation":
				tenant.Status.FoundationHash = "old-foundation"
			case "docker":
				docker.err = fmt.Errorf("Docker inspection unavailable")
			}
			docker.volumes[volumeName] = volume
			kubernetes := fake.NewClientBuilder().
				WithScheme(simplifiedFinalizerScheme(t)).
				WithStatusSubresource(tenant).
				WithObjects(tenant, namespace, cluster).
				WithInterceptorFuncs(interceptor.Funcs{
					List: func(ctx context.Context, underlying client.WithWatch, list client.ObjectList, options ...client.ListOption) error {
						if failure == "management-list" {
							return fmt.Errorf("management API inspection unavailable")
						}
						return underlying.List(ctx, list, options...)
					},
				}).
				Build()
			if failure == "missing-endpoint" {
				tenant.Status.Endpoint = foundation.Endpoint(foundation.PoolStart)
			} else {
				hash := "spec-hash"
				if failure == "endpoint" {
					hash = "foreign-spec"
				}
				endpoint, err := allocateEndpoint(ctx, kubernetes, kubernetes, defaultFoundationNamespace, foundation, tenant, hash)
				if err != nil {
					t.Fatal(err)
				}
				tenant.Status.Endpoint = endpoint
			}
			reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes, Docker: docker}
			if _, err := reconciler.finalizeTenant(ctx, tenant, "spec-hash", foundation); err == nil {
				t.Fatal("identity or inspection failure did not block deletion")
			}
			assertDeletionStateRetained(t, kubernetes, docker, tenant, namespace, volumeName)
			if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(cluster), cluster); err != nil {
				t.Fatalf("Cluster was deleted on failed validation: %v", err)
			}
		})
	}
}

func TestWorkerContainersBlockVolumeRemoval(t *testing.T) {
	tenant := deletingTenant("tenant-a")
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	volume := ownedDeletionVolume(tenant, foundation)
	docker := &fakeDockerClient{
		volumes: map[string]DockerVolume{volume.Name: volume},
		workers: []DockerContainer{{Name: tenant.Name + "-worker-one", ID: "worker-id"}},
	}
	reconciler := &TenantReconciler{Docker: docker}
	absent, err := reconciler.deleteTenantHostState(context.Background(), tenant, "spec-hash", foundation)
	if err != nil || absent || len(docker.removed) != 0 {
		t.Fatalf("worker absence was not required before volume removal: %v %v %v", absent, err, docker.removed)
	}
}

func TestFinalizationUsesOrdinaryExactDeletionAndFinalizerLast(t *testing.T) {
	ctx := context.Background()
	tenant := deletingTenant("tenant-a")
	tenant.Status.FoundationHash = "foundation-hash"
	tenant.Status.ClusterUID = "cluster-uid"
	foundation := testFoundation()
	foundation.Hash = tenant.Status.FoundationHash
	namespace := ownedDeletionNamespace(tenant, foundation)
	cluster := markedManagementObject(clusterGVK, tenant, foundation, "cluster", tenant.Name, "cluster-uid")
	cluster.SetFinalizers([]string{"provider.example/finalizer"})
	volume := ownedDeletionVolume(tenant, foundation)
	docker := &fakeDockerClient{volumes: map[string]DockerVolume{volume.Name: volume}}
	var deleted []string
	kubernetes := fake.NewClientBuilder().
		WithScheme(simplifiedFinalizerScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, namespace, cluster).
		WithInterceptorFuncs(interceptor.Funcs{
			Delete: func(ctx context.Context, underlying client.WithWatch, object client.Object, options ...client.DeleteOption) error {
				combined := &client.DeleteOptions{}
				for _, option := range options {
					option.ApplyToDelete(combined)
				}
				if combined.Preconditions == nil || combined.Preconditions.UID == nil ||
					*combined.Preconditions.UID != object.GetUID() ||
					combined.Preconditions.ResourceVersion == nil ||
					*combined.Preconditions.ResourceVersion != object.GetResourceVersion() ||
					combined.PropagationPolicy == nil ||
					*combined.PropagationPolicy != metav1.DeletePropagationBackground {
					t.Fatalf("deletion lacks exact preconditions/background propagation: %#v", combined)
				}
				deleted = append(deleted, object.GetObjectKind().GroupVersionKind().Kind)
				return underlying.Delete(ctx, object, options...)
			},
		}).
		Build()
	endpoint, err := allocateEndpoint(ctx, kubernetes, kubernetes, defaultFoundationNamespace, foundation, tenant, "spec-hash")
	if err != nil {
		t.Fatal(err)
	}
	tenant.Status.Endpoint = endpoint
	if err := kubernetes.Status().Update(ctx, tenant); err != nil {
		t.Fatal(err)
	}
	reconciler := &TenantReconciler{
		Client: kubernetes, APIReader: kubernetes, Docker: docker,
		TenantClients: forbiddenTenantClientFactory{t: t},
	}
	for attempt := 0; attempt < 2; attempt++ {
		if _, err := reconciler.finalizeTenant(ctx, tenant, "spec-hash", foundation); err != nil {
			t.Fatal(err)
		}
		assertDeletionStateRetained(t, kubernetes, docker, tenant, namespace, volume.Name)
	}
	if len(deleted) != 1 || deleted[0] != "Cluster" {
		t.Fatalf("unexpected deletion before provider completion: %v", deleted)
	}
	if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(cluster), cluster); err != nil {
		t.Fatal(err)
	}
	if len(cluster.GetFinalizers()) != 1 || cluster.GetDeletionTimestamp().IsZero() {
		t.Fatal("Cluster deletion did not retain ordinary provider finalization")
	}
	// Simulate provider completion, not controller removal of provider finalizers.
	cluster.SetFinalizers(nil)
	if err := kubernetes.Update(ctx, cluster); err != nil {
		t.Fatal(err)
	}
	for attempt := 0; attempt < 3; attempt++ {
		if _, err := reconciler.finalizeTenant(ctx, tenant, "spec-hash", foundation); err != nil {
			t.Fatal(err)
		}
		if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(tenant), tenant); err != nil {
			t.Fatalf("Tenant disappeared before terminal status/finalizer step: %v", err)
		}
		switch attempt {
		case 0:
			if len(docker.removed) != 1 || tenant.Status.Endpoint == "" {
				t.Fatal("volume removal did not precede endpoint release")
			}
			if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(namespace), namespace); err != nil {
				t.Fatalf("Namespace removed before confirming volume absence: %v", err)
			}
		case 1:
			if tenant.Status.Endpoint == "" {
				t.Fatal("endpoint released before confirming Namespace absence")
			}
		case 2:
			if tenant.Status.Endpoint != "" || !containsString(tenant.Finalizers, tenantFinalizer) {
				t.Fatal("endpoint release and finalizer-last ordering was not preserved")
			}
		}
	}
	if len(deleted) != 2 || deleted[1] != "Namespace" {
		t.Fatalf("unexpected management deletes: %v", deleted)
	}
	if _, err := reconciler.finalizeTenant(ctx, tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	if err := kubernetes.Get(ctx, client.ObjectKeyFromObject(tenant), tenant); !apierrors.IsNotFound(err) {
		t.Fatalf("finalizer was not removed last: %v", err)
	}
}

func ownedDeletionNamespace(tenant *tenancyv1alpha1.Tenant, foundation Foundation) *corev1.Namespace {
	marked := markedManagementObject(corev1.SchemeGroupVersion.WithKind("Namespace"), tenant, foundation, "namespace", tenant.Name, "namespace-uid")
	return &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name: tenant.Name, UID: marked.GetUID(),
		Labels: marked.GetLabels(), Annotations: marked.GetAnnotations(),
	}}
}

func ownedDeletionVolume(tenant *tenancyv1alpha1.Tenant, foundation Foundation) DockerVolume {
	return DockerVolume{
		Name: foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage",
		Labels: map[string]string{
			foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
			"cnpg-vcluster.capi/role":                  "tenant-storage",
			"cnpg-vcluster.capi/tenant":                tenant.Name,
			"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
			"tenancy.cnpg-vcluster.io/spec-hash":       "spec-hash",
			"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
		},
	}
}

func assertDeletionStateRetained(t *testing.T, kubernetes client.Client, docker *fakeDockerClient, tenant *tenancyv1alpha1.Tenant, namespace *corev1.Namespace, volumeName string) {
	t.Helper()
	if len(docker.removed) != 0 {
		t.Fatalf("storage was removed before safe teardown: %v", docker.removed)
	}
	if _, present := docker.volumes[volumeName]; !present {
		t.Fatal("target storage volume disappeared")
	}
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(namespace), &corev1.Namespace{}); err != nil {
		t.Fatalf("Namespace disappeared before safe teardown: %v", err)
	}
	var current tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(tenant), &current); err != nil {
		t.Fatal(err)
	}
	if !containsString(current.Finalizers, tenantFinalizer) {
		t.Fatal("finalizer was removed before safe teardown")
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
	for _, gvk := range append([]schema.GroupVersionKind{
		clusterGVK,
		devClusterGVK,
		controlPlaneGVK,
		kubeadmTemplateGVK,
		devMachineTemplateGVK,
		machineDeploymentGVK,
	}, deletionDescendantGVKs...) {
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

type forbiddenTenantClientFactory struct {
	t *testing.T
}

func (factory forbiddenTenantClientFactory) ClientFor([]byte, string) (client.Client, error) {
	factory.t.Fatal("finalization must not construct a tenant API client")
	return nil, nil
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
