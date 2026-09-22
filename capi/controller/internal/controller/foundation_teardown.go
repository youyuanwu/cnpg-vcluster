package controller

import (
	"context"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

const FoundationTeardownAuthorizationName = "tenant-foundation-teardown"

type foundationTeardownAuthorization struct {
	Schema         int               `json:"schema"`
	FoundationHash string            `json:"foundationHash"`
	Nonce          string            `json:"nonce"`
	Targets        map[string]string `json:"targets"`
}

func (reconciler *TenantReconciler) foundationTeardownAuthorized(
	ctx context.Context,
	foundation Foundation,
) (bool, error) {
	var configMap corev1.ConfigMap
	err := reconciler.reader().Get(ctx, types.NamespacedName{
		Namespace: reconciler.foundationNamespace(),
		Name:      FoundationTeardownAuthorizationName,
	}, &configMap)
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var authorization foundationTeardownAuthorization
	if err := json.Unmarshal([]byte(configMap.Data["authorization.json"]), &authorization); err != nil {
		return false, fmt.Errorf("decode foundation teardown authorization: %w", err)
	}
	if authorization.Schema != 1 || authorization.FoundationHash != foundation.Hash ||
		authorization.Nonce == "" || len(authorization.Targets) == 0 {
		return false, fmt.Errorf("foundation teardown authorization is invalid")
	}
	var tenants tenancyv1alpha1.TenantList
	if err := reconciler.reader().List(ctx, &tenants); err != nil {
		return false, err
	}
	for index := range tenants.Items {
		tenant := &tenants.Items[index]
		if authorization.Targets[tenant.Name] != string(tenant.UID) {
			return false, fmt.Errorf("foundation teardown authorization Tenant identity mismatch")
		}
	}
	return true, nil
}
