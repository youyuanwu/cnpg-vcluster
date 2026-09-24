package controller

import (
	"context"
	"fmt"
	"os"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcileStableDesiredObjects(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (bool, error) {
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	if tenant.Status.DockerVolume == nil {
		return false, fmt.Errorf("recorded Docker volume is missing")
	}
	resourceContext.VolumePath = tenant.Status.DockerVolume.Mountpoint
	commands, err := workerBootstrapCommands(foundation)
	if err != nil {
		return false, err
	}
	resourceContext.WorkerBootstrapCommands = commands
	cluster, err := resources.Cluster(resourceContext)
	if err != nil {
		return false, err
	}
	devCluster, err := resources.DevCluster(resourceContext)
	if err != nil {
		return false, err
	}
	controlPlane, err := resources.KamajiControlPlane(resourceContext)
	if err != nil {
		return false, err
	}
	for _, desired := range []struct {
		object   *unstructured.Unstructured
		resource string
	}{
		{cluster, "cluster"},
		{devCluster, "dev-cluster"},
		{controlPlane, "kamaji-control-plane"},
		{resources.KubeadmConfigTemplate(resourceContext), "kubeadm-config-template"},
		{resources.DevMachineTemplate(resourceContext), "dev-machine-template"},
		{resources.MachineDeployment(resourceContext), "machine-deployment"},
	} {
		changed, err := reconciler.reconcileStableManagementObject(
			ctx,
			desired.object,
			tenant,
			specHash,
			foundation,
			desired.resource,
		)
		if err != nil || changed {
			return changed, err
		}
	}

	calico, err := os.ReadFile("/assets/calico.yaml")
	if err != nil {
		return false, fmt.Errorf("read staged Calico asset: %w", err)
	}
	images, err := networkImages(foundation)
	if err != nil {
		return false, err
	}
	network, err := resources.BuildNetwork(resourceContext, calico, images)
	if err != nil {
		return false, err
	}
	tenantObjects := append(
		[]*unstructured.Unstructured(nil),
		network.Objects...,
	)
	tenantObjects = append(
		tenantObjects,
		resources.StorageObjects(resourceContext, tenantStorageClass)...,
	)
	controllerImage, ok := archiveByKey(foundation.Cache.ImageArchives, "CNPG_CONTROLLER_IMAGE")
	if !ok {
		return false, fmt.Errorf("CNPG_CONTROLLER_IMAGE is missing")
	}
	postgresImage, ok := archiveByKey(foundation.Cache.ImageArchives, "POSTGRES_IMAGE")
	if !ok {
		return false, fmt.Errorf("POSTGRES_IMAGE is missing")
	}
	cnpgAsset, err := os.ReadFile("/assets/cnpg.yaml")
	if err != nil {
		return false, fmt.Errorf("read staged CNPG asset: %w", err)
	}
	operator, err := resources.CNPGOperator(
		resourceContext,
		cnpgAsset,
		controllerImage.Tagged,
		controllerImage.Reference,
	)
	if err != nil {
		return false, err
	}
	tenantObjects = append(tenantObjects, operator...)
	tenantObjects = append(
		tenantObjects,
		resources.CNPGObjects(resourceContext, tenantStorageClass, postgresImage.Reference)...,
	)
	for _, desired := range tenantObjects {
		identity, changed, err := ensureTenantObjectWithPatchResult(
			ctx,
			tenantClient,
			desired,
			tenant,
			specHash,
			foundation.Hash,
			true,
		)
		if err != nil {
			return false, err
		}
		if !tenantIdentityPresent(tenant.Status.TenantResources, identity) {
			return false, fmt.Errorf(
				"stable tenant resource identity is not recorded for %s/%s",
				identity.Kind,
				identity.Name,
			)
		}
		if changed {
			return true, nil
		}
	}
	return false, nil
}

func (reconciler *TenantReconciler) reconcileStableManagementObject(
	ctx context.Context,
	desired *unstructured.Unstructured,
	tenant *tenancyv1alpha1.Tenant,
	specHash string,
	foundation Foundation,
	resource string,
) (bool, error) {
	recorded := findIdentity(
		tenant.Status,
		desired.GroupVersionKind(),
		desired.GetNamespace(),
		desired.GetName(),
	)
	if recorded == nil {
		return false, fmt.Errorf(
			"stable %s %s identity is not recorded",
			desired.GetKind(),
			desired.GetName(),
		)
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	if err := reconciler.reader().Get(ctx, client.ObjectKeyFromObject(desired), current); err != nil {
		return false, err
	}
	if err := validateRootOwnership(
		current,
		tenant,
		specHash,
		foundation.Hash,
		resource,
		foundation.Inputs.OwnershipLabel,
		foundation.Inputs.LabPrefix,
	); err != nil {
		return false, err
	}
	if recorded.UID != string(current.GetUID()) {
		return false, fmt.Errorf(
			"%s %s identity changed from %s to %s",
			current.GetKind(),
			current.GetName(),
			recorded.UID,
			current.GetUID(),
		)
	}
	if err := validateProviderOwner(current, tenant.Status, false); err != nil {
		return false, err
	}
	if desiredMatchesCurrent(desired, current) {
		return false, nil
	}
	applied := desired.DeepCopy()
	applied.SetUID(current.GetUID())
	applied.SetResourceVersion(current.GetResourceVersion())
	ctrl.LoggerFrom(ctx).Info(
		"repairing stable management resource drift",
		"kind",
		desired.GetKind(),
		"name",
		desired.GetName(),
		"mismatch",
		desiredMismatchPath(desired, current),
	)
	if err := reconciler.Patch(
		ctx,
		applied,
		client.Apply,
		client.FieldOwner("cnpg-vcluster-tenant-controller"),
		client.ForceOwnership,
	); err != nil {
		if apierrors.IsConflict(err) {
			return false, fmt.Errorf("%w: %v", errStableApplyConflict, err)
		}
		return false, err
	}
	refreshed := &unstructured.Unstructured{}
	refreshed.SetGroupVersionKind(desired.GroupVersionKind())
	if err := reconciler.reader().Get(ctx, client.ObjectKeyFromObject(desired), refreshed); err != nil {
		return false, err
	}
	if refreshed.GetUID() != current.GetUID() {
		return false, fmt.Errorf(
			"%s %s identity changed during apply",
			refreshed.GetKind(),
			refreshed.GetName(),
		)
	}
	return true, nil
}
