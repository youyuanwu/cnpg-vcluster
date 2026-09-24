package controller

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/handler"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/sanitize"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

const tenantFinalizer = "tenancy.cnpg-vcluster.io/finalizer"
const progressRequeueInterval = time.Second

func progressRequeue() ctrl.Result {
	return ctrl.Result{RequeueAfter: progressRequeueInterval}
}

// +kubebuilder:rbac:groups=tenancy.cnpg-vcluster.io,resources=tenants,verbs=get;list;watch;update
// +kubebuilder:rbac:groups=tenancy.cnpg-vcluster.io,resources=tenants/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=tenancy.cnpg-vcluster.io,resources=tenants/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch;update
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch;delete
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=cluster.x-k8s.io,resources=clusters;machinedeployments,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=cluster.x-k8s.io,resources=machines;machinesets,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=infrastructure.cluster.x-k8s.io,resources=devclusters;devmachinetemplates,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=infrastructure.cluster.x-k8s.io,resources=devmachines,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=bootstrap.cluster.x-k8s.io,resources=kubeadmconfigtemplates,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=controlplane.cluster.x-k8s.io,resources=kamajicontrolplanes,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch;delete

type TenantReconciler struct {
	client.Client
	APIReader               client.Reader
	Docker                  DockerClient
	TenantClients           TenantClientFactory
	SupportedVersion        string
	MutationEnabled         bool
	FoundationNamespace     string
	FoundationName          string
	ExpectedControllerImage string
}

