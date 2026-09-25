package controller

import (
	"context"
	"errors"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

var (
	errWorkerOwnershipInvalid = errors.New("worker ownership is invalid")
	machineGVK                = schema.GroupVersionKind{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "Machine"}
	postCNIDevMachineGVK      = schema.GroupVersionKind{Group: "infrastructure.cluster.x-k8s.io", Version: "v1beta2", Kind: "DevMachine"}
	postCNINodeGVK            = schema.GroupVersionKind{Version: "v1", Kind: "Node"}
)

type postCNIWorkerState struct {
	machines          []*unstructured.Unstructured
	devMachines       []*unstructured.Unstructured
	nodes             []*unstructured.Unstructured
	containers        []DockerContainer
	inventoryComplete bool
	allReady          bool
	networkReady      bool
}

func (reconciler *TenantReconciler) observePostCNIWorkerState(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (postCNIWorkerState, error) {
	state := postCNIWorkerState{}
	machines := &unstructured.UnstructuredList{}
	machines.SetGroupVersionKind(machineGVK.GroupVersion().WithKind("MachineList"))
	if err := reconciler.reader().List(ctx, machines, client.InNamespace(tenant.Name), client.MatchingLabels{"cluster.x-k8s.io/cluster-name": tenant.Name}); err != nil {
		return state, fmt.Errorf("list Tenant Machines: %w", err)
	}
	machineDeployment := &unstructured.Unstructured{}
	machineDeployment.SetGroupVersionKind(machineDeploymentGVK)
	if err := reconciler.reader().Get(ctx, client.ObjectKey{Namespace: tenant.Name, Name: tenant.Name + "-worker"}, machineDeployment); err != nil {
		return state, fmt.Errorf("read Tenant MachineDeployment: %w", err)
	}
	if err := validateRootOwnership(machineDeployment, tenant, specHash, foundation.Hash, "machine-deployment", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return state, err
	}
	machinesReady := true
	machineNames := map[string]struct{}{}
	machineByUID := map[string]*unstructured.Unstructured{}
	for index := range machines.Items {
		machine := machines.Items[index].DeepCopy()
		if err := validateRootOwnership(machine, tenant, specHash, foundation.Hash, "machine", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return state, err
		}
		if err := validateOwnerChain(ctx, reconciler.reader(), machine, machineDeployment.GetUID()); err != nil {
			return state, err
		}
		if !tenantObjectReady(machine) {
			machinesReady = false
		}
		machineNames[machine.GetName()] = struct{}{}
		machineByUID[string(machine.GetUID())] = machine
		state.machines = append(state.machines, machine)
	}
	containers, err := reconciler.docker().ListWorkerContainers(ctx, tenant.Name)
	if err != nil {
		return state, fmt.Errorf("list Tenant worker containers: %w", err)
	}
	for _, container := range containers {
		if _, expected := machineNames[container.Name]; !expected {
			return state, fmt.Errorf("%w: worker container %s has no exact Machine", errWorkerOwnershipInvalid, container.Name)
		}
		attached := false
		for _, networkID := range container.Networks {
			attached = attached || networkID == foundation.NetworkID
		}
		if !attached {
			return state, fmt.Errorf("%w: worker container %s is not on the foundation network", errWorkerOwnershipInvalid, container.Name)
		}
		if container.State != "running" {
			return state, nil
		}
		state.containers = append(state.containers, container)
	}
	if len(machines.Items) != int(canonical.Workers) || len(containers) != int(canonical.Workers) {
		return state, nil
	}
	devMachines := &unstructured.UnstructuredList{}
	devMachines.SetGroupVersionKind(postCNIDevMachineGVK.GroupVersion().WithKind("DevMachineList"))
	if err := reconciler.reader().List(ctx, devMachines, client.InNamespace(tenant.Name), client.MatchingLabels{"cluster.x-k8s.io/cluster-name": tenant.Name}); err != nil {
		return state, err
	}
	if len(devMachines.Items) != int(canonical.Workers) {
		return state, nil
	}
	devMachinesReady := true
	for index := range devMachines.Items {
		item := &devMachines.Items[index]
		item.SetGroupVersionKind(postCNIDevMachineGVK)
		owners := item.GetOwnerReferences()
		if len(owners) != 1 {
			return state, fmt.Errorf("%w: DevMachine %s owner chain is invalid", errWorkerOwnershipInvalid, item.GetName())
		}
		root, present := machineByUID[string(owners[0].UID)]
		if !present {
			return state, fmt.Errorf("%w: DevMachine %s owner does not match an exact Machine", errWorkerOwnershipInvalid, item.GetName())
		}
		if item.GetName() != root.GetName() {
			return state, fmt.Errorf("%w: DevMachine %s name does not match its exact Machine %s", errWorkerOwnershipInvalid, item.GetName(), root.GetName())
		}
		if err := validateOwnerChain(ctx, reconciler.reader(), item, root.GetUID()); err != nil {
			return state, fmt.Errorf("%w: %v", errWorkerOwnershipInvalid, err)
		}
		if !tenantObjectReady(item) {
			devMachinesReady = false
		}
		state.devMachines = append(state.devMachines, item.DeepCopy())
	}
	nodes := &unstructured.UnstructuredList{}
	nodes.SetGroupVersionKind(schema.GroupVersionKind{Version: "v1", Kind: "NodeList"})
	if err := tenantClient.List(ctx, nodes); err != nil {
		return state, err
	}
	if len(nodes.Items) != int(canonical.Workers) {
		return state, nil
	}
	nodesReady := true
	for index := range nodes.Items {
		nodes.Items[index].SetGroupVersionKind(postCNINodeGVK)
		if _, expected := machineNames[nodes.Items[index].GetName()]; !expected {
			return state, fmt.Errorf("%w: Node %s has no exact Machine", errWorkerOwnershipInvalid, nodes.Items[index].GetName())
		}
		if !tenantObjectReady(&nodes.Items[index]) {
			nodesReady = false
		}
		state.nodes = append(state.nodes, nodes.Items[index].DeepCopy())
	}
	state.inventoryComplete = true
	state.allReady = machinesReady && devMachinesReady && nodesReady
	networkReady, err := networkWorkloadsReady(ctx, tenantClient)
	if err != nil {
		return state, err
	}
	state.networkReady = networkReady
	return state, nil
}

func networkWorkloadsReady(ctx context.Context, tenantClient client.Client) (bool, error) {
	for _, item := range []struct {
		gvk             schema.GroupVersionKind
		namespace, name string
	}{
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "DaemonSet"}, "kube-system", "calico-node"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "kube-system", "calico-kube-controllers"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "DaemonSet"}, "kube-system", "capi-kube-proxy"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "kube-system", "coredns"},
	} {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(item.gvk)
		if err := tenantClient.Get(ctx, types.NamespacedName{Namespace: item.namespace, Name: item.name}, object); err != nil {
			if apierrors.IsNotFound(err) {
				return false, nil
			}
			return false, err
		}
		if !workloadAvailable(object) {
			return false, nil
		}
	}
	return true, nil
}
