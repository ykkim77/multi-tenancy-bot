# KCU Knowledge Portal — 전체 초기 설정 가이드

이 문서는 **Agentic Operator** 및 **3개 실험 (Exp1/Exp2/Exp3)** 을 **처음부터** 구동하는
방법을 단계별로 설명합니다.

## 📋 필수 사전 조건

### 시스템 요구사항
- **Docker** 설치 (KinD 클러스터용)
- **kubectl** 설치
- **Go 1.20+** (Operator 빌드용, 선택)
- **Python 3.8+**
- **helm** (선택, Exp1 Helm 테스트용)

### 설치 확인
```bash
docker --version
kubectl version --client
python3 --version
kind version 2>/dev/null || echo "KinD 설치 필요"
```

---

## 🚀 Step 1: Kubernetes 클러스터 준비

### 1-1. KinD 클러스터 생성

```bash
# 기본 클러스터 생성
kind create cluster --name kcu-demo

# 또는 커스텀 config 로 생성 (선택사항)
kind create cluster --name kcu-demo --config - <<'YAML'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
- role: worker
YAML
```

### 1-2. 클러스터 확인

```bash
kubectl cluster-info
kubectl get nodes
# 출력 예: 3 nodes running (1 control-plane + 2 workers)
```

### 1-3. 컨텍스트 설정 (선택, 이미 활성화됨)

```bash
kubectl config use-context kind-kcu-demo
```

---

## 🔧 Step 2: Agentic Operator 빌드 및 배포

### 2-1. Operator 빌드

```bash
cd /root/kcu-knowledge-portal/operator

# 의존성 다운로드
go mod tidy

# 바이너리 빌드 (sanity check)
go build ./...
go build -o bin/manager .
```

### 2-2. Docker 이미지 빌드

```bash
cd /root/kcu-knowledge-portal/operator

# KinD 클러스터에 직접 빌드 로드
make docker-build IMG=kcu/portal-operator:local

# 확인
docker images | grep kcu/portal-operator
```

### 2-3. KinD 클러스터에 이미지 로드

```bash
# Docker 이미지를 KinD 클러스터로 로드
kind load docker-image kcu/portal-operator:local --name kcu-demo
```

### 2-4. Operator 배포

```bash
# CRD 설치
kubectl apply -f operator/config/crd/bases/portal.kcu.ac.kr_chatspaces.yaml

# Operator Deployment, RBAC, ServiceAccount 설치
kubectl apply -f operator/config/manager/manager.yaml

# 또는 kustomize 사용
cd operator && kustomize build config/default | kubectl apply -f -
```

### 2-5. Operator 확인

```bash
# Operator 배포 확인
kubectl -n kcu-portal-system get deploy portal-operator
kubectl -n kcu-portal-system get pods
kubectl -n kcu-portal-system logs -f deploy/portal-operator

# CRD 확인
kubectl get crd chatspaces.portal.kcu.ac.kr
```

---

## 🧪 Step 3: 실험 환경 준비

### 3-1. Python 의존성 설치

```bash
cd /root/kcu-knowledge-portal

# 실험 필요 패키지
pip install numpy matplotlib scipy pyyaml

# 또는 requirements.txt 가 있으면
pip install -r requirements.txt
```

### 3-2. Helm 설치 (선택, Exp1 Helm 비교용)

```bash
# 시스템 Helm 버전 확인
helm version

# 없으면 설치
# Linux: curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
# macOS: brew install helm
```

### 3-3 디렉토리 구조 확인

```bash
ls -la experiments/
# exp1/  exp2/  exp3/  common/

# 각 실험별 README 확인
cat experiments/exp1/README.md | head -50
cat experiments/exp2/README.md | head -50
cat experiments/exp3/README.md | head -50
```

---

## ✅ Step 4: Operator 작동 검증

