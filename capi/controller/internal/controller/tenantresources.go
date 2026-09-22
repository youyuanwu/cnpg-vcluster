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

func removeTenantProbeIdentities(status *tenancyv1alpha1.TenantStatus, tenantName string) bool {
	before := len(status.TenantResources)
	for _, item := range []struct {
		namespace string
		name      string
	}{
		{"default", tenantName + "-network-verify"},
		{"default", tenantName + "-storage-verify"},
		{"database", tenantName + "-sql-verify"},
	} {
		removeTenantIdentity(status, schema.GroupVersionKind{Version: "v1", Kind: "Pod"}, item.namespace, item.name)
	}
	return len(status.TenantResources) != before
}

func deleteCompletedTenantProbe(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	probe *unstructured.Unstructured,
) (bool, error) {
	current := probe.DeepCopy()
	err := tenantClient.Get(ctx, client.ObjectKeyFromObject(probe), current)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	recorded := findTenantIdentity(tenant.Status.TenantResources, probe.GroupVersionKind(), probe.GetNamespace(), probe.GetName())
	if recorded == nil || recorded.UID != string(current.GetUID()) {
		return false, fmt.Errorf("%s probe identity changed before cleanup", probe.GetName())
	}
	if err := tenantClient.Delete(ctx, current); err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	return false, nil
}

func validateTenantProbeIdentity(
	tenant *tenancyv1alpha1.Tenant,
	probe *unstructured.Unstructured,
	specHash,
	foundationHash,
	expectedResource string,
) error {
	recorded := findTenantIdentity(tenant.Status.TenantResources, probe.GroupVersionKind(), probe.GetNamespace(), probe.GetName())
	if recorded == nil || recorded.UID != string(probe.GetUID()) {
		return fmt.Errorf("%s probe identity changed before success checkpoint", probe.GetName())
	}
	annotations := probe.GetAnnotations()
	if annotations[resources.TenantAnnotation] != tenant.Name ||
		annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
		annotations[resources.SpecHashAnnotation] != specHash ||
		annotations[resources.FoundationAnnotation] != foundationHash ||
		annotations[resources.ResourceAnnotation] != expectedResource {
		return fmt.Errorf("%s probe ownership changed before success checkpoint", probe.GetName())
	}
	return nil
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
