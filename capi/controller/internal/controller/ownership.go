package controller

import (
	"context"
	"errors"
	"fmt"
	"sort"

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

func identityFor(object client.Object) tenancyv1alpha1.ObservedResourceIdentity {
	gvk := object.GetObjectKind().GroupVersionKind()
	return tenancyv1alpha1.ObservedResourceIdentity{
		APIVersion: gvk.GroupVersion().String(),
		Kind:       gvk.Kind,
		Namespace:  object.GetNamespace(),
		Name:       object.GetName(),
		UID:        string(object.GetUID()),
	}
}

func upsertIdentity(status *tenancyv1alpha1.TenantStatus, identity tenancyv1alpha1.ObservedResourceIdentity) error {
	for index := range status.ObservedResources {
		current := &status.ObservedResources[index]
		if current.APIVersion == identity.APIVersion && current.Kind == identity.Kind &&
			current.Namespace == identity.Namespace && current.Name == identity.Name {
			if current.UID != identity.UID {
				return fmt.Errorf("%s %s identity changed from %s to %s", identity.Kind, identity.Name, current.UID, identity.UID)
			}
			return nil
		}
	}
	status.ObservedResources = append(status.ObservedResources, identity)
	sort.Slice(status.ObservedResources, func(left, right int) bool {
		a := status.ObservedResources[left]
		b := status.ObservedResources[right]
		return a.APIVersion+"/"+a.Kind+"/"+a.Namespace+"/"+a.Name <
			b.APIVersion+"/"+b.Kind+"/"+b.Namespace+"/"+b.Name
	})
	return nil
}

func findIdentity(status tenancyv1alpha1.TenantStatus, gvk schema.GroupVersionKind, namespace, name string) *tenancyv1alpha1.ObservedResourceIdentity {
	for index := range status.ObservedResources {
		identity := &status.ObservedResources[index]
		if identity.APIVersion == gvk.GroupVersion().String() && identity.Kind == gvk.Kind &&
			identity.Namespace == namespace && identity.Name == name {
			return identity
		}
	}
	return nil
}

func validateOwnerChain(ctx context.Context, reader client.Reader, object *unstructured.Unstructured, expectedRoot tenancyv1alpha1.ObservedResourceIdentity) error {
	current := object
	visited := map[types.UID]struct{}{}
	for {
		if current.GetUID() == types.UID(expectedRoot.UID) {
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

func validateRecordedResources(ctx context.Context, reader client.Reader, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) error {
	expectedResources := map[string]string{
		"Namespace":             "namespace",
		"Cluster":               "cluster",
		"DevCluster":            "dev-cluster",
		"KamajiControlPlane":    "kamaji-control-plane",
		"KubeadmConfigTemplate": "kubeadm-config-template",
		"DevMachineTemplate":    "dev-machine-template",
		"MachineDeployment":     "machine-deployment",
		"Machine":               "machine",
	}
	controlPlane := findIdentity(tenant.Status, controlPlaneGVK, tenant.Name, tenant.Name)
	for _, identity := range tenant.Status.ObservedResources {
		if identity.Kind == machineGVK.Kind &&
			identity.APIVersion == machineGVK.GroupVersion().String() {
			continue
		}
		groupVersion, err := schema.ParseGroupVersion(identity.APIVersion)
		if err != nil {
			return fmt.Errorf("parse recorded resource API version: %w", err)
		}
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(groupVersion.WithKind(identity.Kind))
		if err := reader.Get(ctx, types.NamespacedName{Namespace: identity.Namespace, Name: identity.Name}, object); err != nil {
			return fmt.Errorf("read recorded %s %s: %w", identity.Kind, identity.Name, err)
		}
		if string(object.GetUID()) != identity.UID {
			return fmt.Errorf("%s %s identity changed from %s to %s", identity.Kind, identity.Name, identity.UID, object.GetUID())
		}
		if identity.Kind == "Secret" {
			if controlPlane == nil || !hasOwnerUID(object.GetOwnerReferences(), types.UID(controlPlane.UID)) {
				return fmt.Errorf("recorded Tenant kubeconfig Secret owner changed")
			}
			continue
		}
		resource, known := expectedResources[identity.Kind]
		if !known {
			return fmt.Errorf("recorded resource kind %s is unsupported", identity.Kind)
		}
		if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return err
		}
		if err := validateProviderOwner(object, tenant.Status, false); err != nil {
			return err
		}
	}

	return nil
}

func validateProviderOwner(object *unstructured.Unstructured, status tenancyv1alpha1.TenantStatus, required bool) error {
	expected := make([]*tenancyv1alpha1.ObservedResourceIdentity, 0, 2)
	switch object.GetKind() {
	case "Namespace", "Cluster":
		if len(object.GetOwnerReferences()) != 0 {
			return fmt.Errorf("%s %s has an unexpected provider owner", object.GetKind(), object.GetName())
		}
		return nil
	case "DevCluster", "KamajiControlPlane", "MachineDeployment":
		if identity := findIdentity(status, clusterGVK, object.GetNamespace(), object.GetNamespace()); identity != nil {
			expected = append(expected, identity)
		}
	case "KubeadmConfigTemplate", "DevMachineTemplate":
		if identity := findIdentity(status, clusterGVK, object.GetNamespace(), object.GetNamespace()); identity != nil {
			expected = append(expected, identity)
		}
		if identity := findIdentity(status, machineDeploymentGVK, object.GetNamespace(), object.GetNamespace()+"-worker"); identity != nil {
			expected = append(expected, identity)
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
	for _, identity := range expected {
		if owners[0].UID == types.UID(identity.UID) &&
			owners[0].Kind == identity.Kind && owners[0].APIVersion == identity.APIVersion {
			return nil
		}
	}
	return fmt.Errorf("%s %s has an unexpected provider owner", object.GetKind(), object.GetName())
}

func validateRecordedUID(status tenancyv1alpha1.TenantStatus, object client.Object) error {
	identity := findIdentity(status, object.GetObjectKind().GroupVersionKind(), object.GetNamespace(), object.GetName())
	if identity != nil && identity.UID != string(object.GetUID()) {
		return fmt.Errorf("%s %s identity changed from %s to %s", identity.Kind, identity.Name, identity.UID, object.GetUID())
	}
	return nil
}
