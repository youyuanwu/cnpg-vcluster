package resources

import (
	"fmt"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

func CNPGOperator(context Context, manifest []byte, taggedImage, exactImage string) ([]*unstructured.Unstructured, error) {
	objects, err := DecodeManifest(manifest)
	if err != nil {
		return nil, err
	}
	counts := map[string]int{}
	for _, object := range objects {
		replaceStrings(object.Object, map[string]string{taggedImage: exactImage}, counts)
		MarkTenantObject(context, object, "cnpg-operator")
	}
	if counts[taggedImage] != 2 {
		return nil, fmt.Errorf("unexpected CNPG operator image count")
	}
	return objects, nil
}

func CNPGObjects(context Context, storageClass, postgresImage string) []*unstructured.Unstructured {
	clusterName := "capi-postgres"
	values := []*unstructured.Unstructured{
		{Object: map[string]any{"apiVersion": "v1", "kind": "Namespace", "metadata": map[string]any{"name": "database"}}},
	}
	for ordinal := int32(1); ordinal <= context.Spec.DatabaseCount; ordinal++ {
		values = append(values, &unstructured.Unstructured{Object: map[string]any{
			"apiVersion": "v1", "kind": "PersistentVolume",
			"metadata": map[string]any{"name": fmt.Sprintf("%s-pv-%d", clusterName, ordinal)},
			"spec": map[string]any{
				"capacity": map[string]any{"storage": "1Gi"}, "accessModes": []any{"ReadWriteOnce"},
				"persistentVolumeReclaimPolicy": "Retain", "storageClassName": storageClass,
				"claimRef": map[string]any{"namespace": "database", "name": fmt.Sprintf("%s-%d", clusterName, ordinal)},
				"hostPath": map[string]any{
					"path": fmt.Sprintf("%s/volumes/cnpg/%d", context.Inputs.StorageContainerPath, ordinal),
					"type": "DirectoryOrCreate",
				},
			},
		}})
	}
	antiAffinity := "required"
	if context.Spec.DatabaseCount > context.Spec.Workers {
		antiAffinity = "preferred"
	}
	values = append(values, &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
		"metadata": map[string]any{"name": clusterName, "namespace": "database"},
		"spec": map[string]any{
			"instances": int64(context.Spec.DatabaseCount), "imageName": postgresImage,
			"affinity": map[string]any{
				"enablePodAntiAffinity": true, "podAntiAffinityType": antiAffinity, "topologyKey": "kubernetes.io/hostname",
			},
			"bootstrap": map[string]any{"initdb": map[string]any{"database": "app", "owner": "app"}},
			"storage":   map[string]any{"size": "1Gi", "storageClass": storageClass},
			"resources": map[string]any{
				"requests": map[string]any{"cpu": "100m", "memory": "256Mi"},
				"limits":   map[string]any{"cpu": "1", "memory": "1Gi"},
			},
		},
	}})
	for _, value := range values {
		MarkTenantObject(context, value, "cnpg")
	}
	return values
}

func SQLProbe(context Context, postgresImage string) *unstructured.Unstructured {
	script := strings.Join([]string{
		"set -eu",
		"export PGPASSWORD=\"$(cat /secret/password)\"",
		"psql -X -qAt -v ON_ERROR_STOP=1 -h capi-postgres-rw -U app -d app -c \"CREATE TABLE IF NOT EXISTS verification(marker text PRIMARY KEY); INSERT INTO verification VALUES ('capi-marker') ON CONFLICT DO NOTHING;\"",
		"test \"$(psql -X -qAt -v ON_ERROR_STOP=1 -h capi-postgres-rw -U app -d app -c \"SELECT marker FROM verification WHERE marker='capi-marker';\")\" = capi-marker",
	}, "; ")
	value := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1", "kind": "Pod",
		"metadata": map[string]any{"name": context.Tenant.Name + "-sql-verify", "namespace": "database"},
		"spec": map[string]any{
			"restartPolicy": "Never", "automountServiceAccountToken": false,
			"containers": []any{map[string]any{
				"name": "psql", "image": postgresImage, "command": []any{"sh", "-ec", script},
				"volumeMounts": []any{map[string]any{"name": "credentials", "mountPath": "/secret", "readOnly": true}},
			}},
			"volumes": []any{map[string]any{"name": "credentials", "secret": map[string]any{"secretName": "capi-postgres-app"}}},
		},
	}}
	MarkTenantObject(context, value, "database-probe")
	return value
}
