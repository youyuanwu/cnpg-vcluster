package resources

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

const (
	NetworkSourceLimit    = 900 * 1024
	NetworkReferenceLimit = 100
)

type NetworkImages struct {
	CalicoCNI            string
	CalicoCNITagged      string
	CalicoNode           string
	CalicoNodeTagged     string
	CalicoControllers    string
	CalicoControllersTag string
	KubeProxy            string
	Verify               string
}

type NetworkBundle struct {
	Sources     []*corev1.ConfigMap
	ResourceSet *unstructured.Unstructured
	Inventory   map[string]string
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
	content, err := EncodeDocuments(objects)
	if err != nil {
		return NetworkBundle{}, err
	}
	sources, inventory, err := packageNetworkSources(context, context.Tenant.Name+"-network", content)
	if err != nil {
		return NetworkBundle{}, err
	}
	resourceSet := object(context,
		schema.GroupVersionKind{Group: "addons.cluster.x-k8s.io", Version: "v1beta2", Kind: "ClusterResourceSet"},
		context.Tenant.Name,
		context.Tenant.Name+"-network",
		"network-resource-set",
		map[string]any{
			"strategy": "Reconcile",
			"clusterSelector": map[string]any{
				"matchLabels": map[string]any{"cnpg-vcluster.capi/addons": context.Tenant.Name},
			},
			"resources": resourceReferences(inventory),
		},
	)
	return NetworkBundle{Sources: sources, ResourceSet: resourceSet, Inventory: inventory}, nil
}

func packageNetworkSources(context Context, baseName, content string) ([]*corev1.ConfigMap, map[string]string, error) {
	documents := strings.Split(content, "\n---\n")
	chunks := make([]string, 0)
	current := ""
	for _, document := range documents {
		candidate := document
		if current != "" {
			candidate = current + "\n---\n" + document
		}
		name := fmt.Sprintf("%s-%03d", baseName, len(chunks))
		candidateMap := networkSource(context, name, candidate)
		encoded, _ := json.Marshal(candidateMap)
		if len(encoded) <= NetworkSourceLimit {
			current = candidate
			continue
		}
		if current == "" {
			return nil, nil, fmt.Errorf("single network document exceeds source limit")
		}
		chunks = append(chunks, current)
		current = document
	}
	if current != "" {
		chunks = append(chunks, current)
	}
	if len(chunks) > NetworkReferenceLimit {
		return nil, nil, fmt.Errorf("network source reference limit exceeded")
	}
	sources := make([]*corev1.ConfigMap, 0, len(chunks))
	inventory := map[string]string{}
	for index, chunk := range chunks {
		name := baseName
		if len(chunks) != 1 {
			name = fmt.Sprintf("%s-%03d", baseName, index)
		}
		source := networkSource(context, name, chunk)
		sources = append(sources, source)
		digest := sha256.Sum256([]byte(chunk))
		inventory[name] = hex.EncodeToString(digest[:])
	}
	return sources, inventory, nil
}

func networkSource(context Context, name, content string) *corev1.ConfigMap {
	labels, annotations := markers(context, "network-source")
	labels["addons.cluster.x-k8s.io/resource-set"] = ""
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: context.Tenant.Name, Labels: labels, Annotations: annotations,
		},
		Data: map[string]string{"addons.yaml": content},
	}
}

func resourceReferences(inventory map[string]string) []any {
	names := make([]string, 0, len(inventory))
	for name := range inventory {
		names = append(names, name)
	}
	sort.Strings(names)
	result := make([]any, 0, len(names))
	for _, name := range names {
		result = append(result, map[string]any{"kind": "ConfigMap", "name": name})
	}
	return result
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
