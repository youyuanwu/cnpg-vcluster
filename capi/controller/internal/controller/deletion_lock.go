package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

const (
	DeletionReservationName    = "tenant-deletion-reservation"
	DeletionLeaseName          = "tenant-deletion-lock"
	deletionReservationKey     = "reservation.json"
	deletionReservationSeconds = 300
	deletionLeaseSeconds       = int32(60)
	deletionLockReleaseStarted = "DeletionLockReleaseStarted"
	deletionLockReleased       = "DeletionLockReleased"
)

type DeletionReservation struct {
	Schema     int     `json:"schema"`
	TenantName string  `json:"tenantName"`
	TenantUID  string  `json:"tenantUID"`
	Requester  string  `json:"requester"`
	Nonce      string  `json:"nonce"`
	ExpiresAt  float64 `json:"expiresAt"`
}

func AcquireDeletionAdmissionLease(
	ctx context.Context,
	kubernetes client.Client,
	namespace string,
	reservation DeletionReservation,
	requestUID string,
	now time.Time,
) error {
	key := types.NamespacedName{Namespace: namespace, Name: DeletionLeaseName}
	var lease coordinationv1.Lease
	err := kubernetes.Get(ctx, key, &lease)
	holder := reservation.Nonce
	renew := metav1.NewMicroTime(now.UTC())
	annotations := map[string]string{
		"tenancy.cnpg-vcluster.io/tenant":        reservation.TenantName,
		"tenancy.cnpg-vcluster.io/tenant-uid":    reservation.TenantUID,
		"tenancy.cnpg-vcluster.io/requester":     reservation.Requester,
		"tenancy.cnpg-vcluster.io/admission-uid": requestUID,
	}
	if apierrors.IsNotFound(err) {
		return kubernetes.Create(ctx, &coordinationv1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Name: key.Name, Namespace: key.Namespace, Annotations: annotations,
			},
			Spec: coordinationv1.LeaseSpec{
				HolderIdentity: &holder, LeaseDurationSeconds: pointer(deletionLeaseSeconds),
				AcquireTime: &renew, RenewTime: &renew,
			},
		})
	}
	if err != nil {
		return err
	}
	currentHolder := ""
	if lease.Spec.HolderIdentity != nil {
		currentHolder = *lease.Spec.HolderIdentity
	}
	if currentHolder == reservation.Nonce &&
		lease.Annotations["tenancy.cnpg-vcluster.io/tenant-uid"] == reservation.TenantUID &&
		lease.Annotations["tenancy.cnpg-vcluster.io/requester"] == reservation.Requester {
		return nil
	}
	if !deletionLeaseExpired(lease, now) {
		return fmt.Errorf("another targeted deletion holds the admission Lease")
	}
	lease.SetAnnotations(annotations)
	lease.Spec.HolderIdentity = &holder
	lease.Spec.LeaseDurationSeconds = pointer(deletionLeaseSeconds)
	lease.Spec.AcquireTime = &renew
	lease.Spec.RenewTime = &renew
	return kubernetes.Update(ctx, &lease)
}

func ReadDeletionReservation(
	ctx context.Context,
	reader client.Reader,
	namespace string,
	now time.Time,
) (DeletionReservation, *corev1.ConfigMap, error) {
	reservation, configMap, err := readDeletionReservation(ctx, reader, namespace)
	if err != nil {
		return DeletionReservation{}, nil, err
	}
	if reservation.ExpiresAt <= float64(now.Unix()) {
		return DeletionReservation{}, nil, fmt.Errorf("deletion reservation is invalid or expired")
	}
	return reservation, configMap, nil
}

func readDeletionReservation(
	ctx context.Context,
	reader client.Reader,
	namespace string,
) (DeletionReservation, *corev1.ConfigMap, error) {
	var configMap corev1.ConfigMap
	if err := reader.Get(ctx, types.NamespacedName{Namespace: namespace, Name: DeletionReservationName}, &configMap); err != nil {
		return DeletionReservation{}, nil, err
	}
	var reservation DeletionReservation
	if err := json.Unmarshal([]byte(configMap.Data[deletionReservationKey]), &reservation); err != nil {
		return DeletionReservation{}, nil, fmt.Errorf("decode deletion reservation: %w", err)
	}
	if reservation.Schema != 1 || reservation.TenantName == "" || reservation.TenantUID == "" ||
		reservation.Requester == "" || reservation.Nonce == "" || reservation.ExpiresAt <= 0 {
		return DeletionReservation{}, nil, fmt.Errorf("deletion reservation is invalid")
	}
	return reservation, &configMap, nil
}

