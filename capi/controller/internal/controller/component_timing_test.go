package controller

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	types "k8s.io/apimachinery/pkg/types"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func TestComponentTimingTrackerTransitionsAndCompletes(t *testing.T) {
	tracker := &componentTimingTracker{}
	tenant := testTenant()
	tracker.transition(context.Background(), tenant, "control-plane")
	key := componentTimingKey(tenant.Name, tenant.UID)
	if tracker.current[key] != "control-plane" {
		t.Fatalf("component transition was not recorded: %v", tracker.current)
	}
	tracker.transition(context.Background(), tenant, "control-plane")
	if len(tracker.current) != 1 || tracker.current[key] != "control-plane" {
		t.Fatalf("duplicate transition changed tracker state: %v", tracker.current)
	}
	tracker.transition(context.Background(), tenant, "network")
	if tracker.current[key] != "network" {
		t.Fatalf("next component transition was not recorded: %v", tracker.current)
	}
	tracker.transition(context.Background(), tenant, "worker-image-preparation")
	if tracker.current[key] != "network" {
		t.Fatalf("regressive component transition was recorded: %v", tracker.current)
	}
	tracker.complete(context.Background(), tenant)
	if len(tracker.current) != 0 {
		t.Fatalf("completed Tenant timing remained tracked: %v", tracker.current)
	}
}

func TestComponentTimingTrackerClearsOnlyRequestedTenantName(t *testing.T) {
	tracker := &componentTimingTracker{}
	first := testTenant()
	second := testTenant()
	second.Name = "tenant-b"
	second.UID = types.UID("tenant-b-uid")
	tracker.transition(context.Background(), first, "network")
	tracker.transition(context.Background(), second, "database")
	tracker.clearName(first.Name)
	if _, present := tracker.current[componentTimingKey(first.Name, first.UID)]; present {
		t.Fatal("requested Tenant timing was not cleared")
	}
	if tracker.current[componentTimingKey(second.Name, second.UID)] != "database" {
		t.Fatalf("unrelated Tenant timing was cleared: %v", tracker.current)
	}
}

func TestComponentTimingTrackerDoesNotRestartForObservedTenant(t *testing.T) {
	tracker := &componentTimingTracker{}
	tenant := testTenant()
	tenant.Status.ObservedGeneration = tenant.Generation
	tenant.Status.Conditions = []metav1.Condition{{
		Type:               "DatabaseReady",
		Status:             metav1.ConditionTrue,
		ObservedGeneration: tenant.Generation,
	}}
	tenant.Status.Phase = tenancyv1alpha1.PhaseReady
	tracker.transition(context.Background(), tenant, "network")
	if len(tracker.current) != 0 {
		t.Fatalf("Ready Tenant restarted component timing: %v", tracker.current)
	}
	tenant.Status.Phase = tenancyv1alpha1.PhaseDegraded
	tracker.transition(context.Background(), tenant, "database")
	if len(tracker.current) != 0 {
		t.Fatalf("Degraded Tenant restarted component timing: %v", tracker.current)
	}
}
