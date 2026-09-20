package webhook

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	admissionv1 "k8s.io/api/admission/v1"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/sanitize"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

var allowedSpecFields = map[string]struct{}{
	"kubernetesVersion": {},
	"workers":           {},
	"databaseCount":     {},
	"podCIDR":           {},
	"serviceCIDR":       {},
}

type TenantValidator struct {
	SupportedVersion string
}

type rawTenant struct {
	Metadata struct {
		Name string `json:"name"`
	} `json:"metadata"`
	Spec json.RawMessage `json:"spec"`
}

// +kubebuilder:webhook:path=/validate-tenancy-cnpg-vcluster-io-v1alpha1-tenant,mutating=false,failurePolicy=fail,sideEffects=None,groups=tenancy.cnpg-vcluster.io,resources=tenants,verbs=create;update,versions=v1alpha1,name=vtenant.tenancy.cnpg-vcluster.io,admissionReviewVersions=v1
func (validator *TenantValidator) Handle(_ context.Context, request admission.Request) admission.Response {
	if request.Operation != admissionv1.Create && request.Operation != admissionv1.Update {
		return admission.Allowed("operation does not mutate Tenant spec")
	}
	current, canonicalHash, err := validator.decodeAndValidate(request.Object.Raw)
	if err != nil {
		return admission.Denied(sanitize.Text(err.Error()))
	}
	if request.Operation == admissionv1.Update {
		_, oldHash, oldErr := validator.decodeAndValidate(request.OldObject.Raw)
		if oldErr != nil {
			return admission.Denied("stored Tenant specification is invalid")
		}
		if canonicalHash != oldHash {
			return admission.Denied("Tenant specification is immutable; delete and recreate the Tenant")
		}
	}
	return admission.Allowed(fmt.Sprintf("accepted Tenant %s", current.Metadata.Name))
}

func (validator *TenantValidator) decodeAndValidate(data []byte) (rawTenant, string, error) {
	var document rawTenant
	if err := json.Unmarshal(data, &document); err != nil {
		return rawTenant{}, "", fmt.Errorf("invalid Tenant JSON: %w", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(document.Spec, &fields); err != nil {
		return rawTenant{}, "", fmt.Errorf("spec must be an object")
	}
	for field := range fields {
		if _, ok := allowedSpecFields[field]; !ok {
			return rawTenant{}, "", fmt.Errorf("unknown Tenant spec field %q", field)
		}
	}
	if len(fields) != len(allowedSpecFields) {
		return rawTenant{}, "", fmt.Errorf("spec must contain exactly kubernetesVersion, workers, databaseCount, podCIDR, and serviceCIDR")
	}
	var spec tenancyv1alpha1.TenantSpec
	if err := json.Unmarshal(document.Spec, &spec); err != nil {
		return rawTenant{}, "", fmt.Errorf("invalid Tenant spec: %w", err)
	}
	supported := validator.SupportedVersion
	if supported == "" {
		supported = os.Getenv("SUPPORTED_KUBERNETES_VERSION")
	}
	if supported == "" {
		supported = validation.DefaultSupportedKubernetesVersion
	}
	_, digest, err := validation.Validate(document.Metadata.Name, spec, supported)
	if err != nil {
		return rawTenant{}, "", err
	}
	return document, digest, nil
}