### 4-1. 테스트 ChatSpace 생성

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: portal.kcu.ac.kr/v1
kind: ChatSpace
metadata:
  name: cs-test
spec:
  tenantId: test-dept
  tier: standard
  agentic:
    enabled: true
    hardIsolation: true
EOF
```

### 4-2. 리소스 확인

```bash
# ChatSpace 상태 확인
kubectl get chatspace cs-test
kubectl get chatspace cs-test -o yaml | grep -A 20 status:

# 생성된 Namespace 확인
kubectl get ns | grep tenant-test

# Namespace 의 리소스 확인
kubectl get resourcequota,limitrange,networkpolicy -n tenant-test-dept

# agenticActions 기록 확인
kubectl get chatspace cs-test -o jsonpath='{.status.agenticActions}' | jq .
```

### 4-3. 정리

```bash
# 테스트 리소스 삭제
kubectl delete chatspace cs-test
```

---

## 🎬 Step 5: 실험 실행

### Exp1 — Provisioning Speed (Manual vs Helm vs Agentic)

```bash
cd experiments/exp1

# 기본 실행 (batch 5,10 / 3 runs / 약 10-15 분)
python3 run_experiment.py

# 빠른 테스트 (batch 5 만 / 1 run)
python3 run_experiment.py --batch-sizes 5 --runs 1

# 결과 그래프 생성
python3 plot_3way.py

# 결과 확인
ls -la results/
```

### Exp2 — Tenant Density & Autonomous Re-balancing

```bash
cd experiments/exp2

# 기본 실행 (N=10,20,30,40,50 / 2 runs / 약 15-20 분)
python3 run_experiment_density.py

# 빠른 테스트 (N=10,20 / 1 run)
python3 run_experiment_density.py --max-tenants 20 --step 10 --runs 1

# Agentic 만 (고급, real Operator 결정 분석)
python3 run_experiment_density.py --modes agentic --max-tenants 50 --step 10

# 결과 그래프 생성
python3 plot_density.py

# 결과 확인
ls -la results/
```

### Exp3 — Hard Isolation under Noisy Neighbors

```bash
cd experiments/exp3

# 기본 실행 (3 scenarios × 3 modes × 1 run / 약 30-45 분)
python3 run_experiment_isolation.py

# 빠른 테스트 (Scenario A 만)
python3 run_experiment_isolation.py --scenarios A --duration 30

# Agentic 분석만 (고급)
python3 run_experiment_isolation.py --modes agentic --scenarios C --duration 60

# Figure 9 생성 (3×4 grid)
python3 plot_isolation.py

# Figure 10 생성 (신규, Agentic 자율 대응 분석)
python3 plot_agentic_analysis.py

# 결과 확인
ls -la results/
```

---

## 📊 Step 6: 결과 분석

### 결과 파일 위치

```
experiments/
├── exp1/results/
│   ├── exp1_results.json              # Raw measurement data
│   └── exp1_3way.png                  # Figure: Manual vs Helm vs Agentic
├── exp2/results/
│   ├── exp2_density_results.json
│   ├── exp2_density_integrated.png    # Figure: Control Plane + Tenant Latency
│   └── exp2_density_integrated.pdf
└── exp3/results/
    ├── exp3_isolation_results.json
    ├── exp3_fig9_isolation.png        # Figure 9: 3×4 grid (3 scenarios × 4 metrics)
    ├── exp3_fig10_agentic_analysis.png  # Figure 10: Operator 자율 대응 분석
    └── ...
```

### JSON 데이터 분석 (Python)

```python
import json
import numpy as np

# Exp1 결과 분석
with open("experiments/exp1/results/exp1_results.json") as f:
    exp1 = json.load(f)

# 각 모드별 평균 ready_latency
for mode in ["manual", "helm", "agentic"]:
    recs = [r for r in exp1 if r["mode"] == mode]
    latencies = [r["ready_latency_s"] for r in recs]
    print(f"{mode}: mean={np.mean(latencies):.2f}s, "
          f"std={np.std(latencies):.2f}s")
