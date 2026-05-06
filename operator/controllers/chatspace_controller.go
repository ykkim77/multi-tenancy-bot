package controllers

import (
	"context"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	portalv1 "github.com/kcu/knowledge-portal-operator/api/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ChatSpaceReconciler is a placeholder reconciler that makes sure the manager starts.
type ChatSpaceReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// Reconcile implements a noop reconciliation loop.
func (r *ChatSpaceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log.FromContext(ctx).Info("noop reconcile", "chatspace", req.NamespacedName)
	return ctrl.Result{}, nil
}

// SetupWithManager wires the controller into the manager.
func (r *ChatSpaceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&portalv1.ChatSpace{}).
		Complete(r)
}
