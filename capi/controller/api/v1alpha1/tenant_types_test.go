package v1alpha1

import (
	"encoding/json"
	"testing"
)

func TestTenantStatusRoundTrip(t *testing.T) {
	tenant := Tenant{
		Spec: TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           2,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
		Status: TenantStatus{
			Phase:    PhaseProgressing,
			SpecHash: "hash",
			FunctionalEvidence: &FunctionalEvidence{
				VerifiedAt: 1,
				ExpiresAt:  2,
				Categories: map[string]bool{"database": true},
			},
		},
	}
	data, err := json.Marshal(&tenant)
	if err != nil {
		t.Fatal(err)
	}
	var decoded Tenant
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Status.Phase != PhaseProgressing || !decoded.Status.FunctionalEvidence.Categories["database"] {
		t.Fatalf("unexpected round trip: %#v", decoded.Status)
	}
}
