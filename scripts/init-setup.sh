#!/bin/bash
# KCU Portal — 전체 초기 설정 자동화 스크립트
# 사용: bash scripts/init-setup.sh

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CLUSTER_NAME="${CLUSTER_NAME:-kcu-demo}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# ═══════════════════════════════════════════════════════════════════════════

log_info "KCU Portal — 초기 설정 스크립트 시작"

# ─── Step 1: 사전 조건 확인 ────────────────────────────────────────────────

log_info "Step 1: 사전 조건 확인 ..."

command -v docker > /dev/null || log_error "Docker 미설치. 설치 후 재시작."
log_info "  ✓ Docker: $(docker --version)"

command -v kubectl > /dev/null || log_error "kubectl 미설치. 설치 후 재시작."
log_info "  ✓ kubectl: $(kubectl version --client --short)"

command -v kind > /dev/null || log_error "KinD 미설치. 설치 후 재시작."
log_info "  ✓ KinD: $(kind version | head -1)"

command -v python3 > /dev/null || log_error "Python3 미설치."
log_info "  ✓ Python3: $(python3 --version)"

# ─── Step 2: KinD 클러스터 생성 ────────────────────────────────────────────

log_info "Step 2: KinD 클러스터 확인/생성 ..."

if kind get clusters | grep -q "^$CLUSTER_NAME$"; then
    log_info "  ✓ 클러스터 '$CLUSTER_NAME' 이미 존재"
else
    log_info "  → 클러스터 '$CLUSTER_NAME' 생성 중 (약 1-2 분) ..."
    kind create cluster --name "$CLUSTER_NAME" \
        --config - <<'KINDCONFIG'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
- role: worker
KINDCONFIG
    log_info "  ✓ 클러스터 생성 완료"
fi

# kubectl 컨텍스트 설정
kubectl config use-context "kind-$CLUSTER_NAME" > /dev/null
log_info "  ✓ 컨텍스트 설정: kind-$CLUSTER_NAME"

# 클러스터 준비 완료 대기
log_info "  → 클러스터 준비 대기 중 ..."
for i in {1..30}; do
    if kubectl get nodes &>/dev/null; then
        kubectl wait --for=condition=Ready node --all --timeout=5s 2>/dev/null && break
    fi
    sleep 2
done
log_info "  ✓ 클러스터 준비 완료"

kubectl get nodes
echo ""

# ─── Step 3: Operator 빌드 및 배포 ────────────────────────────────────────

log_info "Step 3: Agentic Operator 빌드 ..."

cd "$PROJECT_DIR/operator"

# Go 의존성 확인
if [ ! -f go.mod ]; then
    log_error "operator/go.mod 이 없습니다. $PROJECT_DIR/operator 에서 실행하세요."
fi

# 바이너리 빌드
log_info "  → Go 빌드 중 ..."
go build ./... || log_error "Go 빌드 실패"
go build -o bin/manager . || log_error "Manager 바이너리 빌드 실패"
log_info "  ✓ Go 빌드 성공"

# Docker 이미지 빌드
log_info "  → Docker 이미지 빌드 중 ..."
make docker-build IMG=kcu/portal-operator:local 2>/dev/null || \
    log_error "Docker 빌드 실패"
log_info "  ✓ Docker 이미지 빌드 성공"

# KinD 에 로드
log_info "  → KinD 클러스터에 이미지 로드 중 ..."
kind load docker-image kcu/portal-operator:local --name "$CLUSTER_NAME" || \
    log_error "이미지 로드 실패"
log_info "  ✓ 이미지 로드 성공"

cd "$PROJECT_DIR"
echo ""

# ─── Step 4: Operator 배포 ─────────────────────────────────────────────────

log_info "Step 4: Operator 배포 중 ..."

# CRD 설치
log_info "  → CRD 설치 ..."
kubectl apply -f operator/config/crd/bases/portal.kcu.ac.kr_chatspaces.yaml || \
    log_error "CRD 설치 실패"
sleep 2

# Operator 배포
log_info "  → Operator Deployment 설치 ..."
kubectl apply -f operator/config/manager/manager.yaml || \
    log_error "Operator 배포 실패"

