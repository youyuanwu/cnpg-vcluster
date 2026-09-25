package controller

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

var errEndpointAllocationMissing = errors.New("Tenant endpoint allocation is missing")

type endpointAllocation struct {
	TenantName     string `json:"tenantName"`
	TenantUID      string `json:"tenantUID"`
	SpecHash       string `json:"specHash"`
	FoundationHash string `json:"foundationHash"`
}

type endpointAllocationState struct {
	Schema         int                           `json:"schema"`
	FoundationHash string                        `json:"foundationHash"`
	NetworkID      string                        `json:"networkId"`
	PoolStart      string                        `json:"poolStart"`
	PoolEnd        string                        `json:"poolEnd"`
	Allocations    map[string]endpointAllocation `json:"allocations"`
}

func allocateEndpoint(ctx context.Context, kubernetes client.Client, reader client.Reader, namespace string, foundation Foundation, tenant *tenancyv1alpha1.Tenant, specHash string) (string, error) {
	var allocated string
	err := retry.OnError(retry.DefaultRetry, func(err error) bool {
		return apierrors.IsConflict(err) || apierrors.IsAlreadyExists(err)
	}, func() error {
		var configMap corev1.ConfigMap
		key := types.NamespacedName{Namespace: namespace, Name: allocationConfigMapName}
		err := reader.Get(ctx, key, &configMap)
		if apierrors.IsNotFound(err) {
			state := newAllocationState(foundation)
			address, err := claimLowestFree(state, foundation, tenant, specHash)
			if err != nil {
				return err
			}

			encoded, err := encodeAllocationState(state)
			if err != nil {
				return err
			}
			configMap = corev1.ConfigMap{
				ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace},
				Data:       map[string]string{"allocations.json": encoded},
			}
			if err := kubernetes.Create(ctx, &configMap); err != nil {
				return err
			}
			allocated = address
			return nil
		}
		if err != nil {
			return err
		}
		state, err := decodeAllocationState(configMap.Data["allocations.json"], foundation)
		if err != nil {
			return err
		}
		address, err := claimLowestFree(&state, foundation, tenant, specHash)
		if err != nil {
			return err
		}
		encoded, err := encodeAllocationState(&state)
		if err != nil {
			return err
		}
		if configMap.Data["allocations.json"] == encoded {
			allocated = address
			return nil
		}
		configMap.Data["allocations.json"] = encoded
		if err := kubernetes.Update(ctx, &configMap); err != nil {
			return err
		}
		allocated = address
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("allocate Tenant endpoint: %w", err)
	}
	return foundation.Endpoint(allocated), nil
}

func validateEndpoint(ctx context.Context, reader client.Reader, namespace string, foundation Foundation, tenant *tenancyv1alpha1.Tenant, specHash string) error {
	_, _, err := observeEndpoint(ctx, reader, namespace, foundation, tenant, specHash)
	return err
}

func observeEndpoint(ctx context.Context, reader client.Reader, namespace string, foundation Foundation, tenant *tenancyv1alpha1.Tenant, specHash string) (string, bool, error) {
	var configMap corev1.ConfigMap
	if err := reader.Get(ctx, types.NamespacedName{Namespace: namespace, Name: allocationConfigMapName}, &configMap); err != nil {
		if apierrors.IsNotFound(err) && tenant.Status.Endpoint == "" {
			return "", false, nil
		}
		if apierrors.IsNotFound(err) {
			return "", false, fmt.Errorf("%w: allocation ConfigMap is absent", errEndpointAllocationMissing)
		}
		return "", false, fmt.Errorf("read Tenant endpoint allocation: %w", err)
	}
	state, err := decodeAllocationState(configMap.Data["allocations.json"], foundation)
	if err != nil {
		return "", false, err
	}
	for address, allocation := range state.Allocations {
		if allocation.TenantUID != string(tenant.UID) {
			continue
		}
		if allocation.TenantName != tenant.Name || allocation.SpecHash != specHash ||
			allocation.FoundationHash != foundation.Hash ||
			(tenant.Status.Endpoint != "" && foundation.Endpoint(address) != tenant.Status.Endpoint) {
			return "", false, fmt.Errorf("Tenant endpoint allocation identity mismatch")
		}
		return foundation.Endpoint(address), true, nil
	}
	if tenant.Status.Endpoint != "" {
		return "", false, errEndpointAllocationMissing
	}
	return "", false, nil
}

func releaseEndpoint(ctx context.Context, kubernetes client.Client, reader client.Reader, namespace string, foundation Foundation, tenant *tenancyv1alpha1.Tenant) error {
	if err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var configMap corev1.ConfigMap
		key := types.NamespacedName{Namespace: namespace, Name: allocationConfigMapName}
		if err := reader.Get(ctx, key, &configMap); err != nil {
			if apierrors.IsNotFound(err) {
				return nil
			}
			return err
		}
		state, err := decodeAllocationState(configMap.Data["allocations.json"], foundation)
		if err != nil {
			return err
		}
		changed := false
		for address, allocation := range state.Allocations {
			if allocation.TenantUID != string(tenant.UID) {
				continue
			}
			if allocation.TenantName != tenant.Name {
				return fmt.Errorf("endpoint allocation Tenant identity mismatch")
			}
			delete(state.Allocations, address)
			changed = true
		}
		if !changed {
			return nil
		}
		encoded, err := encodeAllocationState(&state)
		if err != nil {
			return err
		}
		configMap.Data["allocations.json"] = encoded
		return kubernetes.Update(ctx, &configMap)
	}); err != nil {
		return err
	}
	var configMap corev1.ConfigMap
	if err := reader.Get(ctx, types.NamespacedName{Namespace: namespace, Name: allocationConfigMapName}, &configMap); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("verify Tenant endpoint release: %w", err)
	}
	state, err := decodeAllocationState(configMap.Data["allocations.json"], foundation)
	if err != nil {
		return err
	}
	for _, allocation := range state.Allocations {
		if allocation.TenantUID == string(tenant.UID) {
			return fmt.Errorf("Tenant endpoint allocation remains after release")
		}
	}
	return nil
}

