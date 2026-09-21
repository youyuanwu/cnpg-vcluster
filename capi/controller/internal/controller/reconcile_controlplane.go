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
)

func (reconciler *TenantReconciler) reconcileControlPlane(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	resourceContext := resources.Context{
		Tenant:         tenant,
		Spec:           canonical,
		SpecHash:       specHash,
		FoundationHash: foundation.Hash,
		Endpoint:       tenant.Status.Endpoint,
		Inputs:         foundation.ResourceInputs(),
	}
	switch tenant.Status.Stage {
	case "":
		endpoint, err := allocateEndpoint(ctx, reconciler.Client, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant, specHash)
		if err != nil {
			return ctrl.Result{}, err
		}
		err = reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Endpoint = endpoint
			status.FoundationHash = foundation.Hash
			status.Stage = tenancyv1alpha1.StageEndpointAllocated
			status.Teardown = &tenancyv1alpha1.TeardownStatus{
				Phase:     "EndpointAllocated",
				Authority: "TenantAPINeverAuthorized",
			}
			return nil
		})
		return ctrl.Result{Requeue: true}, err
	case tenancyv1alpha1.StageEndpointAllocated:
		namespace := resources.Namespace(resourceContext)
		identity, err := reconciler.ensureNamespace(ctx, namespace, tenant, specHash, foundation)
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageNamespaceCreated, identity)
	case tenancyv1alpha1.StageNamespaceCreated:
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageClusterCreationAuthorized
			status.Teardown = &tenancyv1alpha1.TeardownStatus{
				Phase:     "ClusterCreationAuthorized",
				Authority: "TenantAPINeverAuthorized",
			}
			return nil
		})
	case tenancyv1alpha1.StageClusterCreationAuthorized:
		object, err := resources.Cluster(resourceContext)
		if err != nil {
			return ctrl.Result{}, err
		}
		identity, err := reconciler.ensureUnstructured(ctx, object, tenant, specHash, foundation, "cluster")
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if err := upsertIdentity(status, identity); err != nil {
				return err
			}
			status.Stage = tenancyv1alpha1.StageClusterCreated
			status.Teardown = &tenancyv1alpha1.TeardownStatus{
				Phase:      "TenantAPINeverAuthorized",
				Authority:  "TenantAPINeverAuthorized",
				ClusterUID: identity.UID,
			}
			return nil
		})
	case tenancyv1alpha1.StageClusterCreated:
		object, err := resources.DevCluster(resourceContext)
		if err != nil {
			return ctrl.Result{}, err
		}
		identity, err := reconciler.ensureUnstructured(ctx, object, tenant, specHash, foundation, "dev-cluster")
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageDevClusterCreated, identity)
	case tenancyv1alpha1.StageDevClusterCreated:
		object, err := resources.KamajiControlPlane(resourceContext)
		if err != nil {
			return ctrl.Result{}, err
		}
		identity, err := reconciler.ensureUnstructured(ctx, object, tenant, specHash, foundation, "kamaji-control-plane")
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageControlPlaneCreated, identity)
	case tenancyv1alpha1.StageControlPlaneCreated:
		current, err := reconciler.managementObjectsCurrent(ctx, tenant, specHash, foundation)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !current {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		_, secret, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
		if apierrors.IsNotFound(err) {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}

		if err != nil {
			return ctrl.Result{}, err
		}

		controlPlaneIdentity := findIdentity(tenant.Status, controlPlaneGVK, tenant.Name, tenant.Name)
		if controlPlaneIdentity == nil || !hasOwnerUID(secret.OwnerReferences, types.UID(controlPlaneIdentity.UID)) {
			return ctrl.Result{}, fmt.Errorf("Tenant kubeconfig Secret owner does not match the exact KamajiControlPlane")
		}
		secret.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Secret"))
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageKubeconfigReady, identityFor(secret))
	case tenancyv1alpha1.StageKubeconfigReady:
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageTenantAPICleanupRequired
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.Phase = "TenantAPICleanupRequired"
			status.Teardown.Authority = "TenantAPICleanupRequired"
			return nil
		})
	case tenancyv1alpha1.StageTenantAPICleanupRequired:
		tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
		if err != nil {
			return ctrl.Result{}, err
		}
		if err := applyBootstrapRBAC(ctx, tenantClient); err != nil {
			if errors.Is(err, errTenantAdministrativeAccessPending) {
				return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
			}
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageBootstrapRBACApplied
			setCondition(status, tenant, "ControlPlaneReady", metav1.ConditionTrue, "ControlPlaneReady", "Hosted control plane and bootstrap access are ready")
			return nil
		})
	default:
		return reconciler.reconcileWorkers(ctx, tenant, canonical, specHash, foundation)
	}
}

