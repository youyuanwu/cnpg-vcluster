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
			Phase: PhaseProgressing,
			WorkerContainers: []WorkerContainerEvidence{{
				Name: "worker-a",
				ID:   "container-a",
			}},
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
	if decoded.Status.Phase != PhaseProgressing ||
		len(decoded.Status.WorkerContainers) != 1 ||
		decoded.Status.WorkerContainers[0].ID != "container-a" {
		t.Fatalf("unexpected round trip: %#v", decoded.Status)
	}
}
