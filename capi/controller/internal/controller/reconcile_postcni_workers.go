package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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

func (reconciler *TenantReconciler) reconcilePostCNIWorkers(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	if tenant.Status.Stage != tenancyv1alpha1.StageNetworkReady {
		return reconciler.reconcileStorage(ctx, tenant, canonical, specHash, foundation)
	}
	state, err := reconciler.observePostCNIWorkerState(ctx, tenant, canonical, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !state.inventoryComplete || !state.allReady {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		recordPostCNIWorkerState(status, state)
		status.Stage = tenancyv1alpha1.StagePostCNIWorkersReady
		setCondition(status, tenant, "WorkersReady", metav1.ConditionTrue, "WorkersReady", "Exact post-CNI workers and Nodes are ready")
		return nil
	})
}

func (reconciler *TenantReconciler) observePostCNIWorkerState(
	ctx context.Context,
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
	machineByUID := map[string]tenancyv1alpha1.ObservedResourceIdentity{}
	machineNames := map[string]struct{}{}
	for _, machine := range machines {
		identity := identityFor(machine)
		machineByUID[identity.UID] = identity
		machineNames[identity.Name] = struct{}{}
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
		if item.GetName() != root.Name {
			return state, fmt.Errorf("%w: DevMachine %s name does not match its exact Machine %s", errWorkerOwnershipInvalid, item.GetName(), root.Name)
		}
		if err := validateOwnerChain(ctx, reconciler.reader(), item, root); err != nil {
			return state, fmt.Errorf("%w: %v", errWorkerOwnershipInvalid, err)
		}
		if !tenantObjectReady(item) {
			devMachinesReady = false
		}
		state.devMachines = append(state.devMachines, item.DeepCopy())
	}
	tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
	if err != nil {
		return state, err
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

func recordPostCNIWorkerState(status *tenancyv1alpha1.TenantStatus, state postCNIWorkerState) {
	replaceMachineIdentities(status, state.machines)
	status.ObservedResources = replaceResourceIdentities(status.ObservedResources, postCNIDevMachineGVK, state.devMachines)
	status.TenantResources = replaceResourceIdentities(status.TenantResources, postCNINodeGVK, state.nodes)
	status.WorkerContainers = workerContainerReferences(state.containers)
}
