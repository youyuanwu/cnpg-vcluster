package resources

import (
	"fmt"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

type NetworkImages struct {
	CalicoCNI            string
	CalicoCNITagged      string
	CalicoNode           string
	CalicoNodeTagged     string
	CalicoControllers    string
	CalicoControllersTag string
	KubeProxy            string
}

type NetworkBundle struct {
	Objects []*unstructured.Unstructured
}

func BuildNetwork(context Context, calico []byte, images NetworkImages) (NetworkBundle, error) {
	objects, err := DecodeManifest(calico)
	if err != nil {
		return NetworkBundle{}, err
	}
	counts := map[string]int{}
	replacements := map[string]string{
		images.CalicoCNITagged:      images.CalicoCNI,
		images.CalicoNodeTagged:     images.CalicoNode,
		images.CalicoControllersTag: images.CalicoControllers,
	}
	for _, object := range objects {
		replaceStrings(object.Object, replacements, counts)
		if object.GetKind() == "DaemonSet" && object.GetName() == "calico-node" {
			if err := setCalicoPool(object, context.Spec.PodCIDR); err != nil {
				return NetworkBundle{}, err
			}
		}
		MarkTenantObject(context, object, "network-workload")
	}
	for tagged, expected := range map[string]int{
		images.CalicoCNITagged:      2,
		images.CalicoNodeTagged:     2,
		images.CalicoControllersTag: 1,
	} {
		if counts[tagged] != expected {
			return NetworkBundle{}, fmt.Errorf("unexpected Calico image count for %s", tagged)
		}
	}
	endpoint := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata": map[string]any{
			"name":      "kubernetes-services-endpoint",
			"namespace": "kube-system",
		},
		"data": map[string]any{
			"KUBERNETES_SERVICE_HOST":       strings.Split(context.Endpoint, ":")[0],
			"KUBERNETES_SERVICE_PORT":       fmt.Sprint(context.Inputs.APIPort),
			"KUBERNETES_SERVICE_PORT_HTTPS": fmt.Sprint(context.Inputs.APIPort),
		},
	}}
	MarkTenantObject(context, endpoint, "network-endpoint")
	objects = append([]*unstructured.Unstructured{endpoint}, objects...)
	objects = append(objects, kubeProxyObjects(context, images.KubeProxy)...)
	SortObjects(objects)
	return NetworkBundle{Objects: objects}, nil
}

func replaceStrings(value any, replacements map[string]string, counts map[string]int) {
	switch typed := value.(type) {
	case map[string]any:
		for key, item := range typed {
			if text, ok := item.(string); ok {
				if replacement, found := replacements[text]; found {
					typed[key] = replacement
					counts[text]++
				}
				continue
			}
			replaceStrings(item, replacements, counts)
		}
	case []any:
		for index, item := range typed {
			if text, ok := item.(string); ok {
				if replacement, found := replacements[text]; found {
					typed[index] = replacement
					counts[text]++
				}
				continue
			}
			replaceStrings(item, replacements, counts)
		}
	}
}

func setCalicoPool(object *unstructured.Unstructured, podCIDR string) error {
	containers, found, err := unstructured.NestedSlice(object.Object, "spec", "template", "spec", "containers")
	if err != nil || !found {
		return fmt.Errorf("calico-node containers are missing")
	}
	for _, raw := range containers {
		container := raw.(map[string]any)
		if container["name"] != "calico-node" {
			continue
		}
		env, _, _ := unstructured.NestedSlice(container, "env")
		updated := false
		for _, item := range env {
			entry := item.(map[string]any)
			if entry["name"] == "CALICO_IPV4POOL_CIDR" {
				entry["value"] = podCIDR
				updated = true
			}
		}
		if !updated {
			env = append(env, map[string]any{"name": "CALICO_IPV4POOL_CIDR", "value": podCIDR})
		}
		container["env"] = env
		return unstructured.SetNestedSlice(object.Object, containers, "spec", "template", "spec", "containers")
	}
	return fmt.Errorf("calico-node container is missing")
}

