package controller

import (
	"fmt"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func serviceResourceContext(tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) resources.Context {
	return resources.Context{
		Tenant: tenant, Spec: canonical, SpecHash: specHash, FoundationHash: foundation.Hash,
		Endpoint: tenant.Status.Endpoint, Inputs: foundation.ResourceInputs(),
	}
}

func networkImages(foundation Foundation) (resources.NetworkImages, error) {
	get := func(key string) (FoundationArchive, error) {
		value, found := archiveByKey(foundation.Cache.ImageArchives, key)
		if !found {
			return FoundationArchive{}, fmt.Errorf("foundation image %s is missing", key)
		}
		return value, nil
	}
	cni, err := get("CALICO_CNI_IMAGE")
	if err != nil {
		return resources.NetworkImages{}, err
	}
	node, err := get("CALICO_NODE_IMAGE")
	if err != nil {
		return resources.NetworkImages{}, err
	}
	controllers, err := get("CALICO_KUBE_CONTROLLERS_IMAGE")
	if err != nil {
		return resources.NetworkImages{}, err
	}
	proxy, err := get("KUBE_PROXY_IMAGE")
	if err != nil {
		return resources.NetworkImages{}, err
	}
	return resources.NetworkImages{
		CalicoCNI: cni.Reference, CalicoCNITagged: cni.Tagged,
		CalicoNode: node.Reference, CalicoNodeTagged: node.Tagged,
		CalicoControllers: controllers.Reference, CalicoControllersTag: controllers.Tagged,
		KubeProxy: proxy.Reference,
	}, nil
}
