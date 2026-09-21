package resources

import "k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

func StorageObjects(context Context, storageClass, verifyImage string) []*unstructured.Unstructured {
	values := []*unstructured.Unstructured{
		{Object: map[string]any{
			"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
			"metadata":    map[string]any{"name": storageClass},
			"provisioner": "kubernetes.io/no-provisioner", "volumeBindingMode": "Immediate", "reclaimPolicy": "Retain",
		}},
		{Object: map[string]any{
			"apiVersion": "v1", "kind": "PersistentVolume",
			"metadata": map[string]any{"name": context.Tenant.Name + "-storage-smoke"},
			"spec": map[string]any{
				"capacity": map[string]any{"storage": "32Mi"}, "accessModes": []any{"ReadWriteOnce"},
				"persistentVolumeReclaimPolicy": "Retain", "storageClassName": storageClass,
				"claimRef": map[string]any{"namespace": "default", "name": "storage-smoke"},
				"hostPath": map[string]any{"path": context.Inputs.StorageContainerPath + "/volumes/smoke", "type": "DirectoryOrCreate"},
			},
		}},
		{Object: map[string]any{
			"apiVersion": "v1", "kind": "PersistentVolumeClaim",
			"metadata": map[string]any{"name": "storage-smoke", "namespace": "default"},
			"spec": map[string]any{
				"accessModes": []any{"ReadWriteOnce"}, "storageClassName": storageClass,
				"volumeName": context.Tenant.Name + "-storage-smoke",
				"resources":  map[string]any{"requests": map[string]any{"storage": "32Mi"}},
			},
		}},
		{Object: map[string]any{
			"apiVersion": "apps/v1", "kind": "Deployment",
			"metadata": map[string]any{"name": "storage-smoke", "namespace": "default"},
			"spec": map[string]any{
				"replicas": int64(1), "selector": map[string]any{"matchLabels": map[string]any{"app": "storage-smoke"}},
				"template": map[string]any{
					"metadata": map[string]any{"labels": map[string]any{"app": "storage-smoke"}},
					"spec": map[string]any{
						"containers": []any{map[string]any{
							"name": "smoke", "image": verifyImage,
							"command":      []any{"sh", "-ec", "test -f /data/marker || echo machine-independent > /data/marker; sleep 3600"},
							"volumeMounts": []any{map[string]any{"name": "data", "mountPath": "/data"}},
						}},
						"volumes": []any{map[string]any{"name": "data", "persistentVolumeClaim": map[string]any{"claimName": "storage-smoke"}}},
					},
				},
			},
		}},
	}
	for _, value := range values {
		MarkTenantObject(context, value, "storage")
	}
	return values
}

func StorageProbe(context Context, verifyImage string) *unstructured.Unstructured {
	value := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1", "kind": "Pod",
		"metadata": map[string]any{"name": context.Tenant.Name + "-storage-verify", "namespace": "default"},
		"spec": map[string]any{
			"restartPolicy": "Never", "automountServiceAccountToken": false,
			"containers": []any{map[string]any{
				"name": "verify", "image": verifyImage,
				"command":      []any{"sh", "-ec", "test \"$(cat /data/marker)\" = machine-independent"},
				"volumeMounts": []any{map[string]any{"name": "data", "mountPath": "/data", "readOnly": true}},
			}},
			"volumes": []any{map[string]any{"name": "data", "persistentVolumeClaim": map[string]any{"claimName": "storage-smoke"}}},
		},
	}}
	MarkTenantObject(context, value, "storage-probe")
	return value
}
