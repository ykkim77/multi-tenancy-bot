package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ChatSpaceSpec defines the desired state of ChatSpace
type ChatSpaceSpec struct {
	// TenantID is the unique identifier for the tenant
	TenantID string `json:"tenantId"`

	// OutlineConfig contains Outline Wiki configuration
	OutlineConfig *OutlineConfig `json:"outlineConfig,omitempty"`
}

// OutlineConfig defines Outline Wiki configuration
type OutlineConfig struct {
	// Enabled indicates whether Outline is enabled
	Enabled bool `json:"enabled"`

	// StorageSize for Outline data
	StorageSize string `json:"storageSize,omitempty"`
}

// ChatSpaceStatus defines the observed state of ChatSpace
type ChatSpaceStatus struct {
	// Phase represents the current phase of the ChatSpace
	Phase string `json:"phase,omitempty"`

	// Message provides additional status information
	Message string `json:"message,omitempty"`
}

//+kubebuilder:object:root=true
//+kubebuilder:subresource:status
//+kubebuilder:resource:scope=Cluster

// ChatSpace is the Schema for the chatspaces API
type ChatSpace struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ChatSpaceSpec   `json:"spec,omitempty"`
	Status ChatSpaceStatus `json:"status,omitempty"`
}

//+kubebuilder:object:root=true

// ChatSpaceList contains a list of ChatSpace
type ChatSpaceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ChatSpace `json:"items"`
}

func init() {
	SchemeBuilder.Register(&ChatSpace{}, &ChatSpaceList{})
}
