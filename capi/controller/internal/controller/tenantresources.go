package controller

import (
	"context"
	"errors"
	"fmt"

	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
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
	if desiredMatchesCurrent(desired, current) {
		return false, nil
	}
	applied := desired.DeepCopy()
	applied.SetUID(current.GetUID())
	applied.SetResourceVersion(current.GetResourceVersion())
	ctrl.LoggerFrom(ctx).Info(
		"repairing tenant resource drift",
		"kind",
		desired.GetKind(),
		"namespace",
		desired.GetNamespace(),
		"name",
		desired.GetName(),
		"mismatch",
		desiredMismatchPath(desired, current),
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
	return true, nil
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

func desiredMatchesCurrent(desired, current *unstructured.Unstructured) bool {
	return desiredMismatchPath(desired, current) == ""
}

func desiredMismatchPath(desired, current *unstructured.Unstructured) string {
	subset := runtime.DeepCopyJSON(desired.Object)
	delete(subset, "status")
	pruneNilValues(subset)
	return firstDesiredMismatch(subset, current.Object, "")
}

func pruneNilValues(value any) {
	switch typed := value.(type) {
	case map[string]any:
		for key, item := range typed {
			if item == nil {
				delete(typed, key)
				continue
			}
			pruneNilValues(item)
		}
	case []any:
		for _, item := range typed {
			if item != nil {
				pruneNilValues(item)
			}
		}
	}
}

func firstDesiredMismatch(desired, current any, path string) string {
	switch expected := desired.(type) {
	case map[string]any:
		actual, ok := current.(map[string]any)
		if !ok {
			return path + ":type"
		}
		for key, value := range expected {
			currentValue, found := actual[key]
			if !found {
				if isZeroJSONValue(value) {
					continue
				}
				return path + "/" + key + ":missing"
			}
			if mismatch := firstDesiredMismatch(value, currentValue, path+"/"+key); mismatch != "" {
				return mismatch
			}
		}
		return ""
	case []any:
		actual, ok := current.([]any)
		if !ok || len(expected) != len(actual) {
			return path + ":list"
		}
		for index := range expected {
			if mismatch := firstDesiredMismatch(
				expected[index],
				actual[index],
				fmt.Sprintf("%s/%d", path, index),
			); mismatch != "" {
				return mismatch
			}
		}
		return ""
	default:
		if expectedNumber, expectedOK := numericJSONValue(desired); expectedOK {
			if currentNumber, currentOK := numericJSONValue(current); currentOK &&
				expectedNumber == currentNumber {
				return ""
			}
		}
		if !apiequality.Semantic.DeepEqual(desired, current) {
			return path + ":value"
		}
		return ""
	}
}

func numericJSONValue(value any) (float64, bool) {
	switch typed := value.(type) {
	case int:
		return float64(typed), true
	case int32:
		return float64(typed), true
	case int64:
		return float64(typed), true
	case uint:
		return float64(typed), true
	case uint32:
		return float64(typed), true
	case uint64:
		return float64(typed), true
	case float32:
		return float64(typed), true
	case float64:
		return typed, true
	default:
		return 0, false
	}
}

func isZeroJSONValue(value any) bool {
	switch typed := value.(type) {
	case nil:
		return true
	case bool:
		return !typed
	case string:
		return typed == ""
	case int64:
		return typed == 0
	case int32:
		return typed == 0
	case int:
		return typed == 0
	case float64:
		return typed == 0
	case map[string]any:
		return len(typed) == 0
	case []any:
		return len(typed) == 0
	default:
		return false
	}
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