func (reconciler *TenantReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	var tenant tenancyv1alpha1.Tenant
	if err := reconciler.reader().Get(ctx, types.NamespacedName{Name: request.Name}, &tenant); err != nil {
		if ignored := client.IgnoreNotFound(err); ignored != nil {
			return ctrl.Result{}, fmt.Errorf("%s", sanitize.Text(ignored.Error()))
		}
		return ctrl.Result{}, nil
	}
	canonical, specHash, validationErr := validation.Validate(tenant.Name, tenant.Spec, reconciler.SupportedVersion)
	if validationErr != nil {
		return ctrl.Result{}, reconciler.publishValidation(ctx, &tenant, specHash, validationErr)
	}
	managedDeletion := !tenant.DeletionTimestamp.IsZero() &&
		containsString(tenant.Finalizers, tenantFinalizer)
	if !reconciler.MutationEnabled && !managedDeletion {
		return ctrl.Result{}, reconciler.publishMutationDisabled(ctx, &tenant, specHash)
	}
	var foundation Foundation
	var err error
	if managedDeletion {
		foundation, err = loadFoundationForDeletion(
			ctx,
			reconciler.reader(),
			reconciler.foundationNamespace(),
			reconciler.foundationName(),
			tenant.Status.FoundationHash,
		)
	} else {
		foundation, err = loadFoundation(ctx, reconciler.reader(), reconciler.docker(), reconciler.foundationNamespace(), reconciler.foundationName(), reconciler.SupportedVersion, reconciler.ExpectedControllerImage)
	}
	if err != nil {
		return ctrl.Result{}, reconciler.failure(ctx, &tenant, specHash, tenancyv1alpha1.PhaseFailed, "FoundationInvalid", err)
	}
	if !tenant.DeletionTimestamp.IsZero() {
		if !containsString(tenant.Finalizers, tenantFinalizer) {
			return ctrl.Result{}, nil
		}
		result, err := reconciler.finalizeTenant(ctx, &tenant, specHash, foundation)
		if err != nil {
			return result, reconciler.failure(ctx, &tenant, specHash, tenancyv1alpha1.PhaseDeleting, "DeletionBlocked", err)
		}
		return result, nil
	}
	if !foundation.MutationEnabled {
		return ctrl.Result{}, reconciler.failure(ctx, &tenant, specHash, tenancyv1alpha1.PhaseFailed, "FoundationMutationDisabled", fmt.Errorf("Tenant foundation mutation is disabled"))
	}
	if err := validatePeerNetworks(ctx, reconciler.reader(), &tenant, canonical, foundation, reconciler.SupportedVersion); err != nil {
		return ctrl.Result{}, reconciler.failure(ctx, &tenant, specHash, tenancyv1alpha1.PhaseFailed, "NetworkConflict", err)
	}
	if !containsString(tenant.Finalizers, tenantFinalizer) {
		updated := tenant.DeepCopy()
		updated.Finalizers = append(updated.Finalizers, tenantFinalizer)
		if err := reconciler.Update(ctx, updated); err != nil {
			return ctrl.Result{}, fmt.Errorf("%s", sanitize.Text(err.Error()))
		}
		return progressRequeue(), nil
	}
	if tenant.Status.FoundationHash == "" {
		return progressRequeue(), reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.FoundationHash = foundation.Hash
			return nil
		})
	}
	if tenant.Status.FoundationHash != foundation.Hash {
		return ctrl.Result{}, reconciler.failure(ctx, &tenant, specHash, tenancyv1alpha1.PhaseFailed, "FoundationMismatch", fmt.Errorf("Tenant foundation identity changed"))
	}
	if tenant.Status.Endpoint != "" {
		if err := validateEndpoint(ctx, reconciler.reader(), reconciler.foundationNamespace(), foundation, &tenant, specHash); err != nil {
			return ctrl.Result{}, reconciler.failure(ctx, &tenant, specHash, tenancyv1alpha1.PhaseOwnershipInvalid, "EndpointOwnershipInvalid", err)
		}
	}
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		initializeReconcileStatus(status, &tenant)
		return nil
	}); err != nil {
		return ctrl.Result{}, err
	}
	result, err := reconciler.reconcileDesiredState(ctx, &tenant, canonical, specHash, foundation)
	if err != nil {
		if errors.Is(err, errStableApplyConflict) {
			ctrl.LoggerFrom(ctx).Info(
				"retrying stable apply after optimistic-lock conflict",
				"error",
				sanitize.Text(err.Error()),
			)
			return progressRequeue(), nil
		}
		if errors.Is(err, errImmutableDrift) {
			return ctrl.Result{RequeueAfter: readyObservationInterval}, reconciler.degraded(ctx, &tenant, "ImmutableDrift", err)
		}
		if errors.Is(err, errRootClusterMissing) {
			return ctrl.Result{RequeueAfter: readyObservationInterval}, reconciler.degraded(ctx, &tenant, "RootClusterMissing", err)
		}
		phase := tenancyv1alpha1.PhaseFailed
		reason := "ReconcileFailed"
		if isOwnershipError(err) {
			phase = tenancyv1alpha1.PhaseOwnershipInvalid
			reason = "OwnershipInvalid"
		}
		return result, reconciler.failure(ctx, &tenant, specHash, phase, reason, err)
	}
	if result.Requeue || (result.RequeueAfter > 0 && result.RequeueAfter < readyObservationInterval) {
		if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.ObservedGeneration = tenant.Generation
			status.Phase = tenancyv1alpha1.PhaseProgressing
			setCondition(status, &tenant, "Ready", metav1.ConditionFalse, "Progressing", "Tenant reconciliation is progressing")
			return nil
		}); err != nil {
			return ctrl.Result{}, err
		}
	}
	return result, nil
}

func initializeReconcileStatus(status *tenancyv1alpha1.TenantStatus, tenant *tenancyv1alpha1.Tenant) {
	status.ObservedGeneration = tenant.Generation
	if status.Phase == "" || status.Phase == tenancyv1alpha1.PhasePending {
		status.Phase = tenancyv1alpha1.PhaseProgressing
	}
	setCondition(status, tenant, "Accepted", metav1.ConditionTrue, "Accepted", "Tenant specification is accepted")
	setCondition(status, tenant, "FoundationReady", metav1.ConditionTrue, "FoundationReady", "Tenant foundation identity is current")
	setCondition(status, tenant, "OwnershipValid", metav1.ConditionTrue, "OwnershipValid", "Observed Tenant root ownership is valid")
}

func (reconciler *TenantReconciler) publishValidation(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, validationErr error) error {
	return reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.ObservedGeneration = tenant.Generation
		status.Phase = tenancyv1alpha1.PhaseFailed
		setCondition(status, tenant, "Accepted", metav1.ConditionFalse, "InvalidSpec", validationErr.Error())
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, "InvalidSpec", "Tenant specification is invalid")
		return nil
	})
}

