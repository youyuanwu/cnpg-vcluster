package controller

import (
	"context"
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
	ready, err := databaseStructurallyReady(ctx, tenantClient, canonical.DatabaseCount)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !ready {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	return reconciler.reconcileReadiness(ctx, tenantClient, tenant, canonical, specHash, foundation)
}

func databaseStructurallyReady(ctx context.Context, tenantClient client.Client, count int32) (bool, error) {
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
	return phase == "Cluster in healthy state" && readyInstances == int64(count), nil
}
