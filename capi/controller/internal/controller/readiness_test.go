package controller

import (
	"fmt"
	"testing"
	"time"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestWorkerTopologyOwnershipErrorsAreClassified(t *testing.T) {
	err := fmt.Errorf("%w: DevMachine worker-b owner does not match an exact Machine", errWorkerOwnershipInvalid)
	if !isOwnershipError(err) {
		t.Fatal("worker topology mismatch was not classified as OwnershipInvalid")
	}
}

func TestManagementReadinessSeparatesControlPlaneFromWorkers(t *testing.T) {
	controlPlane := managementReadinessConditionTypes("Cluster", false)
	if !containsString(controlPlane, "ControlPlaneAvailable") ||
		containsString(controlPlane, "Available") {
		t.Fatalf("unexpected control-plane readiness conditions: %v", controlPlane)
	}

	full := managementReadinessConditionTypes("Cluster", true)
	if len(full) != 1 || full[0] != "Available" {
		t.Fatalf("unexpected full Cluster readiness conditions: %v", full)
	}
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
