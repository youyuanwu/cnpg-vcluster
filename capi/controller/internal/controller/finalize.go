package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

var deletionDescendantGVKs = []schema.GroupVersionKind{
	{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "MachineSet"},
	machineGVK,
	postCNIDevMachineGVK,
	{Group: "bootstrap.cluster.x-k8s.io", Version: "v1beta2", Kind: "KubeadmConfig"},
}

func (reconciler *TenantReconciler) finalizeTenant(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (ctrl.Result, error) {
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.Phase = tenancyv1alpha1.PhaseDeleting
		setCondition(status, tenant, "Deleting", metav1.ConditionTrue, "Deleting", "Tenant deletion is in progress")
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, "Deleting", "Tenant deletion is in progress")
		return nil
	}); err != nil {
		return ctrl.Result{}, err
	}

	present, err := reconciler.validatePartialDeletionState(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if tenant.Status.FoundationHash == "" {
		if !present {
			return ctrl.Result{}, reconciler.removeTenantFinalizer(ctx, tenant.Name)
		}
		return progressRequeue(), reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.FoundationHash = foundation.Hash
			return nil
		})
	}
	if tenant.Status.FoundationHash != foundation.Hash {
		return ctrl.Result{}, fmt.Errorf("Tenant foundation identity changed")
	}

	cluster, err := reconciler.observeDeletionCluster(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if cluster != nil && tenant.Status.ClusterUID == "" {
		return progressRequeue(), reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.ClusterUID = string(cluster.GetUID())
			return nil
		})
	}
	clusterAbsent, err := reconciler.deleteExactUnstructured(ctx, tenant, specHash, foundation, clusterGVK, tenant.Name, tenant.Name, "cluster")
	if err != nil {
		return ctrl.Result{}, err
	}
	if !clusterAbsent {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	for _, item := range []struct {
		gvk      schema.GroupVersionKind
		name     string
		resource string
	}{
		{kubeadmTemplateGVK, tenant.Name + "-worker", "kubeadm-config-template"},
		{devMachineTemplateGVK, tenant.Name + "-worker", "dev-machine-template"},
		{machineDeploymentGVK, tenant.Name + "-worker", "machine-deployment"},
		{controlPlaneGVK, tenant.Name, "kamaji-control-plane"},
		{devClusterGVK, tenant.Name, "dev-cluster"},
	} {
		absent, err := reconciler.deleteExactUnstructured(ctx, tenant, specHash, foundation, item.gvk, tenant.Name, item.name, item.resource)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !absent {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
		}
	}
	descendantsAbsent, err := reconciler.deletionDescendantsAbsent(ctx, tenant)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !descendantsAbsent {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	hostAbsent, err := reconciler.deleteTenantHostState(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !hostAbsent {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	namespaceAbsent, err := reconciler.deleteExactNamespace(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !namespaceAbsent {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	if err := releaseEndpoint(ctx, reconciler.Client, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant); err != nil {
		return ctrl.Result{}, err
	}
	if tenant.Status.Endpoint != "" {
		return progressRequeue(), reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Endpoint = ""
			return nil
		})
	}
	if err := reconciler.removeTenantFinalizer(ctx, tenant.Name); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (reconciler *TenantReconciler) observeDeletionCluster(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (*unstructured.Unstructured, error) {
	cluster := &unstructured.Unstructured{}
	cluster.SetGroupVersionKind(clusterGVK)
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name}, cluster)
	if apierrors.IsNotFound(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := validateRootOwnership(cluster, tenant, specHash, foundation.Hash, "cluster", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return nil, err
	}
	if err := validateClusterUID(tenant, cluster); err != nil {
		return nil, err
	}
	if err := validateProviderOwner(ctx, reconciler.reader(), cluster, tenant.Name, false); err != nil {
		return nil, err
	}
	return cluster, nil
}

func (reconciler *TenantReconciler) deletionDescendantsAbsent(ctx context.Context, tenant *tenancyv1alpha1.Tenant) (bool, error) {
	for _, gvk := range deletionDescendantGVKs {
		objects := &unstructured.UnstructuredList{}
		objects.SetGroupVersionKind(gvk.GroupVersion().WithKind(gvk.Kind + "List"))
		// The Namespace is dedicated to this Tenant. Even unlabelled provider
		// residue must disappear before host storage or the Namespace is removed.
		if err := reconciler.reader().List(ctx, objects, client.InNamespace(tenant.Name)); err != nil {
			// NoMatch/discovery failures also block host and Namespace destruction:
			// provider residue cannot be authoritatively inspected.
			return false, fmt.Errorf("inspect %s descendants before cleanup: %w", gvk.Kind, err)
		}
		if len(objects.Items) != 0 {
			return false, nil
		}
	}
	return true, nil
}

func (reconciler *TenantReconciler) validatePartialDeletionState(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
	present := false
	endpointMissing := false
	if _, endpointPresent, err := observeEndpoint(ctx, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant, specHash); err != nil {
		if tenant.Status.Endpoint == "" || !errors.Is(err, errEndpointAllocationMissing) {
			return false, err
		}
		endpointMissing = true
	} else if endpointPresent {
		present = true
	}
	var namespace corev1.Namespace
	err := reconciler.reader().Get(ctx, types.NamespacedName{Name: tenant.Name}, &namespace)
	if apierrors.IsNotFound(err) {
	} else if err != nil {
		return false, err
	} else {
		present = true
		if err := validateRootOwnership(&namespace, tenant, specHash, foundation.Hash, "namespace", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return false, err
		}
		if len(namespace.OwnerReferences) != 0 {
			return false, fmt.Errorf("Namespace %s has an unexpected provider owner", namespace.Name)
		}
	}
	var controlPlane, machineDeployment *unstructured.Unstructured
	for _, item := range []struct {
		gvk      schema.GroupVersionKind
		name     string
		resource string
	}{
		{clusterGVK, tenant.Name, "cluster"},
		{devClusterGVK, tenant.Name, "dev-cluster"},
		{controlPlaneGVK, tenant.Name, "kamaji-control-plane"},
		{kubeadmTemplateGVK, tenant.Name + "-worker", "kubeadm-config-template"},
		{devMachineTemplateGVK, tenant.Name + "-worker", "dev-machine-template"},
		{machineDeploymentGVK, tenant.Name + "-worker", "machine-deployment"},
	} {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(item.gvk)
		err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: item.name}, object)
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return false, err
		}
		present = true
		if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, item.resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return false, err
		}
		if item.gvk == clusterGVK {
			if err := validateClusterUID(tenant, object); err != nil {
				return false, err
			}
			if err := validateProviderOwner(ctx, reconciler.reader(), object, tenant.Name, false); err != nil {
				return false, err
			}
		} else if tenant.Status.ClusterUID == "" {
			if err := validateProviderOwner(ctx, reconciler.reader(), object, tenant.Name, false); err != nil {
				return false, err
			}
		} else if err := validateProviderOwnerForDeletion(ctx, reconciler.reader(), object, tenant); err != nil {
			return false, err
		}
		if item.gvk == controlPlaneGVK {
			controlPlane = object
		}
		if item.gvk == machineDeploymentGVK {
			machineDeployment = object
		}
	}
	machineNames := map[string]types.UID{}
	machines := &unstructured.UnstructuredList{}
	machines.SetGroupVersionKind(machineGVK.GroupVersion().WithKind("MachineList"))
	if err := reconciler.reader().List(ctx, machines, client.InNamespace(tenant.Name), client.MatchingLabels{"cluster.x-k8s.io/cluster-name": tenant.Name}); err != nil {
		if !meta.IsNoMatchError(err) {
			return false, err
		}
	} else {
		for index := range machines.Items {
			machine := &machines.Items[index]
			if err := validateRootOwnership(machine, tenant, specHash, foundation.Hash, "machine", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
				return false, err
			}
			if machineDeployment == nil {
				return false, fmt.Errorf("Machine %s ownership cannot be proven before deletion", machine.GetName())
			}
			if err := validateOwnerChain(ctx, reconciler.reader(), machine, machineDeployment.GetUID()); err != nil {
				return false, err
			}
			present = true
			machineNames[machine.GetName()] = machine.GetUID()
		}
	}
	devMachines := &unstructured.UnstructuredList{}
	devMachines.SetGroupVersionKind(postCNIDevMachineGVK.GroupVersion().WithKind("DevMachineList"))
	if err := reconciler.reader().List(ctx, devMachines, client.InNamespace(tenant.Name), client.MatchingLabels{"cluster.x-k8s.io/cluster-name": tenant.Name}); err != nil {
		if !meta.IsNoMatchError(err) {
			return false, err
		}
	} else {
		for index := range devMachines.Items {
			devMachine := &devMachines.Items[index]
			if err := validateRootOwnership(devMachine, tenant, specHash, foundation.Hash, "machine", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
				return false, err
			}
			owners := devMachine.GetOwnerReferences()
			if len(owners) != 1 || owners[0].UID == "" ||
				owners[0].APIVersion != machineGVK.GroupVersion().String() ||
				owners[0].Kind != machineGVK.Kind ||
				machineNames[owners[0].Name] != owners[0].UID {
				return false, fmt.Errorf("DevMachine %s ownership cannot be proven before deletion", devMachine.GetName())
			}
			present = true
		}
	}
	containers, err := reconciler.docker().ListWorkerContainers(ctx, tenant.Name)
	if err != nil {
		return false, fmt.Errorf("inspect provider-owned worker containers: %w", err)
	}
	for _, container := range containers {
		if _, expected := machineNames[container.Name]; !expected {
			return false, fmt.Errorf("worker container %s ownership cannot be proven before deletion", container.Name)
		}
		present = true
	}
	descendantsAbsent, err := reconciler.deletionDescendantsAbsent(ctx, tenant)
	if err != nil {
		return false, err
	}
	present = present || !descendantsAbsent
	var secret corev1.Secret
	err = reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name + "-kubeconfig"}, &secret)
	if apierrors.IsNotFound(err) {
	} else if err != nil {
		return false, err
	} else {
		present = true
		owners := secret.OwnerReferences
		if controlPlane == nil || len(owners) != 1 ||
			owners[0].APIVersion != controlPlaneGVK.GroupVersion().String() ||
			owners[0].Kind != controlPlaneGVK.Kind ||
			owners[0].Name != controlPlane.GetName() ||
			owners[0].UID != controlPlane.GetUID() {
			return false, fmt.Errorf("Tenant kubeconfig Secret ownership cannot be proven before deletion")
		}
	}
	volumeName := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
	volume, err := reconciler.docker().InspectVolume(ctx, volumeName)
	if err != nil {
		return false, err
	}
	if volume != nil {
		present = true
		expected := map[string]string{
			foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
			"cnpg-vcluster.capi/role":                  "tenant-storage",
			"cnpg-vcluster.capi/tenant":                tenant.Name,
			"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
			"tenancy.cnpg-vcluster.io/spec-hash":       specHash,
			"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
		}
		if volume.Name != volumeName || !stringMapEqual(volume.Labels, expected) {
			return false, fmt.Errorf("Docker volume ownership cannot be proven before deletion")
		}
	}
	if endpointMissing && present {
		return false, fmt.Errorf("Tenant endpoint allocation is missing before terminal cleanup")
	}
	return present, nil
}

func (reconciler *TenantReconciler) removeTenantFinalizer(ctx context.Context, name string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var current tenancyv1alpha1.Tenant
		if err := reconciler.reader().Get(ctx, types.NamespacedName{Name: name}, &current); err != nil {
			if apierrors.IsNotFound(err) {
				return nil
			}
			return err
		}
		if !containsString(current.Finalizers, tenantFinalizer) {
			return nil
		}
		current.Finalizers = removeString(current.Finalizers, tenantFinalizer)
		return reconciler.Update(ctx, &current)
	})
}

func removeString(values []string, target string) []string {
	result := values[:0]
	for _, value := range values {
		if value != target {
			result = append(result, value)
		}
	}
	return result
}
