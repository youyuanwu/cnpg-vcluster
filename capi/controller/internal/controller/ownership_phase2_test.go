package controller

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func TestProviderOwnerMustMatchRecordedRoot(t *testing.T) {
	status := tenancyv1alpha1.TenantStatus{ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{{
		APIVersion: clusterGVK.GroupVersion().String(),
		Kind:       clusterGVK.Kind,
		Namespace:  "tenant-a",
		Name:       "tenant-a",
		UID:        "cluster-uid",
	}}}
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(devClusterGVK)
	object.SetNamespace("tenant-a")
	object.SetName("tenant-a")
	object.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion: clusterGVK.GroupVersion().String(),
		Kind:       clusterGVK.Kind,
		Name:       "tenant-a",
		UID:        types.UID("foreign-uid"),
	}})
	if err := validateProviderOwner(object, status, true); err == nil {
		t.Fatal("wrong provider owner UID was accepted")
	}
	object.SetOwnerReferences(nil)
	if err := validateProviderOwner(object, status, true); err == nil {
		t.Fatal("missing required provider owner was accepted")
	}
	object.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion: clusterGVK.GroupVersion().String(),
		Kind:       clusterGVK.Kind,
		Name:       "tenant-a",
		UID:        types.UID("cluster-uid"),
	}})
	if err := validateProviderOwner(object, status, true); err != nil {
		t.Fatal(err)
	}
}

func TestMachineReplacementRetainsPreviousIdentity(t *testing.T) {
	status := tenancyv1alpha1.TenantStatus{ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{
		{APIVersion: machineGVK.GroupVersion().String(), Kind: machineGVK.Kind, Namespace: "tenant-a", Name: "worker-a", UID: "uid-a"},
		{APIVersion: machineGVK.GroupVersion().String(), Kind: machineGVK.Kind, Namespace: "tenant-a", Name: "worker-b", UID: "uid-b"},
	}}
	survivor := &unstructured.Unstructured{}
	survivor.SetGroupVersionKind(machineGVK)
	survivor.SetNamespace("tenant-a")
	survivor.SetName("worker-b")
	survivor.SetUID(types.UID("uid-b"))
	replacement := &unstructured.Unstructured{}
	replacement.SetGroupVersionKind(machineGVK)
	replacement.SetNamespace("tenant-a")
	replacement.SetName("worker-c")
	replacement.SetUID(types.UID("uid-c"))
	replaceMachineIdentities(&status, []*unstructured.Unstructured{survivor, replacement})
	if len(status.ObservedResources) != 2 ||
		status.ObservedResources[0].Name != "worker-b" ||
		len(status.ObservedResources[0].PreviousUIDs) != 0 ||
		status.ObservedResources[1].Name != "worker-c" ||
		len(status.ObservedResources[1].PreviousUIDs) != 1 ||
		status.ObservedResources[1].PreviousUIDs[0] != "uid-a" {
		t.Fatalf("replacement history was not retained: %#v", status.ObservedResources)
	}
}

func TestWorkerEvidenceReplacementRetiresStaleNames(t *testing.T) {
	values := []tenancyv1alpha1.WorkerContainerEvidence{
		{Name: "worker-a", ID: "container-a", CacheGeneration: "generation", Prepared: true},
		{Name: "worker-b", ID: "container-b", CacheGeneration: "generation", Prepared: true},
	}
	containers := []DockerContainer{
		{Name: "worker-b", ID: "container-b"},
		{Name: "worker-c", ID: "container-c"},
	}
	normalized := normalizeWorkerEvidence(values, containers, "generation")
	if len(normalized) != 2 || normalized[0].Name != "worker-b" ||
		normalized[1].Name != "worker-c" ||
		len(normalized[1].PreviousIDs) != 1 ||
		normalized[1].PreviousIDs[0] != "container-a" ||
		normalized[1].Prepared {
		t.Fatalf("worker replacement evidence is invalid: %#v", normalized)
	}
}
