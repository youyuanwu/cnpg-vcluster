package controller

import (
	"fmt"
	"sort"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

type cleanupCoordinate struct {
	GVK       schema.GroupVersionKind
	Namespace string
	Name      string
	Resource  string
	Priority  int
}

func tenantCleanupCatalog(calico, cnpg []byte, databaseCount int32) ([]cleanupCoordinate, error) {
	values := make([]cleanupCoordinate, 0)
	add := func(object *unstructured.Unstructured, resource string) {
		values = append(values, cleanupCoordinate{
			GVK:       object.GroupVersionKind(),
			Namespace: object.GetNamespace(),
			Name:      object.GetName(),
			Resource:  resource,
			Priority:  tenantDeletePriority(object.GetKind()),
		})
	}
	calicoObjects, err := resources.DecodeManifest(calico)
	if err != nil {
		return nil, fmt.Errorf("decode Calico cleanup catalog: %w", err)
	}
	for _, object := range calicoObjects {
		add(object, "network-workload")
	}
	for _, object := range cleanupNetworkExtras() {
		add(object, object.GetAnnotations()[resources.ResourceAnnotation])
	}
	add(&unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "storage.k8s.io/v1",
		"kind":       "StorageClass",
		"metadata":   map[string]any{"name": tenantStorageClass},
	}}, "storage")
	cnpgObjects, err := resources.DecodeManifest(cnpg)
	if err != nil {
		return nil, fmt.Errorf("decode CNPG cleanup catalog: %w", err)
	}
	for _, object := range cnpgObjects {
		add(object, "cnpg-operator")
	}
	add(&unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "Namespace",
		"metadata":   map[string]any{"name": "database"},
	}}, "cnpg")
	for ordinal := int32(1); ordinal <= databaseCount; ordinal++ {
		add(&unstructured.Unstructured{Object: map[string]any{
			"apiVersion": "v1",
			"kind":       "PersistentVolume",
			"metadata":   map[string]any{"name": fmt.Sprintf("capi-postgres-pv-%d", ordinal)},
		}}, "cnpg")
	}
	add(&unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "postgresql.cnpg.io/v1",
		"kind":       "Cluster",
		"metadata": map[string]any{
			"name":      "capi-postgres",
			"namespace": "database",
		},
	}}, "cnpg")
	sort.Slice(values, func(left, right int) bool {
		if values[left].Priority != values[right].Priority {
			return values[left].Priority < values[right].Priority
		}
		a := values[left]
		b := values[right]
		return a.GVK.String()+"/"+a.Namespace+"/"+a.Name <
			b.GVK.String()+"/"+b.Namespace+"/"+b.Name
	})
	return values, nil
}

func cleanupNetworkExtras() []*unstructured.Unstructured {
	object := func(apiVersion, kind, namespace, name, resource string) *unstructured.Unstructured {
		value := &unstructured.Unstructured{Object: map[string]any{
			"apiVersion": apiVersion,
			"kind":       kind,
			"metadata": map[string]any{
				"name": name,
				"annotations": map[string]any{
					resources.ResourceAnnotation: resource,
				},
			},
		}}
		value.SetNamespace(namespace)
		return value
	}
	return []*unstructured.Unstructured{
		object("v1", "ConfigMap", "kube-system", "kubernetes-services-endpoint", "network-endpoint"),
		object("v1", "ServiceAccount", "kube-system", "capi-kube-proxy", "kube-proxy"),
		object("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", "", "capi-system:node-proxier", "kube-proxy"),
		object("v1", "ConfigMap", "kube-system", "capi-kube-proxy", "kube-proxy"),
		object("apps/v1", "DaemonSet", "kube-system", "capi-kube-proxy", "kube-proxy"),
	}
}
