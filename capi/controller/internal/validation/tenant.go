package validation

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/netip"
	"regexp"
	"strings"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

const DefaultSupportedKubernetesVersion = "1.36.4"

var tenantName = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$`)

type CanonicalSpec struct {
	KubernetesVersion string `json:"kubernetesVersion"`
	Workers           int32  `json:"workers"`
	DatabaseCount     int32  `json:"databaseCount"`
	PodCIDR           string `json:"podCIDR"`
	ServiceCIDR       string `json:"serviceCIDR"`
}

func Validate(name string, spec tenancyv1alpha1.TenantSpec, supportedVersion string) (CanonicalSpec, string, error) {
	if !tenantName.MatchString(name) {
		return CanonicalSpec{}, "", fmt.Errorf("tenant name must be a 1-30 character lowercase DNS label")
	}
	version := strings.TrimPrefix(spec.KubernetesVersion, "v")
	supported := strings.TrimPrefix(supportedVersion, "v")
	if version != supported {
		return CanonicalSpec{}, "", fmt.Errorf("kubernetesVersion %q is unsupported; expected %q", spec.KubernetesVersion, supportedVersion)
	}
	if spec.Workers < 1 || spec.Workers > 3 {
		return CanonicalSpec{}, "", fmt.Errorf("workers must be an integer from 1 through 3")
	}
	if spec.DatabaseCount < 1 || spec.DatabaseCount > 3 {
		return CanonicalSpec{}, "", fmt.Errorf("databaseCount must be an integer from 1 through 3")
	}
	pod, err := strictIPv4Prefix("podCIDR", spec.PodCIDR)
	if err != nil {
		return CanonicalSpec{}, "", err
	}
	service, err := strictIPv4Prefix("serviceCIDR", spec.ServiceCIDR)
	if err != nil {
		return CanonicalSpec{}, "", err
	}
	if pod.Overlaps(service) {
		return CanonicalSpec{}, "", fmt.Errorf("podCIDR and serviceCIDR overlap")
	}
	if service.Bits() > 28 {
		return CanonicalSpec{}, "", fmt.Errorf("serviceCIDR must contain more than 11 addresses")
	}
	canonical := CanonicalSpec{
		KubernetesVersion: version,
		Workers:           spec.Workers,
		DatabaseCount:     spec.DatabaseCount,
		PodCIDR:           pod.String(),
		ServiceCIDR:       service.String(),
	}
	data, err := json.Marshal(canonical)
	if err != nil {
		return CanonicalSpec{}, "", fmt.Errorf("encode canonical tenant spec: %w", err)
	}
	digest := sha256.Sum256(data)
	return canonical, hex.EncodeToString(digest[:]), nil
}

func strictIPv4Prefix(field, value string) (netip.Prefix, error) {
	prefix, err := netip.ParsePrefix(value)
	if err != nil || !prefix.Addr().Is4() || prefix != prefix.Masked() {
		return netip.Prefix{}, fmt.Errorf("%s must be a canonical IPv4 CIDR", field)
	}
	return prefix, nil
}
