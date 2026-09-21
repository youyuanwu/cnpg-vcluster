package controller

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/sanitize"
)

func (reconciler *TenantReconciler) patchStatus(ctx context.Context, name string, mutate func(*tenancyv1alpha1.TenantStatus) error) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var current tenancyv1alpha1.Tenant
		if err := reconciler.reader().Get(ctx, types.NamespacedName{Name: name}, &current); err != nil {
			return err
		}
		updated := current.DeepCopy()
		if err := mutate(&updated.Status); err != nil {
			return err
		}
		if equality.Semantic.DeepEqual(current.Status, updated.Status) {
			return nil
		}
		if err := reconciler.Status().Patch(ctx, updated, client.MergeFrom(&current)); err != nil {
			return fmt.Errorf("%s", sanitize.Text(err.Error()))
		}
		return nil
	})
}

func setCondition(status *tenancyv1alpha1.TenantStatus, tenant *tenancyv1alpha1.Tenant, conditionType string, conditionStatus metav1.ConditionStatus, reason, message string) {
	meta.SetStatusCondition(&status.Conditions, metav1.Condition{
		Type:               conditionType,
		Status:             conditionStatus,
		ObservedGeneration: tenant.Generation,
		Reason:             reason,
		Message:            sanitize.Text(message),
	})
}
