#!/bin/bash
# 기존 테넌트 네임스페이스에 tenant 라벨을 붙여 목록 조회에 포함시킵니다.
# 사용법: ./scripts/add-tenant-label.sh [namespace]
# 예: ./scripts/add-tenant-label.sh tenant-test-dept
#     ./scripts/add-tenant-label.sh tenant-test

set -e

NS="${1:-}"

if [[ -z "$NS" ]]; then
  echo "Usage: $0 <namespace>"
  echo "Example: $0 tenant-test-dept   # adds label tenant=test-dept"
  echo "Example: $0 tenant-test         # adds label tenant=test"
  echo ""
  echo "Current tenant-like namespaces:"
  kubectl get namespaces -o name | grep -E 'tenant-' || true
  exit 1
fi

# namespace가 tenant- 로 시작하면 tenant ID는 그 뒤 부분
if [[ "$NS" == tenant-* ]]; then
  TENANT_ID="${NS#tenant-}"
else
  TENANT_ID="$NS"
fi

if ! kubectl get namespace "$NS" &>/dev/null; then
  echo "Namespace $NS does not exist. Create it first, e.g.:"
  echo "  kubectl create namespace $NS"
  echo "  $0 $NS"
  exit 1
fi

kubectl label namespace "$NS" tenant="$TENANT_ID" name="$NS" --overwrite
echo "Labeled namespace $NS with tenant=$TENANT_ID"
echo "Verify: kubectl get namespaces -l tenant"
kubectl get namespace "$NS" --show-labels
