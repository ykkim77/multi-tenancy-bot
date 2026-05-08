#!/bin/bash

# Quick start script for KCU Knowledge Portal

set -e

echo "=== KCU Knowledge Portal Quick Start ==="

# 1. Create namespaces
echo "Creating namespaces..."
kubectl create namespace kcu-system --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace kcu-system name=kcu-system --overwrite

kubectl create namespace tenant-test-dept --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace tenant-test-dept name=tenant-test-dept tenant=test-dept --overwrite

# 2. Create secrets
echo "Creating secrets..."
kubectl create secret generic tenant-secrets \
  --from-literal=OUTLINE_SECRET_KEY="$(openssl rand -hex 16)" \
  --from-literal=OUTLINE_UTILS_SECRET="$(openssl rand -hex 16)" \
  --from-literal=POSTGRES_PASSWORD="outline-pass" \
  --from-literal=REDIS_PASSWORD="redis-pass" \
  --from-literal=EMBEDDING_API_KEY="$OPENAI_API_KEY" \
  --from-literal=LLM_API_KEY="$OPENAI_API_KEY" \
  -n tenant-test-dept \
  --dry-run=client -o yaml | kubectl apply -f -

# 3. Apply RBAC
echo "Applying RBAC..."
kubectl apply -f manifests/security/rbac.yaml

# 4. Deploy services
echo "Deploying Auth Gateway..."
kubectl apply -f manifests/services/auth-gateway.yaml

echo "Deploying Qdrant..."
kubectl apply -f manifests/qdrant/statefulset.yaml

echo "Deploying Embedding Worker..."
kubectl apply -f manifests/services/embedding-worker.yaml

echo "Deploying RAG API..."
kubectl apply -f manifests/services/rag-api.yaml

echo "Deploying Outline..."
kubectl apply -f manifests/outline/deployment.yaml

# 5. Wait for services
echo "Waiting for services to be ready..."
kubectl wait --for=condition=ready pod -l app=auth-gateway -n kcu-system --timeout=120s
kubectl wait --for=condition=ready pod -l app=qdrant -n tenant-test-dept --timeout=120s
kubectl wait --for=condition=ready pod -l app=outline-postgres -n tenant-test-dept --timeout=120s
kubectl wait --for=condition=ready pod -l app=outline -n tenant-test-dept --timeout=120s

echo ""
echo "=== Deployment complete! ==="
echo ""
echo "To access Outline:"
echo "  1. kubectl port-forward -n tenant-test-dept svc/outline 3000:80"
echo "  2. kubectl port-forward -n kcu-system svc/auth-gateway 8000:8000"
echo "  3. Open http://localhost:3000 in your browser"
echo ""
echo "To set up webhooks:"
echo "  kubectl apply -f manifests/outline/webhook-setup-job.yaml"
