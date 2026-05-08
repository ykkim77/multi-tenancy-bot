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
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
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
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=roles;rolebindings,verbs=get;list;watch;create;update;patch;delete

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
		return ctrl.Result{Requeue: true}, nil
	}

	// ── Default values for missing fields ─────────────────────────
	if cs.Spec.Tier == "" {
		cs.Spec.Tier = portalv1.TierStandard
	}
	if cs.Spec.Agentic.ReclaimIdleAfterSeconds == 0 {
		cs.Spec.Agentic.ReclaimIdleAfterSeconds = 120
	}
	if cs.Spec.Agentic.ActiveBoostFactor == "" {
		cs.Spec.Agentic.ActiveBoostFactor = "1.5"
	}

	// ── Cluster-scoped resources ──────────────────────────────────
	if err := r.ensureClusterPriorityClasses(ctx); err != nil {
		logger.Error(err, "ensure priorityclasses")
		return ctrl.Result{}, err
	}

	// ── Agentic decision ──────────────────────────────────────────
	effective, explanation, err := r.computeEffectiveQuota(ctx, cs)
	if err != nil {
		logger.Error(err, "agentic decision — using static quota")
	}

	// ── Provision resources (all idempotent) ──────────────────────
	ns, err := r.reconcileNamespace(ctx, cs)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("namespace: %w", err)
	}
	if err := r.reconcileResourceQuota(ctx, cs, ns.Name, effective); err != nil {
		return ctrl.Result{}, fmt.Errorf("resourcequota: %w", err)
	}
	if err := r.reconcileLimitRange(ctx, cs, ns.Name, effective); err != nil {
		return ctrl.Result{}, fmt.Errorf("limitrange: %w", err)
	}
	if err := r.reconcileNetworkPolicy(ctx, cs, ns.Name); err != nil {
		return ctrl.Result{}, fmt.Errorf("networkpolicy: %w", err)
	}
	if err := r.reconcileRoleBinding(ctx, cs, ns.Name); err != nil {
		return ctrl.Result{}, fmt.Errorf("rolebinding: %w", err)
	}
	if err := r.reconcileRBAC(ctx, cs, ns.Name); err != nil {
		return ctrl.Result{}, fmt.Errorf("rbac: %w", err)
	}

	// ── Status update — single write with RetryOnConflict ─────────
	// Pattern: re-fetch a fresh copy inside the retry loop so we always
	// write against the latest resourceVersion.  This eliminates the
	// "object has been modified" conflict that occurs when:
	//   (a) multiple workers process the same CR simultaneously, or
	//   (b) an intermediate Status().Update() during provisioning
	//       made the in-memory cs stale before the final write.
	nsName := ns.Name
	updateErr := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		fresh := &portalv1.ChatSpace{}
		if ferr := r.Get(ctx, req.NamespacedName, fresh); ferr != nil {
			return ferr
		}
		fresh.Status.AgenticActions = appendAction(
			fresh.Status.AgenticActions, "agentic: "+explanation)
		fresh.Status.Namespace = nsName
		fresh.Status.AppliedQuota = effective
		fresh.Status.Phase = portalv1.PhaseReady
		fresh.Status.Message = "tenant ready"
		fresh.Status.LastUpdated = metav1.Now()
		fresh.Status.ObservedGeneration = fresh.Generation
		r.setReadyCondition(fresh, true, "Provisioned", "All resources reconciled successfully")
		return r.Status().Update(ctx, fresh)
	})
	if updateErr != nil {
		logger.Error(updateErr, "update status")
		return ctrl.Result{}, updateErr
	}

	logger.Info("reconciled", "phase", portalv1.PhaseReady,
		"tier", cs.Spec.Tier, "namespace", nsName)

	return ctrl.Result{RequeueAfter: r.rebalanceInterval()}, nil
}

// setPhase is kept for external callers (e.g. finalize).
// It uses RetryOnConflict so it never returns a stale-resource-version error.
func (r *ChatSpaceReconciler) setPhase(
	ctx context.Context, cs *portalv1.ChatSpace, phase portalv1.ChatSpacePhase, msg string,
) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		fresh := &portalv1.ChatSpace{}
		if ferr := r.Get(ctx, client.ObjectKeyFromObject(cs), fresh); ferr != nil {
			return ferr
		}
		fresh.Status.Phase = phase
		fresh.Status.Message = msg
		fresh.Status.LastUpdated = metav1.Now()
		return r.Status().Update(ctx, fresh)
	})
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
//
// MaxConcurrentReconciles=20 allows parallel provisioning of up to 20
// ChatSpace CRs simultaneously.
//
// ── Why PriorityClass watch is intentionally OMITTED ──────────────────
// PriorityClasses are cluster-scoped and shared.  If we watch them, every
// CreateOrUpdate inside ensureClusterPriorityClasses fires a watch event
// that enqueues ALL ChatSpaces.  With 20 concurrent workers and e.g. 10
// experiment CRs, this creates a thundering-herd: 200 simultaneous
// reconciles competing for the same objects → cascading conflict errors.
// PriorityClasses are owned by the Operator itself and won't drift from
// external sources in normal operation, so the watch provides no benefit
// while causing significant harm to parallel provisioning throughput.
func (r *ChatSpaceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&portalv1.ChatSpace{}).
		WithOptions(controller.Options{
			MaxConcurrentReconciles: 20,
		}).
		// Watch namespace-scoped owned resources so external edits trigger re-reconciliation.
		// PriorityClass watch is intentionally omitted — see comment above.
		Watches(&corev1.Namespace{},
			handler.EnqueueRequestsFromMapFunc(r.namespaceToChatSpace)).
		Watches(&corev1.ResourceQuota{},
			handler.EnqueueRequestsFromMapFunc(r.objectToChatSpaceFromLabels)).
		Watches(&corev1.LimitRange{},
			handler.EnqueueRequestsFromMapFunc(r.objectToChatSpaceFromLabels)).
		Watches(&networkingv1.NetworkPolicy{},
			handler.EnqueueRequestsFromMapFunc(r.objectToChatSpaceFromLabels)).
		// RoleBinding watch intentionally omitted: watching RBAC types causes
		// the controller-runtime cache to also list Role objects at cluster scope,
		// requiring additional permissions. Since the Operator reconciles every
		// 30 s (RequeueAfter), any deleted RoleBinding is re-created on the next
		// cycle without needing an explicit watch.
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

// priorityClassToChatSpaces is no longer wired into SetupWithManager.
// Kept as dead code to document the reasoning; the watch was removed to
// prevent the thundering-herd described in SetupWithManager's comment.
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
