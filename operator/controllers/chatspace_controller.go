/*
Package controllers implements the Agentic Operator for KCU Knowledge Portal.

The Reconcile loop translates a `ChatSpace` Custom Resource into a fully
provisioned tenant — Namespace, ResourceQuota, LimitRange, NetworkPolicy —
and continuously rebalances quotas based on cluster-wide observations.

The autonomy lives in two places:

  1. controllers/agentic.go — observes peer ChatSpaces and computes the
     effective quota for the resource being reconciled.
  2. periodic requeue — the controller schedules its own re-evaluation,
     so re-balancing decisions converge over time without external triggers.
*/
package controllers

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	schedulingv1 "k8s.io/api/scheduling/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	portalv1 "github.com/kcu/knowledge-portal-operator/api/v1"
)

// ChatSpaceReconciler reconciles ChatSpace objects.
type ChatSpaceReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// RebalanceInterval is how often the controller re-runs the agentic
	// loop in steady state. Defaults to 30s when zero.
	RebalanceInterval time.Duration
}

// rebalanceInterval returns the configured interval or a default.
func (r *ChatSpaceReconciler) rebalanceInterval() time.Duration {
	if r.RebalanceInterval > 0 {
		return r.RebalanceInterval
	}
	return 30 * time.Second
}

// +kubebuilder:rbac:groups=portal.kcu.ac.kr,resources=chatspaces,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=portal.kcu.ac.kr,resources=chatspaces/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=portal.kcu.ac.kr,resources=chatspaces/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=namespaces;resourcequotas;limitranges,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=scheduling.k8s.io,resources=priorityclasses,verbs=get;list;watch;create;update;patch

