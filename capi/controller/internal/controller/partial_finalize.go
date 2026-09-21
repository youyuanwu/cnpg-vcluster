package controller

import (
	"context"
	"fmt"
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

func (reconciler *TenantReconciler) finalizePartial(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (ctrl.Result, error) {
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.Phase = tenancyv1alpha1.PhaseDeleting
		setCondition(status, tenant, "Deleting", metav1.ConditionTrue, "PartialFinalization", "Cleaning exact Phase 2 Tenant state")
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, "Deleting", "Tenant deletion is in progress")
		return nil
	}); err != nil {
		return ctrl.Result{}, err
	}
	liveCleanupComplete := tenant.Status.Teardown != nil &&
		tenant.Status.Teardown.Authority == "LiveBootstrapRBACCleanupComplete"
	if stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageTenantAPICleanupRequired) &&
		!liveCleanupComplete {
		tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("live Tenant API cleanup is required before management teardown: %w", err)
		}
		complete, err := deleteBootstrapRBAC(ctx, tenantClient)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !complete {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
		}
		if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.Phase = "LiveBootstrapRBACCleanupComplete"
			status.Teardown.Authority = "LiveBootstrapRBACCleanupComplete"
			return nil
		}); err != nil {
			return ctrl.Result{}, err
		}
	} else if tenant.Status.Teardown != nil && tenant.Status.Teardown.Authority != "" &&
		tenant.Status.Teardown.Authority != "TenantAPINeverAuthorized" &&
		tenant.Status.Teardown.Authority != "LiveBootstrapRBACCleanupComplete" {
		return ctrl.Result{}, fmt.Errorf("partial Tenant cleanup authority is invalid")
	}

	clusterIdentity, clusterPresent, err := reconciler.observeExactUnstructured(ctx, tenant, specHash, foundation, clusterGVK, tenant.Name, tenant.Name, "cluster")
	if err != nil {
		return ctrl.Result{}, err
	}
	if clusterPresent && findIdentity(tenant.Status, clusterGVK, tenant.Name, tenant.Name) == nil {
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if err := upsertIdentity(status, clusterIdentity); err != nil {
				return err
			}
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.ClusterUID = clusterIdentity.UID
			return nil
		})
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

	containers, err := reconciler.docker().ListWorkerContainers(ctx, tenant.Name)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("inspect provider-owned worker containers: %w", err)
	}
	if len(containers) != 0 {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}

	volumeName := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
	volume, err := reconciler.docker().InspectVolume(ctx, volumeName)
	if err != nil {
		return ctrl.Result{}, err
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
			return ctrl.Result{}, fmt.Errorf("Docker volume ownership changed before cleanup")
		}
		if tenant.Status.DockerVolume != nil &&
			(volume.CreatedAt != tenant.Status.DockerVolume.CreatedAt ||
				volume.Mountpoint != tenant.Status.DockerVolume.Mountpoint) {
			return ctrl.Result{}, fmt.Errorf("Docker volume identity changed before cleanup")
		}
		if err := reconciler.docker().RemoveVolume(ctx, volume.Name); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	secretAbsent, err := reconciler.deleteKubeconfigSecret(ctx, tenant)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !secretAbsent {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}

	namespaceAbsent, err := reconciler.deleteExactNamespace(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !namespaceAbsent {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	if tenant.Status.Stage != "EndpointReleased" {
		if err := releaseEndpoint(ctx, reconciler.Client, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant); err != nil {
			return ctrl.Result{}, err
		}
		if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Endpoint = ""
			status.Stage = "EndpointReleased"
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.Phase = "EndpointReleased"
			return nil
		}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}
	if err := reconciler.removeTenantFinalizer(ctx, tenant.Name); err != nil {
		return ctrl.Result{}, err
	}

	return ctrl.Result{}, nil
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

func (reconciler *TenantReconciler) observeExactUnstructured(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation, gvk schema.GroupVersionKind, namespace, name, resource string) (tenancyv1alpha1.ObservedResourceIdentity, bool, error) {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(gvk)
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, object)
	if apierrors.IsNotFound(err) {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, nil
	}
	if err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	return identityFor(object), true, nil
}

func (reconciler *TenantReconciler) deleteKubeconfigSecret(ctx context.Context, tenant *tenancyv1alpha1.Tenant) (bool, error) {
	var secret corev1.Secret
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name + "-kubeconfig"}, &secret)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	controlPlane := findIdentity(tenant.Status, controlPlaneGVK, tenant.Name, tenant.Name)
	if controlPlane == nil || !hasOwnerUID(secret.OwnerReferences, types.UID(controlPlane.UID)) {
		return false, fmt.Errorf("Tenant kubeconfig Secret ownership cannot be proven")
	}
	secret.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Secret"))
	if err := validateRecordedUID(tenant.Status, &secret); err != nil {
		return false, err
	}
	uid := secret.UID
	resourceVersion := secret.ResourceVersion
	err = reconciler.Delete(ctx, &secret, &client.DeleteOptions{
		Preconditions: &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
	})
	if err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	return false, nil
}

func (reconciler *TenantReconciler) deleteExactUnstructured(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation, gvk schema.GroupVersionKind, namespace, name, resource string) (bool, error) {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(gvk)
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, object)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return false, err
	}
	if err := validateRecordedUID(tenant.Status, object); err != nil {
		return false, err
	}
	uid := object.GetUID()
	resourceVersion := object.GetResourceVersion()
	propagation := metav1.DeletePropagationBackground
	err = reconciler.Delete(ctx, object, &client.DeleteOptions{
		Preconditions:     &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
		PropagationPolicy: &propagation,
	})
	if err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	return false, nil
}

func (reconciler *TenantReconciler) deleteExactNamespace(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	var namespace corev1.Namespace
	err := reconciler.reader().Get(ctx, types.NamespacedName{Name: tenant.Name}, &namespace)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	if err := validateRootOwnership(&namespace, tenant, specHash, foundation.Hash, "namespace", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return false, err
	}
	namespace.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Namespace"))
	if err := validateRecordedUID(tenant.Status, &namespace); err != nil {
		return false, err
	}
	uid := namespace.UID
	resourceVersion := namespace.ResourceVersion
	propagation := metav1.DeletePropagationBackground
	err = reconciler.Delete(ctx, &namespace, &client.DeleteOptions{
		Preconditions:     &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
		PropagationPolicy: &propagation,
	})
	if err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	return false, nil
}

func stageAtOrAfter(current, boundary string) bool {
	order := []string{
		"",
		tenancyv1alpha1.StageEndpointAllocated,
		tenancyv1alpha1.StageNamespaceCreated,
		tenancyv1alpha1.StageClusterCreationAuthorized,
		tenancyv1alpha1.StageClusterCreated,
		tenancyv1alpha1.StageDevClusterCreated,
		tenancyv1alpha1.StageControlPlaneCreated,
		tenancyv1alpha1.StageKubeconfigReady,
		tenancyv1alpha1.StageTenantAPICleanupRequired,
		tenancyv1alpha1.StageBootstrapRBACApplied,
		tenancyv1alpha1.StageVolumeCreated,
		tenancyv1alpha1.StageKubeadmTemplateCreated,
		tenancyv1alpha1.StageMachineTemplateCreated,
		tenancyv1alpha1.StageMachineDeploymentCreated,
		tenancyv1alpha1.StageWorkersApplied,
	}
	positions := map[string]int{}
	for index, value := range order {
		positions[value] = index
	}
	return positions[current] >= positions[boundary]
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
