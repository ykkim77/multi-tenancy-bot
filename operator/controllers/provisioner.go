package controllers

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	schedulingv1 "k8s.io/api/scheduling/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	portalv1 "github.com/kcu/knowledge-portal-operator/api/v1"
)

// tenantNamespaceName derives the managed namespace name from the ChatSpace.
func tenantNamespaceName(cs *portalv1.ChatSpace) string {
	return fmt.Sprintf("tenant-%s", cs.Spec.TenantID)
}

// commonLabels are applied to every object the Operator manages so they
// can be identified and cleaned up consistently.
func commonLabels(cs *portalv1.ChatSpace) map[string]string {
	return map[string]string{
		"app.kubernetes.io/managed-by": "kcu-portal-operator",
		"portal.kcu.ac.kr/chatspace":   cs.Name,
		"portal.kcu.ac.kr/tenant":      cs.Spec.TenantID,
		"portal.kcu.ac.kr/tier":        string(cs.Spec.Tier),
	}
}

// reconcileNamespace creates or updates the tenant namespace.
func (r *ChatSpaceReconciler) reconcileNamespace(ctx context.Context, cs *portalv1.ChatSpace) (*corev1.Namespace, error) {
	name := tenantNamespaceName(cs)
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}

	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, ns, func() error {
		if ns.Labels == nil {
			ns.Labels = map[string]string{}
		}
		for k, v := range commonLabels(cs) {
			ns.Labels[k] = v
		}
		// Provide a stable label for cross-tenant NetworkPolicy matching.
		ns.Labels["name"] = name
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("namespace %s: %w", name, err)
	}
	r.recordIfChanged(cs, op, "Namespace", name)
	return ns, nil
}

// reconcileResourceQuota creates or updates the ResourceQuota inside the tenant namespace.
// `effective` is the quota actually applied (after agentic adjustments).
func (r *ChatSpaceReconciler) reconcileResourceQuota(
	ctx context.Context, cs *portalv1.ChatSpace, nsName string, effective portalv1.ResourceQuotaConfig,
) error {
	rq := &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-quota", Namespace: nsName},
	}
	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, rq, func() error {
		if rq.Labels == nil {
			rq.Labels = map[string]string{}
		}
		for k, v := range commonLabels(cs) {
			rq.Labels[k] = v
		}
		rq.Spec = corev1.ResourceQuotaSpec{
			Hard: resourceListFor(effective),
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("resourcequota: %w", err)
	}
	r.recordIfChanged(cs, op, "ResourceQuota", rq.Name)
	return nil
}

// reconcileLimitRange installs sensible per-container defaults so that
// Pods without explicit resources still respect the tenant's envelope.
func (r *ChatSpaceReconciler) reconcileLimitRange(
	ctx context.Context, cs *portalv1.ChatSpace, nsName string, effective portalv1.ResourceQuotaConfig,
) error {
	// Per-container default: a small slice of the limit (10%, with floor)
	cpuDef, _ := resource.ParseQuantity(effective.CPULimits)
	memDef, _ := resource.ParseQuantity(effective.MemoryLimits)
	cpuDef.Set(maxInt64(cpuDef.MilliValue()/10, 100))
	cpuDef.Format = resource.DecimalSI

	cpuPerContainer := resource.NewMilliQuantity(maxInt64(cpuDef.MilliValue(), 100), resource.DecimalSI)
	memPerContainer := resource.NewQuantity(maxInt64(memDef.Value()/10, 64*1024*1024), resource.BinarySI)

	lr := &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-limits", Namespace: nsName},
	}
	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, lr, func() error {
		if lr.Labels == nil {
			lr.Labels = map[string]string{}
		}
		for k, v := range commonLabels(cs) {
			lr.Labels[k] = v
		}
		lr.Spec = corev1.LimitRangeSpec{
			Limits: []corev1.LimitRangeItem{{
				Type: corev1.LimitTypeContainer,
				Default: corev1.ResourceList{
					corev1.ResourceCPU:    *cpuPerContainer,
					corev1.ResourceMemory: *memPerContainer,
				},
				DefaultRequest: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("50m"),
					corev1.ResourceMemory: resource.MustParse("64Mi"),
				},
			}},
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("limitrange: %w", err)
	}
	r.recordIfChanged(cs, op, "LimitRange", lr.Name)
	return nil
}

