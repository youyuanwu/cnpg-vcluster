package controller

import (
	"fmt"
	"testing"
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
