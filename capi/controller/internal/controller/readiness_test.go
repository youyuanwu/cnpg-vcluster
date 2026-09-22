package controller

import (
	"fmt"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func TestPostCNIWorkerStateKeepsOnlyCurrentDescendants(t *testing.T) {
	status := tenancyv1alpha1.TenantStatus{
		ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{{
			APIVersion: postCNIDevMachineGVK.GroupVersion().String(),
			Kind:       postCNIDevMachineGVK.Kind,
			Namespace:  "tenant-a",
			Name:       "worker-old",
			UID:        "devmachine-old",
		}},
		TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
			APIVersion: postCNINodeGVK.GroupVersion().String(),
			Kind:       postCNINodeGVK.Kind,
			Name:       "worker-old",
			UID:        "node-old",
		}},
	}
	state := postCNIWorkerState{
		devMachines: []*unstructured.Unstructured{
			workerObject(postCNIDevMachineGVK, "tenant-a", "worker-new", "devmachine-new"),
		},
		nodes: []*unstructured.Unstructured{
			workerObject(postCNINodeGVK, "", "worker-new", "node-new"),
		},
		containers: []DockerContainer{{Name: "worker-new", ID: "container-new"}},
	}
	recordPostCNIWorkerState(&status, state)

	if len(status.ObservedResources) != 1 || status.ObservedResources[0].UID != "devmachine-new" {
		t.Fatalf("current DevMachine identity was not recorded: %#v", status.ObservedResources)
	}
	if len(status.TenantResources) != 1 || status.TenantResources[0].UID != "node-new" {
		t.Fatalf("current Node identity was not recorded: %#v", status.TenantResources)
	}
	if len(status.WorkerContainers) != 1 || status.WorkerContainers[0].ID != "container-new" {
		t.Fatalf("current worker container was not recorded: %#v", status.WorkerContainers)
	}
}

func TestWorkerContainerReferencesAreStableAndCurrent(t *testing.T) {
	references := workerContainerReferences([]DockerContainer{
		{Name: "worker-b", ID: "container-b"},
		{Name: "worker-a", ID: "container-a"},
	})
	if len(references) != 2 ||
		references[0].Name != "worker-a" ||
		references[1].ID != "container-b" {
		t.Fatalf("unexpected worker references: %#v", references)
	}
}

func TestWorkerTopologyOwnershipErrorsAreClassified(t *testing.T) {
	err := fmt.Errorf("%w: DevMachine worker-b owner does not match an exact Machine", errWorkerOwnershipInvalid)
	if !isOwnershipError(err) {
		t.Fatal("worker topology mismatch was not classified as OwnershipInvalid")
	}
}

func workerObject(gvk schema.GroupVersionKind, namespace, name, uid string) *unstructured.Unstructured {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(gvk)
	object.SetNamespace(namespace)
	object.SetName(name)
	object.SetUID(types.UID(uid))
	return object
}
