package controller

import (
	"context"
	"fmt"
	"os"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcileNetwork(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (ctrl.Result, error) {
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	calico, err := os.ReadFile("/assets/calico.yaml")
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("read staged Calico asset: %w", err)
	}
	images, err := networkImages(foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	bundle, err := resources.BuildNetwork(resourceContext, calico, images)
	if err != nil {
		return ctrl.Result{}, err
	}
	applied, err := ensureTenantObjects(ctx, tenantClient, bundle.Objects, tenant, specHash, foundation.Hash)
	if err != nil {
		return ctrl.Result{}, err
	}
	if applied.Created || applied.Pending {
		return progressRequeue(), nil
	}
	ready, err := networkStructurallyReady(ctx, tenantClient, int64(canonical.Workers))
	if err != nil {
		return ctrl.Result{}, err
	}
	if !ready {
		ctrl.LoggerFrom(ctx).V(1).Info("waiting for Tenant component", "component", "network")
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	return reconciler.reconcilePostCNIWorkers(ctx, tenantClient, tenant, canonical, specHash, foundation)
}

func networkStructurallyReady(ctx context.Context, tenantClient client.Client, workers int64) (bool, error) {
	nodes := &unstructured.UnstructuredList{}
	nodes.SetGroupVersionKind(schema.GroupVersionKind{Version: "v1", Kind: "NodeList"})
	if err := tenantClient.List(ctx, nodes); err != nil {
		return false, err
	}
	if int64(len(nodes.Items)) != workers {
		return false, nil
	}
	for index := range nodes.Items {
		if !tenantObjectReady(&nodes.Items[index]) {
			return false, nil
		}
	}
	for _, item := range []struct {
		gvk             schema.GroupVersionKind
		namespace, name string
	}{
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "DaemonSet"}, "kube-system", "calico-node"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "kube-system", "calico-kube-controllers"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "DaemonSet"}, "kube-system", "capi-kube-proxy"},
		{schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}, "kube-system", "coredns"},
	} {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(item.gvk)
		if err := tenantClient.Get(ctx, types.NamespacedName{Namespace: item.namespace, Name: item.name}, object); err != nil {
			if apierrors.IsNotFound(err) {
				return false, nil
			}
			return false, err
		}
		if !workloadAvailable(object) {
			return false, nil
		}
	}
	return true, nil
}
