package validation

import (
	"testing"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func validSpec() tenancyv1alpha1.TenantSpec {
	return tenancyv1alpha1.TenantSpec{
		KubernetesVersion: "v1.36.4",
		Workers:           1,
		DatabaseCount:     1,
		PodCIDR:           "10.20.0.0/16",
		ServiceCIDR:       "10.21.0.0/16",
	}
}

func TestValidateCanonicalizesEquivalentVersion(t *testing.T) {
	spec := validSpec()
	first, firstHash, err := Validate("tenant-a", spec, "v1.36.4")
	if err != nil {
		t.Fatal(err)
	}
	spec.KubernetesVersion = "1.36.4"
	second, secondHash, err := Validate("tenant-a", spec, "1.36.4")
	if err != nil {
		t.Fatal(err)
	}
	if first != second || firstHash != secondHash {
		t.Fatalf("canonical forms differ: %#v/%s %#v/%s", first, firstHash, second, secondHash)
	}
	if firstHash != "e9afd0733e391c39ea140af4cfbca48afe00d997b350474e41ff118c38e50a15" {
		t.Fatalf("unexpected cross-language canonical hash: %s", firstHash)
	}
}

func TestValidateRejectsInvalidInputs(t *testing.T) {
	tests := []struct {
		name string
		edit func(*tenancyv1alpha1.TenantSpec)
	}{
		{"workers", func(spec *tenancyv1alpha1.TenantSpec) { spec.Workers = 4 }},
		{"databases", func(spec *tenancyv1alpha1.TenantSpec) { spec.DatabaseCount = 0 }},
		{"version", func(spec *tenancyv1alpha1.TenantSpec) { spec.KubernetesVersion = "1.35.0" }},
		{"pod host bits", func(spec *tenancyv1alpha1.TenantSpec) { spec.PodCIDR = "10.20.0.1/16" }},
		{"ipv6", func(spec *tenancyv1alpha1.TenantSpec) { spec.PodCIDR = "fd00::/64" }},
		{"overlap", func(spec *tenancyv1alpha1.TenantSpec) { spec.ServiceCIDR = spec.PodCIDR }},
		{"small service", func(spec *tenancyv1alpha1.TenantSpec) { spec.ServiceCIDR = "10.21.0.0/29" }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			spec := validSpec()
			test.edit(&spec)
			if _, _, err := Validate("tenant-a", spec, "1.36.4"); err == nil {
				t.Fatal("expected validation failure")
			}
		})
	}
	if _, _, err := Validate("Tenant_A", validSpec(), "1.36.4"); err == nil {
		t.Fatal("expected invalid name failure")
	}
}
