package controller

import (
	"context"
	"fmt"
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

const tenantStorageClass = "capi-hostpath"

func (reconciler *TenantReconciler) reconcileStorage(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	if tenant.Status.Stage != tenancyv1alpha1.StagePostCNIWorkersReady &&
		tenant.Status.Stage != tenancyv1alpha1.StageStorageApplied &&
		tenant.Status.Stage != tenancyv1alpha1.StageStorageProbeCreated &&
		tenant.Status.Stage != tenancyv1alpha1.StageStorageProbeSucceeded {
		return reconciler.reconcileCNPG(ctx, tenant, canonical, specHash, foundation)
	}
	tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
	if err != nil {
		return ctrl.Result{}, err
	}
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	verify, found := archiveByKey(foundation.Cache.ImageArchives, "VERIFY_IMAGE")
	if !found {
		return ctrl.Result{}, fmt.Errorf("VERIFY_IMAGE is missing")
	}
	switch tenant.Status.Stage {
	case tenancyv1alpha1.StagePostCNIWorkersReady:
		for _, desired := range resources.StorageObjects(resourceContext, tenantStorageClass, verify.Reference) {
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
		ready, err := storageStructurallyReady(ctx, tenantClient, tenant.Name)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !ready {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		probe := resources.StorageProbe(resourceContext, verify.Reference)
		identity, _, err := ensureTenantObject(ctx, tenantClient, probe, tenant, specHash, foundation.Hash)
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if err := upsertTenantIdentity(status, identity); err != nil {
				return err
			}
			status.Stage = tenancyv1alpha1.StageStorageProbeCreated
			return nil
		})
	case tenancyv1alpha1.StageStorageProbeCreated:
		probe := resources.StorageProbe(resourceContext, verify.Reference)
		current := probe.DeepCopy()
		if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(probe), current); err != nil {
			return ctrl.Result{}, err
		}
		phase, _, _ := unstructured.NestedString(current.Object, "status", "phase")
		if phase == "Failed" {
			return ctrl.Result{}, fmt.Errorf("storage marker probe failed")
		}
		if phase != "Succeeded" {
			return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
		}
		if err := validateTenantProbeIdentity(tenant, current, specHash, foundation.Hash); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageStorageProbeSucceeded
			return nil
		})
	case tenancyv1alpha1.StageStorageProbeSucceeded:
		probe := resources.StorageProbe(resourceContext, verify.Reference)
		absent, err := deleteCompletedTenantProbe(ctx, tenantClient, tenant, probe)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !absent {
			return ctrl.Result{RequeueAfter: time.Second}, nil
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			removeTenantIdentity(status, probe.GroupVersionKind(), probe.GetNamespace(), probe.GetName())
			status.Stage = tenancyv1alpha1.StageStorageReady
			setCondition(status, tenant, "StorageReady", metav1.ConditionTrue, "StorageReady", "Static storage and retained marker are ready")
			return nil
		})
	}
	return ctrl.Result{}, nil
}

func storageStructurallyReady(ctx context.Context, tenantClient client.Client, tenantName string) (bool, error) {
	for _, item := range []struct {
		gvk             schema.GroupVersionKind
		namespace, name string
	}{
		{schema.GroupVersionKind{Version: "v1", Kind: "PersistentVolumeClaim"}, "default", "storage-smoke"},
		{schema.GroupVersionKind{Version: "v1", Kind: "PersistentVolume"}, "", tenantName + "-storage-smoke"},
	} {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(item.gvk)
		if err := tenantClient.Get(ctx, client.ObjectKey{Namespace: item.namespace, Name: item.name}, object); err != nil {
			if apierrors.IsNotFound(err) {
				return false, nil
			}
			return false, err
		}
		phase, _, _ := unstructured.NestedString(object.Object, "status", "phase")
		if phase != "Bound" {
			return false, nil
		}
		if item.gvk.Kind == "PersistentVolume" {
			if _, found, _ := unstructured.NestedFieldNoCopy(object.Object, "spec", "nodeAffinity"); found {
				return false, fmt.Errorf("storage PV unexpectedly has node affinity")
			}
		}
	}
	deployment := &unstructured.Unstructured{}
	deployment.SetGroupVersionKind(schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"})
	if err := tenantClient.Get(ctx, client.ObjectKey{Namespace: "default", Name: "storage-smoke"}, deployment); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return workloadAvailable(deployment), nil
}

func tenantIdentityPresent(values []tenancyv1alpha1.ObservedResourceIdentity, expected tenancyv1alpha1.ObservedResourceIdentity) bool {
	for _, value := range values {
		if value.APIVersion == expected.APIVersion && value.Kind == expected.Kind &&
			value.Namespace == expected.Namespace && value.Name == expected.Name && value.UID == expected.UID {
			return true
		}
	}
	return false
}
