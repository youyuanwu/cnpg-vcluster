package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcilePostCNIWorkers(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	if tenant.Status.Stage != tenancyv1alpha1.StageNetworkReady {
		return reconciler.reconcileStorage(ctx, tenant, canonical, specHash, foundation)
	}
	machines, containers, err := reconciler.observePreCNIWorkers(ctx, tenant, specHash, foundation)
	if err != nil {
		if err == errWorkerRuntimePending {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		return ctrl.Result{}, err
	}
	if len(machines) != int(canonical.Workers) || len(containers) != int(canonical.Workers) {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	for _, machine := range machines {
		if !tenantObjectReady(machine) {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
	}
	devMachines := &unstructured.UnstructuredList{}
	devMachines.SetGroupVersionKind(schema.GroupVersionKind{Group: "infrastructure.cluster.x-k8s.io", Version: "v1beta2", Kind: "DevMachineList"})
	if err := reconciler.reader().List(ctx, devMachines, client.InNamespace(tenant.Name), client.MatchingLabels{"cluster.x-k8s.io/cluster-name": tenant.Name}); err != nil {
		return ctrl.Result{}, err
	}
	if len(devMachines.Items) != int(canonical.Workers) {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	machineByUID := map[string]tenancyv1alpha1.ObservedResourceIdentity{}
	for _, machine := range machines {
		identity := identityFor(machine)
		machineByUID[identity.UID] = identity
	}
	for index := range devMachines.Items {
		item := &devMachines.Items[index]
		owners := item.GetOwnerReferences()
		if len(owners) != 1 {
			return ctrl.Result{}, fmt.Errorf("DevMachine %s owner chain is invalid", item.GetName())
		}
		root, present := machineByUID[string(owners[0].UID)]
		if !present {
			return ctrl.Result{}, fmt.Errorf("DevMachine %s owner does not match an exact Machine", item.GetName())
		}
		if err := validateOwnerChain(ctx, reconciler.reader(), item, root); err != nil {
			return ctrl.Result{}, err
		}
	}
	tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
	if err != nil {
		return ctrl.Result{}, err
	}
	nodes := &unstructured.UnstructuredList{}
	nodes.SetGroupVersionKind(schema.GroupVersionKind{Version: "v1", Kind: "NodeList"})
	if err := tenantClient.List(ctx, nodes); err != nil {
		return ctrl.Result{}, err
	}
	if len(nodes.Items) != int(canonical.Workers) {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	for index := range nodes.Items {
		if !tenantObjectReady(&nodes.Items[index]) {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
	}
	snapshot := make([]string, 0, len(machines)+len(devMachines.Items)+len(nodes.Items)+len(containers))
	for _, machine := range machines {
		snapshot = append(snapshot, "Machine:"+machine.GetName()+":"+string(machine.GetUID()))
	}
	for index := range devMachines.Items {
		item := &devMachines.Items[index]
		snapshot = append(snapshot, "DevMachine:"+item.GetName()+":"+string(item.GetUID()))
	}
	for index := range nodes.Items {
		item := &nodes.Items[index]
		snapshot = append(snapshot, "Node:"+item.GetName()+":"+string(item.GetUID()))
	}
	for _, container := range containers {
		snapshot = append(snapshot, "Container:"+container.Name+":"+container.ID)
	}
	sort.Strings(snapshot)
	encoded, _ := json.Marshal(snapshot)
	digest := sha256.Sum256(encoded)
	return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		replaceMachineIdentities(status, machines)
		for index := range devMachines.Items {
			if err := upsertIdentity(status, identityFor(&devMachines.Items[index])); err != nil {
				return err
			}
		}
		status.TenantResources = removeTenantKind(status.TenantResources, "Node")
		for index := range nodes.Items {
			if err := upsertTenantIdentity(status, identityFor(&nodes.Items[index])); err != nil {
				return err
			}
		}
		status.WorkerSnapshotHash = hex.EncodeToString(digest[:])
		status.Stage = tenancyv1alpha1.StagePostCNIWorkersReady
		setCondition(status, tenant, "WorkersReady", metav1.ConditionTrue, "WorkersReady", "Exact post-CNI workers and Nodes are ready")
		return nil
	})
}

func removeTenantKind(values []tenancyv1alpha1.ObservedResourceIdentity, kind string) []tenancyv1alpha1.ObservedResourceIdentity {
	result := values[:0]
	for _, value := range values {
		if value.Kind != kind {
			result = append(result, value)
		}
	}
	return result
}
