package controller

import (
	"context"
	"errors"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

var errProviderOwnerPending = errors.New("provider owner is pending")

func validateRootOwnership(object metav1.Object, tenant *tenancyv1alpha1.Tenant, specHash, foundationHash, resource string, ownershipLabel, labPrefix string) error {
	if object.GetUID() == "" {
		return fmt.Errorf("%s has no UID", resource)
	}
	labels := object.GetLabels()
	annotations := object.GetAnnotations()
	if labels[ownershipLabel] != labPrefix ||
		annotations[resources.TenantAnnotation] != tenant.Name ||
		annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
		annotations[resources.SpecHashAnnotation] != specHash ||
		annotations[resources.FoundationAnnotation] != foundationHash ||
		annotations[resources.ResourceAnnotation] != resource {
		return fmt.Errorf("%s ownership markers do not match the Tenant", resource)
	}
	for _, owner := range object.GetOwnerReferences() {
		if owner.APIVersion == tenancyv1alpha1.GroupVersion.String() && owner.Kind == "Tenant" {
			return fmt.Errorf("%s must not have a Tenant owner reference", resource)
		}
	}
	return nil
}

func validateOwnerChain(ctx context.Context, reader client.Reader, object *unstructured.Unstructured, expectedRootUID types.UID) error {
	current := object
	visited := map[types.UID]struct{}{}
	for {
		if current.GetUID() == expectedRootUID {
			return nil
		}
		if _, duplicate := visited[current.GetUID()]; duplicate {
			return fmt.Errorf("provider owner chain contains a cycle")
		}
		visited[current.GetUID()] = struct{}{}
		owners := current.GetOwnerReferences()
		if len(owners) != 1 || owners[0].UID == "" {
			return fmt.Errorf("%s/%s has no exact provider owner chain", current.GetKind(), current.GetName())
		}
		owner := owners[0]
		groupVersion, err := schema.ParseGroupVersion(owner.APIVersion)
		if err != nil {
			return fmt.Errorf("parse provider owner API version: %w", err)
		}
		next := &unstructured.Unstructured{}
		next.SetGroupVersionKind(groupVersion.WithKind(owner.Kind))
		if err := reader.Get(ctx, types.NamespacedName{Namespace: current.GetNamespace(), Name: owner.Name}, next); err != nil {
			return fmt.Errorf("read provider owner %s/%s: %w", owner.Kind, owner.Name, err)
		}
		if next.GetUID() != owner.UID {
			return fmt.Errorf("provider owner UID mismatch")
		}
		current = next
	}
}

func validateProviderOwner(ctx context.Context, reader client.Reader, object *unstructured.Unstructured, tenantName string, required bool) error {
	switch object.GetKind() {
	case "Namespace", "Cluster":
		if len(object.GetOwnerReferences()) != 0 {
			return fmt.Errorf("%s %s has an unexpected provider owner", object.GetKind(), object.GetName())
		}
		return nil
	}

	expected := make([]metav1.OwnerReference, 0, 2)
	addExpected := func(gvk schema.GroupVersionKind, name string) error {
		value := &unstructured.Unstructured{}
		value.SetGroupVersionKind(gvk)
		err := reader.Get(ctx, types.NamespacedName{Namespace: tenantName, Name: name}, value)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		expected = append(expected, metav1.OwnerReference{
			APIVersion: gvk.GroupVersion().String(),
			Kind:       gvk.Kind,
			Name:       name,
			UID:        value.GetUID(),
		})
		return nil
	}

	switch object.GetKind() {
	case "DevCluster", "KamajiControlPlane", "MachineDeployment":
		if err := addExpected(clusterGVK, tenantName); err != nil {
			return err
		}
	case "KubeadmConfigTemplate", "DevMachineTemplate":
		if err := addExpected(clusterGVK, tenantName); err != nil {
			return err
		}
		if err := addExpected(machineDeploymentGVK, tenantName+"-worker"); err != nil {
			return err
		}
	default:
		return nil
	}

	owners := object.GetOwnerReferences()
	if len(owners) == 0 {
		if required {
			return errProviderOwnerPending
		}
		return nil
	}
	if len(owners) != 1 {
		return fmt.Errorf("%s %s has an unexpected provider owner", object.GetKind(), object.GetName())
	}
	for _, owner := range expected {
		if owners[0].UID == owner.UID &&
			owners[0].Kind == owner.Kind &&
			owners[0].APIVersion == owner.APIVersion &&
			owners[0].Name == owner.Name {
			return nil
		}
	}
	return fmt.Errorf("%s %s has an unexpected provider owner", object.GetKind(), object.GetName())
}

func validateProviderOwnerForDeletion(ctx context.Context, reader client.Reader, object *unstructured.Unstructured, tenant *tenancyv1alpha1.Tenant) error {
	owners := object.GetOwnerReferences()
	if len(owners) == 0 {
		return nil
	}
	if len(owners) != 1 {
		return fmt.Errorf("%s %s has an unexpected provider owner", object.GetKind(), object.GetName())
	}
	owner := owners[0]
	clusterOwner := owner.APIVersion == clusterGVK.GroupVersion().String() &&
		owner.Kind == clusterGVK.Kind &&
		owner.Name == tenant.Name &&
		tenant.Status.ClusterUID != "" &&
		owner.UID == types.UID(tenant.Status.ClusterUID)
	switch object.GetKind() {
	case "DevCluster", "KamajiControlPlane", "MachineDeployment":
		if clusterOwner {
			return nil
		}
	case "KubeadmConfigTemplate", "DevMachineTemplate":
		if clusterOwner {
			return nil
		}
		if owner.APIVersion == machineDeploymentGVK.GroupVersion().String() &&
			owner.Kind == machineDeploymentGVK.Kind &&
			owner.Name == tenant.Name+"-worker" {
			machineDeployment := &unstructured.Unstructured{}
			machineDeployment.SetGroupVersionKind(machineDeploymentGVK)
			if err := reader.Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: owner.Name}, machineDeployment); err != nil {
				return fmt.Errorf("read template provider owner %s: %w", owner.Name, err)
			}
			if machineDeployment.GetUID() == owner.UID {
				return nil
			}
		}
	default:
		return nil
	}
	return fmt.Errorf("%s %s has an unexpected provider owner", object.GetKind(), object.GetName())
}

func validateClusterUID(tenant *tenancyv1alpha1.Tenant, object metav1.Object) error {
	if tenant.Status.ClusterUID != "" && tenant.Status.ClusterUID != string(object.GetUID()) {
		return fmt.Errorf(
			"Cluster identity changed from %s to %s",
			tenant.Status.ClusterUID,
			object.GetUID(),
		)
	}
	return nil
}

func validateKubeconfigSecret(secret *corev1.Secret, controlPlane *unstructured.Unstructured) error {
	if secret.Type != corev1.SecretType("cluster.x-k8s.io/secret") || len(secret.Data["value"]) == 0 {
		return fmt.Errorf("Tenant kubeconfig Secret contract is invalid")
	}
	if controlPlane == nil || !hasOwnerUID(secret.OwnerReferences, controlPlane.GetUID()) {
		return fmt.Errorf("Tenant kubeconfig Secret owner cannot be proven")
	}
	return nil
}
