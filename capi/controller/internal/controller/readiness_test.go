package controller

import (
	"math"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func readyStatus(now time.Time) tenancyv1alpha1.TenantStatus {
	status := tenancyv1alpha1.TenantStatus{
		SpecHash: "spec", FoundationHash: "foundation", WorkerSnapshotHash: strings.Repeat("a", 64),
		ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{{
			APIVersion: "v1", Kind: "Namespace", Name: "tenant-a", UID: "namespace-uid",
		}},
		TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
			APIVersion: "v1", Kind: "Node", Name: "worker-a", UID: "node-uid",
		}},
	}
	verified := float64(now.Add(-time.Hour).Unix())
	status.ObservationsHash = observationsHash(status)
	status.FunctionalEvidence = &tenancyv1alpha1.FunctionalEvidence{
		VerifiedAt: verified, ExpiresAt: verified + functionalEvidenceLifetime.Seconds(),
		SpecHash: status.SpecHash, FoundationHash: status.FoundationHash, ObservationsHash: status.ObservationsHash,
		Categories: map[string]bool{
			"clusterAccess": true, "workers": true, "network": true, "storage": true, "database": true,
		},
	}
	return status
}

func TestFunctionalEvidenceFreshnessAndIdentity(t *testing.T) {
	now := time.Unix(2_000_000_000, 0)
	status := readyStatus(now)
	if err := validateFunctionalEvidence(status, now); err != nil {
		t.Fatal(err)
	}
	status.FunctionalEvidence.VerifiedAt = float64(now.Unix()) + 1
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("future evidence was accepted")
	}
	status = readyStatus(now)
	status.FunctionalEvidence.VerifiedAt = math.NaN()
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("non-finite evidence was accepted")
	}
	status = readyStatus(now)
	status.FunctionalEvidence.ExpiresAt = float64(now.Unix())
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("expired evidence was accepted")
	}
	status = readyStatus(now)
	status.TenantResources[0].UID = "replacement"
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("mismatched observations were accepted")
	}
}

func TestFunctionalEvidenceRequiresExactCategories(t *testing.T) {
	now := time.Unix(2_000_000_000, 0)
	status := readyStatus(now)
	delete(status.FunctionalEvidence.Categories, "database")
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("incomplete functional categories were accepted")
	}
	status = readyStatus(now)
	status.FunctionalEvidence.Categories["database"] = false
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("failed functional category was accepted")
	}
}

func TestPostCNIWorkerStateRetainsDescendantReplacementHistory(t *testing.T) {
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
	devMachine := workerObject(postCNIDevMachineGVK, "tenant-a", "worker-new", "devmachine-new")
	node := workerObject(postCNINodeGVK, "", "worker-new", "node-new")
	state := postCNIWorkerState{
		devMachines:       []*unstructured.Unstructured{devMachine},
		nodes:             []*unstructured.Unstructured{node},
		inventoryComplete: true,
		snapshotHash:      strings.Repeat("b", 64),
	}
	recordPostCNIWorkerState(&status, state)

	if len(status.ObservedResources) != 1 ||
		len(status.ObservedResources[0].PreviousUIDs) != 1 ||
		status.ObservedResources[0].PreviousUIDs[0] != "devmachine-old" {
		t.Fatalf("DevMachine replacement history was not retained: %#v", status.ObservedResources)
	}
	if len(status.TenantResources) != 1 ||
		len(status.TenantResources[0].PreviousUIDs) != 1 ||
		status.TenantResources[0].PreviousUIDs[0] != "node-old" {
		t.Fatalf("Node replacement history was not retained: %#v", status.TenantResources)
	}
}

func TestPostCNIWorkerSnapshotIncludesProviderDescendants(t *testing.T) {
	state := postCNIWorkerState{
		devMachines: []*unstructured.Unstructured{
			workerObject(postCNIDevMachineGVK, "tenant-a", "worker-a", "devmachine-a"),
		},
		nodes: []*unstructured.Unstructured{
			workerObject(postCNINodeGVK, "", "worker-a", "node-a"),
		},
	}
	first := postCNIWorkerSnapshotHash(state)
	state.nodes[0].SetUID(types.UID("node-b"))
	second := postCNIWorkerSnapshotHash(state)
	if first == second {
		t.Fatal("Node replacement did not invalidate the worker snapshot")
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
