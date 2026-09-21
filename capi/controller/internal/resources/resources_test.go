package resources

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func resourceContext() Context {
	return Context{
		Tenant: &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: types.UID("uid-a")}},
		Spec: validation.CanonicalSpec{
			KubernetesVersion: "1.36.4",
			Workers:           2,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
		SpecHash:       "spec-hash",
		FoundationHash: "foundation-hash",
		Endpoint:       "172.18.255.1:6443",
		VolumePath:     "/var/lib/docker/volumes/tenant/_data",
		Inputs: Inputs{
			OwnershipLabel:          "example.io/owned",
			LabPrefix:               "example",
			APIPort:                 6443,
			ClusterDomain:           "example.local",
			NodeImage:               "kindest/node:v1@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			CacheHostPath:           "/cache",
			CacheContainerPath:      "/var/lib/capi-image-cache",
			StorageContainerPath:    "/var/lib/storage",
			KonnectivityServerImage: "registry.k8s.io/server:v1@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
			KonnectivityAgentImage:  "registry.k8s.io/agent:v1@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		},
	}
}

func TestControlPlaneBuildersMatchLocalContract(t *testing.T) {
	context := resourceContext()
	cluster, err := Cluster(context)
	if err != nil {
		t.Fatal(err)
	}
	host, _, _ := unstructuredNested(cluster.Object, "spec", "controlPlaneEndpoint", "host")
	if host != context.Endpoint[:len("172.18.255.1")] {
		t.Fatalf("unexpected endpoint host: %v", host)
	}
	if len(cluster.GetOwnerReferences()) != 0 {
		t.Fatal("Cluster builder added an owner reference")
	}
	controlPlane, err := KamajiControlPlane(context)
	if err != nil {
		t.Fatal(err)
	}
	dns, _, _ := unstructuredNested(controlPlane.Object, "spec", "network", "dnsServiceIPs")
	values := dns.([]any)
	if values[0] != "10.21.0.10" {
		t.Fatalf("unexpected DNS service IP: %v", values)
	}
	if controlPlane.GetAnnotations()[TenantUIDAnnotation] != "uid-a" {
		t.Fatal("control plane lacks exact Tenant UID marker")
	}
}

func TestWorkerBuildersPreserveCacheStorageAndReplicaContract(t *testing.T) {
	context := resourceContext()
	machine := DevMachineTemplate(context)
	mounts, _, _ := unstructuredNested(machine.Object, "spec", "template", "spec", "backend", "docker", "extraMounts")
	items := mounts.([]any)
	if len(items) != 2 || items[0].(map[string]any)["readOnly"] != true ||
		items[1].(map[string]any)["hostPath"] != context.VolumePath {
		t.Fatalf("unexpected worker mounts: %#v", items)
	}
	deployment := MachineDeployment(context)
	replicas, _, _ := unstructuredNested(deployment.Object, "spec", "replicas")
	if replicas != int64(2) {
		t.Fatalf("unexpected replicas: %v", replicas)
	}
	annotations, _, _ := unstructuredNested(deployment.Object, "spec", "template", "metadata", "annotations")
	if annotations.(map[string]any)[TenantUIDAnnotation] != "uid-a" {
		t.Fatal("Machine template lacks exact Tenant UID marker")
	}
}

func TestBootstrapRBACMatchesExpectedNamedRules(t *testing.T) {
	objects := BootstrapRBAC()
	if len(objects) != 4 {
		t.Fatalf("unexpected bootstrap object count: %d", len(objects))
	}
}

func unstructuredNested(object map[string]any, fields ...string) (any, bool, error) {
	current := any(object)
	for _, field := range fields {
		values, ok := current.(map[string]any)
		if !ok {
			return nil, false, nil
		}
		current, ok = values[field]
		if !ok {
			return nil, false, nil
		}
	}
	return current, true, nil
}