# Operator 준비 대기
log_info "  → Operator Pod 준비 대기 중 ..."
kubectl -n kcu-portal-system wait --for=condition=Ready pod \
    -l app.kubernetes.io/name=portal-operator --timeout=120s 2>/dev/null || \
    log_warn "Operator Pod 준비 타임아웃 (log 확인 권장)"

log_info "  ✓ Operator 배포 완료"
kubectl -n kcu-portal-system get deployment,pod
echo ""

# ─── Step 5: Python 의존성 설치 ────────────────────────────────────────────

log_info "Step 5: Python 의존성 설치 ..."

python3 -m pip install --quiet numpy matplotlib scipy pyyaml 2>/dev/null || \
    log_warn "Python 패키지 설치 실패 (수동 설치 필요: pip install numpy matplotlib scipy pyyaml)"

log_info "  ✓ Python 의존성 확인 완료"
echo ""

# ─── Step 6: 테스트 ───────────────────────────────────────────────────────

log_info "Step 6: Operator 작동 테스트 ..."

log_info "  → 테스트 ChatSpace 생성 ..."
cat <<'EOF' | kubectl apply -f -
apiVersion: portal.kcu.ac.kr/v1
kind: ChatSpace
metadata:
  name: cs-test-setup
spec:
  tenantId: test-setup
  tier: standard
  agentic:
    enabled: true
    hardIsolation: true
EOF

log_info "  → Ready 상태 대기 중 (최대 60s) ..."
for i in {1..60}; do
    PHASE=$(kubectl get chatspace cs-test-setup -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    if [ "$PHASE" = "Ready" ]; then
        log_info "  ✓ ChatSpace Ready 상태 도달"
        break
    fi
    sleep 1
done

if [ "$(kubectl get chatspace cs-test-setup -o jsonpath='{.status.phase}' 2>/dev/null)" != "Ready" ]; then
    log_warn "  ⚠ ChatSpace Ready 타임아웃. log 확인:"
    log_warn "    kubectl -n kcu-portal-system logs deploy/portal-operator --tail=50"
else
    # 테넌트 리소스 확인
    NS="tenant-test-setup"
    log_info "  → 생성된 리소스 확인 ($NS) ..."
    
    if kubectl get ns "$NS" &>/dev/null; then
        log_info "    ✓ Namespace: $NS"
    fi
    if kubectl get quota -n "$NS" &>/dev/null; then
        log_info "    ✓ ResourceQuota"
    fi
    if kubectl get limitrange -n "$NS" &>/dev/null; then
        log_info "    ✓ LimitRange"
    fi
    if kubectl get networkpolicy -n "$NS" &>/dev/null; then
        log_info "    ✓ NetworkPolicy"
    fi
    
    # agenticActions 확인
    ACTIONS=$(kubectl get chatspace cs-test-setup -o jsonpath='{.status.agenticActions[0]}' 2>/dev/null || echo "")
    if [ ! -z "$ACTIONS" ]; then
        log_info "    ✓ AgenticActions: ${ACTIONS:0:60}..."
    fi
fi

# 정리
log_info "  → 테스트 리소스 정리 ..."
kubectl delete chatspace cs-test-setup --ignore-not-found 2>/dev/null
sleep 2

echo ""

# ─── 완료 ──────────────────────────────────────────────────────────────────

log_info "✅ 초기 설정 완료!"
echo ""
log_info "다음 단계:"
echo "  1. Exp1 실행:"
echo "     cd experiments/exp1"
echo "     python3 run_experiment.py --batch-sizes 5 --runs 1"
echo ""
echo "  2. Exp2 실행:"
echo "     cd experiments/exp2"
echo "     python3 run_experiment_density.py --max-tenants 20 --step 10 --runs 1"
echo ""
echo "  3. Exp3 실행:"
echo "     cd experiments/exp3"
echo "     python3 run_experiment_isolation.py --scenarios A --duration 30"
echo ""
log_info "자세한 가이드: QUICKSTART.md 참고"
