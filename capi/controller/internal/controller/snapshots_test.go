package controller

import (
	"context"
	"encoding/json"
	"net/netip"
	"strconv"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func TestUnreadySurvivorBlocksSnapshotBeforeTargetMutation(t *testing.T) {
	target := validTenant("tenant-a")
	target.UID = "target-uid"
	survivor := validTenant("tenant-b")
	survivor.UID = "survivor-uid"
	_, survivor.Status.SpecHash, _ = validation.Validate(survivor.Name, survivor.Spec, "1.36.4")
	survivor.Status.Phase = tenancyv1alpha1.PhaseDegraded
	kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(target, survivor).Build()
	reconciler := &TenantReconciler{
		Client: kubernetes, APIReader: kubernetes, SupportedVersion: "1.36.4",
	}

	_, err := reconciler.captureSurvivorSnapshots(context.Background(), target, testFoundation())
	if err == nil || !strings.Contains(err.Error(), "not Ready") {
		t.Fatalf("unready survivor was accepted: %v", err)
	}
}

func TestFoundationTeardownAuthorizationRequiresEveryLiveTenant(t *testing.T) {
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	tenantA := validTenant("tenant-a")
	tenantA.UID = "uid-a"
	tenantB := validTenant("tenant-b")
	tenantB.UID = "uid-b"
	authorization, err := json.Marshal(foundationTeardownAuthorization{
		Schema: 1, FoundationHash: foundation.Hash, Nonce: "nonce",
		Targets: map[string]string{"tenant-a": "uid-a", "tenant-b": "uid-b"},
	})
	if err != nil {
		t.Fatal(err)
	}
	configMap := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: FoundationTeardownAuthorizationName, Namespace: defaultFoundationNamespace},
		Data:       map[string]string{"authorization.json": string(authorization)},
	}
	kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(tenantA, tenantB, configMap).Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}
	authorized, err := reconciler.foundationTeardownAuthorized(context.Background(), foundation)
	if err != nil || !authorized {
		t.Fatalf("exact foundation teardown authorization was rejected: %v", err)
	}
	tenantC := validTenant("tenant-c")
	tenantC.UID = "uid-c"
	if err := kubernetes.Create(context.Background(), tenantC); err != nil {
		t.Fatal(err)
	}
	if authorized, err = reconciler.foundationTeardownAuthorized(context.Background(), foundation); err == nil || authorized {
		t.Fatal("unauthorized live Tenant was accepted")
	}
}

func TestFoundationSnapshotAllowsOnlyTargetAllocationRemoval(t *testing.T) {
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	target := validTenant("tenant-a")
	target.UID = "target-uid"
	target.Status.Endpoint = foundation.Endpoint(foundation.PoolStart)
	peer := validTenant("tenant-b")
	peer.UID = "peer-uid"
	peerAddress := nextAddress(t, foundation.PoolStart)

	scheme := testScheme(t)
	objects := foundationSnapshotObjects(t, scheme)
	allocation := endpointConfigMap(t, foundation, target, "target-spec")
	state, err := decodeAllocationState(allocation.Data["allocations.json"], foundation)
	if err != nil {
		t.Fatal(err)
	}
	state.Allocations[peerAddress] = endpointAllocation{
		TenantName: peer.Name, TenantUID: string(peer.UID),
		SpecHash: "peer-spec", FoundationHash: foundation.Hash,
	}
	allocation.Data["allocations.json"], err = encodeAllocationState(&state)
	if err != nil {
		t.Fatal(err)
	}
	objects = append(objects, allocation)
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}

	snapshot, err := reconciler.captureFoundationSnapshot(context.Background(), target, foundation)
	if err != nil {
		t.Fatal(err)
	}
	target.Status.FoundationSnapshot = snapshot
	if err := reconciler.verifyFoundationSnapshot(context.Background(), target, foundation, false); err != nil {
		t.Fatal(err)
	}
	if err := releaseEndpoint(context.Background(), kubernetes, kubernetes, defaultFoundationNamespace, foundation, target); err != nil {
		t.Fatal(err)
	}
	target.Status.Endpoint = ""
	if err := reconciler.verifyFoundationSnapshot(context.Background(), target, foundation, true); err != nil {
		t.Fatal(err)
	}

	var changed corev1.ConfigMap
	if err := kubernetes.Get(context.Background(), client.ObjectKey{
		Namespace: defaultFoundationNamespace, Name: allocationConfigMapName,
	}, &changed); err != nil {
		t.Fatal(err)
	}
	state, err = decodeAllocationState(changed.Data["allocations.json"], foundation)
	if err != nil {
		t.Fatal(err)
	}
	peerAllocation := state.Allocations[peerAddress]
	peerAllocation.SpecHash = "changed"
	state.Allocations[peerAddress] = peerAllocation
	changed.Data["allocations.json"], _ = encodeAllocationState(&state)
	if err := kubernetes.Update(context.Background(), &changed); err != nil {
		t.Fatal(err)
	}
	if err := reconciler.verifyFoundationSnapshot(context.Background(), target, foundation, true); err == nil {
		t.Fatal("peer allocation drift was accepted")
	}
}

