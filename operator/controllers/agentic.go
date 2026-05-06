package controllers

import (
	"context"
	"fmt"
	"time"

	"k8s.io/apimachinery/pkg/api/resource"

	portalv1 "github.com/kcu/knowledge-portal-operator/api/v1"
)

// ── Agentic Re-balancing ────────────────────────────────────────────
//
// The Agentic Operator periodically observes all ChatSpaces and decides,
// on its own, how to redistribute resources between active and idle tenants.
// This is the autonomy that distinguishes the Operator from a one-shot
// `kubectl apply` baseline:
//
//   1. Idle detection — a tenant whose Status.LastUpdated is older than
//      Spec.Agentic.ReclaimIdleAfterSeconds, OR whose annotation
//      "portal.kcu.ac.kr/usage" equals "idle", is considered idle.
//
//   2. Reclamation — the Operator shrinks idle tenants' quota to a fraction
//      of the tier default (30% by default).
//
//   3. Active boost — active tenants of the same or higher tier receive a
//      proportional bonus governed by Spec.Agentic.ActiveBoostFactor.
//
//   4. Each decision is recorded in Status.AgenticActions so it can be
//      audited in benchmarks and in cluster-level monitoring.
//
// The current ChatSpace's effective quota (after policy is applied) is
// returned by computeEffectiveQuota.

// usageHint extracts an annotation-driven usage signal, if present.
// Valid values: "active", "idle". Anything else falls back to time-based detection.
func usageHint(cs *portalv1.ChatSpace) string {
	if cs.Annotations == nil {
		return ""
	}
	return cs.Annotations["portal.kcu.ac.kr/usage"]
}

// isIdle decides whether `cs` should be considered idle right now.
func isIdle(cs *portalv1.ChatSpace, now time.Time) bool {
	switch usageHint(cs) {
	case "idle":
		return true
	case "active":
		return false
	}
	// Fallback: time-based heuristic.
	if cs.Spec.Agentic.ReclaimIdleAfterSeconds <= 0 {
		return false
	}
	if cs.Status.LastUpdated.IsZero() {
		return false
	}
	deadline := cs.Status.LastUpdated.Add(
		time.Duration(cs.Spec.Agentic.ReclaimIdleAfterSeconds) * time.Second,
	)
	return now.After(deadline)
}

// scaleQuotaQuantities returns a copy of `q` where every numerical value is
// multiplied by `factor`. CPU is treated as milli-units, memory as bytes.
func scaleQuotaQuantities(q portalv1.ResourceQuotaConfig, factor float64) portalv1.ResourceQuotaConfig {
	out := q
	if v, err := resource.ParseQuantity(q.CPURequests); err == nil {
		out.CPURequests = scaleMilli(v.MilliValue(), factor, "m")
	}
	if v, err := resource.ParseQuantity(q.MemoryRequests); err == nil {
		out.MemoryRequests = scaleBytes(v.Value(), factor)
	}
	if v, err := resource.ParseQuantity(q.CPULimits); err == nil {
		out.CPULimits = scaleMilli(v.MilliValue(), factor, "m")
	}
	if v, err := resource.ParseQuantity(q.MemoryLimits); err == nil {
		out.MemoryLimits = scaleBytes(v.Value(), factor)
	}
	return out
}

func scaleMilli(milli int64, factor float64, suffix string) string {
	scaled := int64(float64(milli) * factor)
	if scaled < 50 {
		scaled = 50
	}
	return fmt.Sprintf("%d%s", scaled, suffix)
}

func scaleBytes(bytes int64, factor float64) string {
	scaled := int64(float64(bytes) * factor)
	const minBytes = 64 * 1024 * 1024 // 64Mi
	if scaled < minBytes {
		scaled = minBytes
	}
	q := resource.NewQuantity(scaled, resource.BinarySI)
	return q.String()
}

// computeEffectiveQuota is the Agentic decision point.
// It returns:
//   - the quota to apply to `cs` right now, after considering global usage state;
//   - a human-readable explanation suitable for Status.AgenticActions.
func (r *ChatSpaceReconciler) computeEffectiveQuota(
	ctx context.Context, cs *portalv1.ChatSpace,
) (portalv1.ResourceQuotaConfig, string, error) {

	// 1) Start from the user-provided spec, merged with tier defaults.
	base := mergeQuota(cs.Spec.Tier, cs.Spec.ResourceQuota)

	// 2) If agentic is disabled, return as-is.
	if !cs.Spec.Agentic.Enabled {
		return base, "agentic disabled — using static quota", nil
	}

	// 3) Look at the cluster-wide population of ChatSpaces.
	all := &portalv1.ChatSpaceList{}
	if err := r.List(ctx, all); err != nil {
		return base, "failed to list peers; falling back to static quota", err
	}

	now := time.Now()
	var idleCount, activeCount int
	for i := range all.Items {
		peer := &all.Items[i]
		if peer.UID == cs.UID {
			continue
		}
		if isIdle(peer, now) {
			idleCount++
		} else {
			activeCount++
		}
	}

	// 4) Decide how to scale this tenant's quota.
	switch {
	case isIdle(cs, now):
		// Idle tenant: reclaim 70% of its quota for the active pool.
		shrunk := scaleQuotaQuantities(base, 0.3)
		shrunk.MaxPods = base.MaxPods // keep pod count
		return shrunk, fmt.Sprintf(
			"reclaimed 70%% of quota (idle tenant; cluster has %d active / %d idle peers)",
			activeCount, idleCount,
		), nil

	case idleCount > 0:
		// Active tenant in a cluster with idle peers: apply boost.
		factor := parseFactor(cs.Spec.Agentic.ActiveBoostFactor)
		// Cap the boost at the proportion of available headroom: roughly
		// (idleCount / max(activeCount, 1)) * 0.7 (fraction reclaimed) extra.
		headroom := float64(idleCount) / float64(activeCount+1) * 0.7
		effective := 1.0 + (factor-1.0)*headroom
		if effective < 1.0 {
			effective = 1.0
		}
		boosted := scaleQuotaQuantities(base, effective)
		boosted.MaxPods = base.MaxPods
		return boosted, fmt.Sprintf(
			"boosted active tenant quota ×%.2f (reclaimed from %d idle peer(s))",
			effective, idleCount,
		), nil

	default:
		// No agentic action needed.
		return base, "no rebalancing needed (no idle peers detected)", nil
	}
}