```

### Operator 감시 (실시간)

```bash
# Operator 로그 실시간 모니터링
kubectl -n kcu-portal-system logs -f deploy/portal-operator --tail=100

# ChatSpace 상태 감시
kubectl get chatspaces -A -w

# 특정 ChatSpace 의 agenticActions 보기
kubectl get cs <NAME> -o jsonpath='{.status.agenticActions}' | jq .
```

---

## 🛠️ 트러블슈팅

### Operator 배포 실패

```bash
# 1. CRD 설치 확인
kubectl get crd | grep chatspace

# 2. RBAC 권한 확인
kubectl get clusterrole manager-role
kubectl get clusterrolebinding manager-rolebinding

# 3. Operator Pod 로그 확인
kubectl -n kcu-portal-system logs deploy/portal-operator --tail=50

# 4. 강제 재시작
kubectl -n kcu-portal-system rollout restart deploy/portal-operator
```

### ChatSpace Ready 상태 안 됨

```bash
# 1. Events 확인
kubectl describe chatspace <NAME>

# 2. 생성된 Namespace 확인
kubectl get ns | grep tenant-

# 3. Operator 로그에서 오류 검색
kubectl -n kcu-portal-system logs deploy/portal-operator | grep -i error

# 4. ResourceQuota 수동 확인
kubectl get resourcequota -n <TENANT_NS> -o yaml
```

### 실험 실행 중 kubectl 명령 실패

```bash
# 1. 클러스터 연결 확인
kubectl cluster-info

# 2. 컨텍스트 확인
kubectl config current-context

# 3. API 서버 응답성 테스트
kubectl get nodes
kubectl get namespaces
```

---

## 📝 Operator 커스터마이징

### Rebalance Interval 변경

```bash
# 기본값 30 s → 10 s 로 변경 (빠른 테스트)
kubectl -n kcu-portal-system set env deploy/portal-operator \
  REBALANCE_INTERVAL=10s

# 확인
kubectl -n kcu-portal-system logs deploy/portal-operator | grep -i interval
```

### Tier 기본 Quota 수정

`operator/controllers/defaults.go` 의 `tierDefault()` 함수 수정 후:

```bash
cd operator
go build ./...
make docker-build IMG=kcu/portal-operator:local
kind load docker-image kcu/portal-operator:local --name kcu-demo
kubectl -n kcu-portal-system rollout restart deploy/portal-operator
```

---

## ✨ 정상 실행 체크리스트

- [ ] KinD 클러스터 실행 중 (`kind get clusters`)
- [ ] 3 nodes 모두 Running (`kubectl get nodes`)
- [ ] Operator Pod Running (`kubectl -n kcu-portal-system get pods`)
- [ ] CRD 설치됨 (`kubectl get crd | grep chatspace`)
- [ ] 테스트 ChatSpace 생성 및 Ready 상태 도달
- [ ] 테스트 Namespace 자동 생성됨 (`kubectl get ns | grep tenant-`)
- [ ] Python 의존성 설치됨 (`python3 -c "import numpy, matplotlib"`)
- [ ] Helm 설치됨 (Exp1 테스트용, 선택) (`helm version`)

모두 체크되면 **준비 완료!** 실험 실행 가능합니다.

---

## 📚 추가 자료

- `operator/README.md` — Agentic Operator 세부사항
- `experiments/exp1/README.md` — Exp1 가설 및 측정 방법론
- `experiments/exp2/README.md` — Exp2 자율 re-balancing
- `experiments/exp3/README.md` — Exp3 Hard Isolation

## 🆘 도움말

```bash
# 특정 실험의 전체 옵션 보기
python3 experiments/exp1/run_experiment.py --help
python3 experiments/exp2/run_experiment_density.py --help
python3 experiments/exp3/run_experiment_isolation.py --help
```
