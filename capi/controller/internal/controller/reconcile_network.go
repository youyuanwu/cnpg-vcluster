package controller

import (
	"context"
	"fmt"
	"os"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcileNetwork(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
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
	switch tenant.Status.Stage {
	case tenancyv1alpha1.StageWorkersApplied:
		for _, source := range bundle.Sources {
			identity, changed, err := reconciler.ensureNetworkSource(ctx, source, tenant, specHash, foundation)
			if err != nil {
				return ctrl.Result{}, err
			}
			if changed || findIdentity(tenant.Status, corev1.SchemeGroupVersion.WithKind("ConfigMap"), source.Namespace, source.Name) == nil {
				return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageWorkersApplied, identity)
			}
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageNetworkSourcesApplied
			return nil
		})
	case tenancyv1alpha1.StageNetworkSourcesApplied:
		identity, err := reconciler.ensureUnstructured(ctx, bundle.ResourceSet, tenant, specHash, foundation, "network-resource-set")
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageNetworkResourceSetApplied, identity)
	case tenancyv1alpha1.StageNetworkResourceSetApplied:
		tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
		if err != nil {
			return ctrl.Result{}, err
		}
		for _, desired := range bundle.Objects {
			identity, changed, err := ensureTenantObject(ctx, tenantClient, desired, tenant, specHash, foundation.Hash)
			if err != nil {
				return ctrl.Result{}, err
			}
			if changed || !tenantIdentityPresent(tenant.Status.TenantResources, identity) {
				return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
					return upsertTenantIdentity(status, identity)
				})
			}
		}
		ready, err := networkStructurallyReady(ctx, tenantClient, int64(canonical.Workers))
		if err != nil {
			return ctrl.Result{}, err
		}
		if !ready {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		probe := networkProbe(resourceContext, images.Verify)
		identity, _, err := ensureTenantObject(ctx, tenantClient, probe, tenant, specHash, foundation.Hash)
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if err := upsertTenantIdentity(status, identity); err != nil {
				return err
			}
			status.Stage = tenancyv1alpha1.StageNetworkProbeCreated
			return nil
		})
	case tenancyv1alpha1.StageNetworkProbeCreated:
		tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
		if err != nil {
			return ctrl.Result{}, err
		}
		probe := networkProbe(resourceContext, images.Verify)
		current := probe.DeepCopy()
		if err := tenantClient.Get(ctx, client.ObjectKeyFromObject(probe), current); err != nil {
			return ctrl.Result{}, err
		}
		phase, _, _ := unstructured.NestedString(current.Object, "status", "phase")
		if phase == "Failed" {
			return ctrl.Result{}, fmt.Errorf("network functional probe failed")
		}
		if phase != "Succeeded" {
			return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
		}
		if err := tenantClient.Delete(ctx, current); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			removeTenantIdentity(status, probe.GroupVersionKind(), probe.GetNamespace(), probe.GetName())
			status.Stage = tenancyv1alpha1.StageNetworkReady
			setCondition(status, tenant, "NetworkReady", metav1.ConditionTrue, "NetworkReady", "Tenant networking and DNS/API probes are ready")
			return nil
		})
	default:
		return reconciler.reconcilePostCNIWorkers(ctx, tenant, canonical, specHash, foundation)
	}
}

func (reconciler *TenantReconciler) ensureNetworkSource(ctx context.Context, desired *corev1.ConfigMap, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (tenancyv1alpha1.ObservedResourceIdentity, bool, error) {
	var current corev1.ConfigMap
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: desired.Namespace, Name: desired.Name}, &current)
	if apierrors.IsNotFound(err) {
		if err := reconciler.Create(ctx, desired); err != nil {
			return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
		}
		desired.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("ConfigMap"))
		return identityFor(desired), true, nil
	}
	if err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	if err := validateRootOwnership(&current, tenant, specHash, foundation.Hash, "network-source", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	current.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("ConfigMap"))
	if current.Data["addons.yaml"] != desired.Data["addons.yaml"] {
		current.Data = desired.Data
		if err := reconciler.Update(ctx, &current); err != nil {
			return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
		}
		return identityFor(&current), true, nil
	}
	return identityFor(&current), false, nil
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

func networkProbe(context resources.Context, image string) *unstructured.Unstructured {
	value := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1", "kind": "Pod",
		"metadata": map[string]any{"name": context.Tenant.Name + "-network-verify", "namespace": "default"},
		"spec": map[string]any{
			"restartPolicy": "Never", "automountServiceAccountToken": false,
			"containers": []any{map[string]any{
				"name": "verify", "image": image,
				"command": []any{"sh", "-ec", fmt.Sprintf("nslookup kubernetes.default.svc.%s && wget -qO- --timeout=5 https://kubernetes.default.svc.%s/version --no-check-certificate >/dev/null", context.Inputs.ClusterDomain, context.Inputs.ClusterDomain)},
			}},
		},
	}}
	resources.MarkTenantObject(context, value, "network-probe")
	return value
}
