package controller

import (
	"context"

	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

type tenantApplyResult struct {
	Created bool
	Pending bool
}

func ensureTenantObjects(
	ctx context.Context,
	tenantClient client.Client,
	objects []*unstructured.Unstructured,
	tenant *tenancyv1alpha1.Tenant,
	specHash, foundationHash string,
	validateDrift bool,
) (tenantApplyResult, error) {
	var groups [3][]*unstructured.Unstructured
	var crds []*unstructured.Unstructured
	for _, object := range objects {
		group := 2
		switch object.GroupVersionKind().GroupKind() {
		case schema.GroupKind{Kind: "Namespace"}:
			group = 0
		case schema.GroupKind{Group: "apiextensions.k8s.io", Kind: "CustomResourceDefinition"}:
			group = 0
			crds = append(crds, object)
		case schema.GroupKind{Kind: "ServiceAccount"},
			schema.GroupKind{Kind: "ConfigMap"},
			schema.GroupKind{Kind: "Secret"},
			schema.GroupKind{Kind: "Service"},
			schema.GroupKind{Kind: "PersistentVolume"},
			schema.GroupKind{Kind: "PersistentVolumeClaim"},
			schema.GroupKind{Group: "storage.k8s.io", Kind: "StorageClass"},
			schema.GroupKind{Group: "rbac.authorization.k8s.io", Kind: "Role"},
			schema.GroupKind{Group: "rbac.authorization.k8s.io", Kind: "ClusterRole"},
			schema.GroupKind{Group: "rbac.authorization.k8s.io", Kind: "RoleBinding"},
			schema.GroupKind{Group: "rbac.authorization.k8s.io", Kind: "ClusterRoleBinding"}:
			group = 1
		}
		groups[group] = append(groups[group], object)
	}
	var result tenantApplyResult
	for index, group := range groups {
		for _, desired := range group {
			created, err := ensureStaticTenantObject(
				ctx,
				tenantClient,
				desired,
				tenant,
				specHash,
				foundationHash,
				validateDrift,
			)
			result.Created = result.Created || created
			if err != nil {
				return result, err
			}
		}
		if index == 0 {
			ready, err := tenantCRDsReady(ctx, tenantClient, crds)
			if err != nil {
				return result, err
			}
			if !ready {
				result.Pending = true
				return result, nil
			}
		}
	}
	return result, nil
}

func tenantCRDsReady(ctx context.Context, tenantClient client.Client, crds []*unstructured.Unstructured) (bool, error) {
	for _, desired := range crds {
		current := &unstructured.Unstructured{}
		current.SetGroupVersionKind(desired.GroupVersionKind())
		if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(desired), current); err != nil {
			return false, err
		}
		if current.GetDeletionTimestamp() != nil {
			return false, nil
		}
		conditions, _, _ := unstructured.NestedSlice(current.Object, "status", "conditions")
		established := false
		for _, raw := range conditions {
			condition, ok := raw.(map[string]any)
			if ok && condition["type"] == "Established" && condition["status"] == "True" {
				established = true
			}
		}
		if !established {
			return false, nil
		}
		group, _, _ := unstructured.NestedString(desired.Object, "spec", "group")
		kind, _, _ := unstructured.NestedString(desired.Object, "spec", "names", "kind")
		versions, _, _ := unstructured.NestedSlice(desired.Object, "spec", "versions")
		for _, raw := range versions {
			version, ok := raw.(map[string]any)
			if !ok || version["served"] != true {
				continue
			}
			name, _ := version["name"].(string)
			if _, err := tenantClient.RESTMapper().RESTMapping(schema.GroupKind{Group: group, Kind: kind}, name); err != nil {
				if meta.IsNoMatchError(err) {
					return false, nil
				}
				return false, err
			}
		}
	}
	return true, nil
}
