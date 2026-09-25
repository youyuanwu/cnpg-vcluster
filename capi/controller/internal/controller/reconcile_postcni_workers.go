package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

var (
	errWorkerOwnershipInvalid = errors.New("worker ownership is invalid")
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
}

func (reconciler *TenantReconciler) reconcilePostCNIWorkers(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (ctrl.Result, error) {
	state, err := reconciler.observePostCNIWorkerState(ctx, tenantClient, tenant, canonical, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !state.inventoryComplete || !state.allReady {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	return reconciler.reconcileStorage(ctx, tenantClient, tenant, canonical, specHash, foundation)
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
	machines, containers, err := reconciler.observePreCNIWorkers(ctx, tenant, specHash, foundation)
	if err == errWorkerRuntimePending {
		return state, nil
	}
	if err != nil {
		return state, err
	}
	state.machines = machines
	state.containers = containers
	if len(machines) != int(canonical.Workers) || len(containers) != int(canonical.Workers) {
		return state, nil
	}
	machinesReady := true
	for _, machine := range machines {
		if !tenantObjectReady(machine) {
			machinesReady = false
		}
	}
	devMachines := &unstructured.UnstructuredList{}
	devMachines.SetGroupVersionKind(postCNIDevMachineGVK.GroupVersion().WithKind("DevMachineList"))
	if err := reconciler.reader().List(ctx, devMachines, client.InNamespace(tenant.Name), client.MatchingLabels{"cluster.x-k8s.io/cluster-name": tenant.Name}); err != nil {
		return state, err
	}
	if len(devMachines.Items) != int(canonical.Workers) {
		return state, nil
	}
	machineByUID := map[string]*unstructured.Unstructured{}
	machineNames := map[string]struct{}{}
	for _, machine := range machines {
		machineByUID[string(machine.GetUID())] = machine
		machineNames[machine.GetName()] = struct{}{}
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
	return state, nil
}
