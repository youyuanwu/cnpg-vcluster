package resources

import (
	"fmt"
	"net"
	"strconv"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

var (
	clusterGVK      = schema.GroupVersionKind{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "Cluster"}
	devClusterGVK   = schema.GroupVersionKind{Group: "infrastructure.cluster.x-k8s.io", Version: "v1beta2", Kind: "DevCluster"}
	controlPlaneGVK = schema.GroupVersionKind{Group: "controlplane.cluster.x-k8s.io", Version: "v1alpha2", Kind: "KamajiControlPlane"}
)

func Namespace(context Context) *corev1.Namespace {
	labels, annotations := markers(context, "namespace")
	return &corev1.Namespace{
		TypeMeta:   typeMeta("v1", "Namespace"),
		ObjectMeta: objectMeta(context.Tenant.Name, "", labels, annotations),
	}
}

func Cluster(context Context) (*unstructured.Unstructured, error) {
	host, port, err := net.SplitHostPort(context.Endpoint)
	if err != nil {
		return nil, fmt.Errorf("parse Tenant endpoint: %w", err)
	}
	apiPort, err := strconv.ParseInt(port, 10, 32)
	if err != nil {
		return nil, fmt.Errorf("parse Tenant API port: %w", err)
	}
	spec := map[string]any{
		"controlPlaneEndpoint": map[string]any{"host": host, "port": apiPort},
		"clusterNetwork": map[string]any{
			"apiServerPort": apiPort,
			"services":      map[string]any{"cidrBlocks": []any{context.Spec.ServiceCIDR}},
			"pods":          map[string]any{"cidrBlocks": []any{context.Spec.PodCIDR}},
			"serviceDomain": context.Inputs.ClusterDomain,
		},
		"infrastructureRef": map[string]any{
			"apiGroup": "infrastructure.cluster.x-k8s.io",
			"kind":     "DevCluster",
			"name":     context.Tenant.Name,
		},
		"controlPlaneRef": map[string]any{
			"apiGroup": "controlplane.cluster.x-k8s.io",
			"kind":     "KamajiControlPlane",
			"name":     context.Tenant.Name,
		},
	}
	value := object(context, clusterGVK, context.Tenant.Name, context.Tenant.Name, "cluster", spec)
	labels := value.GetLabels()
	labels["cnpg-vcluster.capi/addons"] = context.Tenant.Name
	value.SetLabels(labels)
	return value, nil
}

func DevCluster(context Context) (*unstructured.Unstructured, error) {
	host, port, err := net.SplitHostPort(context.Endpoint)
	if err != nil {
		return nil, fmt.Errorf("parse Tenant endpoint: %w", err)
	}
	apiPort, err := strconv.ParseInt(port, 10, 32)
	if err != nil {
		return nil, fmt.Errorf("parse Tenant API port: %w", err)
	}
	return object(context, devClusterGVK, context.Tenant.Name, context.Tenant.Name, "dev-cluster", map[string]any{
		"controlPlaneEndpoint": map[string]any{"host": host, "port": apiPort},
		"backend":              map[string]any{"docker": map[string]any{"loadBalancer": map[string]any{}}},
	}), nil
}

func KamajiControlPlane(context Context) (*unstructured.Unstructured, error) {
	host, _, err := net.SplitHostPort(context.Endpoint)
	if err != nil {
		return nil, fmt.Errorf("parse Tenant endpoint: %w", err)
	}
	dnsIP, err := DNSServiceIP(context.Spec.ServiceCIDR)
	if err != nil {
		return nil, err
	}
	serverRepository, serverVersion, err := splitImage(context.Inputs.KonnectivityServerImage)
	if err != nil {
		return nil, err
	}
	agentRepository, agentVersion, err := splitImage(context.Inputs.KonnectivityAgentImage)
	if err != nil {
		return nil, err
	}
	return object(context, controlPlaneGVK, context.Tenant.Name, context.Tenant.Name, "kamaji-control-plane", map[string]any{
		"version":       "v" + context.Spec.KubernetesVersion,
		"replicas":      int64(1),
		"dataStoreName": "default",
		"network": map[string]any{
			"serviceType":    "LoadBalancer",
			"serviceAddress": host,
			"serviceAnnotations": map[string]any{
				"metallb.io/loadBalancerIPs": host,
			},
			"certSANs":      []any{host, context.Tenant.Name},
			"dnsServiceIPs": []any{dnsIP},
		},
		"addons": map[string]any{
			"coreDNS": map[string]any{"dnsServiceIPs": []any{dnsIP}},
			"konnectivity": map[string]any{
				"server": map[string]any{
					"image":   serverRepository,
					"version": serverVersion,
					"port":    int64(8132),
				},
				"agent": map[string]any{
					"image":       agentRepository,
					"version":     agentVersion,
					"mode":        "DaemonSet",
					"hostNetwork": true,
					"tolerations": []any{
						map[string]any{"key": "CriticalAddonsOnly", "operator": "Exists"},
						map[string]any{"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoSchedule"},
						map[string]any{"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute"},
					},
				},
			},
		},
	}), nil
}

func splitImage(reference string) (string, string, error) {
	at := strings.LastIndex(reference, "@")
	if at < 1 {
		return "", "", fmt.Errorf("image reference must include a tag and digest")
	}
	colon := strings.LastIndex(reference[:at], ":")
	if colon < 1 {
		return "", "", fmt.Errorf("image reference must include a tag and digest")
	}
	return reference[:colon], reference[colon+1:at] + "@" + reference[at+1:], nil
}
