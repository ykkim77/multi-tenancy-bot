package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ── Spec types ───────────────────────────────────────────────────────

// TenantTier categorizes a tenant for the Agentic Operator's
// policy-driven resource allocation.
//
// +kubebuilder:validation:Enum=priority;standard;internal
type TenantTier string

const (
	TierPriority TenantTier = "priority"
	TierStandard TenantTier = "standard"
	TierInternal TenantTier = "internal"
)

// ResourceQuotaConfig describes the desired resource envelope for a tenant.
// Values use Kubernetes resource notation (e.g. "500m", "2Gi").
type ResourceQuotaConfig struct {
	CPURequests    string `json:"cpuRequests,omitempty"`
	MemoryRequests string `json:"memoryRequests,omitempty"`
	CPULimits      string `json:"cpuLimits,omitempty"`
	MemoryLimits   string `json:"memoryLimits,omitempty"`
	MaxPods        int32  `json:"maxPods,omitempty"`
}

// AgenticPolicy controls the autonomous decision-making behavior of the Operator.
// When Enabled, the Operator periodically re-evaluates the tenant's actual usage
// (via Status.Usage) and re-balances resources between active and idle tenants.
type AgenticPolicy struct {
	// Enabled toggles the autonomous re-balancing loop for this tenant.
	// +kubebuilder:default=true
	Enabled bool `json:"enabled"`

	// HardIsolation enables PriorityClass + cross-tenant NetworkPolicy
	// to fully isolate this tenant from noisy neighbors.
	// +kubebuilder:default=true
	HardIsolation bool `json:"hardIsolation"`

	// ReclaimIdleAfterSeconds — if a tenant is observed idle for this long,
	// the Operator may reclaim a portion of its quota for active tenants.
	// +kubebuilder:default=120
	ReclaimIdleAfterSeconds int32 `json:"reclaimIdleAfterSeconds,omitempty"`

	// ActiveBoostFactor — multiplier applied to active tenants' quotas when
	// idle quotas are reclaimed (e.g. 1.5 = 50% bonus).
	// +kubebuilder:default="1.5"
	ActiveBoostFactor string `json:"activeBoostFactor,omitempty"`
}

// OutlineConfig defines optional Outline Wiki provisioning hints.
type OutlineConfig struct {
	Enabled     bool   `json:"enabled"`
	StorageSize string `json:"storageSize,omitempty"`
}

// QdrantConfig is reserved for future per-tenant vector DB provisioning.
type QdrantConfig struct {
	Enabled        bool   `json:"enabled,omitempty"`
	CollectionName string `json:"collectionName,omitempty"`
}

// EmbeddingConfig is reserved for embedding-worker provisioning hints.
type EmbeddingConfig struct {
	Enabled bool   `json:"enabled,omitempty"`
	Model   string `json:"model,omitempty"`
}

// RAGConfig is reserved for RAG-API provisioning hints.
type RAGConfig struct {
	Enabled bool   `json:"enabled,omitempty"`
	Model   string `json:"model,omitempty"`
}

// ChatSpaceSpec defines the desired state of ChatSpace.
//
// A ChatSpace is the Custom Resource consumed by the Agentic Operator;
// it represents the *intent* to provision a tenant. The Operator translates
// this intent into Namespace, ResourceQuota, LimitRange, NetworkPolicy, and
// (optionally) PriorityClass references.
type ChatSpaceSpec struct {
	// TenantID is the unique identifier for the tenant.
	// +kubebuilder:validation:Required
	TenantID string `json:"tenantId"`

	// Tier drives default quota and isolation policy.
	// +kubebuilder:default=standard
	Tier TenantTier `json:"tier,omitempty"`

	// Owner email or team — purely informational.
	Owner string `json:"owner,omitempty"`

	// ResourceQuota overrides the tier defaults if set.
	ResourceQuota ResourceQuotaConfig `json:"resourceQuota,omitempty"`

	// Agentic configures the autonomous behavior of the Operator
	// for this tenant. If omitted, sensible defaults are used.
	Agentic AgenticPolicy `json:"agentic,omitempty"`

	// Optional component-specific configuration (currently informational).
	Outline   OutlineConfig   `json:"outline,omitempty"`
	Qdrant    QdrantConfig    `json:"qdrant,omitempty"`
	Embedding EmbeddingConfig `json:"embedding,omitempty"`
	RAG       RAGConfig       `json:"rag,omitempty"`
}

