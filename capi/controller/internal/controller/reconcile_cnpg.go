package controller

import (
	"context"
	"fmt"
	"os"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcileCNPG(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	if tenant.Status.Stage != tenancyv1alpha1.StageStorageReady &&
		tenant.Status.Stage != tenancyv1alpha1.StageCNPGOperatorApplied &&
		tenant.Status.Stage != tenancyv1alpha1.StageCNPGStoragePrepared &&
		tenant.Status.Stage != tenancyv1alpha1.StageCNPGClusterApplied &&
		tenant.Status.Stage != tenancyv1alpha1.StageDatabaseProbeCreated &&
		tenant.Status.Stage != tenancyv1alpha1.StageDatabaseProbeSucceeded {
		return reconciler.reconcileReadiness(ctx, tenant, canonical, specHash, foundation)
	}
	tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
	if err != nil {
		return ctrl.Result{}, err
	}
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	controllerImage, ok := archiveByKey(foundation.Cache.ImageArchives, "CNPG_CONTROLLER_IMAGE")
	if !ok {
		return ctrl.Result{}, fmt.Errorf("CNPG_CONTROLLER_IMAGE is missing")
	}
	postgresImage, ok := archiveByKey(foundation.Cache.ImageArchives, "POSTGRES_IMAGE")
	if !ok {
		return ctrl.Result{}, fmt.Errorf("POSTGRES_IMAGE is missing")
	}
	switch tenant.Status.Stage {
	case tenancyv1alpha1.StageStorageReady:
		asset, err := os.ReadFile("/assets/cnpg.yaml")
		if err != nil {
			return ctrl.Result{}, err
		}
		objects, err := resources.CNPGOperator(resourceContext, asset, controllerImage.Tagged, controllerImage.Reference)
		if err != nil {
			return ctrl.Result{}, err
		}
		for _, desired := range objects {
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
			status.Stage = tenancyv1alpha1.StageCNPGOperatorApplied
			return nil
		})
	case tenancyv1alpha1.StageCNPGOperatorApplied:
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
		if len(tenant.Status.WorkerContainers) == 0 {
			return ctrl.Result{}, fmt.Errorf("worker container evidence is missing")
		}
		command := fmt.Sprintf("for ordinal in $(seq 1 %d); do mkdir -p %s/volumes/cnpg/$ordinal; chown 26:26 %s/volumes/cnpg/$ordinal; chmod 700 %s/volumes/cnpg/$ordinal; done",
			canonical.DatabaseCount, foundation.Inputs.StorageContainerPath, foundation.Inputs.StorageContainerPath, foundation.Inputs.StorageContainerPath)
		if err := reconciler.execRequired(ctx, tenant.Status.WorkerContainers[0].ID, []string{"sh", "-ec", command}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageCNPGStoragePrepared
			return nil
		})
	case tenancyv1alpha1.StageCNPGStoragePrepared:
		for _, desired := range resources.CNPGObjects(resourceContext, tenantStorageClass, postgresImage.Reference) {
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
			status.Stage = tenancyv1alpha1.StageCNPGClusterApplied
			return nil
		})
	case tenancyv1alpha1.StageCNPGClusterApplied:
		ready, err := databaseStructurallyReady(ctx, tenantClient, canonical.DatabaseCount, postgresImage.Reference)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !ready {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		probe := resources.SQLProbe(resourceContext, postgresImage.Reference)
		identity, _, err := ensureTenantObject(ctx, tenantClient, probe, tenant, specHash, foundation.Hash)
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if err := upsertTenantIdentity(status, identity); err != nil {
				return err
			}
			status.Stage = tenancyv1alpha1.StageDatabaseProbeCreated
			return nil
		})
	case tenancyv1alpha1.StageDatabaseProbeCreated:
		probe := resources.SQLProbe(resourceContext, postgresImage.Reference)
		current := probe.DeepCopy()
		if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(probe), current); err != nil {
			return ctrl.Result{}, err
		}
		phase, _, _ := unstructured.NestedString(current.Object, "status", "phase")
		if phase == "Failed" {
			return ctrl.Result{}, fmt.Errorf("database SQL marker probe failed")
		}
		if phase != "Succeeded" {
			return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageDatabaseProbeSucceeded
			return nil
		})
	case tenancyv1alpha1.StageDatabaseProbeSucceeded:
		probe := resources.SQLProbe(resourceContext, postgresImage.Reference)
		absent, err := deleteCompletedTenantProbe(ctx, tenantClient, tenant, probe)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !absent {
			return ctrl.Result{RequeueAfter: time.Second}, nil
		}
		check := fmt.Sprintf("for ordinal in $(seq 1 %d); do path=%s/volumes/cnpg/$ordinal/pgdata; test -d \"$path\"; test \"$(stat -c %%u:%%g \"$path\")\" = 26:26; test \"$(stat -c %%a \"$path\")\" = 700; test -f \"$path/global/pg_control\"; test \"$(stat -c %%u:%%g:%%a \"$path/global/pg_control\")\" = 26:26:600; done",
			canonical.DatabaseCount, foundation.Inputs.StorageContainerPath)
		if err := reconciler.execRequired(ctx, tenant.Status.WorkerContainers[0].ID, []string{"sh", "-ec", check}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			removeTenantIdentity(status, probe.GroupVersionKind(), probe.GetNamespace(), probe.GetName())
			status.Stage = tenancyv1alpha1.StageDatabaseReady
			setCondition(status, tenant, "DatabaseReady", metav1.ConditionTrue, "DatabaseReady", "CNPG instances, SQL marker, and filesystem ownership are ready")
			return nil
		})
	}
	return ctrl.Result{}, nil
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
	if phase != "Cluster in healthy state" || readyInstances != int64(count) {
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
