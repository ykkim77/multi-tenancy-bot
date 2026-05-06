#!/bin/bash
# "test" 테넌트(네임스페이스 tenant-test) 생성 및 라벨 부여
# 조회: kubectl get namespaces -l tenant

set -e

NS="tenant-test"
TENANT_ID="test"

echo "Creating tenant namespace: $NS (tenant ID: $TENANT_ID)"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NS" name="$NS" tenant="$TENANT_ID" --overwrite

echo "Tenant namespace created. List tenants with:"
echo "  kubectl get namespaces -l tenant"
kubectl get namespace "$NS" --show-labels
