package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

var (
	clusterGVK            = schema.GroupVersionKind{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "Cluster"}
	devClusterGVK         = schema.GroupVersionKind{Group: "infrastructure.cluster.x-k8s.io", Version: "v1beta2", Kind: "DevCluster"}
	controlPlaneGVK       = schema.GroupVersionKind{Group: "controlplane.cluster.x-k8s.io", Version: "v1alpha2", Kind: "KamajiControlPlane"}
	kubeadmTemplateGVK    = schema.GroupVersionKind{Group: "bootstrap.cluster.x-k8s.io", Version: "v1beta2", Kind: "KubeadmConfigTemplate"}
	devMachineTemplateGVK = schema.GroupVersionKind{Group: "infrastructure.cluster.x-k8s.io", Version: "v1beta2", Kind: "DevMachineTemplate"}
	machineDeploymentGVK  = schema.GroupVersionKind{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "MachineDeployment"}
	errRootClusterMissing = errors.New("recorded root Cluster is missing")
)

func (reconciler *TenantReconciler) reconcileDesiredState(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	if tenant.Status.Endpoint == "" {
		endpoint, err := allocateEndpoint(ctx, reconciler.Client, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant, specHash)
		if err != nil {
			return ctrl.Result{}, err
		}
		return progressRequeue(), reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Endpoint = endpoint
			return nil
		})
	}

	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	changed, err := reconciler.ensureNamespace(ctx, resources.Namespace(resourceContext), tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if changed {
		return progressRequeue(), nil
	}
	cluster, err := resources.Cluster(resourceContext)
	if err != nil {
		return ctrl.Result{}, err
	}
	currentCluster, changed, err := reconciler.ensureManagementObject(ctx, cluster, tenant, specHash, foundation, "cluster")
	if err != nil {
		return ctrl.Result{}, err
	}
	if changed {
		return progressRequeue(), nil
	}
	if tenant.Status.ClusterUID == "" {
		return progressRequeue(), reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.ClusterUID = string(currentCluster.GetUID())
			return nil
		})
	}

	devCluster, err := resources.DevCluster(resourceContext)
	if err != nil {
		return ctrl.Result{}, err
	}
	if _, changed, err := reconciler.ensureManagementObject(ctx, devCluster, tenant, specHash, foundation, "dev-cluster"); err != nil {
		return ctrl.Result{}, err
	} else if changed {
		return progressRequeue(), nil
	}
	controlPlane, err := resources.KamajiControlPlane(resourceContext)
	if err != nil {
		return ctrl.Result{}, err
	}
	currentControlPlane, changed, err := reconciler.ensureManagementObject(ctx, controlPlane, tenant, specHash, foundation, "kamaji-control-plane")
	if err != nil {
		return ctrl.Result{}, err
	}
	if changed {
		return progressRequeue(), nil
	}
	current, err := reconciler.managementControlPlaneCurrent(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !current {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	tenantClient, secret, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
	if apierrors.IsNotFound(err) {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := validateKubeconfigSecret(secret, currentControlPlane); err != nil {
		return ctrl.Result{}, err
	}
	if err := ensureBootstrapRBAC(ctx, tenantClient); err != nil {
		if errors.Is(err, errTenantAdministrativeAccessPending) {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		return ctrl.Result{}, err
	}
	return reconciler.reconcileWorkers(ctx, tenantClient, tenant, canonical, specHash, foundation)
}

func (reconciler *TenantReconciler) managementObjectsCurrent(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	return reconciler.managementClusterReady(ctx, tenant, specHash, foundation, "Available")
}

func (reconciler *TenantReconciler) managementControlPlaneCurrent(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	return reconciler.managementClusterReady(ctx, tenant, specHash, foundation, "ControlPlaneReady", "ControlPlaneAvailable")
}

func (reconciler *TenantReconciler) managementClusterReady(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	specHash string,
	foundation Foundation,
	conditionTypes ...string,
) (bool, error) {
	cluster := &unstructured.Unstructured{}
	cluster.SetGroupVersionKind(clusterGVK)
	if err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name}, cluster); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	if err := validateRootOwnership(cluster, tenant, specHash, foundation.Hash, "cluster", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return false, err
	}
	if err := validateClusterUID(tenant, cluster); err != nil {
		return false, err
	}
	if err := validateProviderOwner(ctx, reconciler.reader(), cluster, tenant.Name, false); err != nil {
		if errors.Is(err, errProviderOwnerPending) {
			return false, nil
		}
		return false, err
	}
	return managementConditionsReady(cluster, conditionTypes...)
}

func managementConditionsReady(object *unstructured.Unstructured, conditionTypes ...string) (bool, error) {
	observedGeneration, found, err := unstructured.NestedInt64(object.Object, "status", "observedGeneration")
	if err != nil {
		return false, fmt.Errorf("read %s observedGeneration: %w", object.GetKind(), err)
	}
	if found && observedGeneration < object.GetGeneration() {
		return false, nil
	}
	conditions, found, err := unstructured.NestedSlice(object.Object, "status", "conditions")
	if err != nil {
		return false, fmt.Errorf("read %s conditions: %w", object.GetKind(), err)
	}
	if !found {
		return false, nil
	}
	for _, raw := range conditions {
		condition, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		conditionType, _ := condition["type"].(string)
		if !containsString(conditionTypes, conditionType) || condition["status"] != "True" {
			continue
		}
		conditionGeneration, found, err := unstructured.NestedInt64(condition, "observedGeneration")
		if err != nil {
			return false, fmt.Errorf("read %s %s observedGeneration: %w", object.GetKind(), conditionType, err)
		}
		if found && conditionGeneration >= object.GetGeneration() {
			return true, nil
		}
		if !found && observedGenerationIsCurrent(object) {
			return true, nil
		}
	}
	return false, nil
}

func observedGenerationIsCurrent(object *unstructured.Unstructured) bool {
	observedGeneration, found, err := unstructured.NestedInt64(object.Object, "status", "observedGeneration")
	return err == nil && found && observedGeneration >= object.GetGeneration()
}

func (reconciler *TenantReconciler) ensureNamespace(ctx context.Context, desired *corev1.Namespace, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	var current corev1.Namespace
	err := reconciler.reader().Get(ctx, types.NamespacedName{Name: desired.Name}, &current)
	if apierrors.IsNotFound(err) {
		if err := reconciler.Create(ctx, desired); err != nil {
			if !apierrors.IsAlreadyExists(err) {
				return false, err
			}
			if err := reconciler.reader().Get(ctx, types.NamespacedName{Name: desired.Name}, &current); err != nil {
				return false, err
			}
		} else {
			return true, nil
		}
	} else if err != nil {
		return false, err
	}
	if err := validateRootOwnership(&current, tenant, specHash, foundation.Hash, "namespace", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return false, err
	}
	return false, nil
}

func (reconciler *TenantReconciler) ensureManagementObject(
	ctx context.Context,
	desired *unstructured.Unstructured,
	tenant *tenancyv1alpha1.Tenant,
	specHash string,
	foundation Foundation,
	resource string,
) (*unstructured.Unstructured, bool, error) {
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	err := reconciler.reader().Get(ctx, client.ObjectKeyFromObject(desired), current)
	if apierrors.IsNotFound(err) {
		if desired.GroupVersionKind() == clusterGVK && tenant.Status.ClusterUID != "" {
			return nil, false, errRootClusterMissing
		}
		if err := reconciler.Create(ctx, desired); err != nil {
			if !apierrors.IsAlreadyExists(err) {
				return nil, false, err
			}
			if err := reconciler.reader().Get(ctx, client.ObjectKeyFromObject(desired), current); err != nil {
				return nil, false, err
			}
		} else {
			return desired, true, nil
		}
	} else if err != nil {
		return nil, false, err
	}
	if err := validateRootOwnership(current, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return nil, false, err
	}
	if desired.GroupVersionKind() == clusterGVK {
		if err := validateClusterUID(tenant, current); err != nil {
			return nil, false, err
		}
	}
	if err := validateProviderOwner(ctx, reconciler.reader(), current, tenant.Name, false); err != nil {
		return nil, false, err
	}
	ctrl.LoggerFrom(ctx).V(1).Info(
		"applying management resource desired state",
		"kind",
		desired.GetKind(),
		"name",
		desired.GetName(),
	)
	applied := desired.DeepCopy()
	applied.SetUID(current.GetUID())
	applied.SetResourceVersion(current.GetResourceVersion())
	if err := reconciler.Patch(
		ctx,
		applied,
		client.Apply,
		client.FieldOwner("cnpg-vcluster-tenant-controller"),
		client.ForceOwnership,
	); err != nil {
		if apierrors.IsConflict(err) {
			return nil, false, fmt.Errorf("%w: %v", errStableApplyConflict, err)
		}
		if apierrors.IsInvalid(err) {
			return nil, false, fmt.Errorf("%w: %v", errImmutableDrift, err)
		}
		return nil, false, err
	}
	refreshed := &unstructured.Unstructured{}
	refreshed.SetGroupVersionKind(desired.GroupVersionKind())
	if err := reconciler.reader().Get(ctx, client.ObjectKeyFromObject(desired), refreshed); err != nil {
		return nil, false, err
	}
	if refreshed.GetUID() != current.GetUID() {
		return nil, false, fmt.Errorf("%s %s identity changed during apply", refreshed.GetKind(), refreshed.GetName())
	}
	return refreshed, false, nil
}

func hasOwnerUID(owners []metav1.OwnerReference, uid types.UID) bool {
	for _, owner := range owners {
		if owner.UID == uid {
			return true
		}
	}
	return false
}