// reconcileNetworkPolicy installs a hard cross-tenant deny policy when
// HardIsolation is enabled. Same-namespace traffic is allowed; egress to
// kube-system/dns is permitted so Pods can resolve names.
func (r *ChatSpaceReconciler) reconcileNetworkPolicy(
	ctx context.Context, cs *portalv1.ChatSpace, nsName string,
) error {
	npName := "tenant-isolation"

	if !cs.Spec.Agentic.HardIsolation {
		// Tear down any leftover policy if HardIsolation was disabled.
		old := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: npName, Namespace: nsName}}
		if err := r.Delete(ctx, old); err != nil && !errors.IsNotFound(err) {
			return fmt.Errorf("delete networkpolicy: %w", err)
		}
		return nil
	}

	dnsPort := intstr.FromInt(53)
	udp := corev1.ProtocolUDP

	np := &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: npName, Namespace: nsName},
	}
	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, np, func() error {
		if np.Labels == nil {
			np.Labels = map[string]string{}
		}
		for k, v := range commonLabels(cs) {
			np.Labels[k] = v
		}
		np.Spec = networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{},
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Ingress: []networkingv1.NetworkPolicyIngressRule{{
				From: []networkingv1.NetworkPolicyPeer{
					{PodSelector: &metav1.LabelSelector{}},
				},
			}},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{To: []networkingv1.NetworkPolicyPeer{
					{PodSelector: &metav1.LabelSelector{}},
				}},
				{
					To: []networkingv1.NetworkPolicyPeer{{
						NamespaceSelector: &metav1.LabelSelector{
							MatchLabels: map[string]string{"name": "kube-system"},
						},
					}},
					Ports: []networkingv1.NetworkPolicyPort{{
						Protocol: &udp,
						Port:     &dnsPort,
					}},
				},
			},
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("networkpolicy: %w", err)
	}
	r.recordIfChanged(cs, op, "NetworkPolicy", np.Name)
	return nil
}

// ensureClusterPriorityClasses guarantees the three managed PriorityClasses
// exist. They are cluster-scoped and shared across all tenants.
func (r *ChatSpaceReconciler) ensureClusterPriorityClasses(ctx context.Context) error {
	specs := []struct {
		name    string
		value   int32
		preempt corev1.PreemptionPolicy
		desc    string
	}{
		{"kcu-tenant-priority", 10000, corev1.PreemptLowerPriority,
			"High-priority tenants protected from noisy neighbors"},
		{"kcu-tenant-standard", 1000, corev1.PreemptNever,
			"Default-priority tenants"},
		{"kcu-tenant-internal", 100, corev1.PreemptNever,
			"Best-effort/internal-only tenants, first to be reclaimed"},
	}
	for _, s := range specs {
		preempt := s.preempt
		pc := &schedulingv1.PriorityClass{
			ObjectMeta: metav1.ObjectMeta{Name: s.name},
		}
		_, err := controllerutil.CreateOrUpdate(ctx, r.Client, pc, func() error {
			pc.Value = s.value
			pc.PreemptionPolicy = &preempt
			pc.Description = s.desc
			pc.GlobalDefault = false
			if pc.Labels == nil {
				pc.Labels = map[string]string{}
			}
			pc.Labels["app.kubernetes.io/managed-by"] = "kcu-portal-operator"
			return nil
		})
		if err != nil {
			return fmt.Errorf("priorityclass %s: %w", s.name, err)
		}
	}
	return nil
}

// recordIfChanged appends a human-readable note to Status.AgenticActions
// whenever a sub-reconciler actually modifies a resource.
func (r *ChatSpaceReconciler) recordIfChanged(
	cs *portalv1.ChatSpace, op controllerutil.OperationResult, kind, name string,
) {
	if op == controllerutil.OperationResultNone {
		return
	}
	cs.Status.AgenticActions = appendAction(cs.Status.AgenticActions,
		fmt.Sprintf("%s %s/%s", op, kind, name))
}

// appendAction appends `entry` to actions, capping the slice at 20 entries.
func appendAction(actions []string, entry string) []string {
	const maxActions = 20
	actions = append(actions, entry)
	if len(actions) > maxActions {
		actions = actions[len(actions)-maxActions:]
	}
	return actions
}

// helper: avoid pulling in the standard library's math.Max for ints
func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

// finalizerName is the finalizer key the Operator owns.
const finalizerName = "portal.kcu.ac.kr/finalizer"

// reconcileFinalizer adds the Operator's finalizer if missing. Returns true if updated.
func (r *ChatSpaceReconciler) reconcileFinalizer(ctx context.Context, cs *portalv1.ChatSpace) (bool, error) {
	if !controllerutil.ContainsFinalizer(cs, finalizerName) {
		controllerutil.AddFinalizer(cs, finalizerName)
		if err := r.Update(ctx, cs); err != nil {
			return false, err
		}
		return true, nil
	}
	return false, nil
}

// finalize tears down owned cluster-scoped resources (the namespace) when the
// ChatSpace is deleted. Namespace deletion cascades to all in-namespace objects.
func (r *ChatSpaceReconciler) finalize(ctx context.Context, cs *portalv1.ChatSpace) error {
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: tenantNamespaceName(cs)}}
	if err := r.Delete(ctx, ns); err != nil && !errors.IsNotFound(err) {
		return fmt.Errorf("delete namespace: %w", err)
	}
	return nil
}

// noChange is a sentinel value used by client.IgnoreAlreadyExists patterns.
var _ client.Object = (*portalv1.ChatSpace)(nil)