func (reconciler *TenantReconciler) publishMutationDisabled(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string) error {
	return reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.ObservedGeneration = tenant.Generation
		status.Phase = tenancyv1alpha1.PhaseProgressing
		setCondition(status, tenant, "Accepted", metav1.ConditionTrue, "Accepted", "Tenant specification is accepted")
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, "MutationDisabled", "Tenant controller mutation is disabled until the clean cutover")
		return nil
	})
}

func (reconciler *TenantReconciler) failure(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, phase tenancyv1alpha1.TenantPhase, reason string, cause error) error {
	if err := reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.ObservedGeneration = tenant.Generation
		status.Phase = phase
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, reason, cause.Error())
		if phase == tenancyv1alpha1.PhaseOwnershipInvalid {
			setCondition(status, tenant, "OwnershipValid", metav1.ConditionFalse, reason, cause.Error())
		}
		if reason == "FoundationMismatch" || reason == "FoundationInvalid" {
			setCondition(status, tenant, "FoundationReady", metav1.ConditionFalse, reason, cause.Error())
		}
		return nil
	}); err != nil {
		return fmt.Errorf("%s; status update failed: %s", sanitize.Text(cause.Error()), sanitize.Text(err.Error()))
	}
	return fmt.Errorf("%s", sanitize.Text(cause.Error()))
}

func (reconciler *TenantReconciler) degraded(ctx context.Context, tenant *tenancyv1alpha1.Tenant, reason string, cause error) error {
	return reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.ObservedGeneration = tenant.Generation
		status.Phase = tenancyv1alpha1.PhaseDegraded
		setCondition(status, tenant, "Ready", metav1.ConditionFalse, reason, cause.Error())
		return nil
	})
}

func (reconciler *TenantReconciler) SetupWithManager(manager ctrl.Manager) error {
	mapObject := handler.EnqueueRequestsFromMapFunc(requestsForTenantObject)
	builder := ctrl.NewControllerManagedBy(manager).
		For(&tenancyv1alpha1.Tenant{}).
		WithOptions(controllerOptions())
	for _, gvk := range []schema.GroupVersionKind{
		clusterGVK,
		devClusterGVK,
		controlPlaneGVK,
		kubeadmTemplateGVK,
		devMachineTemplateGVK,
		machineDeploymentGVK,
		machineGVK,
	} {
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(gvk)
		builder = builder.Watches(object, mapObject)
	}

	return builder.Complete(reconciler)
}

func requestsForTenantObject(_ context.Context, object client.Object) []ctrl.Request {
	name := object.GetAnnotations()[resources.TenantAnnotation]
	if name == "" {
		return nil
	}
	return []ctrl.Request{{NamespacedName: types.NamespacedName{Name: name}}}
}

func controllerOptions() controller.Options {
	return controller.Options{MaxConcurrentReconciles: 1}
}

func (reconciler *TenantReconciler) reader() client.Reader {
	if reconciler.APIReader != nil {
		return reconciler.APIReader
	}
	return reconciler.Client
}

func (reconciler *TenantReconciler) docker() DockerClient {
	if reconciler.Docker != nil {
		return reconciler.Docker
	}
	return NewDockerClient("/var/run/docker.sock")
}

func (reconciler *TenantReconciler) tenantFactory() TenantClientFactory {
	if reconciler.TenantClients != nil {
		return reconciler.TenantClients
	}
	return tenantClientFactory{}
}

func (reconciler *TenantReconciler) foundationNamespace() string {
	if reconciler.FoundationNamespace != "" {
		return reconciler.FoundationNamespace
	}
	return defaultFoundationNamespace
}

func (reconciler *TenantReconciler) foundationName() string {
	if reconciler.FoundationName != "" {
		return reconciler.FoundationName
	}
	return defaultFoundationName
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
}

func isOwnershipError(err error) bool {
	if errors.Is(err, errWorkerOwnershipInvalid) {
		return true
	}
	value := sanitize.Text(err.Error())
	return containsAny(value, "ownership", "identity changed", "owner chain", "different endpoint allocation")
}

func containsAny(value string, needles ...string) bool {
	for _, needle := range needles {
		if strings.Contains(strings.ToLower(value), strings.ToLower(needle)) {
			return true
		}
	}
	return false
}
