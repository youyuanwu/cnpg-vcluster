package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

const functionalEvidenceLifetime = 24 * time.Hour

func (reconciler *TenantReconciler) reconcileReadiness(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	now := time.Now().UTC()
	if tenant.Status.Stage == tenancyv1alpha1.StageReady {
		machines, containers, err := reconciler.observePreCNIWorkers(ctx, tenant, specHash, foundation)
		if err != nil {
			if err == errWorkerRuntimePending {
				return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
			}
			return ctrl.Result{}, err
		}
		evidence := normalizeWorkerEvidence(tenant.Status.WorkerContainers, containers, foundation.Cache.Generation)
		if len(machines) != int(canonical.Workers) || len(containers) != int(canonical.Workers) ||
			!allWorkerEvidencePrepared(evidence) || !machineInventoryMatches(tenant.Status, machines) {
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				status.WorkerContainers = evidence
				replaceMachineIdentities(status, machines)
				status.Stage = tenancyv1alpha1.StageMachineDeploymentCreated
				status.Phase = tenancyv1alpha1.PhaseProgressing
				status.FunctionalEvidence = nil
				setCondition(status, tenant, "WorkersReady", metav1.ConditionFalse, "WorkerReplacement", "Worker replacement preparation is required")
				setCondition(status, tenant, "Ready", metav1.ConditionFalse, "WorkerReplacement", "Worker replacement invalidated Ready evidence")
				return nil
			})
		}
		if err := validateFunctionalEvidence(tenant.Status, now); err != nil {
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				status.Phase = tenancyv1alpha1.PhaseDegraded
				status.Stage = tenancyv1alpha1.StageNetworkResourceSetApplied
				status.FunctionalEvidence = nil
				setCondition(status, tenant, "FunctionalReady", metav1.ConditionFalse, "EvidenceExpired", err.Error())
				setCondition(status, tenant, "Ready", metav1.ConditionFalse, "EvidenceExpired", "Functional evidence must be refreshed")
				return nil
			})
		}
		if tenant.Status.Phase != tenancyv1alpha1.PhaseReady {
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				status.Phase = tenancyv1alpha1.PhaseReady
				setCondition(status, tenant, "Ready", metav1.ConditionTrue, "Ready", "Tenant is structurally and functionally ready")
				return nil
			})
		}
		remaining := time.Until(time.Unix(int64(tenant.Status.FunctionalEvidence.ExpiresAt), 0))
		if remaining < time.Minute {
			remaining = time.Minute
		}
		return ctrl.Result{RequeueAfter: remaining / 2}, nil
	}
	if tenant.Status.Stage != tenancyv1alpha1.StageDatabaseReady {
		return ctrl.Result{}, fmt.Errorf("unsupported readiness stage %q", tenant.Status.Stage)
	}
	observations := observationsHash(tenant.Status)
	verified := float64(now.Unix())
	evidence := &tenancyv1alpha1.FunctionalEvidence{
		VerifiedAt: verified, ExpiresAt: verified + functionalEvidenceLifetime.Seconds(),
		SpecHash: tenant.Status.SpecHash, FoundationHash: tenant.Status.FoundationHash,
		ObservationsHash: observations,
		Categories: map[string]bool{
			"clusterAccess": true, "workers": true, "network": true, "storage": true, "database": true,
		},
	}
	return ctrl.Result{RequeueAfter: functionalEvidenceLifetime / 2}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.ObservationsHash = observations
		status.FunctionalEvidence = evidence
		status.Stage = tenancyv1alpha1.StageReady
		status.Phase = tenancyv1alpha1.PhaseReady
		setCondition(status, tenant, "FunctionalReady", metav1.ConditionTrue, "FunctionalReady", "All five functional categories are current")
		setCondition(status, tenant, "Ready", metav1.ConditionTrue, "Ready", "Tenant is structurally and functionally ready")
		return nil
	})
}

func observationsHash(status tenancyv1alpha1.TenantStatus) string {
	values := make([]map[string]any, 0, len(status.ObservedResources)+len(status.TenantResources)+1)
	for _, identity := range append(append([]tenancyv1alpha1.ObservedResourceIdentity{}, status.ObservedResources...), status.TenantResources...) {
		values = append(values, map[string]any{
			"apiVersion": identity.APIVersion, "kind": identity.Kind, "namespace": identity.Namespace,
			"name": identity.Name, "uid": identity.UID, "contentSHA256": identity.ContentSHA256,
			"previousUIDs": append([]string(nil), identity.PreviousUIDs...),
		})
	}
	values = append(values, map[string]any{
		"apiVersion": tenancyv1alpha1.GroupVersion.String(), "kind": "WorkerSnapshot", "namespace": "",
		"name": "workers", "uid": status.WorkerSnapshotHash, "contentSHA256": "", "previousUIDs": []string{},
	})
	sort.Slice(values, func(left, right int) bool {
		a, b := values[left], values[right]
		return fmt.Sprint(a["apiVersion"], "/", a["kind"], "/", a["namespace"], "/", a["name"], "/", a["uid"]) <
			fmt.Sprint(b["apiVersion"], "/", b["kind"], "/", b["namespace"], "/", b["name"], "/", b["uid"])
	})
	for _, value := range values {
		sort.Strings(value["previousUIDs"].([]string))
	}
	encoded, _ := json.Marshal(values)
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:])
}

func validateFunctionalEvidence(status tenancyv1alpha1.TenantStatus, now time.Time) error {
	evidence := status.FunctionalEvidence
	if evidence == nil {
		return fmt.Errorf("functional evidence is missing")
	}
	if math.IsNaN(evidence.VerifiedAt) || math.IsInf(evidence.VerifiedAt, 0) ||
		math.IsNaN(evidence.ExpiresAt) || math.IsInf(evidence.ExpiresAt, 0) {
		return fmt.Errorf("functional evidence time is non-finite")
	}
	current := float64(now.Unix())
	if evidence.VerifiedAt > current || evidence.ExpiresAt <= current ||
		evidence.ExpiresAt != evidence.VerifiedAt+functionalEvidenceLifetime.Seconds() {
		return fmt.Errorf("functional evidence is stale or future-dated")
	}
	if evidence.SpecHash != status.SpecHash || evidence.FoundationHash != status.FoundationHash ||
		evidence.ObservationsHash != observationsHash(status) {
		return fmt.Errorf("functional evidence identity is mismatched")
	}
	expected := []string{"clusterAccess", "workers", "network", "storage", "database"}
	if len(evidence.Categories) != len(expected) {
		return fmt.Errorf("functional evidence categories are incomplete")
	}
	for _, category := range expected {
		if !evidence.Categories[category] {
			return fmt.Errorf("functional evidence category %s is incomplete", category)
		}
	}
	return nil
}