func (reconciler *TenantReconciler) ensureDeletionLock(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
) (bool, error) {
	now := time.Now().UTC()
	reservation, reservationConfigMap, err := readDeletionReservation(ctx, reconciler.reader(), reconciler.foundationNamespace())
	if err != nil {
		return false, fmt.Errorf("read targeted deletion reservation: %w", err)
	}
	if reservation.TenantName != tenant.Name || reservation.TenantUID != string(tenant.UID) {
		return false, fmt.Errorf("targeted deletion reservation does not match the Tenant")
	}
	if tenant.Status.Teardown == nil || tenant.Status.Teardown.Reservation == "" {
		if reservation.ExpiresAt <= float64(now.Unix()) {
			var admitted coordinationv1.Lease
			if err := reconciler.reader().Get(ctx, types.NamespacedName{
				Namespace: reconciler.foundationNamespace(),
				Name:      DeletionLeaseName,
			}, &admitted); err != nil {
				return false, fmt.Errorf("targeted deletion reservation expired before Lease acquisition")
			}
			if !deletionLeaseMatches(admitted, reservation, tenant) {
				return false, fmt.Errorf("expired targeted deletion reservation has no matching admission Lease")
			}
		}
		return false, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			if status.Teardown == nil {
				status.Teardown = &tenancyv1alpha1.TeardownStatus{}
			}

			status.Teardown.Reservation = reservation.Nonce
			return nil
		})
	}
	if tenant.Status.Teardown.Reservation != reservation.Nonce {
		return false, fmt.Errorf("targeted deletion reservation nonce changed")
	}
	if reservation.ExpiresAt < float64(now.Add(2*time.Minute).Unix()) {
		reservation.ExpiresAt = float64(now.Add(deletionReservationSeconds * time.Second).Unix())
		encoded, err := json.Marshal(reservation)
		if err != nil {
			return false, err
		}
		reservationConfigMap.Data[deletionReservationKey] = string(encoded)
		if err := reconciler.Update(ctx, reservationConfigMap); err != nil {
			return false, err
		}
	}

	key := types.NamespacedName{Namespace: reconciler.foundationNamespace(), Name: DeletionLeaseName}
	var lease coordinationv1.Lease
	err = reconciler.reader().Get(ctx, key, &lease)
	if apierrors.IsNotFound(err) {
		holder := reservation.Nonce
		acquire := metav1.NewMicroTime(now)
		lease = coordinationv1.Lease{
			ObjectMeta: metav1.ObjectMeta{
				Name:      key.Name,
				Namespace: key.Namespace,
				Annotations: map[string]string{
					"tenancy.cnpg-vcluster.io/tenant":     tenant.Name,
					"tenancy.cnpg-vcluster.io/tenant-uid": string(tenant.UID),
					"tenancy.cnpg-vcluster.io/requester":  reservation.Requester,
				},
			},
			Spec: coordinationv1.LeaseSpec{
				HolderIdentity:       &holder,
				LeaseDurationSeconds: pointer(deletionLeaseSeconds),
				AcquireTime:          &acquire,
				RenewTime:            &acquire,
			},
		}
		if err := reconciler.Create(ctx, &lease); err != nil {
			if apierrors.IsAlreadyExists(err) {
				return false, nil
			}
			return false, err
		}
		return false, nil
	}
	if err != nil {
		return false, err
	}
	holder := ""
	if lease.Spec.HolderIdentity != nil {
		holder = *lease.Spec.HolderIdentity
	}
	expired := deletionLeaseExpired(lease, now)
	if holder != reservation.Nonce && !expired {
		return false, fmt.Errorf("another targeted deletion holds the destructive Lease")
	}
	annotations := lease.GetAnnotations()
	if holder == reservation.Nonce &&
		(annotations["tenancy.cnpg-vcluster.io/tenant"] != tenant.Name ||
			annotations["tenancy.cnpg-vcluster.io/tenant-uid"] != string(tenant.UID)) {
		return false, fmt.Errorf("targeted deletion Lease identity mismatch")
	}
	lease.Spec.HolderIdentity = &reservation.Nonce
	lease.Spec.LeaseDurationSeconds = pointer(deletionLeaseSeconds)
	renew := metav1.NewMicroTime(now)
	lease.Spec.RenewTime = &renew
	if expired || holder != reservation.Nonce {
		lease.Spec.AcquireTime = &renew
		lease.SetAnnotations(map[string]string{
			"tenancy.cnpg-vcluster.io/tenant":     tenant.Name,
			"tenancy.cnpg-vcluster.io/tenant-uid": string(tenant.UID),
			"tenancy.cnpg-vcluster.io/requester":  reservation.Requester,
		})
	}
	if err := reconciler.Update(ctx, &lease); err != nil {
		return false, err
	}
	return true, nil
}

