package controller

import (
	"context"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func (reconciler *TenantReconciler) deleteExactUnstructured(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation, gvk schema.GroupVersionKind, namespace, name, resource string) (bool, error) {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(gvk)
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, object)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return false, err
	}
	if gvk == clusterGVK {
		if err := validateClusterUID(tenant, object); err != nil {
			return false, err
		}
	} else if err := validateProviderOwnerForDeletion(ctx, reconciler.reader(), object, tenant); err != nil {
		return false, err
	}
	if !object.GetDeletionTimestamp().IsZero() {
		return false, nil
	}
	uid := object.GetUID()
	resourceVersion := object.GetResourceVersion()
	propagation := metav1.DeletePropagationBackground
	err = reconciler.Delete(ctx, object, &client.DeleteOptions{
		Preconditions:     &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
		PropagationPolicy: &propagation,
	})
	if err != nil && !apierrors.IsNotFound(err) && !apierrors.IsConflict(err) {
		return false, err
	}
	return false, nil
}

func (reconciler *TenantReconciler) deleteExactNamespace(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	var namespace corev1.Namespace
	err := reconciler.reader().Get(ctx, types.NamespacedName{Name: tenant.Name}, &namespace)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	if err := validateRootOwnership(&namespace, tenant, specHash, foundation.Hash, "namespace", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return false, err
	}
	namespace.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Namespace"))
	if namespace.DeletionTimestamp != nil {
		return false, nil
	}
	uid := namespace.UID
	resourceVersion := namespace.ResourceVersion
	propagation := metav1.DeletePropagationBackground
	err = reconciler.Delete(ctx, &namespace, &client.DeleteOptions{
		Preconditions:     &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
		PropagationPolicy: &propagation,
	})
	if err != nil && !apierrors.IsNotFound(err) && !apierrors.IsConflict(err) {
		return false, err
	}
	return false, nil
}
