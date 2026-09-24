package controller

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func (reconciler *TenantReconciler) finalizeTenant(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (ctrl.Result, error) {
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.Phase = tenancyv1alpha1.PhaseDeleting
		setCondition(status, tenant, "Deleting", metav1.ConditionTrue, "Deleting", "Tenant deletion is in progress")
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, "Deleting", "Tenant deletion is in progress")
		return nil
	}); err != nil {
		return ctrl.Result{}, err
	}

	present, err := reconciler.validatePartialDeletionState(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if tenant.Status.FoundationHash == "" {
		if !present {
			return ctrl.Result{}, reconciler.removeTenantFinalizer(ctx, tenant.Name)
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.FoundationHash = foundation.Hash
			return nil
		})
	}
	if tenant.Status.FoundationHash != foundation.Hash {
		return ctrl.Result{}, fmt.Errorf("Tenant foundation identity changed")
	}

	cluster, err := reconciler.observeDeletionCluster(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if cluster != nil && tenant.Status.ClusterUID == "" {
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.ClusterUID = string(cluster.GetUID())
			return nil
		})
	}
	if tenant.Status.TenantAPICreationAuthorized {
		if tenant.Status.ClusterUID == "" {
			return ctrl.Result{}, fmt.Errorf("tenant API creation was authorized but Cluster identity is missing")
		}
		if tenant.Status.TenantCleanupClusterUID != tenant.Status.ClusterUID {
			tenantClient, _, err := tenantClientFromSecret(
				ctx,
				reconciler.reader(),
				reconciler.tenantFactory(),
				tenant.Name,
				tenant.Name,
				tenant.Status.Endpoint,
			)
			if err != nil {
				return ctrl.Result{}, fmt.Errorf("Tenant API cleanup is blocked: %w", err)
			}
			calico, err := os.ReadFile("/assets/calico.yaml")
			if err != nil {
				return ctrl.Result{}, fmt.Errorf("read Calico cleanup catalog: %w", err)
			}
			cnpg, err := os.ReadFile("/assets/cnpg.yaml")
			if err != nil {
				return ctrl.Result{}, fmt.Errorf("read CNPG cleanup catalog: %w", err)
			}
			catalog, err := tenantCleanupCatalog(calico, cnpg, tenant.Spec.DatabaseCount)
			if err != nil {
				return ctrl.Result{}, err
			}
			absent, err := deleteTenantResources(
				ctx,
				tenantClient,
				tenant,
				specHash,
				foundation.Hash,
				catalog,
			)
			if err != nil {
				return ctrl.Result{}, err
			}
			if !absent {
				return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
			}
			complete, err := deleteBootstrapRBAC(ctx, tenantClient)
			if err != nil {
				return ctrl.Result{}, err
			}
			if !complete {
				return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
			}
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				status.TenantCleanupClusterUID = status.ClusterUID
				return nil
			})
		}
	} else {
		if err := reconciler.validateTenantAPINotCreated(ctx, tenant); err != nil {
			return ctrl.Result{}, err
		}
	}

	secretAbsent, err := reconciler.deleteKubeconfigSecret(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !secretAbsent {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	clusterAbsent, err := reconciler.deleteExactUnstructured(ctx, tenant, specHash, foundation, clusterGVK, tenant.Name, tenant.Name, "cluster")
	if err != nil {
		return ctrl.Result{}, err
	}
	if !clusterAbsent {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	for _, item := range []struct {
		gvk      schema.GroupVersionKind
		name     string
		resource string
	}{
		{machineDeploymentGVK, tenant.Name + "-worker", "machine-deployment"},
		{devMachineTemplateGVK, tenant.Name + "-worker", "dev-machine-template"},
		{kubeadmTemplateGVK, tenant.Name + "-worker", "kubeadm-config-template"},
		{controlPlaneGVK, tenant.Name, "kamaji-control-plane"},
		{devClusterGVK, tenant.Name, "dev-cluster"},
	} {
		absent, err := reconciler.deleteExactUnstructured(ctx, tenant, specHash, foundation, item.gvk, tenant.Name, item.name, item.resource)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !absent {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
		}
	}
	hostAbsent, err := reconciler.deleteTenantHostState(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !hostAbsent {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	namespaceAbsent, err := reconciler.deleteExactNamespace(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !namespaceAbsent {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	if err := releaseEndpoint(ctx, reconciler.Client, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant); err != nil {
		return ctrl.Result{}, err
	}
	if tenant.Status.Endpoint != "" {
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Endpoint = ""
			return nil
		})
	}
	if err := reconciler.removeTenantFinalizer(ctx, tenant.Name); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (reconciler *TenantReconciler) observeDeletionCluster(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (*unstructured.Unstructured, error) {
	cluster := &unstructured.Unstructured{}
	cluster.SetGroupVersionKind(clusterGVK)
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name}, cluster)
	if apierrors.IsNotFound(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := validateRootOwnership(cluster, tenant, specHash, foundation.Hash, "cluster", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return nil, err
	}
	if err := validateClusterUID(tenant, cluster); err != nil {
		return nil, err
	}
	return cluster, nil
}

func (reconciler *TenantReconciler) validateTenantAPINotCreated(ctx context.Context, tenant *tenancyv1alpha1.Tenant) error {
	for _, item := range []client.Object{
		&unstructured.Unstructured{},
		&corev1.Secret{},
	} {
		switch value := item.(type) {
		case *unstructured.Unstructured:
			value.SetGroupVersionKind(controlPlaneGVK)
		}
		name := tenant.Name
		if _, ok := item.(*corev1.Secret); ok {
			name += "-kubeconfig"
		}
		err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: name}, item)
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return err
		}
		return fmt.Errorf("tenant API state exists before creation authorization")
	}
	return nil
}

func (reconciler *TenantReconciler) validatePartialDeletionState(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	present := false
	endpointMissing := false
	if _, endpointPresent, err := observeEndpoint(ctx, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant, specHash); err != nil {
		if tenant.Status.Endpoint == "" || !strings.Contains(err.Error(), "endpoint allocation is missing") {
			return false, err
		}
		endpointMissing = true
	} else if endpointPresent {
		present = true
	}
	var namespace corev1.Namespace
	err := reconciler.reader().Get(ctx, types.NamespacedName{Name: tenant.Name}, &namespace)
	if apierrors.IsNotFound(err) {
	} else if err != nil {
		return false, err
	} else {
		present = true
		if err := validateRootOwnership(&namespace, tenant, specHash, foundation.Hash, "namespace", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return false, err
		}
	}
	var controlPlane *unstructured.Unstructured
	for _, item := range []struct {
		gvk      schema.GroupVersionKind
		name     string
		resource string
	}{
		{clusterGVK, tenant.Name, "cluster"},
		{devClusterGVK, tenant.Name, "dev-cluster"},
		{controlPlaneGVK, tenant.Name, "kamaji-control-plane"},
		{kubeadmTemplateGVK, tenant.Name + "-worker", "kubeadm-config-template"},
		{devMachineTemplateGVK, tenant.Name + "-worker", "dev-machine-template"},
		{machineDeploymentGVK, tenant.Name + "-worker", "machine-deployment"},
	} {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(item.gvk)
		err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: item.name}, object)
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return false, err
		}
		present = true
		if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, item.resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return false, err
		}
		if item.gvk == clusterGVK {
			if err := validateClusterUID(tenant, object); err != nil {
				return false, err
			}
		}
		if item.gvk == controlPlaneGVK {
			controlPlane = object
		}
	}
	var secret corev1.Secret
	err = reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name + "-kubeconfig"}, &secret)
	if apierrors.IsNotFound(err) {
	} else if err != nil {
		return false, err
	} else {
		present = true
		if err := validateKubeconfigSecret(&secret, controlPlane); err != nil {
			return false, err
		}
	}
	volumeName := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
	volume, err := reconciler.docker().InspectVolume(ctx, volumeName)
	if err != nil {
		return false, err
	}
	if volume != nil {
		present = true
		expected := map[string]string{
			foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
			"cnpg-vcluster.capi/role":                  "tenant-storage",
			"cnpg-vcluster.capi/tenant":                tenant.Name,
			"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
			"tenancy.cnpg-vcluster.io/spec-hash":       specHash,
			"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
		}
		if volume.Name != volumeName || !stringMapEqual(volume.Labels, expected) {
			return false, fmt.Errorf("Docker volume ownership cannot be proven before deletion")
		}
	}
	if endpointMissing && present {
		return false, fmt.Errorf("Tenant endpoint allocation is missing before terminal cleanup")
	}
	return present, nil
}