// ── Status types ─────────────────────────────────────────────────────

// ComponentStatus reports the readiness of a single subsystem
// (namespace, quota, networkpolicy, ...).
type ComponentStatus struct {
	Ready   bool   `json:"ready"`
	Message string `json:"message,omitempty"`
}

// ResourceStatus aggregates per-component readiness.
type ResourceStatus struct {
	Outline   ComponentStatus `json:"outline,omitempty"`
	Qdrant    ComponentStatus `json:"qdrant,omitempty"`
	Embedding ComponentStatus `json:"embedding,omitempty"`
	RAG       ComponentStatus `json:"rag,omitempty"`
}

// ChatSpacePhase represents the high-level provisioning phase.
//
// +kubebuilder:validation:Enum=Pending;Provisioning;Ready;Rebalancing;Failed
type ChatSpacePhase string

const (
	PhasePending      ChatSpacePhase = "Pending"
	PhaseProvisioning ChatSpacePhase = "Provisioning"
	PhaseReady        ChatSpacePhase = "Ready"
	PhaseRebalancing  ChatSpacePhase = "Rebalancing"
	PhaseFailed       ChatSpacePhase = "Failed"
)

// ChatSpaceStatus defines the observed state of ChatSpace.
type ChatSpaceStatus struct {
	// Phase is a coarse-grained provisioning phase.
	Phase ChatSpacePhase `json:"phase,omitempty"`

	// Message provides a human-readable status summary.
	Message string `json:"message,omitempty"`

	// Namespace is the managed tenant namespace name.
	Namespace string `json:"namespace,omitempty"`

	// AppliedQuota is the most recent quota applied by the Operator
	// (after agentic adjustments, if any).
	AppliedQuota ResourceQuotaConfig `json:"appliedQuota,omitempty"`

	// AgenticActions records autonomous decisions made by the Operator,
	// e.g. "reclaimed 200m CPU from idle tenant", in chronological order.
	// Capped at a small ring buffer to keep the resource size bounded.
	AgenticActions []string `json:"agenticActions,omitempty"`

	// Conditions follows the standard Kubernetes condition pattern.
	Conditions []metav1.Condition `json:"conditions,omitempty"`

	// ResourceStatus aggregates per-component readiness.
	ResourceStatus ResourceStatus `json:"resourceStatus,omitempty"`

	// LastUpdated marks the last reconciliation time.
	LastUpdated metav1.Time `json:"lastUpdated,omitempty"`

	// ObservedGeneration is the .metadata.generation that the Operator last acted on.
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// ── Root types ───────────────────────────────────────────────────────

//+kubebuilder:object:root=true
//+kubebuilder:subresource:status
//+kubebuilder:resource:scope=Cluster,shortName=cs
//+kubebuilder:printcolumn:name="Tenant",type="string",JSONPath=".spec.tenantId"
//+kubebuilder:printcolumn:name="Tier",type="string",JSONPath=".spec.tier"
//+kubebuilder:printcolumn:name="Phase",type="string",JSONPath=".status.phase"
//+kubebuilder:printcolumn:name="Namespace",type="string",JSONPath=".status.namespace"
//+kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"

// ChatSpace is the Schema for the chatspaces API and represents
// a tenant managed by the Agentic Operator.
type ChatSpace struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ChatSpaceSpec   `json:"spec,omitempty"`
	Status ChatSpaceStatus `json:"status,omitempty"`
}

//+kubebuilder:object:root=true

// ChatSpaceList contains a list of ChatSpace.
type ChatSpaceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ChatSpace `json:"items"`
}

func init() {
	SchemeBuilder.Register(&ChatSpace{}, &ChatSpaceList{})
}
