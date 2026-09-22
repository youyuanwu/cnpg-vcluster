package resources

import (
	"fmt"
	"net/netip"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

const (
	TenantAnnotation     = "tenancy.cnpg-vcluster.io/tenant"
	TenantUIDAnnotation  = "tenancy.cnpg-vcluster.io/tenant-uid"
	SpecHashAnnotation   = "tenancy.cnpg-vcluster.io/spec-hash"
	FoundationAnnotation = "tenancy.cnpg-vcluster.io/foundation-hash"
	ResourceAnnotation   = "tenancy.cnpg-vcluster.io/resource"
)

type Inputs struct {
	OwnershipLabel          string
	LabPrefix               string
	APIPort                 int32
	ClusterDomain           string
	NodeImage               string
	CacheHostPath           string
	CacheContainerPath      string
	StorageContainerPath    string
	KonnectivityServerImage string
	KonnectivityAgentImage  string
}

type Context struct {
	Tenant                  *tenancyv1alpha1.Tenant
	Spec                    validation.CanonicalSpec
	SpecHash                string
	FoundationHash          string
	Endpoint                string
	VolumePath              string
	WorkerBootstrapCommands []string
	Inputs                  Inputs
}

func markers(context Context, resource string) (map[string]string, map[string]string) {
	return map[string]string{
		context.Inputs.OwnershipLabel: context.Inputs.LabPrefix,
	}, map[string]string{
		TenantAnnotation:     context.Tenant.Name,
		TenantUIDAnnotation:  string(context.Tenant.UID),
		SpecHashAnnotation:   context.SpecHash,
		FoundationAnnotation: context.FoundationHash,
		ResourceAnnotation:   resource,
	}
}

func object(context Context, gvk schema.GroupVersionKind, namespace, name, resource string, spec map[string]any) *unstructured.Unstructured {
	labels, annotations := markers(context, resource)
	value := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": gvk.GroupVersion().String(),
		"kind":       gvk.Kind,
		"metadata": map[string]any{
			"name":        name,
			"labels":      stringMap(labels),
			"annotations": stringMap(annotations),
		},
	}}
	if namespace != "" {
		value.SetNamespace(namespace)
	}
	if spec != nil {
		value.Object["spec"] = spec
	}
	value.SetGroupVersionKind(gvk)
	return value
}

func stringMap(values map[string]string) map[string]any {
	result := make(map[string]any, len(values))
	for key, value := range values {
		result[key] = value
	}
	return result
}

func DNSServiceIP(serviceCIDR string) (string, error) {
	prefix, err := netip.ParsePrefix(serviceCIDR)
	if err != nil {
		return "", fmt.Errorf("parse service CIDR: %w", err)
	}
	address := prefix.Masked().Addr()
	for range 10 {
		address = address.Next()
	}
	if !prefix.Contains(address) {
		return "", fmt.Errorf("service CIDR does not contain DNS service address")
	}
	return address.String(), nil
}
