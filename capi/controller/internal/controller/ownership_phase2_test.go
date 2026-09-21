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
	status := tenancyv1alpha1.TenantStatus{ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{{
		APIVersion: machineGVK.GroupVersion().String(),
		Kind:       machineGVK.Kind,
		Namespace:  "tenant-a",
		Name:       "tenant-a-worker-old",
		UID:        "old-machine-uid",
	}}}
	replacement := &unstructured.Unstructured{}
	replacement.SetGroupVersionKind(machineGVK)
	replacement.SetNamespace("tenant-a")
	replacement.SetName("tenant-a-worker-new")
	replacement.SetUID(types.UID("new-machine-uid"))
	replaceMachineIdentities(&status, []*unstructured.Unstructured{replacement})
	if len(status.ObservedResources) != 1 ||
		status.ObservedResources[0].UID != "new-machine-uid" ||
		len(status.ObservedResources[0].PreviousUIDs) != 1 ||
		status.ObservedResources[0].PreviousUIDs[0] != "old-machine-uid" {
		t.Fatalf("replacement history was not retained: %#v", status.ObservedResources)
	}
}
