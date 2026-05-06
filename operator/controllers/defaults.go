package controllers

import (
	"strconv"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	portalv1 "github.com/kcu/knowledge-portal-operator/api/v1"
)

// tierDefault returns the canonical resource envelope for a given tier.
// These values are intentionally generous in the relative sense (priority > standard > internal)
// so that the Agentic Operator's re-balancing produces visible signals in benchmarks.
func tierDefault(tier portalv1.TenantTier) portalv1.ResourceQuotaConfig {
	switch tier {
	case portalv1.TierPriority:
		return portalv1.ResourceQuotaConfig{
			CPURequests: "2", MemoryRequests: "2Gi",
			CPULimits: "4", MemoryLimits: "4Gi",
			MaxPods: 30,
		}
	case portalv1.TierInternal:
		return portalv1.ResourceQuotaConfig{
			CPURequests: "200m", MemoryRequests: "256Mi",
			CPULimits: "500m", MemoryLimits: "512Mi",
			MaxPods: 5,
		}
	default: // standard
		return portalv1.ResourceQuotaConfig{
			CPURequests: "500m", MemoryRequests: "512Mi",
			CPULimits: "1", MemoryLimits: "1Gi",
			MaxPods: 10,
		}
	}
}

// mergeQuota overlays user-specified values on top of the tier default.
// Empty fields fall back to the default.
func mergeQuota(tier portalv1.TenantTier, override portalv1.ResourceQuotaConfig) portalv1.ResourceQuotaConfig {
	out := tierDefault(tier)
	if override.CPURequests != "" {
		out.CPURequests = override.CPURequests
	}
	if override.MemoryRequests != "" {
		out.MemoryRequests = override.MemoryRequests
	}
	if override.CPULimits != "" {
		out.CPULimits = override.CPULimits
	}
	if override.MemoryLimits != "" {
		out.MemoryLimits = override.MemoryLimits
	}
	if override.MaxPods > 0 {
		out.MaxPods = override.MaxPods
	}
	return out
}

// resourceListFor builds a corev1.ResourceList from the typed config,
// silently skipping any field that fails to parse.
func resourceListFor(cfg portalv1.ResourceQuotaConfig) corev1.ResourceList {
	rl := corev1.ResourceList{}
	if q, err := resource.ParseQuantity(cfg.CPURequests); err == nil {
		rl[corev1.ResourceRequestsCPU] = q
	}
	if q, err := resource.ParseQuantity(cfg.MemoryRequests); err == nil {
		rl[corev1.ResourceRequestsMemory] = q
	}
	if q, err := resource.ParseQuantity(cfg.CPULimits); err == nil {
		rl[corev1.ResourceLimitsCPU] = q
	}
	if q, err := resource.ParseQuantity(cfg.MemoryLimits); err == nil {
		rl[corev1.ResourceLimitsMemory] = q
	}
	if cfg.MaxPods > 0 {
		rl[corev1.ResourcePods] = *resource.NewQuantity(int64(cfg.MaxPods), resource.DecimalSI)
	}
	return rl
}

// priorityClassFor maps a Tier to one of the cluster-scoped PriorityClass names
// the Operator manages (see controllers/agentic.go).
func priorityClassFor(tier portalv1.TenantTier) string {
	switch tier {
	case portalv1.TierPriority:
		return "kcu-tenant-priority"
	case portalv1.TierInternal:
		return "kcu-tenant-internal"
	default:
		return "kcu-tenant-standard"
	}
}

// parseFactor turns "1.5" → 1.5 (defaults to 1.0 if parsing fails).
func parseFactor(s string) float64 {
	if s == "" {
		return 1.0
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 1.0
	}
	return v
}
