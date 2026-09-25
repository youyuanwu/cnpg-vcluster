package controller

import (
	"context"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

const readyObservationInterval = 5 * time.Minute

func tenantHasReadinessObservation(tenant *tenancyv1alpha1.Tenant) bool {
	for _, condition := range tenant.Status.Conditions {
		if condition.Type == "DatabaseReady" &&
			condition.ObservedGeneration == tenant.Generation {
			return true
		}
	}
	return false
}

func (reconciler *TenantReconciler) reconcileReadiness(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (ctrl.Result, error) {
	controlPlaneReady, err := reconciler.managementObjectsCurrent(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	workers, err := reconciler.observePostCNIWorkerState(ctx, tenantClient, tenant, canonical, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	networkReady, err := networkStructurallyReady(ctx, tenantClient, int64(canonical.Workers))
	if err != nil {
		return ctrl.Result{}, err
	}
	storageReady, err := storageClassReady(ctx, tenantClient)
	if err != nil {
		return ctrl.Result{}, err
	}
	databaseReady, err := databaseStructurallyReady(ctx, tenantClient, canonical.DatabaseCount)
	if err != nil {
		return ctrl.Result{}, err
	}
	workersReady := workers.inventoryComplete && workers.allReady
	ready := controlPlaneReady && workersReady && networkReady && storageReady && databaseReady
	if !ready {
		return readinessRequeue(), reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Phase = tenancyv1alpha1.PhaseDegraded
			setReadyObservationConditions(status, tenant, controlPlaneReady, workersReady, networkReady, storageReady, databaseReady)
			setCondition(status, tenant, "Ready", metav1.ConditionFalse, "ComponentsNotReady", "One or more Tenant components are not ready")
			return nil
		})
	}
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.Phase = tenancyv1alpha1.PhaseReady
		setReadyObservationConditions(status, tenant, true, true, true, true, true)
		setCondition(status, tenant, "Ready", metav1.ConditionTrue, "Ready", "Tenant components are ready")
		return nil
	}); err != nil {
		return ctrl.Result{}, err
	}
	return readinessRequeue(), nil
}

func readinessRequeue() ctrl.Result {
	return ctrl.Result{RequeueAfter: readyObservationInterval}
}

func setReadyObservationConditions(status *tenancyv1alpha1.TenantStatus, tenant *tenancyv1alpha1.Tenant, controlPlane, workers, network, storage, database bool) {
	for _, value := range []struct {
		condition string
		ready     bool
	}{
		{"ControlPlaneReady", controlPlane},
		{"WorkersReady", workers},
		{"NetworkReady", network},
		{"StorageReady", storage},
		{"DatabaseReady", database},
	} {
		conditionStatus := metav1.ConditionFalse
		reason := "NotReady"
		message := value.condition + " is false"
		if value.ready {
			conditionStatus = metav1.ConditionTrue
			reason = value.condition
			message = value.condition + " is true"
		}
		setCondition(status, tenant, value.condition, conditionStatus, reason, message)
	}
}

func storageClassReady(ctx context.Context, tenantClient client.Client) (bool, error) {
	storageClass := &unstructured.Unstructured{}
	storageClass.SetGroupVersionKind(schema.GroupVersionKind{Group: "storage.k8s.io", Version: "v1", Kind: "StorageClass"})
	if err := tenantClient.Get(ctx, client.ObjectKey{Name: tenantStorageClass}, storageClass); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	provisioner, _, _ := unstructured.NestedString(storageClass.Object, "provisioner")
	return provisioner == "kubernetes.io/no-provisioner", nil
}
