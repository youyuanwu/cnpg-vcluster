package controller

import (
	"context"
	"fmt"
	"sort"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
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
	workloadsCleanupComplete bool,
) (bool, error) {
	values := append([]tenancyv1alpha1.ObservedResourceIdentity(nil), tenant.Status.TenantResources...)
	sort.Slice(values, func(left, right int) bool {
		leftPriority := tenantDeletePriority(values[left].Kind)
		rightPriority := tenantDeletePriority(values[right].Kind)
		if leftPriority != rightPriority {
			return leftPriority < rightPriority
		}
		leftIdentity := values[left].APIVersion + "/" + values[left].Kind + "/" + values[left].Namespace + "/" + values[left].Name
		rightIdentity := values[right].APIVersion + "/" + values[right].Kind + "/" + values[right].Namespace + "/" + values[right].Name
		return leftIdentity < rightIdentity
	})
	for _, identity := range values {
		if identity.Kind == "Node" {
			continue
		}
		isDefinitionOrNamespace := identity.Kind == "CustomResourceDefinition" || identity.Kind == "Namespace"
		if workloadsCleanupComplete != isDefinitionOrNamespace {
			continue
		}
		gvk := schema.FromAPIVersionAndKind(identity.APIVersion, identity.Kind)
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(gvk)
		err := tenantClient.Get(ctx, client.ObjectKey{Namespace: identity.Namespace, Name: identity.Name}, object)
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return false, err
		}
		annotations := object.GetAnnotations()
		if string(object.GetUID()) != identity.UID ||
			annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
			annotations[resources.SpecHashAnnotation] != specHash ||
			annotations[resources.FoundationAnnotation] != foundationHash {
			return false, fmt.Errorf("tenant resource %s/%s ownership changed before cleanup", identity.Kind, identity.Name)
		}
		uid := object.GetUID()
		resourceVersion := object.GetResourceVersion()
		propagation := metav1.DeletePropagationBackground
		if err := tenantClient.Delete(ctx, object, &client.DeleteOptions{
			Preconditions:     &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
			PropagationPolicy: &propagation,
		}); err != nil && !apierrors.IsNotFound(err) {
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