// Reconcile is the entry point for the controller manager.
//
// On every event the reconciler:
//   1. fetches the ChatSpace (or returns if it's gone),
//   2. handles deletion via finalizer if needed,
//   3. ensures the cluster-scoped PriorityClasses exist,
//   4. asks the Agentic policy what quota to apply,
//   5. provisions Namespace → ResourceQuota → LimitRange → NetworkPolicy,
//   6. updates Status with the decision and any errors,
//   7. schedules a periodic re-evaluation.
func (r *ChatSpaceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("chatspace", req.NamespacedName)

	cs := &portalv1.ChatSpace{}
	if err := r.Get(ctx, req.NamespacedName, cs); err != nil {
		if errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// ── Deletion path ─────────────────────────────────────────────
	if !cs.DeletionTimestamp.IsZero() {
		if controllerutil.ContainsFinalizer(cs, finalizerName) {
			if err := r.finalize(ctx, cs); err != nil {
				return ctrl.Result{}, err
			}
			controllerutil.RemoveFinalizer(cs, finalizerName)
			if err := r.Update(ctx, cs); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{}, nil
	}

	// ── Finalizer ─────────────────────────────────────────────────
	if added, err := r.reconcileFinalizer(ctx, cs); err != nil {
		return ctrl.Result{}, err
	} else if added {
		// The Update above triggered a fresh event; let it requeue naturally.
		return ctrl.Result{Requeue: true}, nil
	}

	// ── Default values for missing fields ─────────────────────────
	if cs.Spec.Tier == "" {
		cs.Spec.Tier = portalv1.TierStandard
	}
	// Note: Agentic.Enabled defaults to true via kubebuilder annotation,
	// but webhook defaults aren't running, so be defensive in code too.
	if cs.Spec.Agentic.ReclaimIdleAfterSeconds == 0 {
		cs.Spec.Agentic.ReclaimIdleAfterSeconds = 120
	}
	if cs.Spec.Agentic.ActiveBoostFactor == "" {
		cs.Spec.Agentic.ActiveBoostFactor = "1.5"
	}

	// ── Phase: Provisioning ───────────────────────────────────────
	if cs.Status.Phase == "" || cs.Status.Phase == portalv1.PhasePending {
		_ = r.setPhase(ctx, cs, portalv1.PhaseProvisioning, "starting provisioning")
	}

	// ── Cluster-scoped resources ──────────────────────────────────
	if err := r.ensureClusterPriorityClasses(ctx); err != nil {
		logger.Error(err, "ensure priorityclasses")
		_ = r.setPhase(ctx, cs, portalv1.PhaseFailed, fmt.Sprintf("priorityclasses: %v", err))
		return ctrl.Result{}, err
	}

	// ── Agentic decision ──────────────────────────────────────────
	effective, explanation, err := r.computeEffectiveQuota(ctx, cs)
	if err != nil {
		logger.Error(err, "agentic decision")
		// Continue with the static quota; the explanation already reflects this.
	}

	// ── Provision: Namespace ──────────────────────────────────────
	ns, err := r.reconcileNamespace(ctx, cs)
	if err != nil {
		_ = r.setPhase(ctx, cs, portalv1.PhaseFailed, err.Error())
		return ctrl.Result{}, err
	}

	// ── Provision: ResourceQuota / LimitRange / NetworkPolicy ─────
	if err := r.reconcileResourceQuota(ctx, cs, ns.Name, effective); err != nil {
		_ = r.setPhase(ctx, cs, portalv1.PhaseFailed, err.Error())
		return ctrl.Result{}, err
	}
	if err := r.reconcileLimitRange(ctx, cs, ns.Name, effective); err != nil {
		_ = r.setPhase(ctx, cs, portalv1.PhaseFailed, err.Error())
		return ctrl.Result{}, err
	}
	if err := r.reconcileNetworkPolicy(ctx, cs, ns.Name); err != nil {
		_ = r.setPhase(ctx, cs, portalv1.PhaseFailed, err.Error())
		return ctrl.Result{}, err
	}

	// ── Status update ─────────────────────────────────────────────
	cs.Status.AgenticActions = appendAction(cs.Status.AgenticActions, "agentic: "+explanation)
	cs.Status.Namespace = ns.Name
	cs.Status.AppliedQuota = effective
	cs.Status.Phase = portalv1.PhaseReady
	cs.Status.Message = "tenant ready"
	cs.Status.LastUpdated = metav1.Now()
	cs.Status.ObservedGeneration = cs.Generation
	r.setReadyCondition(cs, true, "Provisioned", "All resources reconciled successfully")

	if err := r.Status().Update(ctx, cs); err != nil {
		logger.Error(err, "update status")
		return ctrl.Result{}, err
	}

	logger.Info("reconciled", "phase", cs.Status.Phase,
		"tier", cs.Spec.Tier, "namespace", ns.Name)

	// Re-evaluate periodically so agentic re-balancing keeps converging.
	return ctrl.Result{RequeueAfter: r.rebalanceInterval()}, nil
}

// setPhase is a small helper that updates Status.Phase + Message.
// It is best-effort; failures are logged but do not stop reconciliation.
func (r *ChatSpaceReconciler) setPhase(
	ctx context.Context, cs *portalv1.ChatSpace, phase portalv1.ChatSpacePhase, msg string,
) error {
	cs.Status.Phase = phase
	cs.Status.Message = msg
	cs.Status.LastUpdated = metav1.Now()
	return r.Status().Update(ctx, cs)
}

// setReadyCondition writes a Ready condition using the standard Kubernetes pattern.
func (r *ChatSpaceReconciler) setReadyCondition(cs *portalv1.ChatSpace, ready bool, reason, msg string) {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	cond := metav1.Condition{
		Type:               "Ready",
		Status:             status,
		Reason:             reason,
		Message:            msg,
		LastTransitionTime: metav1.Now(),
		ObservedGeneration: cs.Generation,
	}
	// Replace existing or append.
	for i, c := range cs.Status.Conditions {
		if c.Type == cond.Type {
			if c.Status == cond.Status && c.Reason == cond.Reason {
				cond.LastTransitionTime = c.LastTransitionTime
			}
			cs.Status.Conditions[i] = cond
			return
		}
	}
	cs.Status.Conditions = append(cs.Status.Conditions, cond)
}

// SetupWithManager wires the controller into the manager and configures
// watches on owned resources so the Operator reacts to drift.
func (r *ChatSpaceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&portalv1.ChatSpace{}).
		// Watch owned resources so external edits trigger re-reconciliation.
		Watches(&corev1.Namespace{},
			handler.EnqueueRequestsFromMapFunc(r.namespaceToChatSpace)).
		Watches(&corev1.ResourceQuota{},
			handler.EnqueueRequestsFromMapFunc(r.objectToChatSpaceFromLabels)).
		Watches(&corev1.LimitRange{},
			handler.EnqueueRequestsFromMapFunc(r.objectToChatSpaceFromLabels)).
		Watches(&networkingv1.NetworkPolicy{},
			handler.EnqueueRequestsFromMapFunc(r.objectToChatSpaceFromLabels)).
		Watches(&schedulingv1.PriorityClass{},
			handler.EnqueueRequestsFromMapFunc(r.priorityClassToChatSpaces)).
		Complete(r)
}

// namespaceToChatSpace maps a Namespace event back to its owning ChatSpace.
// (Signature follows controller-runtime v0.16+ MapFunc.)
func (r *ChatSpaceReconciler) namespaceToChatSpace(_ context.Context, obj client.Object) []reconcile.Request {
	labels := obj.GetLabels()
	if labels == nil {
		return nil
	}
	csName, ok := labels["portal.kcu.ac.kr/chatspace"]
	if !ok {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Name: csName}}}
}

// objectToChatSpaceFromLabels handles ResourceQuota / LimitRange / NetworkPolicy events.
func (r *ChatSpaceReconciler) objectToChatSpaceFromLabels(ctx context.Context, obj client.Object) []reconcile.Request {
	return r.namespaceToChatSpace(ctx, obj)
}

// priorityClassToChatSpaces enqueues every ChatSpace when a managed PriorityClass changes.
// PriorityClasses are cluster-scoped and shared, so any drift requires a global sweep.
func (r *ChatSpaceReconciler) priorityClassToChatSpaces(ctx context.Context, obj client.Object) []reconcile.Request {
	labels := obj.GetLabels()
	if labels == nil || labels["app.kubernetes.io/managed-by"] != "kcu-portal-operator" {
		return nil
	}
	list := &portalv1.ChatSpaceList{}
	if err := r.List(ctx, list); err != nil {
		return nil
	}
	out := make([]reconcile.Request, 0, len(list.Items))
	for i := range list.Items {
		out = append(out, reconcile.Request{
			NamespacedName: client.ObjectKey{Name: list.Items[i].Name},
		})
	}
	return out
}
