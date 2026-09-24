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

type recordingDeleteClient struct {
	client.Client
	deleteCalls int
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

func (tenantClient *recordingDeleteClient) Delete(ctx context.Context, object client.Object, options ...client.DeleteOption) error {
	tenantClient.deleteCalls++
	return tenantClient.Client.Delete(ctx, object, options...)
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

func TestTenantResourceCleanupWaitsForAlreadyTerminatingObject(t *testing.T) {
	gvk := corev1.SchemeGroupVersion.WithKind("ConfigMap")
	now := metav1.Now()
	object := markedTenantConfigMap(gvk, &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
	}, "value")
	object.SetUID("config-uid")
	object.SetDeletionTimestamp(&now)
	object.SetFinalizers([]string{"example/finalizer"})
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
		Status: tenancyv1alpha1.TenantStatus{
			TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
				APIVersion: gvk.GroupVersion().String(),
				Kind:       gvk.Kind,
				Namespace:  object.GetNamespace(),
				Name:       object.GetName(),
				UID:        string(object.GetUID()),
			}},
		},
	}
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(object).Build()
	tenantClient := &recordingDeleteClient{Client: base}
	absent, err := deleteTenantResources(
		context.Background(),
		tenantClient,
		tenant,
		"spec-hash",
		"foundation-hash",
		false,
	)
	if err != nil {
		t.Fatal(err)
	}
	if absent {
		t.Fatal("terminating object was reported absent")
	}
	if tenantClient.deleteCalls != 0 {
		t.Fatal("terminating object received a redundant delete")
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

func TestLiveCleanupCheckpointRequiresExactClusterUID(t *testing.T) {
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a"},
		Status: tenancyv1alpha1.TenantStatus{
			ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{{
				APIVersion: clusterGVK.GroupVersion().String(), Kind: clusterGVK.Kind,
				Namespace: "tenant-a", Name: "tenant-a", UID: "cluster-uid",
			}},
			Teardown: &tenancyv1alpha1.TeardownStatus{},
		},
	}
	for name, clusterUID := range map[string]string{
		"missing": "",
		"stale":   "other-uid",
		"exact":   "cluster-uid",
	} {
		t.Run(name, func(t *testing.T) {
			tenant.Status.Teardown.ClusterUID = clusterUID
			err := validateManagementCleanupCheckpoint(tenant)
			if name == "exact" && err != nil {
				t.Fatal(err)
			}
			if name != "exact" && err == nil {
				t.Fatal("invalid cleanup checkpoint was accepted")
			}
		})
	}
}

