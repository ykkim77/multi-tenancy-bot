# Experiment 1 — 자동화 완성도 비교: Helm vs Agentic Operator

**핵심 질문**: "동일한 결과를 얻으려면 각 방법이 얼마나 많은 인간의 개입이 필요한가?"

속도 비교(이전)가 아닌 **자동화 완성도**를 측정합니다.

## 측정 지표 (3+1)

| 지표 | 정의 | 핵심 결과 |
|------|------|-----------|
| **PCR** — Policy Coverage Rate | 자동 적용 격리 정책 수 / 전체 7가지 | Helm=71%, Agentic=100% |
| **MTTR** — Mean Time To Recovery | 드리프트 주입 → 자동 복구까지 시간 | Helm=∞, Agentic=~30s |
| **HIS** — Human Intervention Score | 1시간 운영 시 운영자 수동 작업 횟수 | Helm=N+5, Agentic=N+2 |
| 누적 수렴 곡선 | 완전한 격리 스택 완료 시점 (보조) | — |

## 완전한 테넌트 격리 스택 (7가지 정책)

| # | 정책 | Helm (기본) | Helm (현재 chart) | Agentic |
|---|------|:-----------:|:-----------------:|:-------:|
| 1 | Namespace | ✅ | ✅ | ✅ |
| 2 | ResourceQuota | ✅ | ✅ | ✅ |
| 3 | LimitRange | ❌ | ✅ | ✅ |
| 4 | NetworkPolicy | ❌ | ✅ | ✅ |
| 5 | PriorityClass | ❌ | ✅ | ✅ |
| 6 | RBAC | ❌ | ❌ | ✅ |
| 7 | status 기록 | ❌ | ❌ | ✅ |
| **PCR** | | **2/7 = 28%** | **5/7 = 71%** | **7/7 = 100%** |

## 서브 실험 구성

### Exp 1-A: PCR 측정
5개 테넌트를 각 방법으로 배포 후 7가지 정책 존재 여부를 자동으로 검사.

### Exp 1-B: MTTR 드리프트 복구 시간 (핵심 실험)
```
1. N개 테넌트 배포 → 60초 안정화
2. NetworkPolicy를 N/2개 namespace에서 삭제 (drift 주입)
3. 각 방법의 복구 행동 측정:
   Helm    → HELM_MTTR_WAIT(90s) 후에도 미복구 → MTTR = ∞
   Agentic → 다음 reconcile 주기에 자동 복구  → MTTR ≈ 30s
```

### Exp 1-C: HIS 모델 (cluster 불필요)
시나리오(1시간 운영, 드리프트 3회, 확장 2회)에서 방법별 수동 개입 횟수를 수식으로 계산:
- **Helm**: `HIS = N + 3(드리프트수동복구) + 2 = N + 5`
- **Agentic**: `HIS = N + 0(자동복구) + 2 = N + 2`

### Exp 1-D: 누적 수렴 곡선 (보조)
기존 속도 비교 실험의 누적 성공률 곡선 (참고용으로 유지).

## 사전 조건

```bash
# 1) Agentic Operator 배포
kubectl apply -f operator/config/crd/bases/portal.kcu.ac.kr_chatspaces.yaml
kubectl apply -f operator/config/manager/manager.yaml
kubectl -n kcu-portal-system rollout status deploy/portal-operator

# 2) helm CLI 확인
helm version
```

## 실행

```bash
# 전체 실험 (권장)
python3 experiments/exp1/run_experiment.py

# 핵심(MTTR)만 빠르게
python3 experiments/exp1/run_experiment.py \
  --skip-1a --skip-1d \
  --n-tenants 10 --mttr-runs 3

# 기존 결과로 요약만 출력
python3 experiments/exp1/run_experiment.py --skip-apply

# Figure 5 생성
python3 experiments/exp1/plot_results.py
```

### 주요 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--n-tenants` | 10 | 1-A·1-B 테넌트 수 |
| `--mttr-runs` | 5 | 드리프트 주입 반복 횟수 |
| `--batch-sizes` | `5,10` | 1-D 배치 크기 |
| `--conv-runs` | 5 | 1-D 배치당 반복 횟수 |
| `--skip-1a/1b/1d` | — | 서브 실험 건너뜀 |
| `--tag` | `exp1v2` | 테넌트 ID 접두사 |

## 산출물

| 파일 | 설명 |
|------|------|
| `results/exp1_results.json` | 서브 실험별 raw 데이터 |
| `results/fig5_automation_completeness.png` | Figure 5 (4-패널) |

## 가설 (검증 대상)

> **H1 (PCR)**: Agentic Operator는 Helm 대비 더 높은 정책 적용률을 달성한다.
>
> **H2 (MTTR)**: Agentic의 MTTR은 유한하고 (~30s), Helm은 자동 복구가 불가능하다 (MTTR = ∞).  
> → **이것이 논문의 가장 강력한 주장**
>
> **H3 (HIS)**: 테넌트 수가 증가할수록 Agentic의 수동 작업 절감이 커진다.

## 구버전 파일 (참고)

- `plot_3way.py` — 이전 속도 비교 시각화 (속도 JSON과 호환)
