package resources

import "k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

func StorageObjects(context Context, storageClass string) []*unstructured.Unstructured {
	value := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion":        "storage.k8s.io/v1",
		"kind":              "StorageClass",
		"metadata":          map[string]any{"name": storageClass},
		"provisioner":       "kubernetes.io/no-provisioner",
		"volumeBindingMode": "Immediate",
		"reclaimPolicy":     "Retain",
	}}
	MarkTenantObject(context, value, "storage")
	return []*unstructured.Unstructured{value}
}
