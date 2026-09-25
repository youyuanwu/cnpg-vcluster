package controller

import (
	"context"
	"errors"
	"fmt"
	"os"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcileCNPG(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (ctrl.Result, error) {
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	controllerImage, ok := archiveByKey(foundation.Cache.ImageArchives, "CNPG_CONTROLLER_IMAGE")
	if !ok {
		return ctrl.Result{}, fmt.Errorf("CNPG_CONTROLLER_IMAGE is missing")
	}
	postgresImage, ok := archiveByKey(foundation.Cache.ImageArchives, "POSTGRES_IMAGE")
	if !ok {
		return ctrl.Result{}, fmt.Errorf("POSTGRES_IMAGE is missing")
	}
	asset, err := os.ReadFile("/assets/cnpg.yaml")
	if err != nil {
		return ctrl.Result{}, err
	}
	objects, err := resources.CNPGOperator(resourceContext, asset, controllerImage.Tagged, controllerImage.Reference)
	if err != nil {
		return ctrl.Result{}, err
	}
	applied, err := ensureTenantObjects(
		ctx,
		tenantClient,
		objects,
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
	deployment := &unstructured.Unstructured{}
	deployment.SetGroupVersionKind(schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"})
	if err := tenantClient.Get(ctx, client.ObjectKey{Namespace: "cnpg-system", Name: "cnpg-controller-manager"}, deployment); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		return ctrl.Result{}, err
	}
	if !workloadAvailable(deployment) {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	_, containers, err := reconciler.observePreCNIWorkers(ctx, tenant, specHash, foundation)
	if err != nil {
		if errors.Is(err, errWorkerRuntimePending) {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		return ctrl.Result{}, err
	}
	if len(containers) == 0 {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	probe := fmt.Sprintf("for ordinal in $(seq 1 %d); do path=%s/volumes/cnpg/$ordinal; test -d \"$path\" && test \"$(stat -c '%%u:%%g:%%a' \"$path\")\" = '26:26:700' || exit 1; done",
		canonical.DatabaseCount, foundation.Inputs.StorageContainerPath)
	result, err := reconciler.exec(ctx, containers[0].ID, []string{"sh", "-ec", probe})
	if err != nil {
		return ctrl.Result{}, err
	}
	if result.ExitCode != 0 {
		command := fmt.Sprintf("for ordinal in $(seq 1 %d); do mkdir -p %s/volumes/cnpg/$ordinal; chown 26:26 %s/volumes/cnpg/$ordinal; chmod 700 %s/volumes/cnpg/$ordinal; done",
			canonical.DatabaseCount, foundation.Inputs.StorageContainerPath, foundation.Inputs.StorageContainerPath, foundation.Inputs.StorageContainerPath)
		if err := reconciler.execRequired(ctx, containers[0].ID, []string{"sh", "-ec", command}); err != nil {
			return ctrl.Result{}, err
		}
		return progressRequeue(), nil
	}
	databaseObjects := resources.CNPGObjects(resourceContext, tenantStorageClass, postgresImage.Reference)
	staticObjects := make([]*unstructured.Unstructured, 0, len(databaseObjects)-1)
	var databaseCluster *unstructured.Unstructured
	for _, object := range databaseObjects {
		if object.GetAPIVersion() == "postgresql.cnpg.io/v1" && object.GetKind() == "Cluster" {
			if databaseCluster != nil {
				return ctrl.Result{}, fmt.Errorf("database desired state contains multiple CNPG Clusters")
			}
			databaseCluster = object
			continue
		}
		staticObjects = append(staticObjects, object)
	}
	if databaseCluster == nil {
		return ctrl.Result{}, fmt.Errorf("database desired state omits the CNPG Cluster")
	}
	applied, err = ensureTenantObjects(
		ctx,
		tenantClient,
		staticObjects,
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
	if changed, err := ensureTenantObject(
		ctx,
		tenantClient,
		databaseCluster,
		tenant,
		specHash,
		foundation.Hash,
	); err != nil {
		return ctrl.Result{}, err
	} else if changed {
		return progressRequeue(), nil
	}
	ready, err := databaseStructurallyReady(ctx, tenantClient, canonical.DatabaseCount, postgresImage.Reference)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !ready {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	return reconciler.reconcileReadiness(ctx, tenantClient, tenant, canonical, specHash, foundation)
}

func databaseStructurallyReady(ctx context.Context, tenantClient client.Client, count int32, postgresImage string) (bool, error) {
	cluster := &unstructured.Unstructured{}
	cluster.SetGroupVersionKind(schema.GroupVersionKind{Group: "postgresql.cnpg.io", Version: "v1", Kind: "Cluster"})
	if err := tenantClient.Get(ctx, client.ObjectKey{Namespace: "database", Name: "capi-postgres"}, cluster); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	phase, _, _ := unstructured.NestedString(cluster.Object, "status", "phase")
	readyInstances, _, _ := unstructured.NestedInt64(cluster.Object, "status", "readyInstances")
	if phase != "Cluster in healthy state" ||
		readyInstances != int64(count) {
		return false, nil
	}
	pods := &unstructured.UnstructuredList{}
	pods.SetGroupVersionKind(schema.GroupVersionKind{Version: "v1", Kind: "PodList"})
	if err := tenantClient.List(ctx, pods, client.InNamespace("database"), client.MatchingLabels{"cnpg.io/cluster": "capi-postgres"}); err != nil {
		return false, err
	}
	if len(pods.Items) != int(count) {
		return false, nil
	}
	for index := range pods.Items {
		if !tenantObjectReady(&pods.Items[index]) {
			return false, nil
		}
		containers, _, _ := unstructured.NestedSlice(pods.Items[index].Object, "spec", "containers")
		found := false
		for _, raw := range containers {
			container, ok := raw.(map[string]any)
			if ok && container["name"] == "postgres" {
				found = container["image"] == postgresImage
			}
		}
		if !found {
			return false, fmt.Errorf("Postgres image drift")
		}
	}
	pvcs := &unstructured.UnstructuredList{}
	pvcs.SetGroupVersionKind(schema.GroupVersionKind{Version: "v1", Kind: "PersistentVolumeClaimList"})
	if err := tenantClient.List(ctx, pvcs, client.InNamespace("database"), client.MatchingLabels{"cnpg.io/cluster": "capi-postgres"}); err != nil {
		return false, err
	}
	if len(pvcs.Items) != int(count) {
		return false, nil
	}
	for index := range pvcs.Items {
		value, _, _ := unstructured.NestedString(pvcs.Items[index].Object, "status", "phase")
		if value != "Bound" {
			return false, nil
		}
	}
	return true, nil
}
