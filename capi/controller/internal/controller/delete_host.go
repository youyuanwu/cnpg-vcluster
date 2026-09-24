package controller

import (
	"context"
	"fmt"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func (reconciler *TenantReconciler) deleteTenantHostState(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	specHash string,
	foundation Foundation,
) (bool, error) {
	containers, err := reconciler.docker().ListWorkerContainers(ctx, tenant.Name)
	if err != nil {
		return false, fmt.Errorf("inspect provider-owned worker containers: %w", err)
	}
	if len(containers) != 0 {
		return false, nil
	}
	volumeName := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
	volume, err := reconciler.docker().InspectVolume(ctx, volumeName)
	if err != nil {
		return false, err
	}
	if volume != nil {
		expected := map[string]string{
			foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
			"cnpg-vcluster.capi/role":                  "tenant-storage",
			"cnpg-vcluster.capi/tenant":                tenant.Name,
			"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
			"tenancy.cnpg-vcluster.io/spec-hash":       specHash,
			"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
		}
		if volume.Name != volumeName || !stringMapEqual(volume.Labels, expected) {
			return false, fmt.Errorf("Docker volume ownership changed before cleanup")
		}
		if err := reconciler.docker().RemoveVolume(ctx, volume.Name); err != nil {
			return false, err
		}
		return false, nil
	}
	return true, nil
}
