package webhook

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	admissionv1 "k8s.io/api/admission/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	tenantcontroller "github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/controller"
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
	Reader           client.Reader
	Namespace        string
	Now              func() time.Time
}

type rawTenant struct {
	Metadata struct {
		Name              string `json:"name"`
		UID               string `json:"uid"`
		DeletionTimestamp string `json:"deletionTimestamp"`
	} `json:"metadata"`
	Spec json.RawMessage `json:"spec"`
}

// +kubebuilder:webhook:path=/validate-tenancy-cnpg-vcluster-io-v1alpha1-tenant,mutating=false,failurePolicy=fail,sideEffects=None,groups=tenancy.cnpg-vcluster.io,resources=tenants,verbs=create;update;delete,versions=v1alpha1,name=vtenant.tenancy.cnpg-vcluster.io,admissionReviewVersions=v1
func (validator *TenantValidator) Handle(ctx context.Context, request admission.Request) admission.Response {
	if request.Operation == admissionv1.Delete {
		return validator.validateDelete(ctx, request)
	}
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

func (validator *TenantValidator) validateDelete(ctx context.Context, request admission.Request) admission.Response {
	if validator.Reader == nil {
		return admission.Denied("targeted deletion reservation reader is unavailable")
	}
	current, _, err := validator.decodeAndValidate(request.OldObject.Raw)
	if err != nil {
		return admission.Denied("stored Tenant is invalid")
	}
	if current.Metadata.DeletionTimestamp != "" {
		return admission.Allowed(fmt.Sprintf("Tenant %s deletion is already persisted", current.Metadata.Name))
	}
	var tenants tenancyv1alpha1.TenantList
	if err := validator.Reader.List(ctx, &tenants); err != nil {
		return admission.Denied("active Tenant deletion inspection failed")
	}
	for index := range tenants.Items {
		tenant := &tenants.Items[index]
		if tenant.Name != current.Metadata.Name && !tenant.DeletionTimestamp.IsZero() {
			return admission.Denied("another Tenant deletion is already active")
		}
	}
	namespace := validator.Namespace
	if namespace == "" {
		namespace = "tenant-system"
	}
	now := time.Now().UTC()
	if validator.Now != nil {
		now = validator.Now().UTC()
	}
	reservation, _, err := tenantcontroller.ReadDeletionReservation(ctx, validator.Reader, namespace, now)
	if err != nil {
		return admission.Denied(sanitize.Text(err.Error()))
	}
	requester := request.UserInfo.Username
	if reservation.TenantName != current.Metadata.Name ||
		reservation.TenantUID != current.Metadata.UID ||
		reservation.Requester != requester {
		return admission.Denied("targeted deletion reservation does not match Tenant UID and requester")
	}
	return admission.Allowed(fmt.Sprintf("accepted reserved deletion for Tenant %s", current.Metadata.Name))
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