func foundationSnapshotObjects(t *testing.T, scheme *runtime.Scheme) []client.Object {
	t.Helper()
	keys := []struct {
		gvk       schema.GroupVersionKind
		namespace string
		name      string
	}{
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "tenant-system", "tenant-controller"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "capi-system", "capi-controller-manager"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "capd-system", "capd-controller-manager"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "capi-kubeadm-bootstrap-system", "capi-kubeadm-bootstrap-controller-manager"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "kamaji-system", "capi-kamaji-controller-manager"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "capi-kamaji-system", "kamaji"},
		{schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"}, "", "tenants.tenancy.cnpg-vcluster.io"},
		{schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"}, "", "clusters.cluster.x-k8s.io"},
		{schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"}, "", "devclusters.infrastructure.cluster.x-k8s.io"},
		{schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"}, "", "kamajicontrolplanes.controlplane.cluster.x-k8s.io"},
		{schema.GroupVersionKind{Group: "admissionregistration.k8s.io", Version: "v1", Kind: "ValidatingWebhookConfiguration"}, "", "tenant-controller-validating-webhook"},
	}

	result := make([]client.Object, 0, len(keys))
	for index, key := range keys {
		scheme.AddKnownTypeWithName(key.gvk, &unstructured.Unstructured{})
		object := &unstructured.Unstructured{Object: map[string]any{
			"apiVersion": key.gvk.GroupVersion().String(),
			"kind":       key.gvk.Kind,
			"metadata": map[string]any{
				"name": key.name, "namespace": key.namespace,
				"uid": "uid-" + strconv.Itoa(index),
			},
		}}
		if key.gvk.Kind == "Deployment" {
			_ = unstructured.SetNestedField(object.Object, int64(1), "spec", "replicas")
			_ = unstructured.SetNestedField(object.Object, int64(1), "status", "availableReplicas")
			_ = unstructured.SetNestedSlice(object.Object, []any{
				map[string]any{"name": "manager", "image": "controller:exact"},
			}, "spec", "template", "spec", "containers")
		}
		if key.gvk.Kind == "CustomResourceDefinition" {
			_ = unstructured.SetNestedSlice(object.Object, []any{
				map[string]any{"type": "Established", "status": "True"},
			}, "status", "conditions")
		}
		if key.gvk.Kind == "ValidatingWebhookConfiguration" {
			_ = unstructured.SetNestedSlice(object.Object, []any{
				map[string]any{
					"failurePolicy": "Fail",
					"clientConfig": map[string]any{"service": map[string]any{
						"name": "tenant-controller-webhook", "namespace": "tenant-system",
					}},
				},
			}, "webhooks")
		}
		result = append(result, object)
	}
	return result
}

func nextAddress(t *testing.T, value string) string {
	t.Helper()
	address, err := netip.ParseAddr(value)
	if err != nil {
		t.Fatal(err)
	}
	return address.Next().String()
}
