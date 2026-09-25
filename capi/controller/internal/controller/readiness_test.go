package controller

import (
	"fmt"
	"testing"
	"time"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

func TestWorkerTopologyOwnershipErrorsAreClassified(t *testing.T) {
	err := fmt.Errorf("%w: DevMachine worker-b owner does not match an exact Machine", errWorkerOwnershipInvalid)
	if !isOwnershipError(err) {
		t.Fatal("worker topology mismatch was not classified as OwnershipInvalid")
	}
}

func TestManagementReadinessUsesCurrentAggregateClusterConditions(t *testing.T) {
	cluster := managementClusterFixture(2, 2,
		map[string]any{"type": "ControlPlaneReady", "status": "False", "observedGeneration": int64(2)},
		map[string]any{"type": "ControlPlaneAvailable", "status": "True", "observedGeneration": int64(2)},
	)
	ready, err := managementConditionsReady(cluster, "ControlPlaneReady", "ControlPlaneAvailable")
	if err != nil || !ready {
		t.Fatalf("current affirmative alternative was rejected: ready=%t err=%v", ready, err)
	}
	ready, err = managementConditionsReady(cluster, "Available")
	if err != nil || ready {
		t.Fatalf("control-plane condition satisfied final availability: ready=%t err=%v", ready, err)
	}

	cluster = managementClusterFixture(2, 2,
		map[string]any{"type": "Available", "status": "True", "observedGeneration": int64(2)},
	)
	ready, err = managementConditionsReady(cluster, "Available")
	if err != nil || !ready {
		t.Fatalf("current Cluster availability was rejected: ready=%t err=%v", ready, err)
	}

	cluster = managementClusterFixture(2, 1,
		map[string]any{"type": "Available", "status": "True", "observedGeneration": int64(2)},
	)
	ready, err = managementConditionsReady(cluster, "Available")
	if err != nil || ready {
		t.Fatalf("stale top-level observation was accepted: ready=%t err=%v", ready, err)
	}

	cluster = managementClusterFixture(2, 2,
		map[string]any{"type": "Available", "status": "True", "observedGeneration": "invalid"},
	)
	if _, err := managementConditionsReady(cluster, "Available"); err == nil {
		t.Fatal("malformed condition generation was accepted")
	}
}

func managementClusterFixture(generation, observedGeneration int64, conditions ...map[string]any) *unstructured.Unstructured {
	raw := make([]any, 0, len(conditions))
	for _, condition := range conditions {
		raw = append(raw, condition)
	}
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": clusterGVK.GroupVersion().String(),
		"kind":       clusterGVK.Kind,
		"metadata": map[string]any{
			"name":       "tenant-a",
			"namespace":  "tenant-a",
			"generation": generation,
		},
		"status": map[string]any{
			"observedGeneration": observedGeneration,
			"conditions":         raw,
		},
	}}
}

func TestReadyAndDegradedTenantsUseBoundedResync(t *testing.T) {
	result := readinessRequeue()
	if result.RequeueAfter != 5*time.Minute || result.Requeue {
		t.Fatalf("unexpected readiness resync result: %#v", result)
	}
}

func TestEstablishedTenantRecoveryRemainsDegraded(t *testing.T) {
	tenant := testTenant()
	tenant.Generation = 2
	tenant.Status.ObservedGeneration = 2
	tenant.Status.Phase = tenancyv1alpha1.PhaseReady
	tenant.Status.Conditions = []metav1.Condition{{
		Type:               "DatabaseReady",
		Status:             metav1.ConditionTrue,
		ObservedGeneration: 2,
	}}
	status := tenant.Status
	setReconcileProgressStatus(&status, tenant)
	tenant.Status = status
	if tenant.Status.Phase != tenancyv1alpha1.PhaseDegraded {
		t.Fatalf("established recovery became %s", tenant.Status.Phase)
	}
}
