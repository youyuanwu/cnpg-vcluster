package controller

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/sanitize"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

// +kubebuilder:rbac:groups=tenancy.cnpg-vcluster.io,resources=tenants,verbs=get;list;watch
// +kubebuilder:rbac:groups=tenancy.cnpg-vcluster.io,resources=tenants/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=tenancy.cnpg-vcluster.io,resources=tenants/finalizers,verbs=update
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch;update

type TenantReconciler struct {
	client.Client
	SupportedVersion string
	MutationEnabled  bool
}

func (reconciler *TenantReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	var tenant tenancyv1alpha1.Tenant
	if err := reconciler.Get(ctx, types.NamespacedName{Name: request.Name}, &tenant); err != nil {
		if ignored := client.IgnoreNotFound(err); ignored != nil {
			return ctrl.Result{}, fmt.Errorf("%s", sanitize.Text(ignored.Error()))
		}
		return ctrl.Result{}, nil
	}
	_, specHash, validationErr := validation.Validate(tenant.Name, tenant.Spec, reconciler.SupportedVersion)
	updated := tenant.DeepCopy()
	updated.Status.ObservedGeneration = tenant.Generation
	updated.Status.SpecHash = specHash
	if validationErr != nil {
		updated.Status.Phase = tenancyv1alpha1.PhaseFailed
		meta.SetStatusCondition(&updated.Status.Conditions, metav1.Condition{
			Type:               "Accepted",
			Status:             metav1.ConditionFalse,
			ObservedGeneration: tenant.Generation,
			Reason:             "InvalidSpec",
			Message:            sanitize.Text(validationErr.Error()),
		})
		meta.SetStatusCondition(&updated.Status.Conditions, metav1.Condition{
			Type:               "Ready",
			Status:             metav1.ConditionFalse,
			ObservedGeneration: tenant.Generation,
			Reason:             "InvalidSpec",
			Message:            "Tenant specification is invalid",
		})
	} else {
		updated.Status.Phase = tenancyv1alpha1.PhaseProgressing
		meta.SetStatusCondition(&updated.Status.Conditions, metav1.Condition{
			Type:               "Accepted",
			Status:             metav1.ConditionTrue,
			ObservedGeneration: tenant.Generation,
			Reason:             "Accepted",
			Message:            "Tenant specification is accepted",
		})
		reason := "MutationDisabled"
		message := "Tenant controller mutation is disabled until the clean cutover"
		if reconciler.MutationEnabled {
			reason = "ImplementationPending"
			message = "Tenant mutation stages are not implemented in this phase"
		}
		meta.SetStatusCondition(&updated.Status.Conditions, metav1.Condition{
			Type:               "Ready",
			Status:             metav1.ConditionFalse,
			ObservedGeneration: tenant.Generation,
			Reason:             reason,
			Message:            message,
		})
	}
	if equality.Semantic.DeepEqual(tenant.Status, updated.Status) {
		return ctrl.Result{}, nil
	}
	if err := reconciler.Status().Update(ctx, updated); err != nil {
		return ctrl.Result{}, fmt.Errorf("%s", sanitize.Text(err.Error()))
	}
	return ctrl.Result{}, nil
}

func (reconciler *TenantReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&tenancyv1alpha1.Tenant{}).
		Complete(reconciler)
}
