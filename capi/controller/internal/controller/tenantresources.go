package controller

import (
	"context"
	"errors"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

var (
	errStableApplyConflict = errors.New("stable apply conflict")
	errImmutableDrift      = errors.New("immutable owned drift")
)

func ensureTenantObject(
	ctx context.Context,
	tenantClient client.Client,
	desired *unstructured.Unstructured,
	tenant *tenancyv1alpha1.Tenant,
	specHash,
	foundationHash string,
) (bool, error) {
	desired = desired.DeepCopy()
	delete(desired.Object, "status")
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), current)
	if apierrors.IsNotFound(err) {
		if err := tenantClient.Create(ctx, desired); err != nil {
			if !apierrors.IsAlreadyExists(err) {
				return false, err
			}
			if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), current); err != nil {
				return false, err
			}
		} else {
			return true, nil
		}
	} else if err != nil {
		return false, err
	}
	if err := validateTenantObjectOwnership(current, desired, tenant, specHash, foundationHash); err != nil {
		return false, err
	}
	applied := desired.DeepCopy()
	applied.SetUID(current.GetUID())
	applied.SetResourceVersion(current.GetResourceVersion())
	ctrl.LoggerFrom(ctx).V(1).Info(
		"applying tenant resource desired state",
		"kind",
		desired.GetKind(),
		"namespace",
		desired.GetNamespace(),
		"name",
		desired.GetName(),
	)
	if err := tenantClient.Patch(
		ctx,
		applied,
		client.Apply,
		client.FieldOwner("cnpg-vcluster-tenant-controller"),
		client.ForceOwnership,
	); err != nil {
		if apierrors.IsConflict(err) {
			return false, fmt.Errorf("%w: %v", errStableApplyConflict, err)
		}
		if apierrors.IsInvalid(err) {
			return false, fmt.Errorf("%w: %v", errImmutableDrift, err)
		}
		return false, err
	}
	refreshed := &unstructured.Unstructured{}
	refreshed.SetGroupVersionKind(desired.GroupVersionKind())
	if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), refreshed); err != nil {
		return false, err
	}
	if refreshed.GetUID() != current.GetUID() {
		return false, fmt.Errorf(
			"tenant %s %s identity changed during apply",
			refreshed.GetKind(),
			refreshed.GetName(),
		)
	}
	return false, nil
}

func validateTenantObjectOwnership(
	current,
	desired *unstructured.Unstructured,
	tenant *tenancyv1alpha1.Tenant,
	specHash,
	foundationHash string,
) error {
	annotations := current.GetAnnotations()
	if annotations[resources.TenantAnnotation] != tenant.Name ||
		annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
		annotations[resources.SpecHashAnnotation] != specHash ||
		annotations[resources.FoundationAnnotation] != foundationHash ||
		annotations[resources.ResourceAnnotation] != desired.GetAnnotations()[resources.ResourceAnnotation] {
		return fmt.Errorf(
			"tenant resource %s/%s ownership mismatch",
			current.GetKind(),
			current.GetName(),
		)
	}
	return nil
}

func tenantObjectReady(object *unstructured.Unstructured) bool {
	conditions, _, _ := unstructured.NestedSlice(object.Object, "status", "conditions")
	for _, raw := range conditions {
		condition, ok := raw.(map[string]any)
		if ok && condition["type"] == "Ready" && condition["status"] == "True" {
			if observed, found := integerValue(condition["observedGeneration"]); found && observed < object.GetGeneration() {
				return false
			}
			if observed, found, _ := unstructured.NestedInt64(object.Object, "status", "observedGeneration"); found && observed < object.GetGeneration() {
				return false
			}
			return true
		}
	}
	return false
}

func workloadAvailable(object *unstructured.Unstructured) bool {
	if !observedGenerationIsCurrent(object) {
		return false
	}
	kind := object.GetKind()
	if kind == "DaemonSet" {
		desired, _, _ := unstructured.NestedInt64(object.Object, "status", "desiredNumberScheduled")
		available, _, _ := unstructured.NestedInt64(object.Object, "status", "numberAvailable")
		return desired > 0 && desired == available
	}
	desired, _, _ := unstructured.NestedInt64(object.Object, "spec", "replicas")
	available, _, _ := unstructured.NestedInt64(object.Object, "status", "availableReplicas")
	return desired > 0 && desired == available
}

func integerValue(value any) (int64, bool) {
	switch typed := value.(type) {
	case int64:
		return typed, true
	case int32:
		return int64(typed), true
	case int:
		return int64(typed), true
	case float64:
		return int64(typed), true
	default:
		return 0, false
	}
}
