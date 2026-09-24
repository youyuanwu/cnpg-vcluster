package controller

import (
	"context"
	"errors"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

type noMatchClient struct {
	client.Client
}

func (value noMatchClient) Get(context.Context, client.ObjectKey, client.Object, ...client.GetOption) error {
	return &meta.NoResourceMatchError{
		PartialResource: schema.GroupVersionResource{
			Group:    "postgresql.cnpg.io",
			Version:  "v1",
			Resource: "clusters",
		},
	}
}

func TestCleanupCatalogMatchesDesiredCoordinates(t *testing.T) {
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	tenant := validTenant("tenant-a")
	tenant.UID = "tenant-uid"
	tenant.Status.Endpoint = "172.18.255.1:6443"
	canonical := validation.CanonicalSpec{
		KubernetesVersion: tenant.Spec.KubernetesVersion,
		Workers:           tenant.Spec.Workers,
		DatabaseCount:     tenant.Spec.DatabaseCount,
		PodCIDR:           tenant.Spec.PodCIDR,
		ServiceCIDR:       tenant.Spec.ServiceCIDR,
	}
	resourceContext := serviceResourceContext(tenant, canonical, "spec-hash", foundation)
	images, err := networkImages(foundation)
	if err != nil {
		t.Fatal(err)
	}
	calico := []byte(`
apiVersion: v1
kind: ConfigMap
metadata:
  name: calico-config
  namespace: kube-system
data:
  install-one: docker.io/example/calico_cni_image:v1
  install-two: docker.io/example/calico_cni_image:v1
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: calico-node
  namespace: kube-system
spec:
  template:
    spec:
      containers:
      - name: calico-node
        image: docker.io/example/calico_node_image:v1
        env:
        - name: CALICO_IPV4POOL_CIDR
          value: 10.0.0.0/16
      initContainers:
      - name: install-cni
        image: docker.io/example/calico_node_image:v1
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: calico-kube-controllers
  namespace: kube-system
spec:
  template:
    spec:
      containers:
      - name: controller
        image: docker.io/example/calico_kube_controllers_image:v1
`)
	network, err := resources.BuildNetwork(resourceContext, calico, images)
	if err != nil {
		t.Fatal(err)
	}
	controllerImage, _ := archiveByKey(foundation.Cache.ImageArchives, "CNPG_CONTROLLER_IMAGE")
	postgresImage, _ := archiveByKey(foundation.Cache.ImageArchives, "POSTGRES_IMAGE")
	cnpg := []byte(`
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cnpg-controller-manager
  namespace: cnpg-system
spec:
  template:
    spec:
      containers:
      - name: manager
        image: docker.io/example/cnpg_controller_image:v1
      - name: webhook
        image: docker.io/example/cnpg_controller_image:v1
`)
	operator, err := resources.CNPGOperator(resourceContext, cnpg, controllerImage.Tagged, controllerImage.Reference)
	if err != nil {
		t.Fatal(err)
	}
	desired := append([]*unstructured.Unstructured{}, network.Objects...)
	desired = append(desired, resources.StorageObjects(resourceContext, tenantStorageClass)...)
	desired = append(desired, operator...)
	desired = append(desired, resources.CNPGObjects(resourceContext, tenantStorageClass, postgresImage.Reference)...)
	catalog, err := tenantCleanupCatalog(calico, cnpg, canonical.DatabaseCount)
	if err != nil {
		t.Fatal(err)
	}
	desiredSet := map[string]string{}
	for _, object := range desired {
		key := object.GroupVersionKind().String() + "/" + object.GetNamespace() + "/" + object.GetName()
		desiredSet[key] = object.GetAnnotations()[resources.ResourceAnnotation]
	}
	catalogSet := map[string]string{}
	for _, coordinate := range catalog {
		key := coordinate.GVK.String() + "/" + coordinate.Namespace + "/" + coordinate.Name
		catalogSet[key] = coordinate.Resource
	}
	if len(desiredSet) != len(catalogSet) {
		t.Fatalf("desired/catalog size mismatch: desired=%d catalog=%d", len(desiredSet), len(catalogSet))
	}
	for key, resource := range desiredSet {
		if catalogSet[key] != resource {
			t.Fatalf("cleanup catalog mismatch for %s: desired=%q catalog=%q", key, resource, catalogSet[key])
		}
	}
}

func TestEnsureManagementObjectRefusesMissingRecordedRoot(t *testing.T) {
	tenant := validTenant("tenant-a")
	tenant.UID = "tenant-uid"
	tenant.Status.ClusterUID = "recorded-cluster"
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}
	desired := &unstructured.Unstructured{}
	desired.SetGroupVersionKind(clusterGVK)
	desired.SetNamespace(tenant.Name)
	desired.SetName(tenant.Name)
	if _, _, err := reconciler.ensureManagementObject(
		context.Background(),
		desired,
		tenant,
		"spec-hash",
		foundation,
		"cluster",
	); !errors.Is(err, errRootClusterMissing) {
		t.Fatalf("missing recorded root was not refused: %v", err)
	}
}

func TestValidateClusterUIDRejectsReplacement(t *testing.T) {
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a"},
		Status:     tenancyv1alpha1.TenantStatus{ClusterUID: "recorded"},
	}
	cluster := &unstructured.Unstructured{}
	cluster.SetUID("replacement")
	if err := validateClusterUID(tenant, cluster); err == nil {
		t.Fatal("replacement Cluster UID was accepted")
	}
}

func TestTenantCleanupTreatsRemovedCRDAsAuthoritativeAbsence(t *testing.T) {
	tenant := testTenant()
	absent, err := deleteTenantResources(
		context.Background(),
		noMatchClient{},
		tenant,
		"spec-hash",
		"foundation-hash",
		[]cleanupCoordinate{{
			GVK: schema.GroupVersionKind{
				Group:   "postgresql.cnpg.io",
				Version: "v1",
				Kind:    "Cluster",
			},
			Namespace: "database",
			Name:      "capi-postgres",
			Resource:  "cnpg",
		}},
	)
	if err != nil || !absent {
		t.Fatalf("removed CRD was not treated as absence: absent=%v err=%v", absent, err)
	}
}
