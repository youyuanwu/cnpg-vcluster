package controller

import (
	"context"
	"errors"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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

func (factory *recordingTenantFactory) ClientFor([]byte, string) (client.Client, error) {
	factory.called = true
	return nil, errors.New("tenant client must not be constructed")
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
