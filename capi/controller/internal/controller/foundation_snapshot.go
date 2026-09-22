package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

type foundationResourceIdentity struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Namespace  string `json:"namespace"`
	Name       string `json:"name"`
	UID        string `json:"uid"`
	Image      string `json:"image,omitempty"`
}

func (reconciler *TenantReconciler) captureFoundationSnapshot(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	foundation Foundation,
) (*tenancyv1alpha1.FoundationSnapshot, error) {
	resourceHash, err := reconciler.foundationResourceHash(ctx)
	if err != nil {
		return nil, err
	}
	peerHash, targetPresent, err := reconciler.allocationSnapshot(ctx, tenant, foundation)
	if err != nil {
		return nil, err
	}
	if !targetPresent {
		return nil, fmt.Errorf("target endpoint allocation is absent before deletion")
	}
	return &tenancyv1alpha1.FoundationSnapshot{
		FoundationHash:        foundation.Hash,
		ManagementContainerID: foundation.ManagementContainerID,
		NetworkID:             foundation.NetworkID,
		ControllerImage:       foundation.ControllerImage,
		ResourceHash:          resourceHash,
		PeerAllocationsHash:   peerHash,
		TargetEndpoint:        tenant.Status.Endpoint,
	}, nil
}

func (reconciler *TenantReconciler) verifyFoundationSnapshot(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	foundation Foundation,
	endpointReleased bool,
) error {
	snapshot := tenant.Status.FoundationSnapshot
	if snapshot == nil {
		return fmt.Errorf("foundation deletion snapshot is missing")
	}
	if snapshot.FoundationHash != foundation.Hash ||
		snapshot.ManagementContainerID != foundation.ManagementContainerID ||
		snapshot.NetworkID != foundation.NetworkID ||
		snapshot.ControllerImage != foundation.ControllerImage {
		return fmt.Errorf("foundation identity changed during deletion")
	}
	resourceHash, err := reconciler.foundationResourceHash(ctx)
	if err != nil {
		return err
	}
	if resourceHash != snapshot.ResourceHash {
		return fmt.Errorf("foundation Kubernetes resource identity changed during deletion")
	}
	peerHash, targetPresent, err := reconciler.allocationSnapshot(ctx, tenant, foundation)
	if err != nil {
		return err
	}
	if peerHash != snapshot.PeerAllocationsHash {
		return fmt.Errorf("peer endpoint allocations changed during deletion")
	}
	if endpointReleased && targetPresent {
		return fmt.Errorf("target endpoint allocation remains after release")
	}
	if !endpointReleased && !targetPresent {
		return fmt.Errorf("target endpoint allocation changed before release")
	}
	return nil
}

func (reconciler *TenantReconciler) foundationResourceHash(ctx context.Context) (string, error) {
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
	values := make([]foundationResourceIdentity, 0, len(keys))
	for _, key := range keys {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(key.gvk)
		if err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: key.namespace, Name: key.name}, object); err != nil {
			return "", fmt.Errorf("read foundation %s %s: %w", key.gvk.Kind, key.name, err)
		}
		if object.GetUID() == "" {
			return "", fmt.Errorf("foundation %s %s has no UID", key.gvk.Kind, key.name)
		}
		identity := foundationResourceIdentity{
			APIVersion: key.gvk.GroupVersion().String(),
			Kind:       key.gvk.Kind, Namespace: key.namespace, Name: key.name, UID: string(object.GetUID()),
		}
		if key.gvk.Kind == "Deployment" {
			desired, _, _ := unstructured.NestedInt64(object.Object, "spec", "replicas")
			available, _, _ := unstructured.NestedInt64(object.Object, "status", "availableReplicas")
			if desired < 1 || available != desired {
				return "", fmt.Errorf("foundation controller Deployment is not available")
			}
			containers, _, _ := unstructured.NestedSlice(object.Object, "spec", "template", "spec", "containers")
			images := make([]string, 0, len(containers))
			for _, raw := range containers {
				container, ok := raw.(map[string]any)
				if ok {
					if image, ok := container["image"].(string); ok && image != "" {
						images = append(images, image)
					}
				}
			}
			sort.Strings(images)
			identity.Image = strings.Join(images, ",")
			if len(images) == 0 {
				return "", fmt.Errorf("foundation controller image is missing")
			}
		}
		if key.gvk.Kind == "CustomResourceDefinition" {
			conditions, _, _ := unstructured.NestedSlice(object.Object, "status", "conditions")
			established := false
			for _, raw := range conditions {
				condition, ok := raw.(map[string]any)
				established = established || ok && condition["type"] == "Established" && condition["status"] == "True"
			}
			if !established {
				return "", fmt.Errorf("foundation CRD %s is not Established", key.name)
			}
		}
		if key.gvk.Kind == "ValidatingWebhookConfiguration" {
			webhooks, _, _ := unstructured.NestedSlice(object.Object, "webhooks")
			healthy := false
			for _, raw := range webhooks {
				webhook, ok := raw.(map[string]any)
				service, _, _ := unstructured.NestedMap(webhook, "clientConfig", "service")
				healthy = healthy || ok &&
					webhook["failurePolicy"] == "Fail" &&
					service["name"] == "tenant-controller-webhook" &&
					service["namespace"] == "tenant-system"
			}
			if !healthy {
				return "", fmt.Errorf("foundation validating webhook is not healthy")
			}
		}
		values = append(values, identity)
	}
	sort.Slice(values, func(left, right int) bool {
		return values[left].APIVersion+"/"+values[left].Kind+"/"+values[left].Namespace+"/"+values[left].Name <
			values[right].APIVersion+"/"+values[right].Kind+"/"+values[right].Namespace+"/"+values[right].Name
	})
	return hashJSON(values)
}

func (reconciler *TenantReconciler) allocationSnapshot(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	foundation Foundation,
) (string, bool, error) {
	var configMap corev1.ConfigMap
	if err := reconciler.reader().Get(ctx, types.NamespacedName{
		Namespace: reconciler.foundationNamespace(),
		Name:      allocationConfigMapName,
	}, &configMap); err != nil {
		return "", false, fmt.Errorf("read endpoint allocations for foundation snapshot: %w", err)
	}
	state, err := decodeAllocationState(configMap.Data["allocations.json"], foundation)
	if err != nil {
		return "", false, err
	}
	targetAddress := ""
	if tenant.Status.Endpoint != "" {
		targetAddress, _, err = net.SplitHostPort(tenant.Status.Endpoint)
		if err != nil {
			return "", false, fmt.Errorf("parse target endpoint: %w", err)
		}
	} else if tenant.Status.FoundationSnapshot != nil && tenant.Status.FoundationSnapshot.TargetEndpoint != "" {
		targetAddress, _, err = net.SplitHostPort(tenant.Status.FoundationSnapshot.TargetEndpoint)
		if err != nil {
			return "", false, fmt.Errorf("parse snapshotted target endpoint: %w", err)
		}
	}
	targetPresent := false
	peers := make(map[string]endpointAllocation, len(state.Allocations))
	for address, allocation := range state.Allocations {
		if allocation.TenantUID == string(tenant.UID) {
			if targetAddress == "" || address != targetAddress || allocation.TenantName != tenant.Name {
				return "", false, fmt.Errorf("target endpoint allocation identity changed")
			}
			targetPresent = true
			continue
		}
		peers[address] = allocation
	}
	hash, err := hashJSON(peers)
	return hash, targetPresent, err
}

func hashJSON(value any) (string, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:]), nil
}