func newAllocationState(foundation Foundation) *endpointAllocationState {
	return &endpointAllocationState{
		Schema:         1,
		FoundationHash: foundation.Hash,
		NetworkID:      foundation.NetworkID,
		PoolStart:      foundation.PoolStart,
		PoolEnd:        foundation.PoolEnd,
		Allocations:    map[string]endpointAllocation{},
	}
}

func decodeAllocationState(encoded string, foundation Foundation) (endpointAllocationState, error) {
	var state endpointAllocationState
	if err := json.Unmarshal([]byte(encoded), &state); err != nil {
		return endpointAllocationState{}, fmt.Errorf("decode endpoint allocations: %w", err)
	}
	if state.Schema != 1 || state.Allocations == nil {
		return endpointAllocationState{}, fmt.Errorf("endpoint allocation data is invalid")
	}
	if state.FoundationHash != foundation.Hash || state.NetworkID != foundation.NetworkID ||
		state.PoolStart != foundation.PoolStart || state.PoolEnd != foundation.PoolEnd {
		if len(state.Allocations) != 0 {
			return endpointAllocationState{}, fmt.Errorf("endpoint allocation foundation identity mismatch")
		}
		state.FoundationHash = foundation.Hash
		state.NetworkID = foundation.NetworkID
		state.PoolStart = foundation.PoolStart
		state.PoolEnd = foundation.PoolEnd
	}
	seenUIDs := map[string]string{}
	reserved := make([]netip.Prefix, 0, len(foundation.ReservedCIDRs))
	for _, value := range foundation.ReservedCIDRs {
		reserved = append(reserved, netip.MustParsePrefix(value))
	}
	for address, allocation := range state.Allocations {
		parsed, err := netip.ParseAddr(address)
		if err != nil || !parsed.Is4() || parsed.Less(netip.MustParseAddr(state.PoolStart)) ||
			netip.MustParseAddr(state.PoolEnd).Less(parsed) || allocation.TenantName == "" ||
			allocation.TenantUID == "" || allocation.SpecHash == "" || allocation.FoundationHash == "" {
			return endpointAllocationState{}, fmt.Errorf("endpoint allocation data is invalid")
		}
		for _, prefix := range reserved {
			if prefix.Contains(parsed) {
				return endpointAllocationState{}, fmt.Errorf("endpoint allocation uses a reserved address")
			}
		}
		if previous, duplicate := seenUIDs[allocation.TenantUID]; duplicate && previous != address {
			return endpointAllocationState{}, fmt.Errorf("Tenant UID has duplicate endpoint allocations")
		}
		seenUIDs[allocation.TenantUID] = address
	}
	return state, nil
}

func claimLowestFree(state *endpointAllocationState, foundation Foundation, tenant *tenancyv1alpha1.Tenant, specHash string) (string, error) {
	for address, allocation := range state.Allocations {
		if allocation.TenantUID == string(tenant.UID) {
			if allocation.TenantName != tenant.Name || allocation.SpecHash != specHash ||
				allocation.FoundationHash != foundation.Hash {
				return "", fmt.Errorf("existing endpoint allocation identity mismatch")
			}
			return address, nil
		}
		if allocation.TenantName == tenant.Name {
			return "", fmt.Errorf("Tenant name is held by a different endpoint allocation")
		}
	}
	reserved := make([]netip.Prefix, 0, len(foundation.ReservedCIDRs))
	for _, value := range foundation.ReservedCIDRs {
		reserved = append(reserved, netip.MustParsePrefix(value))
	}
	for address := netip.MustParseAddr(state.PoolStart); ; address = address.Next() {
		_, used := state.Allocations[address.String()]
		blocked := false
		for _, prefix := range reserved {
			blocked = blocked || prefix.Contains(address)
		}
		if !used && !blocked {
			state.Allocations[address.String()] = endpointAllocation{
				TenantName:     tenant.Name,
				TenantUID:      string(tenant.UID),
				SpecHash:       specHash,
				FoundationHash: foundation.Hash,
			}
			return address.String(), nil
		}
		if address == netip.MustParseAddr(state.PoolEnd) {
			break
		}
	}
	return "", fmt.Errorf("Tenant endpoint pool is exhausted")
}

func encodeAllocationState(state *endpointAllocationState) (string, error) {
	encoded, err := json.Marshal(state)
	if err != nil {
		return "", fmt.Errorf("encode endpoint allocations: %w", err)
	}
	return string(encoded), nil
}
