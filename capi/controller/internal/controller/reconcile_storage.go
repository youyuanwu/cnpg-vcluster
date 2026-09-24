package controller

import (
	"context"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

const tenantStorageClass = "capi-hostpath"

func (reconciler *TenantReconciler) reconcileStorage(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	if tenant.Status.Stage != tenancyv1alpha1.StagePostCNIWorkersReady &&
		tenant.Status.Stage != tenancyv1alpha1.StageStorageApplied {
		return reconciler.reconcileCNPG(ctx, tenant, canonical, specHash, foundation)
	}
	tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
	if err != nil {
		return ctrl.Result{}, err
	}
	switch tenant.Status.Stage {
	case tenancyv1alpha1.StagePostCNIWorkersReady:
		resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
		for _, desired := range resources.StorageObjects(resourceContext, tenantStorageClass) {
			identity, changed, err := ensureTenantObject(ctx, tenantClient, desired, tenant, specHash, foundation.Hash)
			if err != nil {
				return ctrl.Result{}, err
			}
			if changed || !tenantIdentityPresent(tenant.Status.TenantResources, identity) {
				return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
					return upsertTenantIdentity(status, identity)
				})
			}
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageStorageApplied
			return nil
		})
	case tenancyv1alpha1.StageStorageApplied:
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageStorageReady
			setCondition(status, tenant, "StorageReady", metav1.ConditionTrue, "StorageReady", "Static storage class is ready")
			return nil
		})
	}
	return ctrl.Result{}, nil
}
