package controller

import (
	"context"
	"fmt"
	"testing"
	"time"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
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

func TestManagementClusterReadinessValidatesRootIdentity(t *testing.T) {
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	tenant := validTenant("tenant-a")
	tenant.UID = "tenant-uid"
	tenant.Status.ClusterUID = "cluster-uid"
	cluster := managementClusterFixture(1, 1,
		map[string]any{"type": "Available", "status": "True", "observedGeneration": int64(1)},
	)
	cluster.SetUID("cluster-uid")
	cluster.SetLabels(map[string]string{foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix})
	cluster.SetAnnotations(map[string]string{
		resources.TenantAnnotation:     tenant.Name,
		resources.TenantUIDAnnotation:  string(tenant.UID),
		resources.SpecHashAnnotation:   "spec-hash",
		resources.FoundationAnnotation: foundation.Hash,
		resources.ResourceAnnotation:   "cluster",
	})
	scheme := testScheme(t)
	scheme.AddKnownTypeWithName(clusterGVK, &unstructured.Unstructured{})
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(cluster).Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}
	ready, err := reconciler.managementObjectsCurrent(context.Background(), tenant, "spec-hash", foundation)
	if err != nil || !ready {
		t.Fatalf("valid aggregate Cluster was rejected: ready=%t err=%v", ready, err)
	}

	tenant.Status.ClusterUID = "replacement-uid"
	if _, err := reconciler.managementObjectsCurrent(context.Background(), tenant, "spec-hash", foundation); err == nil || !isOwnershipError(err) {
		t.Fatalf("replacement Cluster UID was not rejected: %v", err)
	}

	tenant.Status.ClusterUID = "cluster-uid"
	cluster.SetAnnotations(map[string]string{resources.TenantAnnotation: "foreign"})
	if err := kubernetes.Update(context.Background(), cluster); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.managementObjectsCurrent(context.Background(), tenant, "spec-hash", foundation); err == nil || !isOwnershipError(err) {
		t.Fatalf("foreign Cluster markers were not rejected: %v", err)
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
