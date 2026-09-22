package controller

import (
	"context"
	"fmt"
	"sort"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

func ensureTenantObject(ctx context.Context, tenantClient client.Client, desired *unstructured.Unstructured, tenant *tenancyv1alpha1.Tenant, specHash, foundationHash string) (tenancyv1alpha1.ObservedResourceIdentity, bool, error) {
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), current)
	if apierrors.IsNotFound(err) {
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
	if recorded := findTenantIdentity(tenant.Status.TenantResources, desired.GroupVersionKind(), desired.GetNamespace(), desired.GetName()); recorded != nil &&
		recorded.UID != string(current.GetUID()) {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, fmt.Errorf(
			"tenant %s %s identity changed from %s to %s",
			current.GetKind(),
			current.GetName(),
			recorded.UID,
			current.GetUID(),
		)
	}
	applied := desired.DeepCopy()
	if err := tenantClient.Patch(
		ctx,
		applied,
		client.Apply,
		client.FieldOwner("cnpg-vcluster-tenant-controller"),
		client.ForceOwnership,
	); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	refreshed := &unstructured.Unstructured{}
	refreshed.SetGroupVersionKind(desired.GroupVersionKind())
	if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), refreshed); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	return identityFor(refreshed), false, nil
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
			return true
		}
	}
	return false
}

func workloadAvailable(object *unstructured.Unstructured) bool {
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