func (reconciler *TenantReconciler) managementObjectsCurrent(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	for _, item := range []struct {
		gvk      schema.GroupVersionKind
		resource string
	}{
		{clusterGVK, "cluster"},
		{devClusterGVK, "dev-cluster"},
		{controlPlaneGVK, "kamaji-control-plane"},
	} {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(item.gvk)
		if err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name}, object); err != nil {
			return false, err
		}
		if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, item.resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return false, err
		}
		if identity := findIdentity(tenant.Status, item.gvk, tenant.Name, tenant.Name); identity == nil || identity.UID != string(object.GetUID()) {
			return false, fmt.Errorf("%s exact identity is not recorded", item.gvk.Kind)
		}
		observedGeneration, found, err := unstructured.NestedInt64(object.Object, "status", "observedGeneration")
		if err != nil {
			return false, fmt.Errorf("read %s observedGeneration: %w", item.gvk.Kind, err)
		}
		if found && observedGeneration < object.GetGeneration() {
			return false, nil
		}
		conditions, found, err := unstructured.NestedSlice(object.Object, "status", "conditions")
		if err != nil {
			return false, fmt.Errorf("read %s conditions: %w", item.gvk.Kind, err)
		}
		if !found {
			continue
		}
		for _, raw := range conditions {
			condition, ok := raw.(map[string]any)
			if !ok || condition["status"] != "False" {
				continue
			}
			conditionType, _ := condition["type"].(string)
			reason, _ := condition["reason"].(string)
			severity, _ := condition["severity"].(string)
			if (conditionType == "Ready" || conditionType == "Available" || conditionType == "ControlPlaneReady") &&
				(severity == "Error" || containsAny(reason, "failed", "invalid", "error")) {
				return false, fmt.Errorf("%s reports current failure condition %s: %s", item.gvk.Kind, conditionType, reason)
			}
		}
	}
	return true, nil
}

func (reconciler *TenantReconciler) ensureNamespace(ctx context.Context, desired *corev1.Namespace, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (tenancyv1alpha1.ObservedResourceIdentity, error) {
	var current corev1.Namespace
	err := reconciler.reader().Get(ctx, types.NamespacedName{Name: desired.Name}, &current)
	if apierrors.IsNotFound(err) {
		if err := reconciler.Create(ctx, desired); err != nil {
			return tenancyv1alpha1.ObservedResourceIdentity{}, err
		}
		current = *desired
		current.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Namespace"))
	} else if err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, err
	}
	if err := validateRootOwnership(&current, tenant, specHash, foundation.Hash, "namespace", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, err
	}
	current.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Namespace"))
	return identityFor(&current), nil
}

func (reconciler *TenantReconciler) ensureUnstructured(ctx context.Context, desired *unstructured.Unstructured, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation, resource string) (tenancyv1alpha1.ObservedResourceIdentity, error) {
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	key := client.ObjectKeyFromObject(desired)
	err := reconciler.reader().Get(ctx, key, current)
	if apierrors.IsNotFound(err) {
		if err := reconciler.Create(ctx, desired); err != nil {
			return tenancyv1alpha1.ObservedResourceIdentity{}, err
		}
		current = desired
	} else if err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, err
	}
	if err := validateRootOwnership(current, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, err
	}
	return identityFor(current), nil
}

func (reconciler *TenantReconciler) advanceWithIdentity(ctx context.Context, tenant *tenancyv1alpha1.Tenant, stage string, identity tenancyv1alpha1.ObservedResourceIdentity) error {
	return reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		if err := upsertIdentity(status, identity); err != nil {
			return err
		}
		status.Stage = stage
		return nil
	})
}

func hasOwnerUID(owners []metav1.OwnerReference, uid types.UID) bool {
	for _, owner := range owners {
		if owner.UID == uid {
			return true
		}
	}
	return false
}
