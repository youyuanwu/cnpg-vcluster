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

func TestMachineReplacementRecordsOnlyCurrentIdentities(t *testing.T) {
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
		status.ObservedResources[1].Name != "worker-c" ||
		status.ObservedResources[1].UID != "uid-c" {
		t.Fatalf("current replacement identities were not recorded: %#v", status.ObservedResources)
	}
}
