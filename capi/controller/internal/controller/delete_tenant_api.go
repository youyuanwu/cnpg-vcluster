package controller

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

func deleteTenantResources(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	specHash,
	foundationHash string,
	catalog []cleanupCoordinate,
) (bool, error) {
	for _, coordinate := range catalog {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(coordinate.GVK)
		err := tenantClient.Get(ctx, client.ObjectKey{
			Namespace: coordinate.Namespace,
			Name:      coordinate.Name,
		}, object)
		if apierrors.IsNotFound(err) || meta.IsNoMatchError(err) {
			continue
		}
		if err != nil {
			return false, err
		}
		annotations := object.GetAnnotations()
		if annotations[resources.TenantAnnotation] != tenant.Name ||
			annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
			annotations[resources.SpecHashAnnotation] != specHash ||
			annotations[resources.FoundationAnnotation] != foundationHash ||
			annotations[resources.ResourceAnnotation] != coordinate.Resource {
			return false, fmt.Errorf(
				"tenant resource %s/%s ownership changed before cleanup",
				object.GetKind(),
				object.GetName(),
			)
		}
		if !object.GetDeletionTimestamp().IsZero() {
			return false, nil
		}
		uid := object.GetUID()
		resourceVersion := object.GetResourceVersion()
		propagation := metav1.DeletePropagationBackground
		if err := tenantClient.Delete(ctx, object, &client.DeleteOptions{
			Preconditions:     &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
			PropagationPolicy: &propagation,
		}); err != nil && !apierrors.IsNotFound(err) && !apierrors.IsConflict(err) {
			return false, err
		}
		return false, nil
	}
	return true, nil
}

func tenantDeletePriority(kind string) int {
	switch kind {
	case "Cluster":
		return 0
	case "Deployment", "DaemonSet", "StatefulSet":
		return 1
	case "Pod", "PodDisruptionBudget":
		return 2
	case "PersistentVolumeClaim":
		return 3
	case "PersistentVolume":
		return 4
	case "StorageClass":
		return 5
	case "CustomResourceDefinition":
		return 7
	case "Namespace":
		return 8
	default:
		return 6
	}
}
