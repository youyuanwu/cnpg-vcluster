package controller

import (
	"context"
	"fmt"
	"sort"
	"time"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) captureSurvivorSnapshots(
	ctx context.Context,
	target *tenancyv1alpha1.Tenant,
	foundation Foundation,
) ([]tenancyv1alpha1.SurvivorSnapshot, error) {
	var tenants tenancyv1alpha1.TenantList
	if err := reconciler.reader().List(ctx, &tenants); err != nil {
		return nil, fmt.Errorf("list survivor Tenants: %w", err)
	}
	snapshots := make([]tenancyv1alpha1.SurvivorSnapshot, 0, len(tenants.Items))
	for index := range tenants.Items {
		survivor := &tenants.Items[index]
		if survivor.UID == target.UID {
			continue
		}
		if !survivor.DeletionTimestamp.IsZero() {
			return nil, fmt.Errorf("survivor Tenant %s is deleting", survivor.Name)
		}
		_, specHash, err := validation.Validate(survivor.Name, survivor.Spec, reconciler.SupportedVersion)
		if err != nil || survivor.Status.SpecHash != specHash {
			return nil, fmt.Errorf("survivor Tenant %s specification evidence is invalid", survivor.Name)
		}
		if survivor.Status.Phase != tenancyv1alpha1.PhaseReady ||
			survivor.Status.Stage != tenancyv1alpha1.StageReady {
			return nil, fmt.Errorf("survivor Tenant %s is not Ready", survivor.Name)
		}
		if err := validateFunctionalEvidence(survivor.Status, time.Now().UTC()); err != nil {
			return nil, fmt.Errorf("survivor Tenant %s functional evidence is invalid: %w", survivor.Name, err)
		}
		if err := validateEndpoint(ctx, reconciler.reader(), reconciler.foundationNamespace(), foundation, survivor, specHash); err != nil {
			return nil, fmt.Errorf("survivor Tenant %s endpoint is invalid: %w", survivor.Name, err)
		}
		if err := validateRecordedResources(ctx, reconciler.reader(), survivor, specHash, foundation); err != nil {
			return nil, fmt.Errorf("survivor Tenant %s ownership is invalid: %w", survivor.Name, err)
		}
		if err := reconciler.validateRecordedVolume(ctx, survivor, specHash, foundation); err != nil {
			return nil, fmt.Errorf("survivor Tenant %s volume is invalid: %w", survivor.Name, err)
		}
		if err := reconciler.validateSurvivorTenantResources(ctx, survivor, specHash, foundation); err != nil {
			return nil, fmt.Errorf("survivor Tenant %s tenant resources are invalid: %w", survivor.Name, err)
		}
		snapshots = append(snapshots, tenancyv1alpha1.SurvivorSnapshot{
			Name:             survivor.Name,
			UID:              string(survivor.UID),
			SpecHash:         survivor.Status.SpecHash,
			ObservationsHash: survivor.Status.ObservationsHash,
			Endpoint:         survivor.Status.Endpoint,
		})
	}

	sort.Slice(snapshots, func(left, right int) bool { return snapshots[left].Name < snapshots[right].Name })
	return snapshots, nil
}

func (reconciler *TenantReconciler) validateSurvivorTenantResources(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	specHash string,
	foundation Foundation,
) error {
	tenantClient, _, err := tenantClientFromSecret(
		ctx,
		reconciler.reader(),
		reconciler.tenantFactory(),
		tenant.Name,
		tenant.Name,
		tenant.Status.Endpoint,
	)
	if err != nil {
		return err
	}
	for _, identity := range tenant.Status.TenantResources {
		gvk := schema.FromAPIVersionAndKind(identity.APIVersion, identity.Kind)
		object := &unstructured.Unstructured{}
		object.SetGroupVersionKind(gvk)
		if err := tenantClient.Get(ctx, client.ObjectKey{Namespace: identity.Namespace, Name: identity.Name}, object); err != nil {
			return err
		}
		if string(object.GetUID()) != identity.UID {
			return fmt.Errorf("%s %s UID changed", identity.Kind, identity.Name)
		}
		if identity.Kind == "Node" {
			continue
		}
		annotations := object.GetAnnotations()
		if annotations[resources.TenantUIDAnnotation] != string(tenant.UID) ||
			annotations[resources.SpecHashAnnotation] != specHash ||
			annotations[resources.FoundationAnnotation] != foundation.Hash {
			return fmt.Errorf("%s %s ownership changed", identity.Kind, identity.Name)
		}
	}
	return nil
}

func (reconciler *TenantReconciler) verifySurvivorSnapshots(
	ctx context.Context,
	target *tenancyv1alpha1.Tenant,
	foundation Foundation,
) error {
	current, err := reconciler.captureSurvivorSnapshots(ctx, target, foundation)
	if err != nil {
		return err
	}
	if len(current) != len(target.Status.SurvivorSnapshots) {
		return fmt.Errorf("survivor Tenant set changed during deletion")
	}
	expected := append([]tenancyv1alpha1.SurvivorSnapshot(nil), target.Status.SurvivorSnapshots...)
	sort.Slice(expected, func(left, right int) bool { return expected[left].Name < expected[right].Name })
	for index := range current {
		if current[index] != expected[index] {
			return fmt.Errorf("survivor Tenant %s changed during deletion", current[index].Name)
		}
	}
	return nil
}

func (reconciler *TenantReconciler) ensureDeletionSnapshots(
	ctx context.Context,
	target *tenancyv1alpha1.Tenant,
	foundation Foundation,
) (bool, error) {
	foundationTeardown := target.Status.Teardown != nil && target.Status.Teardown.FoundationTeardown
	if !foundationTeardown {
		authorized, err := reconciler.foundationTeardownAuthorized(ctx, foundation)
		if err != nil {
			return false, err
		}
		foundationTeardown = authorized
	} else {
		authorized, err := reconciler.foundationTeardownAuthorized(ctx, foundation)
		if err != nil || !authorized {
			return false, fmt.Errorf("foundation teardown authorization changed during deletion")
		}
	}
	if target.Status.FoundationSnapshot == nil {
		survivors := []tenancyv1alpha1.SurvivorSnapshot{}
		if !foundationTeardown {
			var err error
			survivors, err = reconciler.captureSurvivorSnapshots(ctx, target, foundation)
			if err != nil {
				return false, err
			}
		}
		foundationSnapshot, err := reconciler.captureFoundationSnapshot(ctx, target, foundation)
		if err != nil {
			return false, err
		}
		return false, reconciler.patchStatus(ctx, target.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.SurvivorSnapshots = survivors
			status.FoundationSnapshot = foundationSnapshot
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}
			status.Teardown.FoundationTeardown = foundationTeardown
			return nil
		})
	}
	endpointReleased := target.Status.Stage == tenancyv1alpha1.StageEndpointReleased
	if !endpointReleased {
		return true, nil
	}
	if !foundationTeardown {
		if err := reconciler.verifySurvivorSnapshots(ctx, target, foundation); err != nil {
			return false, err
		}
	}
	if err := reconciler.verifyFoundationSnapshot(ctx, target, foundation, endpointReleased); err != nil {
		return false, err
	}
	return true, nil
}