func TestUnavailableTenantAPICheckpointAcceptsAuthoritativeClusterAbsence(t *testing.T) {
	scheme := testScheme(t)
	tenant := validTenant("tenant-a")
	tenant.Status.ObservedResources = []tenancyv1alpha1.ObservedResourceIdentity{{
		APIVersion: clusterGVK.GroupVersion().String(),
		Kind:       clusterGVK.Kind,
		Namespace:  tenant.Name,
		Name:       tenant.Name,
		UID:        "cluster-uid",
	}}
	kubernetes := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(tenant).
		WithObjects(tenant).
		Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}
	if _, err := reconciler.checkpointTenantAPICleanupUnavailable(
		context.Background(),
		tenant,
		tenancyv1alpha1.ObservedResourceIdentity{},
		false,
		errors.New("tenant API unavailable"),
	); err != nil {
		t.Fatal(err)
	}
	var current tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &current); err != nil {
		t.Fatal(err)
	}
	if current.Status.Teardown == nil ||
		current.Status.Teardown.Authority != tenantAPICleanupUnavailable ||
		current.Status.Teardown.ClusterUID != "cluster-uid" {
		t.Fatalf("authoritative Cluster absence was not checkpointed: %#v", current.Status.Teardown)
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

func TestUnavailableTenantAPICheckpointsAndContinuesManagementCleanup(t *testing.T) {
	scheme := testScheme(t)
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	now := metav1.Now()
	tenant := validTenant("tenant-a")
	tenant.UID = "tenant-uid"
	tenant.DeletionTimestamp = &now
	tenant.Finalizers = []string{tenantFinalizer}
	tenant.Status.Endpoint = "172.18.255.1:6443"
	tenant.Status.Stage = tenancyv1alpha1.StageTenantAPICleanupRequired
	tenant.Status.Teardown = &tenancyv1alpha1.TeardownStatus{Phase: "OwnershipPreflightComplete"}
	tenant.Status.TenantResources = []tenancyv1alpha1.ObservedResourceIdentity{{
		APIVersion: "v1", Kind: "ConfigMap", Namespace: "default",
		Name: "optional", UID: "optional-uid",
	}}
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
		Endpoint:       tenant.Status.Endpoint,
		Inputs:         foundation.ResourceInputs(),
	}
	namespace := resources.Namespace(resourceContext)
	namespace.UID = "namespace-uid"
	cluster, err := resources.Cluster(resourceContext)
	if err != nil {
		t.Fatal(err)
	}
	cluster.SetUID("cluster-uid")
	devCluster, err := resources.DevCluster(resourceContext)
	if err != nil {
		t.Fatal(err)
	}
	devCluster.SetUID("dev-cluster-uid")
	devCluster.SetOwnerReferences([]metav1.OwnerReference{providerOwner(cluster)})
	controlPlane, err := resources.KamajiControlPlane(resourceContext)
	if err != nil {
		t.Fatal(err)
	}
	controlPlane.SetUID("control-plane-uid")
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
	tenantClient := failingTenantClient{
		Client: fake.NewClientBuilder().WithScheme(scheme).Build(),
		err:    errors.New("connection refused"),
	}
	reconciler := &TenantReconciler{
		Client:        kubernetes,
		APIReader:     kubernetes,
		Docker:        &fakeDockerClient{volumes: map[string]DockerVolume{}},
		TenantClients: staticTenantFactory{client: tenantClient},
	}
	if _, err := reconciler.finalizePartial(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var checkpointed tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &checkpointed); err != nil {
		t.Fatal(err)
	}
	if checkpointed.Status.Teardown == nil ||
		checkpointed.Status.Teardown.Authority != tenantAPICleanupUnavailable ||
		checkpointed.Status.Teardown.ClusterUID != "cluster-uid" {
		t.Fatalf("Tenant API unavailability was not checkpointed: %#v", checkpointed.Status.Teardown)
	}
	var remainingCluster unstructured.Unstructured
	remainingCluster.SetGroupVersionKind(clusterGVK)
	managementCleanupStarted := false
	currentTenant := &checkpointed
	for attempt := 0; attempt < 5; attempt++ {
		if _, err := reconciler.finalizePartial(context.Background(), currentTenant, "spec-hash", foundation); err != nil {
			t.Fatal(err)
		}
		err = kubernetes.Get(context.Background(), client.ObjectKey{Namespace: tenant.Name, Name: tenant.Name}, &remainingCluster)
		if apierrors.IsNotFound(err) || err == nil && !remainingCluster.GetDeletionTimestamp().IsZero() {
			managementCleanupStarted = true
			break
		}
		if err != nil {
			t.Fatalf("inspect hosted Cluster after Tenant API fallback: %v", err)
		}
		if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, currentTenant); err != nil {
			t.Fatal(err)
		}
	}
	if !managementCleanupStarted {
		t.Fatal("management cleanup did not continue after Tenant API fallback")
	}
	var remainingTenant tenancyv1alpha1.Tenant
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &remainingTenant); err != nil {
		t.Fatal(err)
	}
	if !containsString(remainingTenant.Finalizers, tenantFinalizer) {
		t.Fatal("Tenant finalizer was removed before provider and host cleanup")
	}
	if err == nil && !remainingCluster.GetDeletionTimestamp().IsZero() {
		remainingCluster.SetFinalizers(nil)
		if err := kubernetes.Update(context.Background(), &remainingCluster); err != nil && !apierrors.IsNotFound(err) {
			t.Fatal(err)
		}
	}
	for attempt := 0; attempt < 20; attempt++ {
		var deletingTenant tenancyv1alpha1.Tenant
		err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &deletingTenant)
		if apierrors.IsNotFound(err) {
			var allocations corev1.ConfigMap
			if err := kubernetes.Get(context.Background(), client.ObjectKey{
				Namespace: defaultFoundationNamespace,
				Name:      allocationConfigMapName,
			}, &allocations); err != nil {
				t.Fatal(err)
			}
			state, err := decodeAllocationState(allocations.Data["allocations.json"], foundation)
			if err != nil {
				t.Fatal(err)
			}
			for _, allocation := range state.Allocations {
				if allocation.TenantUID == string(tenant.UID) {
					t.Fatal("endpoint allocation remained after fallback finalization")
				}
			}
			return
		}
		if err != nil {
			t.Fatal(err)
		}
		if _, err := reconciler.finalizePartial(context.Background(), &deletingTenant, "spec-hash", foundation); err != nil {
			t.Fatal(err)
		}
	}
	t.Fatal("Tenant API fallback did not complete finalization")
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
