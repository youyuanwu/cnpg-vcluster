package controller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

const tenantAPIWorkloadsCleanupComplete = "TenantAPIWorkloadsCleanupComplete"
const tenantAPICleanupUnavailable = "TenantAPICleanupUnavailable"

func (reconciler *TenantReconciler) finalizePartial(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (ctrl.Result, error) {
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.Phase = tenancyv1alpha1.PhaseDeleting
		setCondition(status, tenant, "Deleting", metav1.ConditionTrue, "PartialFinalization", "Cleaning exact Phase 2 Tenant state")
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, "Deleting", "Tenant deletion is in progress")
		return nil
	}); err != nil {
		return ctrl.Result{}, err
	}
	clusterIdentity, clusterPresent, err := reconciler.observeExactUnstructured(ctx, tenant, specHash, foundation, clusterGVK, tenant.Name, tenant.Name, "cluster")
	if err != nil {
		return ctrl.Result{}, err
	}

	preflightPartialOwnership := func(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (bool, error) {
		adopted := make([]tenancyv1alpha1.ObservedResourceIdentity, 0)
		observedObjects := make([]*unstructured.Unstructured, 0)
		endpoint, endpointPresent, err := observeEndpoint(
			ctx,
			reconciler.reader(),
			reconciler.foundationNamespace(),
			foundation,
			tenant,
			specHash,
		)
		if err != nil {
			return false, err
		}
		if stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageEndpointAllocated) && !endpointPresent {
			return false, fmt.Errorf("expected endpoint allocation is absent before deletion")
		}
		adoptedEndpoint := ""
		if endpointPresent && tenant.Status.Endpoint == "" {
			adoptedEndpoint = endpoint
		}
		checkUnstructured := func(gvk schema.GroupVersionKind, namespace, name, resource string, expected bool) error {
			object := &unstructured.Unstructured{}
			object.SetGroupVersionKind(gvk)
			err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, object)
			if apierrors.IsNotFound(err) {
				if expected {
					return fmt.Errorf("expected %s %s is absent before deletion", gvk.Kind, name)
				}
				return nil
			}
			if err != nil {
				return err
			}
			if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
				return err
			}
			observedObjects = append(observedObjects, object)
			recorded := findIdentity(tenant.Status, gvk, namespace, name)
			if recorded != nil {
				if recorded.UID != string(object.GetUID()) {
					return fmt.Errorf("%s %s identity changed before deletion", gvk.Kind, name)
				}
				return nil
			}
			adopted = append(adopted, identityFor(object))
			return nil
		}

		var namespace corev1.Namespace
		err = reconciler.reader().Get(ctx, types.NamespacedName{Name: tenant.Name}, &namespace)
		namespaceExpected := stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageNamespaceCreated)
		if apierrors.IsNotFound(err) {
			if namespaceExpected {
				return false, fmt.Errorf("expected Namespace %s is absent before deletion", tenant.Name)
			}
		} else if err != nil {
			return false, err
		} else {
			if err := validateRootOwnership(&namespace, tenant, specHash, foundation.Hash, "namespace", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
				return false, err
			}
			if len(namespace.OwnerReferences) != 0 {
				return false, fmt.Errorf("Namespace has an unexpected owner before deletion")
			}
			namespace.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Namespace"))
			recorded := findIdentity(tenant.Status, corev1.SchemeGroupVersion.WithKind("Namespace"), "", tenant.Name)
			if recorded != nil && recorded.UID != string(namespace.UID) {
				return false, fmt.Errorf("Namespace identity changed before deletion")
			}
			if recorded == nil {
				adopted = append(adopted, identityFor(&namespace))
			}
		}

		checks := []struct {
			gvk      schema.GroupVersionKind
			name     string
			resource string
			expected bool
		}{
			{clusterGVK, tenant.Name, "cluster", stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageClusterCreated)},
			{devClusterGVK, tenant.Name, "dev-cluster", stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageDevClusterCreated)},
			{controlPlaneGVK, tenant.Name, "kamaji-control-plane", stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageControlPlaneCreated)},
			{kubeadmTemplateGVK, tenant.Name + "-worker", "kubeadm-config-template", stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageKubeadmTemplateCreated)},
			{devMachineTemplateGVK, tenant.Name + "-worker", "dev-machine-template", stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageMachineTemplateCreated)},
			{machineDeploymentGVK, tenant.Name + "-worker", "machine-deployment", stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageMachineDeploymentCreated)},
		}
		for _, check := range checks {
			if err := checkUnstructured(check.gvk, tenant.Name, check.name, check.resource, check.expected); err != nil {
				return false, err
			}
		}
		combinedStatus := tenant.Status.DeepCopy()
		for _, identity := range adopted {
			if err := upsertIdentity(combinedStatus, identity); err != nil {
				return false, err
			}
		}
		for _, object := range observedObjects {
			if err := validateProviderOwner(object, *combinedStatus, false); err != nil {
				return false, err
			}
		}

		var secret corev1.Secret
		err = reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name + "-kubeconfig"}, &secret)
		secretExpected := stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageKubeconfigReady)
		if apierrors.IsNotFound(err) {
			if secretExpected {
				return false, fmt.Errorf("expected Tenant kubeconfig Secret is absent before deletion")
			}
		} else if err != nil {
			return false, err
		} else {
			if secret.Type != corev1.SecretType("cluster.x-k8s.io/secret") || len(secret.Data["value"]) == 0 {
				return false, fmt.Errorf("Tenant kubeconfig Secret contract is invalid")
			}
			controlPlane := findIdentity(*combinedStatus, controlPlaneGVK, tenant.Name, tenant.Name)
			if controlPlane == nil || !hasOwnerUID(secret.OwnerReferences, types.UID(controlPlane.UID)) {
				return false, fmt.Errorf("Tenant kubeconfig Secret owner cannot be proven")
			}
			secret.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Secret"))
			recorded := findIdentity(tenant.Status, corev1.SchemeGroupVersion.WithKind("Secret"), tenant.Name, secret.Name)
			liveIdentity := kubeconfigSecretIdentity(&secret)
			if recorded != nil &&
				(recorded.UID != string(secret.UID) ||
					recorded.ContentSHA256 == "" ||
					recorded.ContentSHA256 != liveIdentity.ContentSHA256) {
				return false, fmt.Errorf("Tenant kubeconfig Secret identity changed before deletion")
			}
			if recorded == nil {
				adopted = append(adopted, liveIdentity)
			}
		}

		volumeName := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
		volume, err := reconciler.docker().InspectVolume(ctx, volumeName)
		if err != nil {
			return false, err
		}
		volumeExpected := stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageVolumeCreated)
		if volume == nil && volumeExpected {
			return false, fmt.Errorf("expected Docker volume is absent before deletion")
		}
		var adoptedVolume *tenancyv1alpha1.DockerVolumeIdentity
		if volume != nil {
			expectedLabels := map[string]string{
				foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
				"cnpg-vcluster.capi/role":                  "tenant-storage",
				"cnpg-vcluster.capi/tenant":                tenant.Name,
				"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
				"tenancy.cnpg-vcluster.io/spec-hash":       specHash,
				"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
			}
			if !stringMapEqual(volume.Labels, expectedLabels) {
				return false, fmt.Errorf("Docker volume ownership cannot be proven before deletion")
			}
			if tenant.Status.DockerVolume != nil {
				if volume.CreatedAt != tenant.Status.DockerVolume.CreatedAt ||
					volume.Mountpoint != tenant.Status.DockerVolume.Mountpoint {
					return false, fmt.Errorf("Docker volume identity changed before deletion")
				}
			} else {
				adoptedVolume = &tenancyv1alpha1.DockerVolumeIdentity{
					Name:       volume.Name,
					CreatedAt:  volume.CreatedAt,
					Mountpoint: volume.Mountpoint,
					Labels:     volume.Labels,
				}
			}
		}
		if len(adopted) == 0 && adoptedVolume == nil && adoptedEndpoint == "" {
			return false, nil
		}
		if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			for _, identity := range adopted {
				if err := upsertIdentity(status, identity); err != nil {
					return err
				}
			}
			if adoptedVolume != nil {
				status.DockerVolume = adoptedVolume
			}
			if adoptedEndpoint != "" {
				status.Endpoint = adoptedEndpoint
			}
			return nil
		}); err != nil {
			return false, err
		}
		return true, nil
	}
	destructiveStarted := tenant.Status.Teardown != nil &&
		(tenant.Status.Teardown.Phase == "ManagementDeletionStarted" ||
			tenant.Status.Teardown.Phase == tenantAPIWorkloadsCleanupComplete ||
			tenant.Status.Teardown.Authority == "LiveBootstrapRBACCleanupComplete" ||
			tenant.Status.Teardown.Phase == tenancyv1alpha1.StageEndpointReleased) ||
		tenant.Status.Stage == tenancyv1alpha1.StageEndpointReleased
	if !destructiveStarted {
		adopted, err := preflightPartialOwnership(ctx, tenant, specHash, foundation)
		if err != nil {
			return ctrl.Result{}, err
		}
		if adopted {
			return ctrl.Result{Requeue: true}, nil
		}
		if tenant.Status.Teardown == nil || tenant.Status.Teardown.Phase != "OwnershipPreflightComplete" {
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				if status.Teardown == nil {
					status.Teardown = &tenancyv1alpha1.TeardownStatus{}
				}
				status.Teardown.Phase = "OwnershipPreflightComplete"
				if status.Teardown.Authority == "" {
					status.Teardown.Authority = "TenantAPINeverAuthorized"
				}
				return nil
			})
		}
	}
	managementCleanupAuthorized := tenant.Status.Teardown != nil &&
		(tenant.Status.Teardown.Authority == "LiveBootstrapRBACCleanupComplete" ||
			tenant.Status.Teardown.Authority == tenantAPICleanupUnavailable)
	if managementCleanupAuthorized {
		if err := validateManagementCleanupCheckpoint(tenant); err != nil {
			return ctrl.Result{}, err
		}
	}
	if tenant.Status.Stage != tenancyv1alpha1.StageEndpointReleased &&
		stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageTenantAPICleanupRequired) &&
		!managementCleanupAuthorized {
		tenantClient, _, err := tenantClientFromSecret(ctx, reconciler.reader(), reconciler.tenantFactory(), tenant.Name, tenant.Name, tenant.Status.Endpoint)
		if err != nil {
			return reconciler.checkpointTenantAPICleanupUnavailable(ctx, tenant, clusterIdentity, clusterPresent, err)
		}
		workloadsCleanupComplete := tenant.Status.Teardown != nil &&
			tenant.Status.Teardown.Phase == tenantAPIWorkloadsCleanupComplete
		tenantResourcesAbsent, err := deleteTenantResources(
			ctx,
			tenantClient,
			tenant,
			specHash,
			foundation.Hash,
			workloadsCleanupComplete,
		)
		if err != nil {
			if isOwnershipError(err) {
				return ctrl.Result{}, err
			}
			return reconciler.checkpointTenantAPICleanupUnavailable(ctx, tenant, clusterIdentity, clusterPresent, err)
		}
		if !tenantResourcesAbsent {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
		}
		if !workloadsCleanupComplete {
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				if status.Teardown == nil {
					status.Teardown = &tenancyv1alpha1.TeardownStatus{}
				}
				status.Teardown.Phase = tenantAPIWorkloadsCleanupComplete
				return nil
			})
		}
		complete, err := deleteBootstrapRBAC(ctx, tenantClient)
		if err != nil {
			return reconciler.checkpointTenantAPICleanupUnavailable(ctx, tenant, clusterIdentity, clusterPresent, err)
		}
		if !complete {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
		}
		if !clusterPresent {
			return ctrl.Result{}, fmt.Errorf("live Tenant API cleanup checkpoint requires the exact hosted Cluster")
		}
		recordedCluster := findIdentity(tenant.Status, clusterGVK, tenant.Name, tenant.Name)
		if recordedCluster == nil || recordedCluster.UID != clusterIdentity.UID {
			return ctrl.Result{}, fmt.Errorf("live Tenant API cleanup checkpoint Cluster identity is not recorded")
		}
		if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.Phase = "LiveBootstrapRBACCleanupComplete"
			status.Teardown.Authority = "LiveBootstrapRBACCleanupComplete"
			status.Teardown.ClusterUID = clusterIdentity.UID
			return nil
		}); err != nil {
			return ctrl.Result{}, err
		}
	} else if tenant.Status.Teardown != nil && tenant.Status.Teardown.Authority != "" &&
		tenant.Status.Teardown.Authority != "TenantAPINeverAuthorized" &&
		tenant.Status.Teardown.Authority != "LiveBootstrapRBACCleanupComplete" &&
		tenant.Status.Teardown.Authority != tenantAPICleanupUnavailable {
		return ctrl.Result{}, fmt.Errorf("partial Tenant cleanup authority is invalid")
	}
	for _, identity := range tenant.Status.ObservedResources {
		resource := ""
		switch identity.Kind {
		case "ConfigMap":
			if identity.Namespace == tenant.Name {
				resource = "network-source"
			}
		case "ClusterResourceSet":
			resource = "network-resource-set"
		}
		if resource == "" {
			continue
		}
		gvk := schema.FromAPIVersionAndKind(identity.APIVersion, identity.Kind)
		absent, err := reconciler.deleteExactUnstructured(ctx, tenant, specHash, foundation, gvk, identity.Namespace, identity.Name, resource)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !absent {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
		}
	}
	if !stageAtOrAfter(tenant.Status.Stage, tenancyv1alpha1.StageTenantAPICleanupRequired) &&
		tenant.Status.Teardown != nil && tenant.Status.Teardown.Phase != "ManagementDeletionStarted" {
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Teardown.Phase = "ManagementDeletionStarted"
			return nil
		})
	}
	if !managementCleanupAuthorized && clusterPresent && findIdentity(tenant.Status, clusterGVK, tenant.Name, tenant.Name) == nil {
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if err := upsertIdentity(status, clusterIdentity); err != nil {
				return err
			}

			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.ClusterUID = clusterIdentity.UID
			return nil
		})
	}
	clusterAbsent, err := reconciler.deleteExactUnstructured(ctx, tenant, specHash, foundation, clusterGVK, tenant.Name, tenant.Name, "cluster")
	if err != nil {
		return ctrl.Result{}, err
	}

	if !clusterAbsent {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}

	for _, item := range []struct {
		gvk      schema.GroupVersionKind
		name     string
		resource string
	}{
		{machineDeploymentGVK, tenant.Name + "-worker", "machine-deployment"},
		{devMachineTemplateGVK, tenant.Name + "-worker", "dev-machine-template"},
		{kubeadmTemplateGVK, tenant.Name + "-worker", "kubeadm-config-template"},
		{controlPlaneGVK, tenant.Name, "kamaji-control-plane"},
		{devClusterGVK, tenant.Name, "dev-cluster"},
	} {
		absent, err := reconciler.deleteExactUnstructured(ctx, tenant, specHash, foundation, item.gvk, tenant.Name, item.name, item.resource)
		if err != nil {
			return ctrl.Result{}, err
		}
		if !absent {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
		}
	}

	hostAbsent, err := reconciler.deleteTenantHostState(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !hostAbsent {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}

	namespaceAbsent, err := reconciler.deleteExactNamespace(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !namespaceAbsent {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	if tenant.Status.Stage != tenancyv1alpha1.StageEndpointReleased {
		if err := releaseEndpoint(ctx, reconciler.Client, reconciler.reader(), reconciler.foundationNamespace(), foundation, tenant); err != nil {
			return ctrl.Result{}, err
		}
		if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Endpoint = ""
			status.Stage = tenancyv1alpha1.StageEndpointReleased
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.Phase = "EndpointReleased"
			return nil
		}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}
	if err := reconciler.removeTenantFinalizer(ctx, tenant.Name); err != nil {
		return ctrl.Result{}, err
	}

	return ctrl.Result{}, nil
}

func validateManagementCleanupCheckpoint(tenant *tenancyv1alpha1.Tenant) error {
	if tenant.Status.Teardown == nil || tenant.Status.Teardown.ClusterUID == "" {
		return fmt.Errorf("Tenant management cleanup checkpoint Cluster UID is missing")
	}
	cluster := findIdentity(tenant.Status, clusterGVK, tenant.Name, tenant.Name)
	if cluster == nil || cluster.UID != tenant.Status.Teardown.ClusterUID {
		return fmt.Errorf("Tenant management cleanup checkpoint Cluster UID is stale or mismatched")
	}
	return nil
}

func (reconciler *TenantReconciler) checkpointTenantAPICleanupUnavailable(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	clusterIdentity tenancyv1alpha1.ObservedResourceIdentity,
	clusterPresent bool,
	cause error,
) (ctrl.Result, error) {
	if !clusterPresent {
		return ctrl.Result{}, fmt.Errorf("Tenant API cleanup is unavailable and the exact hosted Cluster is absent: %w", cause)
	}
	recordedCluster := findIdentity(tenant.Status, clusterGVK, tenant.Name, tenant.Name)
	if recordedCluster == nil || recordedCluster.UID != clusterIdentity.UID {
		return ctrl.Result{}, fmt.Errorf("Tenant API cleanup fallback Cluster identity is not recorded")
	}
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		if status.Teardown == nil {
			status.Teardown = &tenancyv1alpha1.TeardownStatus{}
		}
		status.Teardown.Phase = tenantAPICleanupUnavailable
		status.Teardown.Authority = tenantAPICleanupUnavailable
		status.Teardown.ClusterUID = clusterIdentity.UID
		setCondition(status, tenant, "Deleting", metav1.ConditionTrue, tenantAPICleanupUnavailable, "Tenant API cleanup is unavailable; continuing disposable local cluster teardown")
		return nil
	}); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{Requeue: true}, nil
}

