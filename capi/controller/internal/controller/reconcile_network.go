package controller

import (
	"context"
	"fmt"
	"os"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcileNetwork(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (ctrl.Result, error) {
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	calico, err := os.ReadFile("/assets/calico.yaml")
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("read staged Calico asset: %w", err)
	}
	images, err := networkImages(foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	bundle, err := resources.BuildNetwork(resourceContext, calico, images)
	if err != nil {
		return ctrl.Result{}, err
	}
	applied, err := ensureTenantObjects(
		ctx,
		tenantClient,
		bundle.Objects,
		tenant,
		specHash,
		foundation.Hash,
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	if applied.Created || applied.Pending {
		return progressRequeue(), nil
	}
	return reconciler.reconcileStorage(ctx, tenantClient, tenant, canonical, specHash, foundation)
}
