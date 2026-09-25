package controller

import (
	"context"
	"errors"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func TestObservePostCNIWorkerStateValidatesExactTopologyAndReadiness(t *testing.T) {
	state, nodeLists, err := observePostCNIFixture(t, "worker-a", "worker-a", true, true)
	if err != nil {
		t.Fatal(err)
	}
	if !state.inventoryComplete || !state.allReady || !state.networkReady {
		t.Fatalf("exact ready topology was rejected: %#v", state)
	}
	if nodeLists != 1 {
		t.Fatalf("combined readiness listed Nodes %d times", nodeLists)
	}

	_, _, err = observePostCNIFixture(t, "worker-b", "worker-a", true, true)
	if !errors.Is(err, errWorkerOwnershipInvalid) || !isOwnershipError(err) {
		t.Fatalf("DevMachine name mismatch was not OwnershipInvalid: %v", err)
	}

	_, _, err = observePostCNIFixture(t, "worker-a", "worker-b", true, true)
	if !errors.Is(err, errWorkerOwnershipInvalid) || !isOwnershipError(err) {
		t.Fatalf("Node set mismatch was not OwnershipInvalid: %v", err)
	}

	state, _, err = observePostCNIFixture(t, "worker-a", "worker-a", false, true)
	if err != nil {
		t.Fatal(err)
	}
	if !state.inventoryComplete || state.allReady || !state.networkReady {
		t.Fatalf("worker and network readiness were not independent: %#v", state)
	}

	state, _, err = observePostCNIFixture(t, "worker-a", "worker-a", true, false)
	if err != nil {
		t.Fatal(err)
	}
	if !state.inventoryComplete || !state.allReady || state.networkReady {
		t.Fatalf("unavailable network workload did not remain distinct: %#v", state)
	}
}

type nodeListCountingClient struct {
	client.Client
	nodeLists int
}

func (value *nodeListCountingClient) List(ctx context.Context, list client.ObjectList, options ...client.ListOption) error {
	if list.GetObjectKind().GroupVersionKind().Kind == "NodeList" {
		value.nodeLists++
	}
	return value.Client.List(ctx, list, options...)
}

func observePostCNIFixture(t *testing.T, devMachineName, nodeName string, nodeReady, networkReady bool) (postCNIWorkerState, int, error) {
	t.Helper()
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	tenant := validTenant("tenant-a")
	tenant.UID = "tenant-uid"
	tenant.Status.Endpoint = "172.18.255.1:6443"

	machineSetGVK := schema.GroupVersionKind{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "MachineSet"}
	managementScheme := testScheme(t)
	for _, gvk := range []schema.GroupVersionKind{machineDeploymentGVK, machineSetGVK, machineGVK, postCNIDevMachineGVK} {
		managementScheme.AddKnownTypeWithName(gvk, &unstructured.Unstructured{})
		managementScheme.AddKnownTypeWithName(gvk.GroupVersion().WithKind(gvk.Kind+"List"), &unstructured.UnstructuredList{})
	}
	machineDeployment := topologyObject(machineDeploymentGVK, tenant.Name, tenant.Name+"-worker", "deployment-uid", true)
	machineDeployment.SetLabels(map[string]string{
		foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
	})
	machineDeployment.SetAnnotations(map[string]string{
		resources.TenantAnnotation:     tenant.Name,
		resources.TenantUIDAnnotation:  string(tenant.UID),
		resources.SpecHashAnnotation:   "spec-hash",
		resources.FoundationAnnotation: foundation.Hash,
		resources.ResourceAnnotation:   "machine-deployment",
	})
	machineSet := topologyObject(machineSetGVK, tenant.Name, tenant.Name+"-set", "set-uid", true)
	machineSet.SetOwnerReferences([]metav1.OwnerReference{topologyOwner(machineDeployment)})
	machine := topologyObject(machineGVK, tenant.Name, "worker-a", "machine-uid", true)
	machine.SetLabels(map[string]string{
		"cluster.x-k8s.io/cluster-name":  tenant.Name,
		foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
	})
	machine.SetAnnotations(map[string]string{
		resources.TenantAnnotation:     tenant.Name,
		resources.TenantUIDAnnotation:  string(tenant.UID),
		resources.SpecHashAnnotation:   "spec-hash",
		resources.FoundationAnnotation: foundation.Hash,
		resources.ResourceAnnotation:   "machine",
	})
	machine.SetOwnerReferences([]metav1.OwnerReference{topologyOwner(machineSet)})
	devMachine := topologyObject(postCNIDevMachineGVK, tenant.Name, devMachineName, "devmachine-uid", true)
	devMachine.SetLabels(map[string]string{"cluster.x-k8s.io/cluster-name": tenant.Name})
	devMachine.SetOwnerReferences([]metav1.OwnerReference{topologyOwner(machine)})
	management := fake.NewClientBuilder().
		WithScheme(managementScheme).
		WithObjects(machineDeployment, machineSet, machine, devMachine).
		Build()

	tenantScheme := runtime.NewScheme()
	if err := corev1.AddToScheme(tenantScheme); err != nil {
		t.Fatal(err)
	}
	workloads := []*unstructured.Unstructured{
		workloadObject("apps/v1", "DaemonSet", "calico-node", networkReady),
		workloadObject("apps/v1", "Deployment", "calico-kube-controllers", true),
		workloadObject("apps/v1", "DaemonSet", "capi-kube-proxy", true),
		workloadObject("apps/v1", "Deployment", "coredns", true),
	}
	for _, workload := range workloads {
		gvk := workload.GroupVersionKind()
		tenantScheme.AddKnownTypeWithName(gvk, &unstructured.Unstructured{})
	}
	node := topologyObject(postCNINodeGVK, "", nodeName, "node-uid", nodeReady)
	objects := []client.Object{node}
	for _, workload := range workloads {
		objects = append(objects, workload)
	}
	tenantClient := &nodeListCountingClient{Client: fake.NewClientBuilder().WithScheme(tenantScheme).WithObjects(objects...).Build()}
	reconciler := &TenantReconciler{
		Client:    management,
		APIReader: management,
		Docker: &fakeDockerClient{
			workers: []DockerContainer{{
				Name: "worker-a", ID: "container-uid", State: "running",
				Networks: map[string]string{"kind": foundation.NetworkID},
			}},
		},
	}
	state, err := reconciler.observePostCNIWorkerState(
		context.Background(),
		tenantClient,
		tenant,
		validation.CanonicalSpec{Workers: 1},
		"spec-hash",
		foundation,
	)
	return state, tenantClient.nodeLists, err
}

func workloadObject(apiVersion, kind, name string, available bool) *unstructured.Unstructured {
	desired, current := int64(1), int64(1)
	if !available {
		current = 0
	}
	status := map[string]any{
		"observedGeneration": int64(1),
		"availableReplicas":  current,
	}
	spec := map[string]any{"replicas": desired}
	if kind == "DaemonSet" {
		status = map[string]any{
			"observedGeneration":     int64(1),
			"desiredNumberScheduled": desired,
			"numberAvailable":        current,
		}
		spec = map[string]any{}
	}
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": apiVersion,
		"kind":       kind,
		"metadata": map[string]any{
			"name":       name,
			"namespace":  "kube-system",
			"generation": int64(1),
		},
		"spec":   spec,
		"status": status,
	}}
}

func topologyObject(gvk schema.GroupVersionKind, namespace, name, uid string, ready bool) *unstructured.Unstructured {
	status := "False"
	if ready {
		status = "True"
	}
	object := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": gvk.GroupVersion().String(),
		"kind":       gvk.Kind,
		"metadata": map[string]any{
			"name":      name,
			"namespace": namespace,
			"uid":       uid,
		},
		"status": map[string]any{
			"conditions": []any{map[string]any{"type": "Ready", "status": status}},
		},
	}}
	return object
}

func topologyOwner(object *unstructured.Unstructured) metav1.OwnerReference {
	return metav1.OwnerReference{
		APIVersion: object.GetAPIVersion(),
		Kind:       object.GetKind(),
		Name:       object.GetName(),
		UID:        types.UID(object.GetUID()),
	}
}
