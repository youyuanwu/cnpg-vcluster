package controller

import (
	"math"
	"strings"
	"testing"
	"time"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

func readyStatus(now time.Time) tenancyv1alpha1.TenantStatus {
	status := tenancyv1alpha1.TenantStatus{
		SpecHash: "spec", FoundationHash: "foundation", WorkerSnapshotHash: strings.Repeat("a", 64),
		ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{{
			APIVersion: "v1", Kind: "Namespace", Name: "tenant-a", UID: "namespace-uid",
		}},
		TenantResources: []tenancyv1alpha1.ObservedResourceIdentity{{
			APIVersion: "v1", Kind: "Node", Name: "worker-a", UID: "node-uid",
		}},
	}
	verified := float64(now.Add(-time.Hour).Unix())
	status.ObservationsHash = observationsHash(status)
	status.FunctionalEvidence = &tenancyv1alpha1.FunctionalEvidence{
		VerifiedAt: verified, ExpiresAt: verified + functionalEvidenceLifetime.Seconds(),
		SpecHash: status.SpecHash, FoundationHash: status.FoundationHash, ObservationsHash: status.ObservationsHash,
		Categories: map[string]bool{
			"clusterAccess": true, "workers": true, "network": true, "storage": true, "database": true,
		},
	}
	return status
}

func TestFunctionalEvidenceFreshnessAndIdentity(t *testing.T) {
	now := time.Unix(2_000_000_000, 0)
	status := readyStatus(now)
	if err := validateFunctionalEvidence(status, now); err != nil {
		t.Fatal(err)
	}
	status.FunctionalEvidence.VerifiedAt = float64(now.Unix()) + 1
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("future evidence was accepted")
	}
	status = readyStatus(now)
	status.FunctionalEvidence.VerifiedAt = math.NaN()
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("non-finite evidence was accepted")
	}
	status = readyStatus(now)
	status.FunctionalEvidence.ExpiresAt = float64(now.Unix())
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("expired evidence was accepted")
	}
	status = readyStatus(now)
	status.TenantResources[0].UID = "replacement"
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("mismatched observations were accepted")
	}
}

func TestFunctionalEvidenceRequiresExactCategories(t *testing.T) {
	now := time.Unix(2_000_000_000, 0)
	status := readyStatus(now)
	delete(status.FunctionalEvidence.Categories, "database")
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("incomplete functional categories were accepted")
	}
	status = readyStatus(now)
	status.FunctionalEvidence.Categories["database"] = false
	if err := validateFunctionalEvidence(status, now); err == nil {
		t.Fatal("failed functional category was accepted")
	}
}
