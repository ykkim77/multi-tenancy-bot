#!/bin/bash

# Cleanup script for KCU Knowledge Portal

set -e

echo "=== Cleaning up KCU Knowledge Portal ==="

# Stop port-forwards
echo "Stopping port-forwards..."
pkill -f "port-forward" || true

# Delete deployments
echo "Deleting deployments..."
kubectl delete -f manifests/outline/deployment.yaml --ignore-not-found=true
kubectl delete -f manifests/services/embedding-worker.yaml --ignore-not-found=true
kubectl delete -f manifests/services/rag-api.yaml --ignore-not-found=true
kubectl delete -f manifests/qdrant/statefulset.yaml --ignore-not-found=true
kubectl delete -f manifests/services/auth-gateway.yaml --ignore-not-found=true

# Delete jobs
echo "Deleting jobs..."
kubectl delete job outline-webhook-setup -n tenant-test-dept --ignore-not-found=true

# Delete namespaces (optional - commented out to preserve data)
# echo "Deleting namespaces..."
# kubectl delete namespace tenant-test-dept
# kubectl delete namespace kcu-system

echo "=== Cleanup complete! ==="