func (reconciler *TenantReconciler) deletionMutationBlocked(
	ctx context.Context,
	currentName string,
	now time.Time,
) (bool, error) {
	var tenants tenancyv1alpha1.TenantList
	if err := reconciler.reader().List(ctx, &tenants); err != nil {
		return false, err
	}
	for index := range tenants.Items {
		tenant := &tenants.Items[index]
		if tenant.Name != currentName && !tenant.DeletionTimestamp.IsZero() {
			return true, nil
		}
	}
	if _, _, err := ReadDeletionReservation(ctx, reconciler.reader(), reconciler.foundationNamespace(), now); err == nil {
		return true, nil
	} else if !apierrors.IsNotFound(err) && !containsAny(err.Error(), "invalid or expired") {
		return false, err
	}
	var lease coordinationv1.Lease
	err := reconciler.reader().Get(ctx, types.NamespacedName{
		Namespace: reconciler.foundationNamespace(), Name: DeletionLeaseName,
	}, &lease)
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return !deletionLeaseExpired(lease, now), nil
}

func deletionLeaseMatches(
	lease coordinationv1.Lease,
	reservation DeletionReservation,
	tenant *tenancyv1alpha1.Tenant,
) bool {
	return lease.Spec.HolderIdentity != nil &&
		*lease.Spec.HolderIdentity == reservation.Nonce &&
		lease.Annotations["tenancy.cnpg-vcluster.io/tenant"] == tenant.Name &&
		lease.Annotations["tenancy.cnpg-vcluster.io/tenant-uid"] == string(tenant.UID) &&
		lease.Annotations["tenancy.cnpg-vcluster.io/requester"] == reservation.Requester
}

func (reconciler *TenantReconciler) releaseDeletionLock(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
) (bool, error) {
	if tenant.Status.Teardown == nil || tenant.Status.Teardown.Reservation == "" {
		return false, fmt.Errorf("targeted deletion reservation status is missing")
	}
	nonce := tenant.Status.Teardown.Reservation
	var reservation corev1.ConfigMap
	reservationKey := types.NamespacedName{Namespace: reconciler.foundationNamespace(), Name: DeletionReservationName}
	if err := reconciler.reader().Get(ctx, reservationKey, &reservation); err != nil {
		if !apierrors.IsNotFound(err) {
			return false, err
		}
	} else {
		decoded, _, err := readDeletionReservation(ctx, reconciler.reader(), reconciler.foundationNamespace())
		if err != nil || decoded.Nonce != nonce || decoded.TenantUID != string(tenant.UID) {
			return false, fmt.Errorf("targeted deletion reservation identity changed before release")
		}
		if err := deleteWithPreconditions(ctx, reconciler.Client, &reservation); err != nil {
			return false, err
		}
		return false, nil
	}

	var lease coordinationv1.Lease
	leaseKey := types.NamespacedName{Namespace: reconciler.foundationNamespace(), Name: DeletionLeaseName}
	if err := reconciler.reader().Get(ctx, leaseKey, &lease); err != nil {
		if apierrors.IsNotFound(err) {
			return true, nil
		}
		return false, err
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != nonce ||
		lease.Annotations["tenancy.cnpg-vcluster.io/tenant-uid"] != string(tenant.UID) {
		return false, fmt.Errorf("targeted deletion Lease identity changed before release")
	}
	if err := deleteWithPreconditions(ctx, reconciler.Client, &lease); err != nil {
		return false, err
	}
	return false, nil
}

func deletionLeaseExpired(lease coordinationv1.Lease, now time.Time) bool {
	if lease.Spec.RenewTime == nil || lease.Spec.LeaseDurationSeconds == nil {
		return true
	}
	return !now.Before(lease.Spec.RenewTime.Add(time.Duration(*lease.Spec.LeaseDurationSeconds) * time.Second))
}

func deleteWithPreconditions(ctx context.Context, kubernetes client.Client, object client.Object) error {
	uid := object.GetUID()
	resourceVersion := object.GetResourceVersion()
	propagation := metav1.DeletePropagationBackground
	if err := kubernetes.Delete(ctx, object, &client.DeleteOptions{
		Preconditions:     &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
		PropagationPolicy: &propagation,
	}); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

func pointer[T any](value T) *T {
	return &value
}