func (reconciler *TenantReconciler) removeTenantFinalizer(ctx context.Context, name string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var current tenancyv1alpha1.Tenant
		if err := reconciler.reader().Get(ctx, types.NamespacedName{Name: name}, &current); err != nil {
			if apierrors.IsNotFound(err) {
				return nil
			}
			return err
		}
		if !containsString(current.Finalizers, tenantFinalizer) {
			return nil
		}
		current.Finalizers = removeString(current.Finalizers, tenantFinalizer)
		return reconciler.Update(ctx, &current)
	})
}

func (reconciler *TenantReconciler) observeExactUnstructured(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation, gvk schema.GroupVersionKind, namespace, name, resource string) (tenancyv1alpha1.ObservedResourceIdentity, bool, error) {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(gvk)
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, object)
	if apierrors.IsNotFound(err) {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, nil
	}
	if err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	if err := validateRootOwnership(object, tenant, specHash, foundation.Hash, resource, foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
		return tenancyv1alpha1.ObservedResourceIdentity{}, false, err
	}
	return identityFor(object), true, nil
}

func (reconciler *TenantReconciler) deleteKubeconfigSecret(ctx context.Context, tenant *tenancyv1alpha1.Tenant) (bool, error) {
	var secret corev1.Secret
	err := reconciler.reader().Get(ctx, types.NamespacedName{Namespace: tenant.Name, Name: tenant.Name + "-kubeconfig"}, &secret)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	controlPlane := findIdentity(tenant.Status, controlPlaneGVK, tenant.Name, tenant.Name)
	if controlPlane == nil || !hasOwnerUID(secret.OwnerReferences, types.UID(controlPlane.UID)) {
		return false, fmt.Errorf("Tenant kubeconfig Secret ownership cannot be proven")
	}
	secret.GetObjectKind().SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Secret"))
	if err := validateRecordedUID(tenant.Status, &secret); err != nil {
		return false, err
	}
	uid := secret.UID
	resourceVersion := secret.ResourceVersion
	err = reconciler.Delete(ctx, &secret, &client.DeleteOptions{
		Preconditions: &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
	})
	if err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	return false, nil
}