func kubeProxyObjects(context Context, image string) []*unstructured.Unstructured {
	common := func(kind, name, namespace string, body map[string]any) *unstructured.Unstructured {
		value := &unstructured.Unstructured{Object: body}
		value.SetAPIVersion(map[string]string{
			"ServiceAccount": "v1", "ConfigMap": "v1", "DaemonSet": "apps/v1", "ClusterRoleBinding": "rbac.authorization.k8s.io/v1",
		}[kind])
		value.SetKind(kind)
		value.SetName(name)
		value.SetNamespace(namespace)
		MarkTenantObject(context, value, "kube-proxy")
		return value
	}
	serviceAccount := common("ServiceAccount", "capi-kube-proxy", "kube-system", map[string]any{})
	binding := common("ClusterRoleBinding", "capi-system:node-proxier", "", map[string]any{
		"roleRef":  map[string]any{"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "system:node-proxier"},
		"subjects": []any{map[string]any{"kind": "ServiceAccount", "name": "capi-kube-proxy", "namespace": "kube-system"}},
	})
	config := common("ConfigMap", "capi-kube-proxy", "kube-system", map[string]any{
		"data": map[string]any{
			"config.conf":     fmt.Sprintf("apiVersion: kubeproxy.config.k8s.io/v1alpha1\nkind: KubeProxyConfiguration\nbindAddress: 0.0.0.0\nclientConnection:\n  kubeconfig: /var/lib/kube-proxy/kubeconfig.conf\nclusterCIDR: %s\nconntrack:\n  maxPerCore: 0\n  min: 0\nmode: iptables\n", context.Spec.PodCIDR),
			"kubeconfig.conf": fmt.Sprintf("apiVersion: v1\nkind: Config\nclusters:\n- cluster:\n    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt\n    server: https://%s\n  name: default\ncontexts:\n- context:\n    cluster: default\n    namespace: default\n    user: default\n  name: default\ncurrent-context: default\nusers:\n- name: default\n  user:\n    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token\n", context.Endpoint),
		},
	})
	daemonSet := common("DaemonSet", "capi-kube-proxy", "kube-system", map[string]any{
		"spec": map[string]any{
			"selector": map[string]any{"matchLabels": map[string]any{"k8s-app": "capi-kube-proxy"}},
			"template": map[string]any{
				"metadata": map[string]any{"labels": map[string]any{"k8s-app": "capi-kube-proxy"}},
				"spec": map[string]any{
					"priorityClassName": "system-node-critical", "serviceAccountName": "capi-kube-proxy", "hostNetwork": true,
					"tolerations": []any{map[string]any{"operator": "Exists"}},
					"containers": []any{map[string]any{
						"name": "kube-proxy", "image": image,
						"command":         []any{"/usr/local/bin/kube-proxy", "--config=/var/lib/kube-proxy/config.conf", "--v=2"},
						"securityContext": map[string]any{"privileged": true},
						"volumeMounts": []any{
							map[string]any{"name": "kube-proxy", "mountPath": "/var/lib/kube-proxy"},
							map[string]any{"name": "xtables-lock", "mountPath": "/run/xtables.lock"},
							map[string]any{"name": "lib-modules", "mountPath": "/lib/modules", "readOnly": true},
						},
					}},
					"volumes": []any{
						map[string]any{"name": "kube-proxy", "configMap": map[string]any{"name": "capi-kube-proxy"}},
						map[string]any{"name": "xtables-lock", "hostPath": map[string]any{"path": "/run/xtables.lock", "type": "FileOrCreate"}},
						map[string]any{"name": "lib-modules", "hostPath": map[string]any{"path": "/lib/modules", "type": "Directory"}},
					},
				},
			},
		},
	})
	return []*unstructured.Unstructured{serviceAccount, binding, config, daemonSet}
}
