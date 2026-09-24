package controller

import (
	"context"
	"errors"
	"fmt"
	"sort"

	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

var errStableApplyConflict = errors.New("stable apply conflict")

func ensureTenantObject(ctx context.Context, tenantClient client.Client, desired *unstructured.Unstructured, tenant *tenancyv1alpha1.Tenant, specHash, foundationHash string) (tenancyv1alpha1.ObservedResourceIdentity, bool, error) {
	return ensureTenantObjectWithPatchResult(
		ctx,
		tenantClient,
		desired,
		tenant,
		specHash,
		foundationHash,
		false,
	)
}

func ensureTenantObjectWithPatchResult(
	ctx context.Context,
	tenantClient client.Client,
	desired *unstructured.Unstructured,
	tenant *tenancyv1alpha1.Tenant,
	specHash,
	foundationHash string,
	reportPatch bool,
) (tenancyv1alpha1.ObservedResourceIdentity, bool, error) {
	desired = desired.DeepCopy()
	delete(desired.Object, "status")
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), current)
	if apierrors.IsNotFound(err) {
		if recorded := findTenantIdentity(tenant.Status.TenantResources, desired.GroupVersionKind(), desired.GetNamespace(), desired.GetName()); recorded != nil {
			return tenancyv1alpha1.ObservedResourceIdentity{}, false, fmt.Errorf(
				"recorded tenant %s %s is absent",
				recorded.Kind,
				recorded.Name,
			)
		}
		if err := tenantClient.Create(ctx, desired); err != nil {
			return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
		}
		return identityFor(desired), true, nil
	}
	if err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	annotations := current.GetAnnotations()
	if annotations[resources.TenantAnnotation] != tenant.Name ||
		annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
		annotations[resources.SpecHashAnnotation] != specHash ||
		annotations[resources.FoundationAnnotation] != foundationHash ||
		annotations[resources.ResourceAnnotation] != desired.GetAnnotations()[resources.ResourceAnnotation] {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, fmt.Errorf("tenant resource %s/%s ownership mismatch", current.GetKind(), current.GetName())
	}
	recorded := findTenantIdentity(tenant.Status.TenantResources, desired.GroupVersionKind(), desired.GetNamespace(), desired.GetName())
	if recorded != nil && recorded.UID != string(current.GetUID()) {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, fmt.Errorf(
			"tenant %s %s identity changed from %s to %s",
			current.GetKind(),
			current.GetName(),
			recorded.UID,
			current.GetUID(),
		)
	}
	if desiredMatchesCurrent(desired, current) {
		return identityFor(current), false, nil
	}
	applied := desired.DeepCopy()
	applied.SetUID(current.GetUID())
	applied.SetResourceVersion(current.GetResourceVersion())
	if reportPatch {
		ctrl.LoggerFrom(ctx).Info(
			"repairing stable tenant resource drift",
			"kind",
			desired.GetKind(),
			"namespace",
			desired.GetNamespace(),
			"name",
			desired.GetName(),
			"mismatch",
			desiredMismatchPath(desired, current),
		)
	}
	if err := tenantClient.Patch(
		ctx,
		applied,
		client.Apply,
		client.FieldOwner("cnpg-vcluster-tenant-controller"),
		client.ForceOwnership,
	); err != nil {
		if apierrors.IsConflict(err) {
			return tenancyv1alpha1.ObservedResourceIdentity{}, false, fmt.Errorf(
				"%w: %v",
				errStableApplyConflict,
				err,
			)
		}
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}

	refreshed := &unstructured.Unstructured{}
	refreshed.SetGroupVersionKind(desired.GroupVersionKind())
	if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), refreshed); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	if refreshed.GetUID() != current.GetUID() {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, fmt.Errorf(
			"tenant %s %s identity changed during apply",
			refreshed.GetKind(),
			refreshed.GetName(),
		)
	}
	return identityFor(refreshed), reportPatch, nil
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

func findTenantIdentity(
	values []tenancyv1alpha1.ObservedResourceIdentity,
	gvk schema.GroupVersionKind,
	namespace,
	name string,
) *tenancyv1alpha1.ObservedResourceIdentity {
	for index := range values {
		identity := &values[index]
		if identity.APIVersion == gvk.GroupVersion().String() &&
			identity.Kind == gvk.Kind &&
			identity.Namespace == namespace &&
			identity.Name == name {
			return identity
		}
	}
	return nil
}

func upsertTenantIdentity(status *tenancyv1alpha1.TenantStatus, identity tenancyv1alpha1.ObservedResourceIdentity) error {
	for index := range status.TenantResources {
		current := &status.TenantResources[index]
		if current.APIVersion == identity.APIVersion && current.Kind == identity.Kind &&
			current.Namespace == identity.Namespace && current.Name == identity.Name {
			if current.UID != identity.UID {
				return fmt.Errorf("tenant %s %s identity changed from %s to %s", identity.Kind, identity.Name, current.UID, identity.UID)
			}
			return nil
		}
	}
	status.TenantResources = append(status.TenantResources, identity)
	sort.Slice(status.TenantResources, func(left, right int) bool {
		a := status.TenantResources[left]
		b := status.TenantResources[right]
		return a.APIVersion+"/"+a.Kind+"/"+a.Namespace+"/"+a.Name <
			b.APIVersion+"/"+b.Kind+"/"+b.Namespace+"/"+b.Name
	})
	return nil
}

func tenantIdentityPresent(values []tenancyv1alpha1.ObservedResourceIdentity, expected tenancyv1alpha1.ObservedResourceIdentity) bool {
	for _, value := range values {
		if value.APIVersion == expected.APIVersion &&
			value.Kind == expected.Kind &&
			value.Namespace == expected.Namespace &&
			value.Name == expected.Name &&
			value.UID == expected.UID {
			return true
		}
	}
	return false
}

func validateTenantResourceOwnership(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	specHash,
	foundationHash string,
) error {
	for _, identity := range tenant.Status.TenantResources {
		if identity.Kind == "Node" {
			continue
		}
		gvk := schema.FromAPIVersionAndKind(identity.APIVersion, identity.Kind)
		current := &unstructured.Unstructured{}
		current.SetGroupVersionKind(gvk)
		if err := tenantClient.Get(ctx, client.ObjectKey{Namespace: identity.Namespace, Name: identity.Name}, current); err != nil {
			return fmt.Errorf("tenant resource ownership inspection failed for %s/%s: %w", identity.Kind, identity.Name, err)
		}
		annotations := current.GetAnnotations()
		if string(current.GetUID()) != identity.UID ||
			annotations[resources.TenantAnnotation] != tenant.Name ||
			annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
			annotations[resources.SpecHashAnnotation] != specHash ||
			annotations[resources.FoundationAnnotation] != foundationHash {
			return fmt.Errorf("tenant resource ownership changed for %s/%s", identity.Kind, identity.Name)
		}
	}
	return nil
}

func removeTenantIdentity(
	status *tenancyv1alpha1.TenantStatus,
	gvk schema.GroupVersionKind,
	namespace,
	name string,
) {
	result := status.TenantResources[:0]
	for _, identity := range status.TenantResources {
		if identity.APIVersion == gvk.GroupVersion().String() &&
			identity.Kind == gvk.Kind &&
			identity.Namespace == namespace &&
			identity.Name == name {
			continue
		}
		result = append(result, identity)
	}
	status.TenantResources = result
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
