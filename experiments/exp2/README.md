# Experiment 2 — Tenant Density & **Autonomous** Re-balancing

`Static (kubectl)` vs `Agentic Operator (CRD-driven)`.

## ✨ 측정 방법론 — 무엇이 바뀌었나

| 항목 | 이전 | **현재** |
|------|------|----------|
| Re-balancing 주체 | Python 스크립트가 40 % active / 60 % idle 비율을 **수동** 적용 | **실제 Go Operator 가 30 초마다 클러스터 전체를 자율 관찰** 후 결정 |
| Static 비교군 | Python script | 동일 (직접 kubectl apply, baseline) |
| 데이터 출처 | 스크립트 변수 | `ChatSpace.status.{phase, agenticActions, lastUpdated, appliedQuota}` |
| 추가 측정 | × | (1) Rebalance 결정까지 걸린 시간<br>(2) `agenticActions` 기록 분석<br>(3) idle 판정 정확도(혼동행렬) |

## 실험 절차 (per tenant count `N`)

1. **Static 모드** — N 개의 namespace + ResourceQuota + LimitRange + NetworkPolicy 를
   동일 quota 로 **병렬** kubectl apply.
2. **Agentic 모드** — N 개의 `ChatSpace` CR 을 병렬 apply.  각 CR 에 ground-truth
   라벨 `portal.kcu.ac.kr/usage = "active" | "idle"` 어노테이션 부착.
   - 60 % `idle`, 40 % `active` (`--idle-ratio` 로 변경 가능)
3. 두 모드 모두 `Phase=Ready` 까지 polling 으로 대기.
4. **Agentic 만**: `--rebalance-wait` 만큼 추가 대기 (default 35 초 = Operator
   30 초 cycle 한 번 이상 + 마진).
5. 두 모드의 **CP latency** (`kubectl get ns`) ×5, **per-tenant
   latency** (`kubectl get all,quota,netpol,limitrange -n <ns>`) 측정.
6. Agentic 의 경우 각 ChatSpace 의 `status.agenticActions` 마지막 entry 를
   카테고리화하여 다음을 수집:
   - `reclaimed`  (Operator 가 idle 로 판정 후 quota 70% 회수)
   - `boosted`    (idle 피어가 있는 active 테넌트에 quota boost)
   - `no_rebal`   (idle 피어 없음 → 행동 없음)
   - `other` / `agentic_off` / `fallback`

## Idle 판정 정확도

```
expected = annotation 의 ground-truth ("idle" / "active")
predicted = (latest action 이 "reclaimed" 인가?)  →  idle / active

TP = expected idle ∧ predicted idle
TN = expected active ∧ predicted active   (= "boosted" 또는 "no_rebal")
FP = expected active ∧ predicted idle
FN = expected idle ∧ predicted active
accuracy = (TP + TN) / (TP + TN + FP + FN)
```

## Time-to-Rebalance Decision

`status.agenticActions` 는 ring-buffer 라 entry 별 timestamp 가 없다.
대신 Operator 는 reconcile 마다 `status.lastUpdated` 를 새로 찍는다 (그리고 그
reconcile 에서 actions 한 줄을 추가). 따라서:

```
decision_time ≈ status.lastUpdated  −  metadata.creationTimestamp
                (단, 마지막 action 이 reclaimed/boosted/no_rebal 인 경우만 채택)
```

이는 결정 시각의 **상한선**이며, 30 초 rebalance 주기 내로 떨어지는지를
명확히 검증할 수 있다.

## 실행

```bash
# Operator 가 클러스터에 떠 있는 상태에서
kubectl -n kcu-portal-system get deploy portal-operator
kubectl get crd chatspaces.portal.kcu.ac.kr

# 기본: N = 10, 20, 30, 40, 50 (step=10), 2 runs
python3 experiments/exp2/run_experiment_density.py

# 100 까지, idle 70% 가정
python3 experiments/exp2/run_experiment_density.py \
  --max-tenants 100 --step 10 --idle-ratio 0.7

# Agentic 만 (static 비교군 생략)
python3 experiments/exp2/run_experiment_density.py --modes agentic

# 결과 그래프만 다시
python3 experiments/exp2/run_experiment_density.py --skip-apply
python3 experiments/exp2/plot_density.py
```

권장 옵션:
- `--rebalance-wait 35` (default): Operator 30 초 cycle 한 번 보장.
- `--rebalance-wait 65`: 두 cycle — convergence 확인용.

## Operator 측 설정 (참고)

`operator/main.go` 에 `--rebalance-interval` flag 가 있다. 기본 30 s.
실험을 빠르게 돌리려면 Operator 를 `--rebalance-interval=10s` 로 재배포하고
실험 스크립트는 `--rebalance-wait 12` 로 맞추면 된다.

## 산출물

- `results/exp2_density_results.json` — 모든 raw 측정값 + per-CR snapshot
- `results/exp2_density_integrated.png` / `.pdf`
  - (a) Control Plane Latency vs N
  - (b) Per-Tenant Resource Listing Latency vs N
  - (c) Operator Decision Latency boxplot per N (30 s reference line 포함)
  - (d) Stacked bar: action 카테고리 분포 + 우측 축에 Idle Classification Accuracy

## 가설

> **H1**: Agentic Operator 의 CP latency 는 N 이 증가해도 Static 대비 더 완만한
> 증가율을 보인다 (idle quota 회수로 API 서버 압박 감소).
>
> **H2**: 90 % 이상의 ChatSpace 가 Operator 의 다음 rebalance cycle (30 s)
> 안에 첫 결정을 받는다.
>
> **H3**: idle/active classification accuracy ≥ 0.95 (annotation 힌트가
> 명시된 경우).
