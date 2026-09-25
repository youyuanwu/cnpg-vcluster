package v1alpha1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

const (
	PhasePending          TenantPhase = "Pending"
	PhaseProgressing      TenantPhase = "Progressing"
	PhaseReady            TenantPhase = "Ready"
	PhaseDeleting         TenantPhase = "Deleting"
	PhaseDegraded         TenantPhase = "Degraded"
	PhaseFailed           TenantPhase = "Failed"
	PhaseOwnershipInvalid TenantPhase = "OwnershipInvalid"
)

// +kubebuilder:validation:Enum=Pending;Progressing;Ready;Deleting;Degraded;Failed;OwnershipInvalid
type TenantPhase string

type TenantSpec struct {
	// +kubebuilder:validation:Pattern=`^v?[0-9]+\.[0-9]+\.[0-9]+$`
	KubernetesVersion string `json:"kubernetesVersion"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=3
	Workers int32 `json:"workers"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=3
	DatabaseCount int32 `json:"databaseCount"`

	PodCIDR string `json:"podCIDR"`

	ServiceCIDR string `json:"serviceCIDR"`
}

type TenantStatus struct {
	ObservedGeneration          int64              `json:"observedGeneration,omitempty"`
	Phase                       TenantPhase        `json:"phase,omitempty"`
	Conditions                  []metav1.Condition `json:"conditions,omitempty"`
	Endpoint                    string             `json:"endpoint,omitempty"`
	FoundationHash              string             `json:"foundationHash,omitempty"`
	ClusterUID                  string             `json:"clusterUID,omitempty"`
	TenantAPICreationAuthorized bool               `json:"tenantAPICreationAuthorized,omitempty"`
	TenantCleanupClusterUID     string             `json:"tenantCleanupClusterUID,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=tn
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
// +kubebuilder:printcolumn:name="Endpoint",type=string,JSONPath=`.status.endpoint`
// +kubebuilder:printcolumn:name="Workers",type=integer,JSONPath=`.spec.workers`
// +kubebuilder:printcolumn:name="Databases",type=integer,JSONPath=`.spec.databaseCount`
type Tenant struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// Preserve unknown fields until the validating webhook can reject them.
	// +kubebuilder:pruning:PreserveUnknownFields
	Spec TenantSpec `json:"spec"`

	Status TenantStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type TenantList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Tenant `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Tenant{}, &TenantList{})
}
