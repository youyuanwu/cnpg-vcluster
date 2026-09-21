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

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

type staticTenantFactory struct {
	client client.Client
}

func (factory staticTenantFactory) ClientFor([]byte, string) (client.Client, error) {
	return factory.client, nil
}

func TestObservePostCNIWorkerStateValidatesExactTopologyAndReadiness(t *testing.T) {
	state, err := observePostCNIFixture(t, "worker-a", "worker-a", true)
	if err != nil {
		t.Fatal(err)
	}
	if !state.inventoryComplete || !state.allReady {
		t.Fatalf("exact ready topology was rejected: %#v", state)
	}

	_, err = observePostCNIFixture(t, "worker-b", "worker-a", true)
	if !errors.Is(err, errWorkerOwnershipInvalid) || !isOwnershipError(err) {
		t.Fatalf("DevMachine name mismatch was not OwnershipInvalid: %v", err)
	}

	_, err = observePostCNIFixture(t, "worker-a", "worker-b", true)
	if !errors.Is(err, errWorkerOwnershipInvalid) || !isOwnershipError(err) {
		t.Fatalf("Node set mismatch was not OwnershipInvalid: %v", err)
	}

	state, err = observePostCNIFixture(t, "worker-a", "worker-a", false)
	if err != nil {
		t.Fatal(err)
	}
	if !state.inventoryComplete || state.allReady {
		t.Fatalf("unready Node did not invalidate readiness: %#v", state)
	}
}

func observePostCNIFixture(t *testing.T, devMachineName, nodeName string, nodeReady bool) (postCNIWorkerState, error) {
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
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: tenant.Name + "-kubeconfig", Namespace: tenant.Name},
		Type:       corev1.SecretType("cluster.x-k8s.io/secret"),
		Data:       map[string][]byte{"value": []byte("kubeconfig")},
	}
	tenant.Status.ObservedResources = []tenancyv1alpha1.ObservedResourceIdentity{identityFor(machineDeployment)}
	management := fake.NewClientBuilder().
		WithScheme(managementScheme).
		WithObjects(machineDeployment, machineSet, machine, devMachine, secret).
		Build()

	tenantScheme := runtime.NewScheme()
	if err := corev1.AddToScheme(tenantScheme); err != nil {
		t.Fatal(err)
	}
	node := topologyObject(postCNINodeGVK, "", nodeName, "node-uid", nodeReady)
	tenantClient := fake.NewClientBuilder().WithScheme(tenantScheme).WithObjects(node).Build()
	reconciler := &TenantReconciler{
		Client:    management,
		APIReader: management,
		Docker: &fakeDockerClient{
			workers: []DockerContainer{{
				Name: "worker-a", ID: "container-uid", State: "running",
				Networks: map[string]string{"kind": foundation.NetworkID},
			}},
		},
		TenantClients: staticTenantFactory{client: tenantClient},
	}
	return reconciler.observePostCNIWorkerState(
		context.Background(),
		tenant,
		validation.CanonicalSpec{Workers: 1},
		"spec-hash",
		foundation,
	)
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
