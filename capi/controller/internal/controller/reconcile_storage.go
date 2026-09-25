package controller

import (
	"context"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

const tenantStorageClass = "capi-hostpath"

func (reconciler *TenantReconciler) reconcileStorage(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (ctrl.Result, error) {
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	applied, err := ensureTenantObjects(ctx, tenantClient, resources.StorageObjects(resourceContext, tenantStorageClass), tenant, specHash, foundation.Hash)
	if err != nil {
		return ctrl.Result{}, err
	}
	if applied.Created || applied.Pending {
		return progressRequeue(), nil
	}
	return reconciler.reconcileCNPG(ctx, tenantClient, tenant, canonical, specHash, foundation)
}