func (reconciler *TenantReconciler) deleteKubeconfigSecret(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	var secret corev1.Secret
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name + "-kubeconfig"}, &secret)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	controlPlane := &unstructured.Unstructured{}
	controlPlane.SetGroupVersionKind(controlPlaneGVK)
	if err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name}, controlPlane); err != nil {
		return false, fmt.Errorf("read KamajiControlPlane before kubeconfig deletion: %w", err)
	}
	if err := validateRootOwnership(controlPlane, tenant, specHash, foundation.Hash, "kamaji-control-plane", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return false, err
	}
	if err := validateKubeconfigSecret(&secret, controlPlane); err != nil {
		return false, err
	}
	if secret.DeletionTimestamp != nil {
		return false, nil
	}
	uid := secret.UID
	resourceVersion := secret.ResourceVersion
	err = reconciler.Delete(ctx, &secret, &client.DeleteOptions{
		Preconditions: &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
	})
	if err != nil && !apierrors.IsNotFound(err) && !apierrors.IsConflict(err) {
		return false, err
	}
	return false, nil
}

func (reconciler *TenantReconciler) removeTenantFinalizer(ctx context.Context, name string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var current tenancyv1alpha1.Tenant
		if err := reconciler.reader().Get(ctx, types.NamespacedName{Name: name}, &current); err != nil {
			if apierrors.IsNotFound(err) {
				return nil
			}
			return err
		}
		if !containsString(current.Finalizers, tenantFinalizer) {
			return nil
		}
		current.Finalizers = removeString(current.Finalizers, tenantFinalizer)
		return reconciler.Update(ctx, &current)
	})
}

func removeString(values []string, target string) []string {
	result := values[:0]
	for _, value := range values {
		if value != target {
			result = append(result, value)
		}
	}
	return result
}
