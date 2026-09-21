package controller

import (
	"context"
	"fmt"
	"net/netip"

	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func validatePeerNetworks(ctx context.Context, reader client.Reader, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, foundation Foundation, supportedVersion string) error {
	pod := netip.MustParsePrefix(canonical.PodCIDR)
	service := netip.MustParsePrefix(canonical.ServiceCIDR)
	managementSubnet := netip.MustParsePrefix(foundation.Subnet)
	if pod.Overlaps(managementSubnet) || service.Overlaps(managementSubnet) {
		return fmt.Errorf("Tenant networks overlap the management Docker subnet %s", foundation.Subnet)
	}
	for _, value := range foundation.ReservedCIDRs {
		reserved := netip.MustParsePrefix(value)
		if pod.Overlaps(reserved) || service.Overlaps(reserved) {
			return fmt.Errorf("Tenant networks overlap reserved foundation CIDR %s", value)
		}
	}
	var tenants tenancyv1alpha1.TenantList
	if err := reader.List(ctx, &tenants); err != nil {
		return fmt.Errorf("list peer Tenants: %w", err)
	}
	for index := range tenants.Items {
		peer := &tenants.Items[index]
		if peer.UID == tenant.UID {
			continue
		}
		peerSpec, _, err := validation.Validate(peer.Name, peer.Spec, supportedVersion)
		if err != nil {
			return fmt.Errorf("peer Tenant %s has an invalid reserved specification: %w", peer.Name, err)
		}
		peerPod := netip.MustParsePrefix(peerSpec.PodCIDR)
		peerService := netip.MustParsePrefix(peerSpec.ServiceCIDR)
		if pod.Overlaps(peerPod) || pod.Overlaps(peerService) ||
			service.Overlaps(peerPod) || service.Overlaps(peerService) {
			return fmt.Errorf("Tenant networks overlap peer Tenant %s", peer.Name)
		}
	}
	return nil
}
