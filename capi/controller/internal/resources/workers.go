package resources

import (
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

var (
	kubeadmTemplateGVK   = schema.GroupVersionKind{Group: "bootstrap.cluster.x-k8s.io", Version: "v1beta2", Kind: "KubeadmConfigTemplate"}
	devMachineGVK        = schema.GroupVersionKind{Group: "infrastructure.cluster.x-k8s.io", Version: "v1beta2", Kind: "DevMachineTemplate"}
	machineDeploymentGVK = schema.GroupVersionKind{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "MachineDeployment"}
)

func KubeadmConfigTemplate(context Context) *unstructured.Unstructured {
	spec := map[string]any{
		"template": map[string]any{
			"spec": map[string]any{
				"joinConfiguration": map[string]any{
					"nodeRegistration": map[string]any{
						"kubeletExtraArgs": []any{
							map[string]any{
								"name":  "eviction-hard",
								"value": "nodefs.available<0%,nodefs.inodesFree<0%,imagefs.available<0%",
							},
						},
					},
				},
			},
		},
	}
	if len(context.WorkerBootstrapCommands) != 0 {
		commands := make([]any, 0, len(context.WorkerBootstrapCommands))
		for _, command := range context.WorkerBootstrapCommands {
			commands = append(commands, command)
		}
		spec["template"].(map[string]any)["spec"].(map[string]any)["preKubeadmCommands"] = commands
	}
	return object(context, kubeadmTemplateGVK, context.Tenant.Name, context.Tenant.Name+"-worker", "kubeadm-config-template", spec)
}

func DevMachineTemplate(context Context) *unstructured.Unstructured {
	return object(context, devMachineGVK, context.Tenant.Name, context.Tenant.Name+"-worker", "dev-machine-template", map[string]any{
		"template": map[string]any{
			"spec": map[string]any{
				"backend": map[string]any{
					"docker": map[string]any{
						"customImage":      context.Inputs.NodeImage,
						"bootstrapTimeout": "5m",
						"extraMounts": []any{
							map[string]any{
								"hostPath":      context.Inputs.CacheHostPath,
								"containerPath": context.Inputs.CacheContainerPath,
								"readOnly":      true,
							},
							map[string]any{
								"hostPath":      context.VolumePath,
								"containerPath": context.Inputs.StorageContainerPath,
								"readOnly":      false,
							},
						},
					},
				},
			},
		},
	})
}

func MachineDeployment(context Context) *unstructured.Unstructured {
	labels, annotations := markers(context, "machine")
	templateLabels := stringMap(labels)
	templateLabels["cluster.x-k8s.io/cluster-name"] = context.Tenant.Name
	templateLabels["cnpg-vcluster.capi/nodepool"] = "worker"
	return object(context, machineDeploymentGVK, context.Tenant.Name, context.Tenant.Name+"-worker", "machine-deployment", map[string]any{
		"clusterName": context.Tenant.Name,
		"replicas":    int64(context.Spec.Workers),
		"machineNaming": map[string]any{
			"template": "{{ .cluster.name }}-worker-{{ .random }}",
		},
		"selector": map[string]any{
			"matchLabels": map[string]any{
				"cluster.x-k8s.io/cluster-name": context.Tenant.Name,
				"cnpg-vcluster.capi/nodepool":   "worker",
			},
		},
		"template": map[string]any{
			"metadata": map[string]any{
				"labels":      templateLabels,
				"annotations": stringMap(annotations),
			},
			"spec": map[string]any{
				"clusterName": context.Tenant.Name,
				"version":     "v" + context.Spec.KubernetesVersion,
				"bootstrap": map[string]any{
					"configRef": map[string]any{
						"apiGroup": "bootstrap.cluster.x-k8s.io",
						"kind":     "KubeadmConfigTemplate",
						"name":     context.Tenant.Name + "-worker",
					},
				},
				"infrastructureRef": map[string]any{
					"apiGroup": "infrastructure.cluster.x-k8s.io",
					"kind":     "DevMachineTemplate",
					"name":     context.Tenant.Name + "-worker",
				},
			},
		},
	})
}