func stageAtOrAfter(current, boundary string) bool {
	order := []string{
		"",
		tenancyv1alpha1.StageEndpointAllocated,
		tenancyv1alpha1.StageNamespaceCreated,
		tenancyv1alpha1.StageClusterCreationAuthorized,
		tenancyv1alpha1.StageClusterCreated,
		tenancyv1alpha1.StageDevClusterCreated,
		tenancyv1alpha1.StageControlPlaneCreated,
		tenancyv1alpha1.StageKubeconfigReady,
		tenancyv1alpha1.StageTenantAPICleanupRequired,
		tenancyv1alpha1.StageBootstrapRBACApplied,
		tenancyv1alpha1.StageVolumeCreated,
		tenancyv1alpha1.StageKubeadmTemplateCreated,
		tenancyv1alpha1.StageMachineTemplateCreated,
		tenancyv1alpha1.StageMachineDeploymentCreated,
		tenancyv1alpha1.StageWorkersApplied,
		tenancyv1alpha1.StageNetworkSourcesApplied,
		tenancyv1alpha1.StageNetworkResourceSetApplied,
		tenancyv1alpha1.StageNetworkProbeCreated,
		tenancyv1alpha1.StageNetworkProbeSucceeded,
		tenancyv1alpha1.StageNetworkReady,
		tenancyv1alpha1.StagePostCNIWorkersReady,
		tenancyv1alpha1.StageStorageApplied,
		tenancyv1alpha1.StageStorageProbeCreated,
		tenancyv1alpha1.StageStorageProbeSucceeded,
		tenancyv1alpha1.StageStorageReady,
		tenancyv1alpha1.StageCNPGOperatorApplied,
		tenancyv1alpha1.StageCNPGStoragePrepared,
		tenancyv1alpha1.StageCNPGClusterApplied,
		tenancyv1alpha1.StageDatabaseProbeCreated,
		tenancyv1alpha1.StageDatabaseProbeSucceeded,
		tenancyv1alpha1.StageDatabaseReady,
		tenancyv1alpha1.StageReady,
		tenancyv1alpha1.StageEndpointReleased,
	}
	positions := map[string]int{}
	for index, value := range order {
		positions[value] = index
	}
	return positions[current] >= positions[boundary]
}

func removeString(values []string, target string) []string {
	result := values[:0]
	for _, value := range values {
		if value != target {
			result = append(result, value)
		}
	}
	return result
}
